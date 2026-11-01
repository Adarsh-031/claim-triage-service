"""Grounding verifier: deterministic checks first, then one LLM support check.

The verifier never writes a decision. It either passes the draft it was given
unchanged, or reports which checks failed so the graph can retry retrieval or
escalate. Deterministic checks short-circuit before any provider call.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from triage_common.contracts import Route

from ..tools import corpus
from .research import COMPARISON_CLAUSES, execute_tool

PROMPT_VERSION = "verifier-v1"
PROMPT_PATH = Path(__file__).resolve().parents[4] / "prompts" / "verifier_v1.md"
MAX_RETRIES = 2

# The base clause compute_compensation is called with for each disruption type.
COMPENSATION_CLAUSE = {"cancellation": "3.3.2", "denied_boarding": "3.2.2", "downgrade": "3.5.1"}
ALLOWED_ROUTES = {Route.PAY, Route.DECLINE, Route.REQUEST_EVIDENCE}
# Wording that would read as the service authorising money. An officer approves payment.
PAYMENT_AUTHORISATION = re.compile(
    r"payment (?:is |has been )?(?:approved|authori[sz]ed|released|processed)"
    r"|(?:approve|authori[sz]e|release)[sd]? (?:the |this )?payment"
    r"|(?:has|have) been paid|will be paid|we (?:have )?paid|refund (?:is |has been )?issued",
    re.IGNORECASE,
)
CLAUSE_REF = re.compile(r"\b\d+(?:\.\d+)+(?:\([a-z0-9]+\))*", re.IGNORECASE)
SENTENCE_END = re.compile(r"(?<=\.)\s+")


def normalise(text: str) -> str:
    """Collapse whitespace only; quotes must otherwise match the source exactly."""
    return " ".join(str(text).split())


def _check(name: str, failures: list[str]) -> dict:
    return {"check": name, "passed": not failures, "failures": failures}


def sentences(summary: str) -> list[str]:
    return [s.strip() for s in SENTENCE_END.split(summary or "") if s.strip()]


def sentence_clauses(sentence: str) -> list[str]:
    """Clause numbers a sentence attributes to the CAR, e.g. '(CAR 3.3.2, 3.8.1(a))'."""
    if "CAR" not in sentence:
        return []
    return CLAUSE_REF.findall(sentence)


# --- Deterministic checks ---------------------------------------------------------

def citations_retrieved(draft: dict, state: dict) -> dict:
    """Every citation was retrieved in this run, and every clause the summary names is cited."""
    retrieved = {c["chunk_id"]: c for c in state.get("retrieved", [])}
    failures = []
    for citation in draft.get("citations") or []:
        chunk = retrieved.get(citation.get("chunk_id"))
        if chunk is None:
            failures.append(f"{citation.get('chunk_id')}: not retrieved in this run")
        elif (chunk["clause_id"], chunk["doc_id"]) != (citation.get("clause_id"), citation.get("doc_id")):
            failures.append(f"{citation.get('chunk_id')}: clause/doc does not match the retrieved chunk")
    cited = {c.get("clause_id") for c in draft.get("citations") or [] if c.get("doc_id") == corpus.CAR_DOC}
    for sentence in sentences(draft.get("summary", "")):
        failures += [f"summary names CAR {clause} without citing it"
                     for clause in sentence_clauses(sentence) if clause not in cited]
    return _check("citations_retrieved", failures)


def quotes_grounded(draft: dict, state: dict) -> dict:
    """Each quote is a substring of its source chunk, after whitespace normalisation only."""
    chunks = corpus.get_corpus().chunks  # authoritative text, never the run's copy
    failures = []
    for citation in draft.get("citations") or []:
        chunk = chunks.get(citation.get("chunk_id"))
        quote = normalise(citation.get("quote", ""))
        if not quote:
            failures.append(f"{citation.get('chunk_id')}: empty quote")
        elif chunk is None or quote not in normalise(chunk["text"]):
            failures.append(f"{citation.get('chunk_id')}: quote is not in the source text")
    return _check("quotes_grounded", failures)


def checklist_complete(draft: dict, state: dict) -> dict:
    """Every checklist item has its legal sources. Missing facts are a REQUEST_EVIDENCE matter."""
    failures = []
    for item, entry in (state.get("checklist") or {}).items():
        if entry.get("missing_chunks"):
            failures.append(f"{item}: missing sources {', '.join(entry['missing_chunks'])}")
        if draft.get("route") in {Route.PAY, Route.DECLINE} and entry.get("status") == "pending":
            failures.append(f"{item}: research did not finish")
    if not state.get("checklist"):
        failures.append("no checklist for this disruption type")
    return _check("checklist_complete", failures)


def amount_matches(draft: dict, state: dict) -> dict:
    """The amount equals a fresh compute_compensation run on the trusted claim facts."""
    route, amount = draft.get("route"), draft.get("amount")
    failures = []
    if route in {Route.PAY, Route.DECLINE}:
        clause = COMPENSATION_CLAUSE.get(str(state["claim"].get("disruption_type")))
        if clause is None:
            failures.append("no compensation calculator for this disruption type")
        else:
            try:
                fresh = execute_tool("compute_compensation", {"clause_id": clause}, state["claim"])
            except (ValueError, TypeError, KeyError) as exc:
                fresh = {"status": "error", "reason": str(exc)}
            if fresh.get("status") != "computed":
                failures.append(f"compute_compensation status is {fresh.get('status')}")
            elif amount != fresh.get("amount"):
                failures.append(f"draft amount {amount} != computed {fresh.get('amount')}")
            elif route is Route.PAY and not amount:
                failures.append("pay route with a zero amount")
            elif route is Route.DECLINE and amount:
                failures.append("decline route with a non-zero amount")
    elif amount is not None:
        failures.append(f"{route} must not carry an amount")
    return _check("amount_matches", failures)


def route_allowed(draft: dict, state: dict) -> dict:
    route = draft.get("route")
    failures = []
    if route not in ALLOWED_ROUTES:
        failures.append(f"route {route} cannot be verified; only pay, decline or request evidence")
    if route in {Route.PAY, Route.DECLINE} and not draft.get("citations"):
        failures.append(f"{route} needs at least one citation")
    if draft.get("escalation_reason"):
        failures.append("non-escalation route carries an escalation reason")
    return _check("route_allowed", failures)


def no_payment_authorisation(draft: dict, state: dict) -> dict:
    match = PAYMENT_AUTHORISATION.search(str(draft.get("summary", "")))
    return _check("no_payment_authorisation", [f"summary says '{match.group(0)}'"] if match else [])


DETERMINISTIC_CHECKS = (citations_retrieved, quotes_grounded, checklist_complete,
                        amount_matches, route_allowed, no_payment_authorisation)


# --- LLM support check --------------------------------------------------------------

class ClaimVerdict(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: int
    supported: bool
    reason: str = Field(default="", max_length=500)


class ComparisonVerdict(BaseModel):
    model_config = ConfigDict(extra="forbid")
    lower_protection: bool
    reason: str = Field(default="", max_length=500)


class Verdict(BaseModel):
    model_config = ConfigDict(extra="forbid")
    claims: list[ClaimVerdict] = Field(default_factory=list)
    comparison: ComparisonVerdict | None = None


def support_pairs(draft: dict) -> list[dict]:
    """Pair each summary sentence with the quotes of the CAR clauses it names."""
    by_clause = {c["clause_id"]: c for c in draft.get("citations") or [] if c.get("doc_id") == corpus.CAR_DOC}
    pairs = []
    for sentence in sentences(draft.get("summary", "")):
        for clause in sentence_clauses(sentence):
            if clause in by_clause:
                pairs.append({"id": len(pairs), "sentence": sentence, "clause_id": clause,
                              "quote": by_clause[clause]["quote"]})
    return pairs


def comparison_sources(state: dict) -> dict | None:
    """CAR 1.4/1.5 and both IndiGo snapshots' 1.1/14.2, when the claim is against IndiGo."""
    if state["claim"].get("airline_code") != "6E":
        return None
    retrieved = {corpus.qualified(c["clause_id"], c["doc_id"]): c for c in state.get("retrieved", [])}
    sources = {}
    for doc, clauses in COMPARISON_CLAUSES.items():
        for clause in clauses:
            chunk = retrieved.get(corpus.qualified(clause, doc))
            if chunk:
                sources[corpus.qualified(clause, doc)] = chunk["text"]
    return sources


def llm_messages(draft: dict, state: dict) -> tuple[list[dict], list[dict], dict | None]:
    pairs = support_pairs(draft)
    sources = comparison_sources(state)
    payload: dict = {"claims": pairs}
    if sources is not None:
        payload["comparison"] = {
            "facts": {k: state["claim"].get(k) for k in ("disruption_type", "stated_cause")},
            "sources": sources,
        }
    messages = [{"role": "system", "content": PROMPT_PATH.read_text(encoding="utf-8")},
                {"role": "user", "content": json.dumps(payload)}]
    return messages, pairs, sources


def llm_support(draft: dict, state: dict, llm, *, timeout_s: float | None = None) -> dict:
    """One provider call; any malformed or incomplete answer fails the check.

    Provider/network errors propagate so the graph can escalate as PROVIDER_DEGRADED.
    """
    messages, pairs, sources = llm_messages(draft, state)
    if not pairs and sources is None:
        return {**_check("llm_support", []), "skipped": "nothing to judge"}
    reply = llm.chat(messages, None, timeout_s=timeout_s)
    try:
        verdict = Verdict.model_validate(json.loads((reply or {}).get("content") or ""))
    except (ValueError, TypeError, ValidationError):
        return _check("llm_support", ["verifier response was not valid JSON for the schema"])
    answered = {v.id: v for v in verdict.claims}
    failures = []
    for pair in pairs:
        v = answered.get(pair["id"])
        if v is None:
            failures.append(f"no verdict for CAR {pair['clause_id']}")
        elif not v.supported:
            failures.append(f"CAR {pair['clause_id']} does not support: {v.reason or 'no reason given'}")
    result = _check("llm_support", failures)
    if sources is not None:
        if verdict.comparison is None:
            result["failures"].append("no CAR/CoC comparison verdict")
            result["passed"] = False
        else:
            result["comparison"] = verdict.comparison.model_dump()
    return result


def run_checks(draft: dict, state: dict, llm, *, timeout_s: float | None = None) -> dict:
    """Return the outcome for one verification attempt."""
    checks = []
    for check in DETERMINISTIC_CHECKS:
        result = check(draft, state)
        checks.append(result)
    deterministic_ok = all(c["passed"] for c in checks)
    if deterministic_ok:
        checks.append(llm_support(draft, state, llm, timeout_s=timeout_s))
    comparison = next((c.get("comparison") for c in checks if c.get("comparison")), None)
    return {
        "verified": all(c["passed"] for c in checks) and not (comparison or {}).get("lower_protection"),
        "checks": checks,
        "llm_called": deterministic_ok,
        "lower_protection": bool((comparison or {}).get("lower_protection")),
        "comparison": comparison,
        "prompt_version": PROMPT_VERSION,
    }


# --- Retry expansion ----------------------------------------------------------------

def _missing_keys(state: dict) -> list[str]:
    return sorted({key for entry in (state.get("checklist") or {}).values()
                   for key in entry.get("missing_chunks", [])})


def expand_cross_references(state: dict) -> list[dict]:
    """Retry 1: follow refs.json edges out of every retrieved clause and fetch the targets."""
    seen = {c["chunk_id"] for c in state.get("retrieved", [])}
    found = []
    for chunk in list(state.get("retrieved", [])):
        for target in corpus.get_references(chunk["clause_id"], chunk["doc_id"]):
            hit = corpus.get_clause(target)
            if hit and hit["chunk_id"] not in seen:
                seen.add(hit["chunk_id"])
                found.append(hit)
    # A missing checklist clause that is a known clause is fetched directly too.
    for key in _missing_keys(state):
        hit = corpus.get_clause(key)
        if hit and hit["chunk_id"] not in seen:
            seen.add(hit["chunk_id"])
            found.append(hit)
    return found


def expand_bm25(state: dict) -> list[dict]:
    """Retry 2: lexical search for each missing clause and for the disruption itself."""
    seen = {c["chunk_id"] for c in state.get("retrieved", [])}
    queries = [(key.split(":", 1)[1], key.split(":", 1)[0]) for key in _missing_keys(state)]
    queries.append((str(state["claim"].get("disruption_type", "")).replace("_", " "), corpus.CAR_DOC))
    found = []
    for query, doc_id in queries:
        if not query.strip():
            continue
        for hit in corpus.search_corpus(query, k=3, doc_id=doc_id, mode="bm25"):
            hit = {k: v for k, v in hit.items() if k not in {"score", "retrieval"}}
            if hit["chunk_id"] not in seen:
                seen.add(hit["chunk_id"])
                found.append(hit)
    return found


RETRIES = {1: ("cross_references", expand_cross_references), 2: ("bm25", expand_bm25)}
