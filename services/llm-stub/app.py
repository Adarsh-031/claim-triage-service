"""Tiny OpenAI-compatible chat-completions stub for local development and tests."""
from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from typing import Any

from fastapi import FastAPI, HTTPException


def _configured_delay() -> float:
    try:
        return max(0.0, float(os.getenv("STUB_DELAY", "1.5")))
    except ValueError:
        return 1.5


STUB_DELAY = _configured_delay()
STUB_MODE = os.getenv("STUB_MODE", "normal").strip().lower()
STUB_SLOW_DELAY = max(STUB_DELAY, float(os.getenv("STUB_SLOW_DELAY", "30")))
STUB_REPLY = "STUB"
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
    count = 0
    for phase in phases:
        if len(completed) == count and (claim.get("disruption_type") == "cancellation" or count == 0):
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


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/v1/chat/completions")
async def chat_completions(payload: dict[str, Any]) -> dict[str, Any]:
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
