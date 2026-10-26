import asyncio
import importlib.util
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

APP_PATH = Path(__file__).parents[1] / "services" / "llm-stub" / "app.py"
SPEC = importlib.util.spec_from_file_location("llm_stub_app", APP_PATH)
assert SPEC is not None and SPEC.loader is not None
llm_stub_app = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(llm_stub_app)
client = TestClient(llm_stub_app.app)


def test_healthz_is_healthy():
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_chat_completions_matches_openai_shape(monkeypatch):
    monkeypatch.setattr(llm_stub_app, "STUB_DELAY", 0)
    response = client.post(
        "/v1/chat/completions",
        json={"model": "test-model", "messages": [{"role": "user", "content": "hello"}]},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["object"] == "chat.completion"
    assert body["model"] == "test-model"
    assert body["choices"][0]["message"]["role"] == "assistant"
    assert body["choices"][0]["message"]["content"] == "STUB"
    assert body["choices"][0]["finish_reason"] == "stop"
    assert set(body["usage"]) == {"prompt_tokens", "completion_tokens", "total_tokens"}


def test_error_fault_injection_returns_provider_error(monkeypatch):
    monkeypatch.setattr(llm_stub_app, "STUB_MODE", "error")
    with pytest.raises(HTTPException) as exc:
        asyncio.run(llm_stub_app.chat_completions({"model": "test", "messages": []}))
    assert exc.value.status_code == 503


def test_slow_fault_injection_uses_configured_delay(monkeypatch):
    delays = []

    async def no_wait(_delay):
        delays.append(_delay)
        return None

    monkeypatch.setattr(llm_stub_app, "STUB_MODE", "slow")
    monkeypatch.setattr(llm_stub_app, "STUB_SLOW_DELAY", 17)
    monkeypatch.setattr(llm_stub_app.asyncio, "sleep", no_wait)
    result = asyncio.run(llm_stub_app.chat_completions({"model": "test", "messages": []}))
    assert result["object"] == "chat.completion"
    assert delays == [17]
