"""Fixture search for issue #6; full retrieval tools remain future work (A2)."""
from __future__ import annotations

import contextlib
from hashlib import sha256

import redis
from triage_common.cache import cache_get, cache_set, current_corpus_version, retrieval_cache_key
from triage_common.contracts import Chunk
from triage_common.settings import settings

from ..metrics import CACHE_HITS

_redis = redis.Redis.from_url(settings.redis_url, decode_responses=True)
DEMO_CORPUS_VERSION = "demo-fixture-v1"
# Hand-written summaries, not authoritative legal source text. Keep fixture IDs
# distinct from real ingested chunks so provenance is never confused.
DEMO_CHUNKS = tuple(
    Chunk(
        chunk_id=f"DEMO-CAR:{clause_id}", doc_id="DEMO-CAR", clause_id=clause_id,
        text=text, hash=sha256(text.encode()).hexdigest(),
    )
    for clause_id, text in [
        ("3.3.1", "Cancellation: offer an alternate flight or refund acceptable to the passenger."),
        ("3.3.2", "Cancellation: provide an acceptable alternate flight, or compensation and a full refund. "
                    "Provide airport facilities while passengers already reported wait for the alternate flight."),
        ("3.8.1(a)", "Meals and refreshments in relation to waiting time."),
    ]
)


def search_corpus(query: str, k: int = 5, doc_id: str | None = None) -> list[dict]:
    """Offline fixture search; cancellation queries include related airport facilities."""
    corpus_version = current_corpus_version(_redis) if settings.cache_enabled else DEMO_CORPUS_VERSION
    cache_key = retrieval_cache_key(f"{doc_id or '*'}:{query}", "fixture", k, corpus_version)
    if settings.cache_enabled:
        try:
            cached = cache_get(_redis, cache_key)
            if cached is not None:
                CACHE_HITS.labels("retrieval").inc()
                return cached
        except redis.RedisError:
            pass
    if k <= 0 or "cancellation" not in query.lower():
        return []
    result = [
        {**chunk.model_dump(mode="json"), "score": 1.0}
        for chunk in DEMO_CHUNKS if doc_id is None or chunk.doc_id == doc_id
    ][:k]
    if settings.cache_enabled:
        with contextlib.suppress(redis.RedisError):
            cache_set(_redis, cache_key, result, settings.cache_ttl_s)
    return result


def get_clause(clause_id: str, doc_id: str = "CAR-S3-M-IV-R4") -> dict:
    """Exact lookup by clause id, e.g. '3.3.2'."""
    raise NotImplementedError


def get_references(clause_id: str) -> list[str]:
    """Clauses this clause points to (e.g. 3.3.2 -> 3.3.1, 3.8.1)."""
    raise NotImplementedError


def get_referrers(clause_id: str) -> list[str]:
    """Clauses that point to this one (finds exemptions/modifiers)."""
    raise NotImplementedError
