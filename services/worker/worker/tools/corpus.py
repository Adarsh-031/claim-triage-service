"""Fixture search for issue #6; full retrieval tools remain future work (A2)."""
from __future__ import annotations

from hashlib import sha256

from triage_common.contracts import Chunk

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
    if k <= 0 or "cancellation" not in query.lower():
        return []
    return [
        {**chunk.model_dump(mode="json"), "score": 1.0}
        for chunk in DEMO_CHUNKS if doc_id is None or chunk.doc_id == doc_id
    ][:k]


def get_clause(clause_id: str, doc_id: str = "CAR-S3-M-IV-R4") -> dict:
    """Exact lookup by clause id, e.g. '3.3.2'."""
    raise NotImplementedError


def get_references(clause_id: str) -> list[str]:
    """Clauses this clause points to (e.g. 3.3.2 -> 3.3.1, 3.8.1)."""
    raise NotImplementedError


def get_referrers(clause_id: str) -> list[str]:
    """Clauses that point to this one (finds exemptions/modifiers)."""
    raise NotImplementedError
