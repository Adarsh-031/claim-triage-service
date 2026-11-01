"""Bounded tool-calling research graph with a separate grounding verifier gate."""
from __future__ import annotations

import json
import socket
import time
from collections.abc import Callable
from contextlib import suppress
from contextvars import ContextVar
from functools import wraps
from pathlib import Path
from uuid import uuid4

import httpx
from langgraph.graph import END, StateGraph
from opentelemetry import trace
from triage_common import guardrails
from triage_common.contracts import EscalationReason, Result, Route, RunState
from triage_common.settings import settings

from ..metrics import NODE_LATENCY
from ..tools.corpus import get_corpus
from . import verifier
from .intake import (
    PROMPT_VERSION as INTAKE_PROMPT_VERSION,
)
from .intake import IntakeError, LocalStructuredStubLLM, extract_intake, is_in_scope_route, reconcile_claim
from .llm import CircuitOpen, get_llm
from .research import TOOLS, checklist, collect_chunks, execute_tool, make_draft, research_facts, validate_report
from .research import citations as decision_citations

PROMPT_VERSION = "research-v1"
PROMPT_PATH = Path(__file__).resolve().parents[4] / "prompts" / "research_v1.md"
_INTAKE_LLM: ContextVar[object | None] = ContextVar("intake_llm", default=None)


def _log(state: RunState, step: str, **detail: object) -> None:
    state["trace"] = [*state.get("trace", []), {"step": step, "t": time.time(), **detail}]


def _budget_exceeded(state: RunState) -> bool:
    exceeded = (
        state.get("step_count", 0) >= settings.agent_max_steps
        or state.get("tool_calls", 0) >= settings.agent_max_tool_calls
        or time.monotonic() - state["started_at"] >= settings.agent_run_timeout_s
    )
    if exceeded:
        state["budget_exceeded"] = True
    return state.get("budget_exceeded", False)


def _bounded(node: Callable[[RunState], RunState]) -> Callable[[RunState], RunState]:
    @wraps(node)
    def invoke(state: RunState) -> RunState:
        # LangGraph passes a state snapshot. Return budget flags as updates rather
        # than relying on mutations made inside a conditional-edge callback.
        state = dict(state)
        if _budget_exceeded(state):
            return state
        state["step_count"] = state.get("step_count", 0) + 1
        if not _budget_exceeded(state):
            with trace.get_tracer(__name__).start_as_current_span(f"agent.{node.__name__}"), \
                    NODE_LATENCY.labels(node.__name__).time():
                state = node(state)
        _budget_exceeded(state)
        return state

    return invoke


def _timed(node: Callable[[RunState], RunState]) -> Callable[[RunState], RunState]:
    """Deterministic draft/verify/retry nodes: bounded by wall clock and MAX_RETRIES, not the
    model's step and tool budget, so a fully researched claim can always be verified."""
    @wraps(node)
    def invoke(state: RunState) -> RunState:
        state = dict(state)
        if time.monotonic() - state["started_at"] >= settings.agent_run_timeout_s:
            state["budget_exceeded"] = True
            return state
        with trace.get_tracer(__name__).start_as_current_span(f"agent.{node.__name__}"), \
                NODE_LATENCY.labels(node.__name__).time():
            return node(state)

    return invoke


def intake(state: RunState) -> RunState:
    state.setdefault("prompt_version", "v1")
    state.setdefault("corpus_version", "unavailable")
    claim = state["claim"]
    extracted = None
    # Defence in depth: the API screens before queueing, and the worker screens again
    # because it is the last step before a model sees the free text.
    hits = guardrails.screen_input(claim)
    withheld = guardrails.withheld_fields(hits)
    if hits:
        _log(state, "guardrail", stage="input", hits=[h.as_dict() for h in hits],
             blocked=guardrails.blocked(hits), withheld=sorted(withheld))
    if guardrails.blocked(hits):
        state["draft"] = {"route": Route.ESCALATE, "escalation_reason": EscalationReason.GUARDRAIL_BLOCKED,
                          "summary": guardrails.block_summary(hits)}
    elif not is_in_scope_route(str(claim.get("origin") or "")):
        state["draft"] = {
            "route": Route.ESCALATE,
            "summary": "The route is outside the India-focused source corpus. Routed to a human officer.",
            "escalation_reason": EscalationReason.OUT_OF_CORPUS,
        }
    else:
        try:
            llm = _INTAKE_LLM.get()
            if llm is None:
                llm = LocalStructuredStubLLM() if settings.llm_provider == "stub" else get_llm()
            remaining = settings.agent_run_timeout_s - (time.monotonic() - state["started_at"])
            # A narrative carrying instructions is withheld: intake extracts from the
            # structured form fields only, which are canonical anyway.
            intake_input = {k: v for k, v in claim.items() if k not in withheld}
            facts = extract_intake(intake_input, llm=llm, timeout_s=remaining)
            # Research tools, checklists and calculations must see the same reconciled facts.
            state["claim"] = reconcile_claim(claim, facts)
            extracted = facts.model_dump()
        except IntakeError as exc:
            state["draft"] = {
                "route": Route.ESCALATE,
                "summary": "Claim intake could not be validated safely. Routed to a human officer.",
                "escalation_reason": exc.reason,
            }
    # Research receives only eligibility facts, without booking identifiers or narrative.
    state["facts"] = research_facts(state["claim"])
    state["retrieved"] = []
    state["checklist"] = checklist(state)
    if state.get("draft"):
        # Early referrals still identify the deployed corpus for evaluation/provenance.
        # Resolve local corpus identity without retrieving evidence or calling a provider.
        with suppress(OSError, ValueError):
            state["corpus_version"] = get_corpus().version
    _log(state, "intake", facts=extracted if extracted is not None else state["facts"],
         prompt_version=state["prompt_version"], intake_prompt_version=INTAKE_PROMPT_VERSION,
         corpus_version=state["corpus_version"])
    if state.get("draft") or _budget_exceeded(state):
        return state
    try:
        state["corpus_version"] = get_corpus().version
        prompt_path = Path(settings.research_prompt_path) if settings.research_prompt_path else PROMPT_PATH
        state["messages"] = [
            {"role": "system", "content": prompt_path.read_text(encoding="utf-8") + "\n\n" +
             (prompt_path.parent / f"{state['prompt_version']}.md").read_text(encoding="utf-8")},
            {"role": "user", "content": json.dumps({"claim": state["facts"], "checklist": state["checklist"]})},
        ]
    except (OSError, ValueError, KeyError):
        state["draft"] = {"route": Route.ESCALATE, "escalation_reason": EscalationReason.OUT_OF_CORPUS,
                          "summary": "Processed corpus or research prompt unavailable. Human review required."}
    state["trace"][-1]["corpus_version"] = state["corpus_version"]
    return state


def _reserve_tool_call(state: RunState) -> bool:
    """Reserve a slot and reject it if the reservation reaches a hard budget."""
    if _budget_exceeded(state):
        return False
    state["tool_calls"] = state.get("tool_calls", 0) + 1
    # Counts reservations, including the terminal slot. No tool side effects
    # may begin with the counter at the configured exhaustion threshold.
    return not _budget_exceeded(state)


def research(state: RunState) -> RunState:
    if state.get("draft"):
        return state
    try:
        if get_corpus().version != state["corpus_version"]:
            raise ValueError("Corpus changed during research")
        if _budget_exceeded(state):
            return state
        remaining = settings.agent_run_timeout_s - (time.monotonic() - state["started_at"])
        messages = list(state["messages"])
        messages.append({"role": "system", "content": json.dumps({
            "remaining_steps": settings.agent_max_steps - state["step_count"],
            "remaining_tool_calls": settings.agent_max_tool_calls - state["tool_calls"] - 1,
            "remaining_seconds": max(0, remaining), "checklist": state["checklist"],
        })})
        with trace.get_tracer(__name__).start_as_current_span("research.llm"):
            reply = get_llm().chat(messages, TOOLS, timeout_s=remaining)
        if _budget_exceeded(state):
            return state
        if not isinstance(reply, dict):
            raise httpx.DecodingError("Invalid provider response")
        calls = reply.get("tool_calls") or []
        if not isinstance(calls, list) or any(
            not isinstance(c, dict) or not isinstance(c.get("id"), str)
            or not isinstance(c.get("function"), dict)
            or not isinstance(c["function"].get("name"), str)
            or not isinstance(c["function"].get("arguments"), str) for c in calls
        ):
            raise httpx.DecodingError("Invalid provider tool-call envelope")
        state["messages"] = [*state["messages"], {"role": "assistant", "content": reply.get("content"),
                                                     **({"tool_calls": calls} if calls else {})}]
        for call in calls:
            if not _reserve_tool_call(state):
                return state
            name = call.get("function", {}).get("name", "")
            arguments = {}
            tool_started = time.monotonic()
            try:
                arguments = json.loads(call["function"]["arguments"])
                with trace.get_tracer(__name__).start_as_current_span(f"tool.{name}"):
                    output = execute_tool(name, arguments, state["claim"])
                if name == "compute_compensation":
                    state["compensation"] = output
                retrieved = {c["chunk_id"]: c for c in state.get("retrieved", [])}
                retrieved.update({c["chunk_id"]: c for c in collect_chunks(output)})
                state["retrieved"] = list(retrieved.values())
            except (ValueError, KeyError, TypeError, OSError) as exc:
                output = {"error": str(exc)}
            _log(state, "tool", name=name, arguments=arguments,
                 chunk_ids=[c["chunk_id"] for c in collect_chunks(output)],
                 references=output if name in {"get_references", "get_referrers"} else None,
                 duration_ms=(time.monotonic() - tool_started) * 1000,
                 error=isinstance(output, dict) and "error" in output)
            state["messages"].append({"role": "tool", "tool_call_id": call["id"],
                                      "content": json.dumps(output)})
            if _budget_exceeded(state):
                return state
        if not calls:
            try:
                report = json.loads(reply.get("content") or "{}")
                if not isinstance(report, dict) or not isinstance(report.get("comparison"), dict):
                    raise ValueError("Expected a comparison object")
                state["research_report"] = validate_report(report, state)
                state["research_complete"] = True
            except (ValueError, TypeError):
                state["messages"].append({"role": "user", "content": "Return the research JSON comparison object, "
                                           "or continue tools to fill the checklist."})
        state["checklist"] = checklist(state, complete=state.get("research_complete", False))
        _log(state, "research", n=len(state["retrieved"]), checklist=state["checklist"],
             complete=state.get("research_complete", False))
    except (httpx.HTTPError, CircuitOpen):
        state["draft"] = {"route": Route.ESCALATE, "escalation_reason": EscalationReason.PROVIDER_DEGRADED,
                          "summary": "Research provider unavailable. Human review required."}
    except (OSError, ValueError, KeyError, TypeError):
        state["draft"] = {"route": Route.ESCALATE, "escalation_reason": EscalationReason.OUT_OF_CORPUS,
                          "summary": "Research artifacts or provider response invalid. Human review required."}
    return state


def draft(state: RunState) -> RunState:
    state["draft"] = make_draft(state)
    _log(state, "draft", **state["draft"], checklist=state["checklist"], compensation=state.get("compensation"))
    return state


def _escalate(state: RunState, reason: EscalationReason, summary: str, citations: list | None = None) -> None:
    state["draft"] = {"route": Route.ESCALATE, "escalation_reason": reason, "summary": summary,
                      "citations": citations if citations is not None else []}


def verify(state: RunState) -> RunState:
    draft = dict(state.get("draft") or {})
    attempt = state.get("attempts", 0)
    remaining = settings.agent_run_timeout_s - (time.monotonic() - state["started_at"])
    state["verified"] = False
    try:
        with trace.get_tracer(__name__).start_as_current_span("verify.checks"):
            outcome = verifier.run_checks(draft, state, get_llm(), timeout_s=remaining)
    except (httpx.HTTPError, CircuitOpen):
        outcome = {"verified": False, "error": "provider_degraded", "checks": []}
        _escalate(state, EscalationReason.PROVIDER_DEGRADED,
                  "Grounding verifier provider unavailable. Human review required.")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        outcome = {"verified": False, "error": type(exc).__name__, "checks": []}
    outcome = {**outcome, "attempt": attempt}
    state["verifier_outcomes"] = [*state.get("verifier_outcomes", []), outcome]
    if outcome.get("lower_protection"):
        comparison_ids = {c["chunk_id"] for c in state.get("retrieved", [])
                          if (c["doc_id"], c["clause_id"]) in {(doc, clause)
                          for doc, clauses in verifier.COMPARISON_CLAUSES.items() for clause in clauses}}
        _escalate(state, EscalationReason.PROVISION_CONFLICT,
                  "IndiGo's terms may give this passenger less protection than CAR: "
                  + str((outcome.get("comparison") or {}).get("reason", "")) + " Human review required.",
                  # The draft cites only its own decision, so take the compared provisions
                  # from everything the checklist relied on.
                  [c for c in decision_citations(state) if c["chunk_id"] in comparison_ids])
    elif outcome["verified"]:
        state["verified"] = True
        state["verified_draft"] = draft
    _log(state, "verify", attempt=attempt, verified=state["verified"],
         prompt_version=verifier.PROMPT_VERSION, llm_called=outcome.get("llm_called", False),
         lower_protection=outcome.get("lower_protection", False), error=outcome.get("error"),
         failed=[{"check": c["check"], "failures": c["failures"]} for c in outcome["checks"] if not c["passed"]],
         passed=[c["check"] for c in outcome["checks"] if c["passed"]])
    return state


def verify_route(state: RunState) -> str:
    if state.get("verified") or state.get("budget_exceeded"):
        return "finalize"
    if (state.get("draft") or {}).get("route") == Route.ESCALATE:
        return "finalize"
    return "retry" if state.get("attempts", 0) < verifier.MAX_RETRIES else "finalize"


def retry(state: RunState) -> RunState:
    """Retry 1 follows cross-references; retry 2 runs BM25 searches. Then re-draft."""
    state["attempts"] = state.get("attempts", 0) + 1
    strategy, expand = verifier.RETRIES[state["attempts"]]
    try:
        found = expand(state)
    except (OSError, ValueError, KeyError, TypeError):
        found = []
    state["retrieved"] = [*state.get("retrieved", []), *found]
    state["checklist"] = checklist(state, complete=True)
    _log(state, "verify_retry", attempt=state["attempts"], strategy=strategy,
         chunk_ids=[c["chunk_id"] for c in found], checklist=state["checklist"])
    return state


def route_fn(state: RunState) -> str:
    if state.get("budget_exceeded"):
        return "finalize"
    if state.get("draft"):
        return "finalize"
    return "draft" if state.get("research_complete") else "research"


def finalize(state: RunState) -> RunState:
    _budget_exceeded(state)
    draft = state.get("draft")
    if state.get("budget_exceeded"):
        draft = {
            "route": Route.ESCALATE,
            "summary": "Agent work budget exhausted. Routed to a human officer.",
            "escalation_reason": EscalationReason.BUDGET_EXCEEDED,
        }
    elif not draft:
        draft = {
            "route": Route.ESCALATE,
            "summary": "No draft produced. Routed to a human officer.",
            "escalation_reason": EscalationReason.VERIFICATION_FAILED,
        }
    elif draft.get("route") != Route.ESCALATE and not (state.get("verified") and draft == state.get("verified_draft")):
        # Only the exact draft the verifier passed may publish a consequential route.
        last = (state.get("verifier_outcomes") or [{}])[-1]
        failures = [f for c in last.get("checks", []) if not c["passed"] for f in c["failures"]]
        draft = {
            "route": Route.ESCALATE,
            "summary": "Grounding verification failed after "
                       f"{state.get('attempts', 0)} retries: " + ("; ".join(failures[:3]) or "draft not verified") +
                       ". No final claim determination made.",
            "escalation_reason": EscalationReason.VERIFICATION_FAILED,
            # A citation that failed grounding is never returned.
            "citations": [c for c in draft.get("citations", []) if not verifier.quotes_grounded(
                {"citations": [c]}, state)["failures"]],
        }
        state["verified"] = False
    if draft.get("route") != Route.ESCALATE:
        # Output filter: the last gate before a determination is published.
        computation = state.get("compensation") or {}
        computed = computation.get("amount") if computation.get("status") == "computed" else None
        if hits := guardrails.screen_output(draft, computed):
            _log(state, "guardrail", stage="output", hits=[h.as_dict() for h in hits], blocked=True)
            draft = {"route": Route.ESCALATE, "escalation_reason": EscalationReason.GUARDRAIL_BLOCKED,
                     "summary": guardrails.block_summary(hits)}
            state["verified"] = False
    result = Result(
        **draft, run_id=state["run_id"],
        prompt_version=f"{INTAKE_PROMPT_VERSION}+{PROMPT_VERSION}+{state['prompt_version']}",
        corpus_version=state["corpus_version"],
    )
    state["result"] = result.model_dump(mode="json")
    state["verified"] = bool(state.get("verified")) and result.route is not Route.ESCALATE
    _log(state, "finalize", step_count=state.get("step_count", 0), verified=state["verified"],
         verifier_attempts=state.get("attempts", 0),
         tool_calls=state.get("tool_calls", 0), escalation_reason=result.escalation_reason)
    return state


def traced_finalize(state: RunState) -> RunState:
    with trace.get_tracer(__name__).start_as_current_span("agent.finalize"), \
            NODE_LATENCY.labels("finalize").time():
        return finalize(state)


def handoff(state: RunState) -> RunState:
    """Split mode: stop after drafting; the verifier-worker continues from the stored draft."""
    state["awaiting_verification"] = True
    _log(state, "handoff", host=socket.gethostname(), service="agent-worker", attempt=state.get("attempts", 0))
    return state


def _after_draft(split: bool):
    def route(state: RunState) -> str:
        if state.get("budget_exceeded") or (state.get("draft") or {}).get("route") == Route.ESCALATE:
            return "finalize"
        return "handoff" if split else "verify"
    return route


def build_graph(*, split: bool = False, entry: str = "intake"):
    """inline: the whole pipeline in one process (tests, CLI eval, VERIFIER_MODE=inline).
    split: stop at handoff after draft; entry='retry' resumes a run the verifier sent back."""
    g = StateGraph(RunState)
    for name, fn in [("intake", intake), ("research", research)]:
        g.add_node(name, _bounded(fn))
    for name, fn in [("draft", draft), ("verify", verify), ("retry", retry)]:
        g.add_node(name, _timed(fn))
    g.add_node("handoff", handoff)
    g.add_node("finalize", traced_finalize)
    g.set_entry_point(entry)
    g.add_conditional_edges("intake",
                            lambda s: "finalize" if s.get("budget_exceeded") or s.get("draft") else "research",
                            {"finalize": "finalize", "research": "research"})
    g.add_conditional_edges("research", route_fn,
                            {"finalize": "finalize", "research": "research", "draft": "draft"})
    g.add_conditional_edges("draft", _after_draft(split),
                            {"finalize": "finalize", "verify": "verify", "handoff": "handoff"})
    g.add_conditional_edges("verify", verify_route, {"finalize": "finalize", "retry": "retry"})
    g.add_edge("retry", "draft")
    g.add_edge("handoff", END)
    g.add_edge("finalize", END)
    return g.compile()


GRAPH = build_graph()
AGENT_GRAPH = build_graph(split=True)
RESUME_GRAPH = build_graph(split=True, entry="retry")
RECURSION_LIMIT = settings.agent_max_steps + 4 + 3 * verifier.MAX_RETRIES


def _initial_state(claim: dict, correlation_id: str, run_id: str | None, prompt_version: str) -> RunState:
    if prompt_version not in {"v1", "v2"}:
        raise ValueError("prompt_version must be 'v1' or 'v2'")
    return {"claim": claim, "run_id": run_id or str(uuid4()), "prompt_version": prompt_version,
            "corpus_version": "unavailable", "started_at": time.monotonic(),
            "step_count": 0, "tool_calls": 0, "attempts": 0,
            "trace": [{"step": "start", "cid": correlation_id}]}


def run_agent(
    claim: dict,
    correlation_id: str = "",
    *,
    run_id: str | None = None,
    prompt_version: str = "v1",
    llm=None,
) -> tuple[Result, list[dict]]:
    state = _initial_state(claim, correlation_id, run_id, prompt_version)
    # Provider objects stay outside serializable graph state and are reset after each run.
    token = _INTAKE_LLM.set(llm)
    try:
        # Room for finalization after the last work step plus draft/verify/retry rounds.
        out = GRAPH.invoke(state, {"recursion_limit": RECURSION_LIMIT})
    finally:
        _INTAKE_LLM.reset(token)
    return Result.model_validate(out["result"]), out.get("trace", [])


# --- Split mode (#28): agent-worker -> drafts row + q:verify -> verifier-worker ------------

# Research conversation stays in the agent process; the verifier needs evidence, not chat.
_NOT_HANDED_OFF = frozenset({"messages", "started_at", "awaiting_verification"})


def dump_state(state: RunState) -> dict:
    """JSON-safe state for the drafts row. Monotonic time becomes a wall-clock deadline."""
    elapsed = time.monotonic() - state["started_at"]
    out = {k: v for k, v in state.items() if k not in _NOT_HANDED_OFF}
    out["deadline_epoch"] = time.time() + settings.agent_run_timeout_s - elapsed
    return json.loads(json.dumps(out, default=str))


def load_state(data: dict) -> RunState:
    """Rebuild graph state in another process; the remaining wall-clock budget carries over."""
    state = {k: v for k, v in data.items() if k != "deadline_epoch"}
    remaining = float(data.get("deadline_epoch", time.time())) - time.time()
    state["started_at"] = time.monotonic() - (settings.agent_run_timeout_s - remaining)
    for key in ("draft", "verified_draft"):
        if isinstance(state.get(key), dict) and state[key].get("route"):
            state[key] = {**state[key], "route": Route(state[key]["route"])}
            if state[key].get("escalation_reason"):
                state[key]["escalation_reason"] = EscalationReason(state[key]["escalation_reason"])
    return state


def run_until_handoff(claim: dict, correlation_id: str = "", *, run_id: str | None = None,
                      prompt_version: str = "v1", llm=None) -> RunState:
    """Agent-worker half: returns a finalized state (early escalation) or one awaiting verification."""
    token = _INTAKE_LLM.set(llm)
    try:
        return AGENT_GRAPH.invoke(_initial_state(claim, correlation_id, run_id, prompt_version),
                                  {"recursion_limit": RECURSION_LIMIT})
    finally:
        _INTAKE_LLM.reset(token)


def resume_until_handoff(state: RunState) -> RunState:
    """Agent-worker half of a verifier retry: expand retrieval, re-draft, hand off again."""
    return RESUME_GRAPH.invoke(state, {"recursion_limit": RECURSION_LIMIT})


def verify_once(state: RunState) -> tuple[RunState, str]:
    """Verifier-worker half: one verification attempt. Returns (state, 'finalize' | 'retry').

    On 'finalize' the state carries `result`; on 'retry' the agent-worker resumes it.
    """
    state = dict(state)
    state.pop("awaiting_verification", None)
    state = _timed(verify)(state)
    if state["trace"][-1].get("step") == "verify":
        state["trace"][-1].update(host=socket.gethostname(), service="verifier-worker")
    decision = verify_route(state)
    if decision == "finalize":
        state = traced_finalize(state)
    return state, decision
