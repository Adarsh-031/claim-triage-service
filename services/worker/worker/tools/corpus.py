"""Retrieval tools the agent CHOOSES to call (A2). Signatures fixed now; bodies are P0 TODO."""
from __future__ import annotations


def search_corpus(query: str, k: int = 5, doc_id: str | None = None) -> list[dict]:
    """Semantic search over clause-level chunks. Returns [{chunk_id, doc_id, clause_id, text, score}]."""
    raise NotImplementedError


def get_clause(clause_id: str, doc_id: str = "CAR-S3-M-IV-R4") -> dict:
    """Exact lookup by clause id, e.g. '3.3.2'."""
    raise NotImplementedError


def get_references(clause_id: str) -> list[str]:
    """Clauses this clause points to (e.g. 3.3.2 -> 3.3.1, 3.8.1)."""
    raise NotImplementedError


def get_referrers(clause_id: str) -> list[str]:
    """Clauses that point to this one (finds exemptions/modifiers)."""
    raise NotImplementedError
