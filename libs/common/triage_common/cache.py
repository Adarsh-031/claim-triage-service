"""Shared cache keys and atomic per-client run throttling."""
from __future__ import annotations

import hashlib
import json
import unicodedata
from contextlib import suppress
from typing import Any

import redis
from pydantic import ValidationError

from .contracts import Recommendation

DEMO_CORPUS_VERSION = "demo-fixture-v1"
CORPUS_VERSION_KEY = "triage:corpus:version"
RUN_CACHE_VERSION = "graph-v1"

TOKEN_BUCKET_SCRIPT = """
local clock = redis.call('TIME')
local now = tonumber(clock[1]) + tonumber(clock[2]) / 1000000
local tokens = tonumber(redis.call('HGET', KEYS[1], 'tokens'))
local updated = tonumber(redis.call('HGET', KEYS[1], 'updated'))
local capacity = tonumber(ARGV[1])
local refill = tonumber(ARGV[2])
if tokens == nil then tokens = capacity; updated = now end
tokens = math.min(capacity, tokens + math.max(0, now - updated) * refill)
local allowed = 0
local retry = 0
if tokens >= 1 then
  tokens = tokens - 1
  allowed = 1
else
  retry = math.ceil((1 - tokens) / refill)
end
redis.call('HSET', KEYS[1], 'tokens', tokens, 'updated', now)
redis.call('EXPIRE', KEYS[1], math.ceil(capacity / refill) * 2)
return {allowed, retry}
"""


def _normalize(value: Any, key: str | None = None) -> Any:
    if isinstance(value, dict):
        return {
            name: _normalize(item, name)
            for name, item in sorted(value.items())
            if name not in {"prompt_version", "client_id"}
        }
    if isinstance(value, list):
        return [_normalize(item) for item in value]
    if isinstance(value, str):
        normalized = " ".join(unicodedata.normalize("NFKC", value).split())
        return normalized.casefold() if key in {"statement", "stated_cause"} else normalized
    return value


def run_cache_key(
    claim: dict[str, Any], prompt_version: str, corpus_version: str, client_scope: str = "shared"
) -> str:
    payload = json.dumps({"claim": _normalize(claim), "prompt_version": prompt_version,
                          "corpus_version": corpus_version,
                          "graph_version": RUN_CACHE_VERSION,
                          "client_scope": hashlib.sha256(client_scope.encode("utf-8")).hexdigest()},
                         sort_keys=True, separators=(",", ":"))
    return "triage:cache:run:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def current_corpus_version(client) -> str:
    try:
        return client.get(CORPUS_VERSION_KEY) or DEMO_CORPUS_VERSION
    except redis.RedisError:
        return DEMO_CORPUS_VERSION


def cache_get(client, key: str) -> dict[str, Any] | None:
    try:
        value = client.get(key)
    except redis.RedisError:
        return None
    if value is None:
        return None
    try:
        cached = json.loads(value)
    except (TypeError, ValueError):
        cached = None
    if isinstance(cached, dict):
        status = cached.get("status")
        result = cached.get("result")
        if isinstance(status, str) and status in {"done", "completed", "escalated"} and isinstance(result, dict):
            try:
                recommendation = Recommendation.model_validate(result)
                if (status == "escalated") == (recommendation.route.value == "escalate"):
                    return cached
            except ValidationError:
                pass
    # A stale deployment or partial write must not make claim submission fail.
    with suppress(AttributeError, redis.RedisError):
        client.delete(key)
    return None


def cache_set(client, key: str, value: dict[str, Any], ttl_seconds: int) -> None:
    try:
        client.set(key, json.dumps(value, separators=(",", ":")), ex=ttl_seconds)
    except redis.RedisError:
        return


CACHEABLE_ESCALATION_REASONS = frozenset({
    "CAUSE_DISPUTED",
    "PROVISION_CONFLICT",
    "OUT_OF_CORPUS",
    "VERIFICATION_FAILED",
    "GUARDRAIL_BLOCKED",
})
CACHEABLE_VERIFIED_ROUTES = frozenset({"pay_as_computed", "request_further_evidence", "decline"})


def cacheable_result(result: dict[str, Any]) -> bool:
    """Cache verified results or known deterministic escalations only."""
    if result.get("route") == "escalate":
        return result.get("escalation_reason") in CACHEABLE_ESCALATION_REASONS
    return result.get("route") in CACHEABLE_VERIFIED_ROUTES and result.get("verified") is True


def allow_run(client, client_id: str, limit: int) -> tuple[bool, int]:
    client_hash = hashlib.sha256(client_id.encode("utf-8")).hexdigest()
    allowed, retry_after = client.eval(TOKEN_BUCKET_SCRIPT, 1, f"triage:rl:{client_hash}", limit, limit / 60)
    return bool(int(allowed)), int(retry_after)
