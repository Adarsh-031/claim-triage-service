"""Tiny OpenAI-compatible chat-completions stub for local development and tests."""
from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from typing import Any

from fastapi import FastAPI, HTTPException, Request


def _configured_delay() -> float:
    try:
        return max(0.0, float(os.getenv("STUB_DELAY", "1.5")))
    except ValueError:
        return 1.5


STUB_DELAY = _configured_delay()
STUB_MODE = os.getenv("STUB_MODE", "normal").strip().lower()
STUB_SLOW_DELAY = max(STUB_DELAY, float(os.getenv("STUB_SLOW_DELAY", "30")))
STUB_REPLY = "STUB"
# Optional scenario IDs make the HTTP stub deterministic for eval/test requests.
# A scenario ID is test metadata; production claims do not need to include it.
SCRIPTED_INTAKE_RESPONSES: dict[str, dict[str, Any]] = {
    "s01_demo_cancellation_alternate_taken": {
        "disruption_type": "cancellation", "notice_minutes_before_departure": 90,
        "block_time_minutes": 165, "alternate_offered": True, "alternate_accepted": True,
        "contact_details_given": True, "stated_cause": None,
    },
    "s08_delay_two_hours_short_block": {
        "disruption_type": "delay", "notice_minutes_before_departure": None,
        "block_time_minutes": 120, "alternate_offered": None, "alternate_accepted": None,
        "contact_details_given": True, "stated_cause": None,
    },
    "s14_denied_boarding_alternate_within_hour": {
        "disruption_type": "denied_boarding", "notice_minutes_before_departure": None,
        "block_time_minutes": 115, "alternate_offered": True, "alternate_accepted": True,
        "contact_details_given": True, "stated_cause": None,
    },
    "s19_involuntary_domestic_downgrade": {
        "disruption_type": "downgrade",
        "involuntary_downgrade": True, "sector": "domestic", "notice_minutes_before_departure": None,
        "block_time_minutes": 165, "alternate_offered": None, "alternate_accepted": None,
        "contact_details_given": True, "stated_cause": None,
    },
    "s22_vietnam_route_out_of_corpus": {
        "disruption_type": "cancellation", "notice_minutes_before_departure": 60,
        "block_time_minutes": 125, "alternate_offered": None, "alternate_accepted": None,
        "contact_details_given": True, "stated_cause": None,
    },
    "s23_statement_prompt_injection": {
        "disruption_type": "other", "notice_minutes_before_departure": None,
        "block_time_minutes": None, "alternate_offered": None, "alternate_accepted": None,
        "contact_details_given": None, "stated_cause": None,
    },
}


SCRIPTED_INTAKE_RESPONSES.update(
{'s14_denied_boarding_alternate_within_hour': {'disruption_type': 'denied_boarding',
                                               'block_time_minutes': 115,
                                               'alternate_offered': True,
                                               'alternate_accepted': True,
                                               'involuntary_denial': True,
                                               'signed_voucher_agreement': None,
                                               'voucher_issued': None},
 's15_denied_boarding_alternate_within_day': {'disruption_type': 'denied_boarding',
                                              'block_time_minutes': 90,
                                              'alternate_offered': True,
                                              'alternate_accepted': True,
                                              'involuntary_denial': True,
                                              'signed_voucher_agreement': None,
                                              'voucher_issued': None},
 's16_denied_boarding_no_alternate': {'disruption_type': 'denied_boarding',
                                      'block_time_minutes': 130,
                                      'alternate_offered': True,
                                      'alternate_accepted': False,
                                      'involuntary_denial': True,
                                      'signed_voucher_agreement': None,
                                      'voucher_issued': None},
 's17_denied_boarding_signed_voucher': {'disruption_type': 'denied_boarding',
                                        'block_time_minutes': 45,
                                        'alternate_offered': True,
                                        'alternate_accepted': True,
                                        'involuntary_denial': True,
                                        'signed_voucher_agreement': True,
                                        'voucher_issued': True},
 's18_denied_boarding_unsigned_voucher_conflict': {'disruption_type': 'denied_boarding',
                                                   'block_time_minutes': 170,
                                                   'alternate_offered': True,
                                                   'alternate_accepted': True,
                                                   'involuntary_denial': True,
                                                   'signed_voucher_agreement': False,
                                                   'voucher_issued': True}}
)


def _intake_stub_content(scenario_id: str | None = None) -> str | None:
    # Script selection is available only to local test callers via an HTTP header.
    # Ordinary claim JSON cannot select a canned response or steer production behavior.
    scripted = SCRIPTED_INTAKE_RESPONSES.get(scenario_id or "")
    return json.dumps(scripted) if scripted is not None else None


app = FastAPI(title="LLM Stub", version="1.0.0")


def research_reply(messages: list[dict]) -> dict:
    """Stateless scripted cancellation research, driven by the chat transcript.

    The HTTP service and offline tests exercise exactly the same tool sequence.
    No model/provider credentials or source fixtures live inside the worker loop.
    """
    claim_message = next((m for m in messages if m.get("role") == "user"), {})
    claim = json.loads(claim_message.get("content", "{}")).get("claim", {})
    completed = [m for m in messages if m.get("role") == "tool"]
    car = "CAR-S3-M-IV-R4"
    phases = [
        [("search_corpus", {"query": claim.get("disruption_type", "unknown"), "doc_id": car, "k": 3}),
         ("get_claim_field", {"name": "alternate"})],
        [("get_clause", {"clause_id": "3.3.2"}),
         ("get_references", {"clause_id": "3.3.2"}),
         ("get_referrers", {"clause_id": "3.8.1(a)"}),
         ("get_definition", {"term": "force majeure"})],
        [("get_clause", {"clause_id": clause}) for clause in ("3.3.1", "3.8.1(a)", "3.3.3", "3.3.4", "1.5")]
        + [("get_clause", {"clause_id": "14.2", "doc_id": doc})
           for doc in ("INDIGO-COC-DOM", "INDIGO-COC-INTL")],
        [("compute_compensation", {"clause_id": "3.3.2"})],
    ]
    if claim.get("disruption_type") == "denied_boarding":
        phases = [
            [("get_clause", {"clause_id": c}) for c in ("3.2.1", "3.2.2", "3.7.1", "1.4", "1.5")]
            + [("get_clause", {"clause_id": c, "doc_id": d})
               for d in ("INDIGO-COC-DOM", "INDIGO-COC-INTL") for c in ("1.1", "14.2", "10.2")],
            [("compute_compensation", {"clause_id": "3.2.2"})],
        ]
    if claim.get("disruption_type") == "downgrade":
        phases = [
            [("get_clause", {"clause_id": c}) for c in ("3.5.1", "1.4", "1.5")]
            + [("get_clause", {"clause_id": c, "doc_id": d})
               for d in ("INDIGO-COC-DOM", "INDIGO-COC-INTL") for c in ("1.1", "14.2")],
            [("compute_compensation", {"clause_id": "3.5.1"})],
        ]
    count = 0
    for phase in phases:
        if len(completed) == count and (
                claim.get("disruption_type") in {"cancellation", "denied_boarding", "downgrade"} or count == 0):
            return {"role": "assistant", "content": None, "tool_calls": [
                {"id": f"research-{count + i}", "type": "function",
                 "function": {"name": name, "arguments": json.dumps(args)}}
                for i, (name, args) in enumerate(phase)
            ]}
        count += len(phase)
    # Report only evidence that really appeared in successful tool responses.
    chunks = []
    for message in completed:
        output = json.loads(message["content"])
        values = output if isinstance(output, list) else [output]
        chunks.extend(v for v in values if isinstance(v, dict) and "chunk_id" in v)
    car_chunk = next((c for c in chunks if c["doc_id"] == car and c["clause_id"] == "1.4"), None)
    airline = next((c for c in chunks if c["doc_id"].startswith("INDIGO-COC-") and c["clause_id"] == "1.1"), None)
    technical = any(term in str(claim.get("stated_cause", "")).casefold() for term in ("technical", "mechanical"))
    conflict = technical and claim.get("airline_code") == "6E" and car_chunk and airline
    return {"role": "assistant", "content": json.dumps({"comparison": {
        "status": "conflict" if conflict else "missing" if not car_chunk or not airline else "compatible",
        "car_chunk_id": car_chunk["chunk_id"] if car_chunk else None,
        "indigo_chunk_id": airline["chunk_id"] if airline else None,
        "reason": "Compare technical-fault exclusion with CAR beyond-control and reasonable-measures conditions."
        if conflict else "No lower protection identified by the scripted demo.",
    }})}


VERIFIER_MARKER = "You are the grounding verifier"


def verifier_reply(messages: list[dict]) -> dict:
    """Scripted grounding verdicts: a quote supports its sentence if it is non-empty;
    IndiGo wording is flagged as lower protection only for a technical-fault cause."""
    payload = json.loads(next((m.get("content") or "{}" for m in messages if m.get("role") == "user"), "{}"))
    verdict: dict[str, Any] = {"claims": [
        {"id": c["id"], "supported": bool(str(c.get("quote", "")).strip()), "reason": "scripted stub verdict"}
        for c in payload.get("claims", [])]}
    if "comparison" in payload:
        cause = str((payload["comparison"].get("facts") or {}).get("stated_cause") or "").casefold()
        verdict["comparison"] = {"lower_protection": "technical" in cause or "mechanical" in cause,
                                 "reason": "scripted stub comparison"}
    return {"role": "assistant", "content": json.dumps(verdict)}


def is_verifier_request(messages: list[dict]) -> bool:
    return any(m.get("role") == "system" and str(m.get("content") or "").startswith(VERIFIER_MARKER)
               for m in messages)


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/v1/chat/completions")
async def chat_completions(payload: dict[str, Any], request: Request) -> dict[str, Any]:
    """Return a stable response matching the OpenAI Chat Completions schema."""
    if STUB_MODE == "error":
        raise HTTPException(status_code=503, detail="fault injection: provider unavailable")
    if STUB_MODE == "slow":
        await asyncio.sleep(STUB_SLOW_DELAY)
    elif STUB_MODE == "normal":
        await asyncio.sleep(STUB_DELAY)
    else:
        raise HTTPException(status_code=500, detail="STUB_MODE must be normal, error, or slow")
    tools = {tool.get("function", {}).get("name") for tool in payload.get("tools", [])}
    message = (research_reply(payload.get("messages", [])) if "compute_compensation" in tools
               else {"role": "assistant", "content": STUB_REPLY})
    scripted = _intake_stub_content(request.headers.get("x-stub-scenario"))
    if is_verifier_request(payload.get("messages", [])):
        message = verifier_reply(payload["messages"])
    elif scripted is not None and "compute_compensation" not in tools:
        message = {"role": "assistant", "content": scripted}
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": str(payload.get("model") or "stub"),
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": "tool_calls" if message.get("tool_calls") else "stop",
            }
        ],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }
