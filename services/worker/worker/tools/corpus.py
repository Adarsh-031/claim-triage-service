"""Real Chroma and local BM25 retrieval tools for the clause corpus.

Both public functions are LangChain tools. Infrastructure errors and timeouts are
returned as structured data, never raised into the agent graph.
"""
from __future__ import annotations

import json
import logging
import math
import re
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from functools import lru_cache
from pathlib import Path
from typing import Literal

import chromadb
from chromadb.utils.embedding_functions import DefaultEmbeddingFunction
from langchain_core.tools import tool
from triage_common.contracts import Chunk
from triage_common.settings import settings

log = logging.getLogger(__name__)
CORPUS_VERSION = "processed-chunks-v1"
ERROR_KEY = "retrieval_error"
DEFAULT_CHUNKS_PATH = Path(settings.corpus_chunks_path)


def _tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+(?:[.][a-z0-9]+)*(?:\([a-z0-9]+\))*", text.casefold())


@lru_cache(maxsize=1)
def _load_chunks_cached(path_text: str, mtime_ns: int, size: int) -> tuple[dict, ...]:
    path = Path(path_text)
    chunks: list[Chunk] = []
    seen: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        chunk = Chunk.model_validate_json(line)
        if chunk.chunk_id in seen:
            raise ValueError(f"Duplicate chunk ID in processed corpus: {chunk.chunk_id}")
        seen.add(chunk.chunk_id)
        chunks.append(chunk)
    if not chunks:
        raise ValueError(f"Processed corpus is empty: {path}")
    return tuple(chunk.model_dump(mode="json") for chunk in chunks)


def load_chunks(path: Path = DEFAULT_CHUNKS_PATH) -> list[dict]:
    """Load and validate normalized chunks once per corpus file version."""
    stat = path.stat()
    return list(_load_chunks_cached(str(path.resolve()), stat.st_mtime_ns, stat.st_size))


@lru_cache(maxsize=1)
def _bm25_index(path_text: str, mtime_ns: int, size: int):
    chunks = load_chunks(Path(path_text))
    documents = [_tokens(" ".join([chunk["clause_id"], *chunk.get("heading_path", []), chunk["text"]]))
                 for chunk in chunks]
    document_frequency: dict[str, int] = {}
    for tokens in documents:
        for token in set(tokens):
            document_frequency[token] = document_frequency.get(token, 0) + 1
    average_length = sum(map(len, documents)) / max(len(documents), 1)
    return chunks, documents, document_frequency, average_length


def _metadata_value(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return value
    return value


def _normalize_chroma(ids: list[str], documents: list[str], metadatas: list[dict]) -> list[dict]:
    normalized = []
    for chunk_id, document, metadata in zip(ids, documents, metadatas, strict=False):
        metadata = metadata or {}
        # Chroma's stored document includes provenance headings on its first line.
        text = document.split("\n", 1)[1] if "\n" in document else document
        normalized.append({
            "chunk_id": chunk_id,
            "doc_id": metadata.get("doc_id", ""),
            "clause_id": metadata.get("clause_id", ""),
            "heading_path": _metadata_value(metadata.get("heading_path", [])) or [],
            "text": text,
            "score": 0.0,
            "page": (int(metadata["page"]) if metadata.get("page") is not None and int(metadata["page"]) > 0 else None),
            "refs": _metadata_value(metadata.get("refs", [])) or [],
            "hash": metadata.get("hash", ""),
        })
    return normalized


def _vector_search(query: str, k: int, doc_id: str | None) -> list[dict]:
    client = chromadb.HttpClient(host=settings.chroma_url)
    collection = client.get_collection(settings.chroma_collection, embedding_function=DefaultEmbeddingFunction())
    kwargs = {"query_texts": [query], "n_results": k, "include": ["documents", "metadatas", "distances"]}
    if doc_id:
        kwargs["where"] = {"doc_id": doc_id}
    result = collection.query(**kwargs)
    ids = (result.get("ids") or [[]])[0]
    docs = (result.get("documents") or [[]])[0]
    metas = (result.get("metadatas") or [[]])[0]
    distances = (result.get("distances") or [[]])[0]
    chunks = _normalize_chroma(ids, docs, metas)
    # Chroma is configured for cosine distance: convert distance to an intuitive
    # higher-is-better similarity score and guard odd server values.
    for chunk, distance in zip(chunks, distances, strict=False):
        chunk["score"] = round(1.0 - float(distance), 6) if distance is not None else 0.0
    return chunks


def _bm25_search(query: str, k: int, doc_id: str | None) -> list[dict]:
    path = DEFAULT_CHUNKS_PATH
    stat = path.stat()
    chunks, documents, document_frequency, average_length = _bm25_index(
        str(path.resolve()), stat.st_mtime_ns, stat.st_size
    )
    query_tokens = _tokens(query)
    count = len(documents)
    k1, b = 1.5, 0.75
    scores: list[float] = []
    for tokens in documents:
        frequencies = {token: tokens.count(token) for token in set(tokens)}
        score = 0.0
        length_norm = k1 * (1 - b + b * len(tokens) / max(average_length, 1))
        for token in query_tokens:
            frequency = frequencies.get(token, 0)
            if not frequency:
                continue
            inverse_frequency = max(0.0, math.log(
                1 + (count - document_frequency.get(token, 0) + 0.5)
                / (document_frequency.get(token, 0) + 0.5)
            ))
            score += inverse_frequency * frequency * (k1 + 1) / (frequency + length_norm)
        scores.append(score)
    ranked = sorted(enumerate(scores), key=lambda pair: (-float(pair[1]), chunks[pair[0]]["chunk_id"]))
    output = []
    for index_position, score in ranked:
        chunk = chunks[index_position]
        if doc_id and chunk["doc_id"] != doc_id:
            continue
        if not _tokens(query) or float(score) <= 0:
            continue
        output.append({**chunk, "score": float(score)})
        if len(output) >= k:
            break
    return output


def _within_timeout(function, *args):
    # Use a daemon thread so a stuck HTTP client cannot hold up graph execution.
    # The underlying tool has its own HTTP timeout where supported; this boundary
    # guarantees that the caller receives control within the configured budget.
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="corpus-tool")
    future = executor.submit(function, *args)
    try:
        return future.result(timeout=settings.tool_timeout_s)
    except FutureTimeout as exc:
        future.cancel()
        raise TimeoutError(f"retrieval exceeded {settings.tool_timeout_s:g}s") from exc
    finally:
        executor.shutdown(wait=False, cancel_futures=True)


def _search_impl(
    query: str, k: int = 5, doc_id: str | None = None, mode: Literal["vector", "bm25"] = "vector",
) -> dict:
    if not query.strip():
        return {"chunks": [], ERROR_KEY: "empty query"}
    if k <= 0:
        return {"chunks": [], ERROR_KEY: "k must be greater than zero"}
    if mode not in ("vector", "bm25"):
        return {"chunks": [], ERROR_KEY: "mode must be 'vector' or 'bm25'"}
    try:
        search = _vector_search if mode == "vector" else _bm25_search
        chunks = _within_timeout(search, query.strip(), min(k, 100), doc_id)
        return {"chunks": chunks, ERROR_KEY: None}
    except (TimeoutError, FutureTimeout) as exc:
        log.warning("corpus search timed out: %s", exc)
        return {"chunks": [], ERROR_KEY: "timeout"}
    except Exception as exc:
        log.warning("corpus search failed (%s): %s", type(exc).__name__, exc)
        return {"chunks": [], ERROR_KEY: "unavailable"}


@tool
def search_corpus(
    query: str,
    k: int = 5,
    doc_id: str | None = None,
    mode: Literal["vector", "bm25"] = "vector",
) -> dict:
    """Search indexed clause chunks by semantic vector similarity or lexical BM25 ranking.

    Args:
        query: Natural-language query (for example, "cancellation compensation").
        k: Maximum number of results to return, from 1 to 100.
        doc_id: Optional exact source document ID filter.
        mode: "vector" queries the configured Chroma collection; "bm25" ranks the local
            corpus/processed/chunks.jsonl file using a per-worker cached BM25 index.

    Returns a dictionary with "chunks" (each containing clause_id, heading_path, text,
    score and provenance fields) and "retrieval_error". On timeout or backend failure,
    chunks is empty and retrieval_error is set; failures are not raised into the graph.
    """
    return _search_impl(query, k, doc_id, mode)


def _get_clause_impl(clause_id: str, doc_id: str = "CAR-S3-M-IV-R4") -> dict:
    if not clause_id.strip() or not doc_id.strip():
        return {"chunk": None, ERROR_KEY: "clause_id and doc_id are required"}
    try:
        chunks = _within_timeout(load_chunks, DEFAULT_CHUNKS_PATH)
        matches = [
            chunk for chunk in chunks
            if chunk["clause_id"].casefold() == clause_id.strip().casefold()
            and chunk["doc_id"] == doc_id.strip()
        ]
        if not matches:
            return {"chunk": None, ERROR_KEY: "not found"}
        # Exact clause lookup is deterministic even where a corpus has multiple views.
        matches.sort(key=lambda chunk: chunk["chunk_id"])
        return {"chunk": matches[0], ERROR_KEY: None}
    except (TimeoutError, FutureTimeout) as exc:
        log.warning("clause lookup timed out: %s", exc)
        return {"chunk": None, ERROR_KEY: "timeout"}
    except Exception as exc:
        log.warning("clause lookup failed (%s): %s", type(exc).__name__, exc)
        return {"chunk": None, ERROR_KEY: "unavailable"}


@tool
def get_clause(clause_id: str, doc_id: str = "CAR-S3-M-IV-R4") -> dict:
    """Retrieve the exact normalized chunk for a clause from chunks.jsonl.

    Args:
        clause_id: Clause identifier such as "3.8.1(a)".
        doc_id: Exact source document ID; defaults to CAR-S3-M-IV-R4.

    Returns a dictionary containing the exact normalized "chunk" and "retrieval_error".
    Returns chunk=None and a retrieval_error when the clause is unavailable.
    """
    return _get_clause_impl(clause_id, doc_id)
