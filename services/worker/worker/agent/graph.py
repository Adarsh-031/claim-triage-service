"""LangGraph agent topology (A1-A3).

classify -> plan/retrieve (agent chooses tools) -> reason -> verify -> route | retry | escalate

P0 TODO: replace node bodies. The skeleton returns an honest escalation so the E2E path works
before the reasoning layer exists.
"""
from __future__ import annotations

import time
from typing import Any, TypedDict

from langgraph.graph import END, StateGraph
from triage_common.contracts import EscalationReason, Recommendation, Route
from triage_common.settings import settings


class AgentState(TypedDict, total=False):
    claim: dict
    facts: dict
    retrieved: list[dict]          # chunks retrieved this run; citations must resolve here
    draft: dict | None
    verified: bool
    attempts: int
    steps: int
    tool_calls: int
    trace: list[dict]
    recommendation: dict


def _log(state: AgentState, step: str, **detail: Any) -> None:
    state.setdefault("trace", []).append({"step": step, "t": time.time(), **detail})
    state["steps"] = state.get("steps", 0) + 1


def classify(state: AgentState) -> AgentState:
    # TODO: LLM extracts disruption type + facts; falls back to structured fields.
    state["facts"] = {"disruption_type": state["claim"].get("disruption_type", "unknown")}
    _log(state, "classify", facts=state["facts"])
    return state


def retrieve(state: AgentState) -> AgentState:
    # TODO: agent loop with tools: search_corpus, get_clause, get_references, get_referrers,
    # get_claim_field, compute_compensation. Bounded by AGENT_MAX_TOOL_CALLS.
    state["retrieved"] = []
    _log(state, "retrieve", n=0)
    return state


def reason(state: AgentState) -> AgentState:
    # TODO: LLM drafts a cited recommendation from retrieved text only.
    state["draft"] = None
    _log(state, "reason")
    return state


def verify(state: AgentState) -> AgentState:
    # TODO: separate verifier: every citation resolves to a retrieved chunk; each claim supported.
    state["verified"] = False
    state["attempts"] = state.get("attempts", 0) + 1
    _log(state, "verify", verified=False)
    return state


def after_verify(state: AgentState) -> str:
    if state.get("verified"):
        return "finalize"
    if state.get("attempts", 0) < 2 and state.get("steps", 0) < settings.agent_max_steps and state.get("draft"):
        return "retrieve"           # try another retrieval strategy
    return "escalate"


def finalize(state: AgentState) -> AgentState:
    state["recommendation"] = state["draft"]  # type: ignore[assignment]
    _log(state, "finalize")
    return state


def escalate(state: AgentState) -> AgentState:
    rec = Recommendation(
        route=Route.escalate,
        summary="Reasoning layer not implemented yet (scaffold). Routed to a human officer.",
        escalation_reason=EscalationReason.verification_failed,
    )
    state["recommendation"] = rec.model_dump(mode="json")
    _log(state, "escalate", reason=rec.escalation_reason.value)
    return state


def build_graph():
    g = StateGraph(AgentState)
    for name, fn in [("classify", classify), ("retrieve", retrieve), ("reason", reason),
                     ("verify", verify), ("finalize", finalize), ("escalate", escalate)]:
        g.add_node(name, fn)
    g.set_entry_point("classify")
    g.add_edge("classify", "retrieve")
    g.add_edge("retrieve", "reason")
    g.add_edge("reason", "verify")
    g.add_conditional_edges("verify", after_verify, {"finalize": "finalize", "retrieve": "retrieve",
                                                     "escalate": "escalate"})
    g.add_edge("finalize", END)
    g.add_edge("escalate", END)
    return g.compile()


GRAPH = build_graph()


def run_agent(claim: dict, correlation_id: str = "") -> tuple[Recommendation, list[dict]]:
    out = GRAPH.invoke({"claim": claim, "trace": [{"step": "start", "cid": correlation_id}]},
                       {"recursion_limit": settings.agent_max_steps * 2})
    return Recommendation(**out["recommendation"]), out.get("trace", [])
