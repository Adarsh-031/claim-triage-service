"""Research tool registry and deterministic evidence coverage/decision gates."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from triage_common.contracts import Citation, EscalationReason, Route

from ..tools import corpus
from ..tools.compensation import compute_compensation
from ..tools.retrieval import LANGCHAIN_TOOLS, ClauseArgs, SearchArgs

# Provider-visible eligibility facts only. New contract fields are private by default;
# fare inputs stay in the server-side calculator, and booking identifiers stay local.
RESEARCH_FIELDS = frozenset({
    "airline_code", "disruption_type", "block_time_minutes", "notice_minutes_before_departure",
    "delay_minutes", "reported_at_airport", "contact_info_provided", "stated_cause",
})
RESEARCH_ALTERNATE_FIELDS = frozenset({"offered", "accepted", "departure_delay_minutes"})
RESEARCH_FIELD_PATHS = RESEARCH_FIELDS | {"alternate"} | {
    f"alternate.{name}" for name in RESEARCH_ALTERNATE_FIELDS
}
COMPARISON_CLAUSES = {
    corpus.CAR_DOC: ("1.4", "1.5"),
    "INDIGO-COC-DOM": ("1.1", "14.2"),
    "INDIGO-COC-INTL": ("1.1", "14.2"),
}


def research_facts(claim: dict) -> dict:
    facts = {key: value for key, value in claim.items() if key in RESEARCH_FIELDS}
    if "alternate" in claim:
        alternate = claim["alternate"]
        facts["alternate"] = ({key: value for key, value in alternate.items()
                               if key in RESEARCH_ALTERNATE_FIELDS} if isinstance(alternate, dict) else None)
    return facts

# A checklist is data, not an assertion the model can waive in its prompt.
# Each entry specifies required CAR clauses and structured claim facts.
CHECKLISTS = {
    "cancellation": {
        "notice_period": (("3.3.1",), ("notice_minutes_before_departure",)),
        "alternate_offered_accepted": (("3.3.2",), ("alternate.offered", "alternate.accepted")),
        "contact_details": (("3.3.3",), ("contact_info_provided",)),
        "extraordinary_circumstances": (("3.3.4", "1.4", "1.5"), ("stated_cause",)),
        "meals_facilities": (("3.3.2", "3.8.1(a)"), ("reported_at_airport", "alternate.accepted",
                                                     "alternate.departure_delay_minutes")),
    },
    "delay": {
        "delay_threshold": (("3.4.1",), ("delay_minutes", "block_time_minutes", "reported_at_airport")),
        "alternate_refund": (("3.4.2",), ("delay_minutes", "alternate.offered", "alternate.accepted")),
        "hotel_facilities": (("3.4.3", "3.8.1(b)"), ("delay_minutes",)),
        "extraordinary_circumstances": (("3.4.4", "1.4", "1.5"), ("stated_cause",)),
    },
    "denied_boarding": {
        "involuntary_denial": (("3.2.1", "3.2.2"), ("reported_at_airport", "involuntary_denial")),
        "alternate": (("3.2.2",), ("alternate.offered", "alternate.accepted",
                                  "alternate.departure_delay_minutes")),
        "payment_mode": (("3.7.1",), ("signed_voucher_agreement",)),
    },
    "downgrade": {
        "downgrade_refund": (("3.5.1",), ("sector", "ticket_cost_including_taxes_inr")),
        "involuntary_downgrade": (("3.5.1",), ("involuntary_downgrade",)),
    },
}


class Args(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DefinitionArgs(Args):
    term: str = Field(min_length=1, max_length=150)
    doc_id: str | None = None


class ClaimFieldArgs(Args):
    name: str = Field(min_length=1, max_length=150, json_schema_extra={"enum": sorted(RESEARCH_FIELD_PATHS)})


class CompensationArgs(Args):
    clause_id: str = Field(min_length=1, max_length=150)


class Comparison(Args):
    status: Literal["compatible", "conflict", "missing"]
    car_chunk_id: str | None = None
    indigo_chunk_id: str | None = None
    reason: str = ""


def validate_report(report: dict, state: dict) -> dict:
    comparison = Comparison.model_validate(report["comparison"])
    if comparison.status != "missing" and state["claim"].get("airline_code") == "6E":
        chunks = {c["chunk_id"]: c for c in state.get("retrieved", [])}
        car = chunks.get(comparison.car_chunk_id)
        airline = chunks.get(comparison.indigo_chunk_id)
        if (not car or not airline or car["doc_id"] != corpus.CAR_DOC
                or car["clause_id"] not in COMPARISON_CLAUSES[corpus.CAR_DOC]
                or airline["doc_id"] not in COMPARISON_CLAUSES or airline["doc_id"] == corpus.CAR_DOC
                or airline["clause_id"] not in COMPARISON_CLAUSES[airline["doc_id"]]):
            raise ValueError("A source comparison needs retrieved CAR 1.4/1.5 and IndiGo 1.1/14.2 chunk IDs")
        retrieved = {corpus.qualified(c["clause_id"], c["doc_id"]) for c in chunks.values()}
        required = {corpus.qualified(clause, doc) for doc, clauses in COMPARISON_CLAUSES.items()
                    for clause in clauses}
        if not required <= retrieved:
            raise ValueError("A source comparison needs all required CAR and both IndiGo snapshot clauses")
        # Relevant citations establish coverage, not the truth of a relationship.
        # Neither compatibility nor conflict is trusted without an independent verifier.
        comparison = Comparison(status="missing", car_chunk_id=comparison.car_chunk_id,
                                indigo_chunk_id=comparison.indigo_chunk_id,
                                reason="Source relationship is unverified. Human review required.")
    return {"comparison": comparison.model_dump()}


def _langchain(name: str):
    lc_tool = LANGCHAIN_TOOLS[name]
    return lambda **args: lc_tool.invoke(args)


REGISTRY = {
    "search_corpus": (SearchArgs, _langchain("search_corpus"), LANGCHAIN_TOOLS["search_corpus"].description),
    "get_clause": (ClauseArgs, _langchain("get_clause"), LANGCHAIN_TOOLS["get_clause"].description),
    "get_references": (ClauseArgs, corpus.get_references, "Outgoing qualified edges from refs.json."),
    "get_referrers": (ClauseArgs, corpus.get_referrers, "Incoming qualified edges; discover modifying clauses."),
    "get_definition": (DefinitionArgs, corpus.get_definition, "Citable definitions from definitions.json."),
    "get_claim_field": (ClaimFieldArgs, None, "Read allowlisted eligibility facts, including nested alternate fields."),
    "compute_compensation": (CompensationArgs, None,
                             "Compute from the run's trusted claim facts; never supply or invent monetary inputs."),
}
TOOLS = [{"type": "function", "function": {"name": name, "description": description,
          "parameters": schema.model_json_schema()}} for name, (schema, _, description) in REGISTRY.items()]


def field_value(claim: dict, name: str):
    value = claim
    for part in name.split("."):
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value


def get_claim_field(claim: dict, name: str):
    if name not in RESEARCH_FIELD_PATHS:
        raise ValueError("Unknown structured claim field")
    return field_value(research_facts(claim), name)


def execute_tool(name: str, arguments: dict, claim: dict):
    schema, function, _ = REGISTRY[name]
    args = schema.model_validate(arguments).model_dump()
    if name == "get_claim_field":
        return get_claim_field(claim, args["name"])
    if name == "compute_compensation":
        # Only fields from the actual run are usable; never accept model-supplied inputs.
        inputs = {key: value for key, value in claim.items() if key != "statement"}
        inputs["alternate_flight_accepted"] = field_value(claim, "alternate.accepted")
        result = compute_compensation(args["clause_id"], inputs)
        # Accommodates #15's structured CompensationResult when that PR lands.
        return result.to_dict() if hasattr(result, "to_dict") else result
    return function(**args)


def collect_chunks(output) -> list[dict]:
    if isinstance(output, list):
        return [chunk for value in output for chunk in collect_chunks(value)]
    if isinstance(output, dict) and {"chunk_id", "text", "doc_id", "clause_id", "hash"} <= output.keys():
        return [output]
    return []


def checklist(state: dict, *, complete: bool = False) -> dict:
    claim = state["claim"]
    retrieved = {corpus.qualified(c["clause_id"], c["doc_id"]): c for c in state.get("retrieved", [])}
    entries = dict(CHECKLISTS.get(str(claim.get("disruption_type")), {}))
    if claim.get("airline_code") == "6E":
        # The current claim contract has no jurisdiction field. Both airline snapshots
        # must be examined rather than guessing domestic/international from IATA codes.
        entries["airline_comparison"] = (("1.4", "1.5"), ())
    results = {}
    for item, (clauses, fields) in entries.items():
        required = [corpus.qualified(c) for c in clauses]
        if item == "airline_comparison":
            required = [corpus.qualified(clause, doc) for doc, clauses in COMPARISON_CLAUSES.items()
                        for clause in clauses]
        missing_chunks = [key for key in required if key not in retrieved]
        missing_fields = [key for key in fields if field_value(claim, key) is None]
        missing = bool(missing_chunks or missing_fields)
        results[item] = {
            "status": ("missing" if complete else "pending") if missing else "cited",
            "chunk_ids": [retrieved[key]["chunk_id"] for key in required if key in retrieved],
            "missing_chunks": missing_chunks, "missing_fields": missing_fields,
        }
        if item == "airline_comparison" and complete:
            # Coverage is not a finding. The verifier compares CAR and CoC wording.
            results[item]["status"] = "missing" if missing else "needs_verification"
            results[item]["reason"] = ("Comparison sources missing. Human review required." if missing else
                                       "CAR/CoC relationship is checked by the grounding verifier.")
    return results


def citations(state: dict) -> list[dict]:
    """Cite the chunks the checklist relies on; other retrieved context is not a citation."""
    relied = {key for entry in (state.get("checklist") or {}).values() for key in entry.get("chunk_ids", [])}
    return [Citation(chunk_id=c["chunk_id"], doc_id=c["doc_id"], clause_id=c["clause_id"],
                     quote=c["text"]).model_dump(mode="json") for c in state.get("retrieved", [])
            if not relied or c["chunk_id"] in relied]


def provision_conflict(state: dict) -> list[str]:
    """Accept a conservative conflict referral only with retrieved CAR + CoC evidence.

    Technical faults are a review trigger, not a finding that every fault is
    compensable. CAR requires beyond-control/unavoidability evidence; CoC explicitly
    lists technical failures. Do not silently let the broader airline wording prevail.
    """
    if state["claim"].get("airline_code") != "6E":
        return []
    # Model-authored comparisons never establish a conflict. This narrow,
    # deterministic condition only requests review of potentially broader wording.
    chunks = {c["chunk_id"]: c for c in state.get("retrieved", [])}
    cause = str(state["claim"].get("stated_cause") or "").casefold()
    if not any(term in cause for term in ("technical", "mechanical")):
        return []
    car = next((c for c in chunks.values() if c["doc_id"] == corpus.CAR_DOC and c["clause_id"] == "1.4"
                and "reasonable measures" in c["text"] and "beyond the control" in c["text"]), None)
    airline = next((c for c in chunks.values() if c["doc_id"] in {"INDIGO-COC-DOM", "INDIGO-COC-INTL"}
                    and c["clause_id"] == "1.1"
                    and "mechanical, technical, or operational issues or failures" in c["text"].casefold()), None)
    return [car["chunk_id"], airline["chunk_id"]] if car and airline else []


def make_draft(state: dict) -> dict:
    cited = citations(state)
    if conflict := provision_conflict(state):
        technical = any(term in str(state["claim"].get("stated_cause") or "").casefold()
                        for term in ("technical", "mechanical"))
        return {"route": Route.ESCALATE, "escalation_reason": EscalationReason.PROVISION_CONFLICT,
                "summary": ("IndiGo's Force Majeure wording includes technical failures; CAR 1.4 requires "
                           "evidence that the cause was beyond the airline's control and unavoidable despite "
                           "reasonable measures. Potentially lower protection requires human review." if technical else
                            "Potentially lower protection in IndiGo's terms than CAR. "
                            "Both source provisions are cited for human review."),
                "citations": [c for c in cited if c["chunk_id"] in conflict]}
    if not cited or state["claim"].get("disruption_type") not in CHECKLISTS:
        return {"route": Route.ESCALATE, "escalation_reason": EscalationReason.OUT_OF_CORPUS,
                "summary": "No supported disruption provisions retrieved. Human review required.", "citations": cited}
    computation = state.get("compensation") or {}
    # Missing legal sources can never support a substantive draft.
    missing_sources = any(item["missing_chunks"] for item in state["checklist"].values())
    accepted = field_value(state["claim"], "alternate.accepted") is True
    if (state["claim"].get("disruption_type") == "cancellation" and not accepted and not missing_sources
            and computation.get("status") == "computed" and (computation.get("amount") or 0) > 0
            and state["claim"].get("contact_info_provided") is True and not state["claim"].get("stated_cause")):
        # No alternate taken, contact details given, no extraordinary cause claimed.
        return {"route": Route.PAY, "citations": cited, "amount": computation["amount"],
                "summary": (f"Cancellation compensation of INR {computation['amount']} is due for late notice "
                            "with no alternate taken (CAR 3.3.1, 3.3.2). "
                            "A claims officer must approve any payment.")}
    accepted_alternate_decline = (state["claim"].get("disruption_type") == "cancellation" and accepted
                     and computation.get("status") == "computed" and computation.get("amount") == 0
                     and not missing_sources)
    route = Route.DECLINE if accepted_alternate_decline else Route.REQUEST_EVIDENCE
    summary = ("Cancellation compensation declined: alternate accepted (CAR 3.3.1, 3.3.2)."
               if accepted_alternate_decline else
               "Further evidence or eligibility review is needed before determining compensation.")
    if (not missing_sources and state["claim"].get("disruption_type") == "cancellation" and accepted
            and state["claim"].get("reported_at_airport") is True
            and (field_value(state["claim"], "alternate.departure_delay_minutes") or 0) > 0):
        summary += " Meals and refreshments are owed in relation to waiting time (CAR 3.3.2, 3.8.1(a))."
    missing_fields = sorted({f for item in state["checklist"].values() for f in item["missing_fields"]}
                            | set(computation.get("missing_fields", [])))
    if missing_fields:
        summary += " Missing evidence: " + ", ".join(missing_fields) + "."
    # No model-authored amount or summary is copied into the consequential draft.
    return {"route": route, "summary": summary, "citations": cited,
            "amount": computation["amount"] if accepted_alternate_decline else None}
