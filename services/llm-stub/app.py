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
        "disruption_type": "downgrade", "notice_minutes_before_departure": None,
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


def _intake_stub_content(messages: list[dict[str, Any]]) -> str | None:
    user = next((str(message.get("content", "")) for message in messages if message.get("role") == "user"), "")
    if "<claim_json>" not in user:
        return None
    import json
    import re

    match = re.search(r"<claim_json>(.*?)</claim_json>", user, flags=re.DOTALL)
    if not match:
        return "{}"
    try:
        claim = json.loads(match.group(1))
    except json.JSONDecodeError:
        return "{}"
    scenario_id = claim.get("_scenario_id")
    scripted = SCRIPTED_INTAKE_RESPONSES.get(scenario_id)
    if scripted is not None:
        return json.dumps(scripted)
    alternate = claim.get("alternate") or {}
    disruption = claim.get("disruption_type")
    if disruption in (None, "unknown"):
        disruption = "other"
    return json.dumps({
        "disruption_type": disruption,
        "notice_minutes_before_departure": claim.get("notice_minutes_before_departure"),
        "block_time_minutes": claim.get("block_time_minutes"),
        "alternate_offered": alternate.get("offered"),
        "alternate_accepted": alternate.get("accepted"),
        "contact_details_given": claim.get("contact_info_provided"),
        "stated_cause": claim.get("stated_cause"),
    })
app = FastAPI(title="LLM Stub", version="1.0.0")


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/v1/chat/completions")
async def chat_completions(payload: dict[str, Any]) -> dict[str, Any]:
    """Return a stable response matching the OpenAI Chat Completions schema."""
    await asyncio.sleep(STUB_DELAY)
    scripted_content = _intake_stub_content(payload.get("messages") or [])
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": str(payload.get("model") or "stub"),
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": scripted_content if scripted_content is not None else STUB_REPLY,
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }
