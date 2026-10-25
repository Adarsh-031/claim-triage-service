"""Real Chroma and local BM25 retrieval tools for the clause corpus.

Both public functions are LangChain tools. Infrastructure errors and timeouts are
returned as structured data, never raised into the agent graph.
"""
from __future__ import annotations

import copy
import hashlib
import json
import logging
import math
import queue
import re
import threading
from collections import Counter
from functools import lru_cache
from pathlib import Path
from typing import Literal

import chromadb
import httpx
from chromadb.errors import ChromaError
from chromadb.utils.embedding_functions import DefaultEmbeddingFunction
from langchain_core.tools import tool
from pydantic import ValidationError
from triage_common.contracts import Chunk
from triage_common.settings import settings

log = logging.getLogger(__name__)
CORPUS_VERSION = f"{settings.chroma_collection}:contextual-minilm-v1"
MIN_VECTOR_SIMILARITY = 0.25
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
        if hashlib.sha256(chunk.text.encode("utf-8")).hexdigest() != chunk.hash:
            raise ValueError(f"Chunk text hash mismatch: {chunk.chunk_id}")
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
    return copy.deepcopy(list(_load_chunks_cached(str(path.resolve()), stat.st_mtime_ns, stat.st_size)))


@lru_cache(maxsize=1)
def _bm25_index(path_text: str, mtime_ns: int, size: int):
    chunks = load_chunks(Path(path_text))
    documents = [_tokens(" ".join([chunk["clause_id"], *chunk.get("heading_path", []), chunk["text"]]))
                 for chunk in chunks]
    term_frequencies = [Counter(tokens) for tokens in documents]
    document_frequency: Counter[str] = Counter()
    for frequencies in term_frequencies:
        document_frequency.update(frequencies.keys())
    average_length = sum(map(len, documents)) / max(len(documents), 1)
    return chunks, documents, term_frequencies, document_frequency, average_length


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
        text = document.split("\n", 1)[1] if "\n" in document else document
        expected_hash = str(metadata.get("hash", ""))
        if not expected_hash or hashlib.sha256(text.encode("utf-8")).hexdigest() != expected_hash:
            log.warning("dropping Chroma result with source-text hash mismatch: %s", chunk_id)
            continue
        normalized.append({
            "chunk_id": chunk_id,
            "doc_id": metadata.get("doc_id", ""),
            "clause_id": metadata.get("clause_id", ""),
            "heading_path": _metadata_value(metadata.get("heading_path", [])) or [],
            "text": text,
            "score": 0.0,
            "page": (int(metadata["page"]) if metadata.get("page") is not None and int(metadata["page"]) > 0 else None),
            "refs": _metadata_value(metadata.get("refs", [])) or [],
            "hash": expected_hash,
        })
    return normalized


_VECTOR_LOCK = threading.Lock()
_VECTOR_BACKEND: tuple[object, object, object] | None = None
_TIMEOUT_GATE = threading.BoundedSemaphore(1)


def _vector_backend():
    global _VECTOR_BACKEND
    if _VECTOR_BACKEND is None:
        with _VECTOR_LOCK:
            if _VECTOR_BACKEND is None:
                client = chromadb.HttpClient(host=settings.chroma_url)
                embedding_function = DefaultEmbeddingFunction()
                collection = client.get_collection(
                    settings.chroma_collection, embedding_function=embedding_function
                )
                _VECTOR_BACKEND = (client, embedding_function, collection)
    return _VECTOR_BACKEND


def _reset_vector_backend() -> None:
    global _VECTOR_BACKEND
    with _VECTOR_LOCK:
        _VECTOR_BACKEND = None


def _vector_search(query: str, k: int, doc_id: str | None) -> list[dict]:
    try:
        _, _embedding_function, collection = _vector_backend()
        kwargs = {"query_texts": [query], "n_results": k,
                  "include": ["documents", "metadatas", "distances"]}
        if doc_id:
            kwargs["where"] = {"doc_id": doc_id}
        result = collection.query(**kwargs)
        ids = (result.get("ids") or [[]])[0]
        docs = (result.get("documents") or [[]])[0]
        metas = (result.get("metadatas") or [[]])[0]
        distances = (result.get("distances") or [[]])[0]
        chunks = _normalize_chroma(ids, docs, metas)
        for chunk, distance in zip(chunks, distances, strict=False):
            chunk["score"] = round(1.0 - float(distance), 6) if distance is not None else 0.0
        return [chunk for chunk in chunks if chunk["score"] >= MIN_VECTOR_SIMILARITY]
    except Exception:
        _reset_vector_backend()
        raise


def _bm25_search(query: str, k: int, doc_id: str | None) -> list[dict]:
    path = DEFAULT_CHUNKS_PATH
    stat = path.stat()
    chunks, documents, term_frequencies, document_frequency, average_length = _bm25_index(
        str(path.resolve()), stat.st_mtime_ns, stat.st_size
    )
    query = re.sub(r"(?<=\d)\s+\(", "(", query.strip().casefold())
    query_tokens = _tokens(query)
    count = len(documents)
    k1, b = 1.5, 0.75
    scores: list[float] = []
    for tokens, frequencies in zip(documents, term_frequencies, strict=True):
        score = 0.0
        length_norm = k1 * (1 - b + b * len(tokens) / max(average_length, 1))
        for token in query_tokens:
            frequency = frequencies.get(token, 0)
            if not frequency:
                continue
            document_count = document_frequency.get(token, 0)
            inverse_frequency = math.log(
                1 + (count - document_count + 0.5) / (document_count + 0.5)
            )
            score += inverse_frequency * frequency * (k1 + 1) / (frequency + length_norm)
        scores.append(score)
    ranked = sorted(enumerate(scores), key=lambda pair: (-float(pair[1]), chunks[pair[0]]["chunk_id"]))
    output = []
    for index_position, score in ranked:
        chunk = chunks[index_position]
        if doc_id and chunk["doc_id"] != doc_id:
            continue
        if not query_tokens or float(score) <= 0:
            continue
        output.append({**copy.deepcopy(chunk), "score": float(score)})
        if len(output) >= k:
            break
    return output


def _within_timeout(function, *args):
    # Python cannot forcibly stop a blocked HTTP call. One daemon worker and a
    # single-flight gate prevent repeated timeouts from leaking unbounded threads.
    if not _TIMEOUT_GATE.acquire(blocking=False):
        raise TimeoutError("a previous retrieval call is still blocked")
    completed: queue.Queue[tuple[bool, object]] = queue.Queue(maxsize=1)

    def run() -> None:
        try:
            completed.put((True, function(*args)))
        except BaseException as exc:
            completed.put((False, exc))
        finally:
            _TIMEOUT_GATE.release()

    threading.Thread(target=run, name="corpus-tool", daemon=True).start()
    try:
        succeeded, value = completed.get(timeout=settings.tool_timeout_s)
    except queue.Empty as exc:
        raise TimeoutError(f"retrieval exceeded {settings.tool_timeout_s:g}s") from exc
    if succeeded:
        return value
    raise value


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
    except TimeoutError as exc:
        if mode == "vector":
            _reset_vector_backend()
        log.warning("corpus search timed out: %s", exc)
        return {"chunks": [], ERROR_KEY: "timeout"}
    except (ChromaError, httpx.HTTPError, OSError, ValidationError, ValueError) as exc:
        if mode == "vector":
            _reset_vector_backend()
        log.warning("corpus search backend/data unavailable (%s): %s", type(exc).__name__, exc)
        return {"chunks": [], ERROR_KEY: "unavailable"}
    except Exception:
        log.exception("unexpected internal error during corpus search")
        return {"chunks": [], ERROR_KEY: "internal_error"}


@tool
def search_corpus(
    query: str,
    k: int = 5,
    doc_id: Literal["CAR-S3-M-IV-R4", "INDIGO-COC-DOM", "INDIGO-COC-INTL"] | None = None,
    mode: Literal["vector", "bm25"] = "vector",
) -> dict:
    """Search indexed clause chunks by semantic vector similarity or lexical BM25 ranking.

    Args:
        query: Natural-language query (for example, "cancellation compensation").
        k: Maximum number of results to return, from 1 to 100.
        doc_id: Optional exact source ID: CAR-S3-M-IV-R4 for DGCA regulatory requirements,
            INDIGO-COC-DOM for domestic IndiGo carriage, or INDIGO-COC-INTL for international
            IndiGo carriage. Filter to CAR for the regulatory floor; use the matching IndiGo
            document only for additional carrier terms.
        mode: "vector" queries Chroma; "bm25" ranks the local corpus with a cached BM25 index.
        Vector matches below the minimum cosine-similarity threshold are discarded.

    Returns a dictionary with "chunks" (each containing clause_id, heading_path, text,
    score and provenance fields) and "retrieval_error". On timeout or backend failure,
    chunks is empty and retrieval_error is set; failures are not raised into the graph.
    """
    return _search_impl(query, k, doc_id, mode)


def _get_clause_impl(clause_id: str, doc_id: str = "CAR-S3-M-IV-R4") -> dict:
    clause_key = re.sub(r"\s+", "", clause_id).casefold()
    if not clause_key or not doc_id.strip():
        return {"chunk": None, ERROR_KEY: "clause_id and doc_id are required"}
    try:
        chunks = _within_timeout(load_chunks, DEFAULT_CHUNKS_PATH)
        matches = [
            chunk for chunk in chunks
            if re.sub(r"\s+", "", chunk["clause_id"]).casefold() == clause_key
            and chunk["doc_id"] == doc_id.strip()
        ]
        if not matches:
            return {"chunk": None, ERROR_KEY: "not found"}
        # Exact clause lookup is deterministic even where a corpus has multiple views.
        matches.sort(key=lambda chunk: chunk["chunk_id"])
        return {"chunk": copy.deepcopy(matches[0]), ERROR_KEY: None}
    except TimeoutError as exc:
        log.warning("clause lookup timed out: %s", exc)
        return {"chunk": None, ERROR_KEY: "timeout"}
    except (ChromaError, httpx.HTTPError, OSError, ValidationError, ValueError) as exc:
        log.warning("clause lookup backend/data unavailable (%s): %s", type(exc).__name__, exc)
        return {"chunk": None, ERROR_KEY: "unavailable"}
    except Exception:
        log.exception("unexpected internal error during clause lookup")
        return {"chunk": None, ERROR_KEY: "internal_error"}


@tool
def get_clause(
    clause_id: str,
    doc_id: Literal["CAR-S3-M-IV-R4", "INDIGO-COC-DOM", "INDIGO-COC-INTL"] = "CAR-S3-M-IV-R4",
) -> dict:
    """Retrieve the exact normalized chunk for a clause from chunks.jsonl.

    Args:
        clause_id: Clause identifier such as "3.8.1(a)".
        doc_id: Exact source ID: CAR-S3-M-IV-R4 (DGCA regulatory floor), INDIGO-COC-DOM
            (domestic carriage), or INDIGO-COC-INTL (international carriage).

    Returns a dictionary containing the exact normalized "chunk" and "retrieval_error".
    Returns chunk=None and a retrieval_error when the clause is unavailable.
    """
    return _get_clause_impl(clause_id, doc_id)
