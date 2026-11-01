"""LangChain tools the research agent calls for source retrieval.

The docstrings are the tool descriptions the model reads, so they say when to use
each tool and what comes back. The functions only delegate to ``corpus``.
"""
from __future__ import annotations

from langchain_core.tools import tool
from pydantic import BaseModel, ConfigDict, Field

from . import corpus


class SearchArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=1, max_length=500, description="What to look for, in plain words or clause numbers.")
    k: int = Field(default=5, ge=1, le=corpus.MAX_K, description="Number of chunks to return.")
    doc_id: str | None = Field(default=None, description="Limit to one document, e.g. CAR-S3-M-IV-R4.")
    mode: corpus.SearchMode = Field(default="hybrid", description="hybrid (default), vector, or bm25.")


class ClauseArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    clause_id: str = Field(min_length=1, max_length=150, description="Clause number, e.g. 3.8.1(a), or DOC:clause.")
    doc_id: str = Field(default=corpus.CAR_DOC, description="Document the clause belongs to.")


@tool("search_corpus", args_schema=SearchArgs)
def search_corpus(query: str, k: int = 5, doc_id: str | None = None, mode: corpus.SearchMode = "hybrid") -> list[dict]:
    """Search the DGCA CAR and IndiGo Conditions of Carriage for relevant chunks.

    Use it to discover which clauses apply; then use get_clause for exact text and
    get_references/get_referrers to follow cross-references. Each result has chunk_id,
    doc_id, clause_id, heading_path, text, score and retrieval (vector, bm25, hybrid,
    or bm25_fallback). Use mode="bm25" for exact clause numbers or legal terms.
    """
    return corpus.search_corpus(query, k=k, doc_id=doc_id, mode=mode)


@tool("get_clause", args_schema=ClauseArgs)
def get_clause(clause_id: str, doc_id: str = corpus.CAR_DOC) -> dict | None:
    """Return the exact source chunk for one clause, or null if it does not exist.

    The text is verbatim from the processed corpus, so it is safe to quote in citations.
    Qualified IDs such as INDIGO-COC-DOM:14.2 are accepted.
    """
    return corpus.get_clause(clause_id, doc_id)


LANGCHAIN_TOOLS = {t.name: t for t in (search_corpus, get_clause)}
