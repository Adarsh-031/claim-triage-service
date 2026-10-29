"""Bounded tool-calling research graph; grounding verification remains a separate gate."""
from __future__ import annotations

import json
import time
from collections.abc import Callable
from functools import wraps
from pathlib import Path
from uuid import uuid4

import httpx
from langgraph.graph import END, StateGraph
from opentelemetry import trace
from triage_common.contracts import EscalationReason, Result, Route, RunState
from triage_common.settings import settings

from ..metrics import NODE_LATENCY
from ..tools.corpus import get_corpus
from .llm import CircuitOpen, get_llm
from .research import TOOLS, checklist, collect_chunks, execute_tool, make_draft, research_facts, validate_report

PROMPT_VERSION = "research-v1"
PROMPT_PATH = Path(__file__).resolve().parents[4] / "prompts" / "research_v1.md"


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


def intake(state: RunState) -> RunState:
    # Share only eligibility facts; booking identifiers and the narrative stay local.
    state.setdefault("prompt_version", "v1")
    state["facts"] = research_facts(state["claim"])
    state["retrieved"] = []
    state["checklist"] = checklist(state)
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
    _log(state, "intake", disruption_type=state["facts"].get("disruption_type"),
         prompt_version=state["prompt_version"], corpus_version=state["corpus_version"])
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


def verify(state: RunState) -> RunState:
    # A separate grounding verifier is future work; never claim verified=True.
    state["verified"] = False
    _log(state, "verify", mode="pass_through", verified=False)
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
    elif draft.get("route") != Route.ESCALATE:
        # Until grounding verification exists, even an injected/replaced draft
        # must never publish a consequential route through the worker/API.
        draft = {
            "route": Route.ESCALATE,
            "summary": "Unverified research draft: " + str(draft.get("summary", "")) +
                       " Grounding verification is not implemented. No final claim determination made.",
            "escalation_reason": EscalationReason.VERIFICATION_FAILED,
            "citations": draft.get("citations", []),
        }
    result = Result(
        **draft, run_id=state["run_id"], prompt_version=f"{PROMPT_VERSION}+{state['prompt_version']}",
        corpus_version=state["corpus_version"],
    )
    state["result"] = result.model_dump(mode="json")
    _log(state, "finalize", step_count=state.get("step_count", 0),
         tool_calls=state.get("tool_calls", 0), escalation_reason=result.escalation_reason)
    return state


def traced_finalize(state: RunState) -> RunState:
    with trace.get_tracer(__name__).start_as_current_span("agent.finalize"), \
            NODE_LATENCY.labels("finalize").time():
        return finalize(state)


def build_graph():
    g = StateGraph(RunState)
    for name, fn in [("intake", intake), ("research", research), ("draft", draft), ("verify", verify)]:
        g.add_node(name, _bounded(fn))
    g.add_node("finalize", traced_finalize)
    g.set_entry_point("intake")
    g.add_conditional_edges("intake",
                            lambda s: "finalize" if s.get("budget_exceeded") or s.get("draft") else "research",
                            {"finalize": "finalize", "research": "research"})
    g.add_conditional_edges("research", route_fn,
                            {"finalize": "finalize", "research": "research", "draft": "draft"})
    g.add_conditional_edges("draft", lambda s: "finalize" if s.get("budget_exceeded")
                            or s.get("draft", {}).get("route") == Route.ESCALATE else "verify",
                            {"finalize": "finalize", "verify": "verify"})
    g.add_edge("verify", "finalize")
    g.add_edge("finalize", END)
    return g.compile()


GRAPH = build_graph()


def run_agent(
    claim: dict,
    correlation_id: str = "",
    *,
    run_id: str | None = None,
    prompt_version: str = "v1",
    corpus_version: str = "unavailable",
) -> tuple[Result, list[dict]]:
    if prompt_version not in {"v1", "v2"}:
        raise ValueError("prompt_version must be 'v1' or 'v2'")
    out = GRAPH.invoke(
        {"claim": claim, "run_id": run_id or str(uuid4()), "prompt_version": prompt_version,
         "corpus_version": corpus_version, "started_at": time.monotonic(),
         "step_count": 0, "tool_calls": 0, "trace": [{"step": "start", "cid": correlation_id}]},
        # Allow finalization after the last permitted work step; LangGraph's
        # recursion guard remains a second safety net, not the budget mechanism.
        {"recursion_limit": settings.agent_max_steps + 2},
    )
    return Result.model_validate(out["result"]), out.get("trace", [])
