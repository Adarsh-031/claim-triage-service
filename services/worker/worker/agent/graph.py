"""Bounded, offline LangGraph skeleton for issue #6.

The corpus and drafting step are fixtures; verification is deliberately pass-through.
Finalization is outside the work-step budget so exhaustion always yields a Result.
"""
from __future__ import annotations

import time
from collections.abc import Callable
from functools import wraps
from uuid import uuid4

from langgraph.graph import END, StateGraph
from triage_common.contracts import Citation, EscalationReason, Result, Route, RunState
from triage_common.settings import settings

from ..tools.corpus import DEMO_CORPUS_VERSION, search_corpus

PROMPT_VERSION = "skeleton-v1"


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
        state = node(state)
        _budget_exceeded(state)
        return state

    return invoke


def intake(state: RunState) -> RunState:
    state["facts"] = {"disruption_type": state["claim"].get("disruption_type", "unknown")}
    _log(state, "intake", facts=state["facts"])
    return state


def _stub_draft(state: RunState) -> dict:
    """Deterministic stand-in for an LLM; only the demo pattern is decided."""
    citations = [Citation(
        chunk_id=chunk["chunk_id"], doc_id=chunk["doc_id"],
        clause_id=chunk["clause_id"], quote=chunk["text"],
    ).model_dump(mode="json") for chunk in state["retrieved"]]
    claim = state["claim"]
    alternate = claim.get("alternate") or {}
    if (
        claim.get("disruption_type") == "cancellation"
        and alternate.get("offered") is True
        and alternate.get("accepted") is True
        and claim.get("reported_at_airport") is True
        and citations
    ):
        return {
            "route": Route.DECLINE,
            "summary": "Demo stub: decline cancellation compensation after an accepted alternate flight; "
                       "meals_and_refreshments are owed during the airport wait. Human review required.",
            "citations": citations,
        }
    return {
        "route": Route.ESCALATE,
        "summary": "Demo skeleton cannot decide this claim. Routed to a human officer.",
        "escalation_reason": EscalationReason.OUT_OF_CORPUS,
        "citations": citations,
    }


def research(state: RunState) -> RunState:
    state["tool_calls"] = state.get("tool_calls", 0) + 1
    state["retrieved"] = search_corpus(str(state["facts"]["disruption_type"]))
    # Do not start drafting if the search consumed the remaining time/tool budget.
    if not _budget_exceeded(state):
        state["draft"] = _stub_draft(state)
    _log(state, "research", n=len(state["retrieved"]), tool_calls=state["tool_calls"])
    return state


def verify(state: RunState) -> RunState:
    # A separate grounding verifier is future work; never claim verified=True.
    state["verified"] = False
    _log(state, "verify", mode="pass_through", verified=False)
    return state


def route_fn(state: RunState) -> str:
    if state.get("budget_exceeded"):
        return "finalize"
    # A fake research node can withhold its draft to exercise bounded retries.
    return "verify" if state.get("draft") else "research"


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
    result = Result(
        **draft, run_id=state["run_id"], prompt_version=state["prompt_version"],
        corpus_version=state["corpus_version"],
    )
    state["result"] = result.model_dump(mode="json")
    _log(state, "finalize", step_count=state.get("step_count", 0),
         tool_calls=state.get("tool_calls", 0), escalation_reason=result.escalation_reason)
    return state


def build_graph():
    g = StateGraph(RunState)
    for name, fn in [("intake", intake), ("research", research), ("verify", verify)]:
        g.add_node(name, _bounded(fn))
    g.add_node("finalize", finalize)
    g.set_entry_point("intake")
    g.add_conditional_edges("intake", lambda s: "finalize" if s.get("budget_exceeded") else "research",
                            {"finalize": "finalize", "research": "research"})
    g.add_conditional_edges("research", route_fn,
                            {"finalize": "finalize", "research": "research", "verify": "verify"})
    g.add_edge("verify", "finalize")
    g.add_edge("finalize", END)
    return g.compile()


GRAPH = build_graph()


def run_agent(claim: dict, correlation_id: str = "", *, run_id: str | None = None) -> tuple[Result, list[dict]]:
    out = GRAPH.invoke(
        {"claim": claim, "run_id": run_id or str(uuid4()), "prompt_version": PROMPT_VERSION,
         "corpus_version": DEMO_CORPUS_VERSION, "started_at": time.monotonic(),
         "step_count": 0, "tool_calls": 0, "trace": [{"step": "start", "cid": correlation_id}]},
        # Allow finalization after the last permitted work step; LangGraph's
        # recursion guard remains a second safety net, not the budget mechanism.
        {"recursion_limit": settings.agent_max_steps + 2},
    )
    return Result.model_validate(out["result"]), out.get("trace", [])
