"""Tiny OpenAI-compatible chat-completions stub for local development and tests."""
from __future__ import annotations

import asyncio
import os
import time
import uuid
from typing import Any

from fastapi import FastAPI


def _configured_delay() -> float:
    try:
        return max(0.0, float(os.getenv("STUB_DELAY", "1.5")))
    except ValueError:
        return 1.5


STUB_DELAY = _configured_delay()
STUB_REPLY = "STUB"
app = FastAPI(title="LLM Stub", version="1.0.0")


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/v1/chat/completions")
async def chat_completions(payload: dict[str, Any]) -> dict[str, Any]:
    """Return a stable response matching the OpenAI Chat Completions schema."""
    await asyncio.sleep(STUB_DELAY)
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": str(payload.get("model") or "stub"),
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": STUB_REPLY},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }
