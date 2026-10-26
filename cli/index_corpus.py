"""Synchronize processed chunks with Chroma, or verify contents and known retrieval."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
from collections import Counter
from pathlib import Path

import chromadb
from chromadb.utils.embedding_functions import DefaultEmbeddingFunction
from triage_common.contracts import Chunk
from triage_common.settings import settings

INDEX_VERSION = "contextual-minilm-v1"
MODEL = "sentence-transformers/all-MiniLM-L6-v2"


def load_chunks(path: Path) -> list[Chunk]:
    chunks = [Chunk.model_validate_json(line) for line in path.read_text().splitlines() if line.strip()]
    if not chunks or len({chunk.chunk_id for chunk in chunks}) != len(chunks):
        raise ValueError("Corpus must contain nonempty, unique chunk IDs")
    if any(hashlib.sha256(chunk.text.encode()).hexdigest() != chunk.hash for chunk in chunks):
        raise ValueError("Chunk text hash mismatch; rebuild the processed corpus")
    return sorted(chunks, key=lambda chunk: chunk.chunk_id)


def record(chunk: Chunk) -> tuple[str, dict]:
    document = " > ".join([chunk.doc_id, *chunk.heading_path]) + "\n" + chunk.text
    fingerprint = hashlib.sha256(chunk.model_dump_json().encode()).hexdigest()
    return document, {
        "doc_id": chunk.doc_id, "clause_id": chunk.clause_id, "hash": chunk.hash,
        "fingerprint": fingerprint, "heading_path": json.dumps(chunk.heading_path),
        "page": chunk.page if chunk.page is not None else -1, "refs": json.dumps(chunk.refs),
    }


def validate_collection(target) -> None:
    if settings.embedding_model != MODEL:
        raise ValueError(f"Supported EMBEDDING_MODEL is {MODEL}; use a new index version for other models")
    metadata = target.metadata or {}
    if metadata.get("embedding_model") != MODEL or metadata.get("index_version") != INDEX_VERSION:
        raise ValueError("Collection model/index version differs; configure a new CHROMA_COLLECTION")


def collection(client, name: str):
    if settings.embedding_model != MODEL:
        raise ValueError(f"Supported EMBEDDING_MODEL is {MODEL}; use a new index version for other models")
    result = client.get_or_create_collection(
        name, embedding_function=DefaultEmbeddingFunction(),
        metadata={"embedding_model": MODEL, "index_version": INDEX_VERSION},
        configuration={"hnsw": {"space": "cosine"}},
    )
    validate_collection(result)
    return result


def sync(chunks: list[Chunk], target, batch_size: int = 64, *, force: bool = False) -> dict:
    """Upsert changes before pruning stale IDs. Reruns repair interrupted writes.

    This collection is owned by ingestion; serialize ingest jobs and run before consumers.
    """
    existing = target.get(include=["metadatas"])
    fingerprints = {key: (meta or {}).get("fingerprint")
                    for key, meta in zip(existing["ids"], existing["metadatas"], strict=True)}
    changed = [chunk for chunk in chunks
               if force or fingerprints.get(chunk.chunk_id) != record(chunk)[1]["fingerprint"]]
    for start in range(0, len(changed), batch_size):
        batch = changed[start:start + batch_size]
        records = [record(chunk) for chunk in batch]
        target.upsert(ids=[chunk.chunk_id for chunk in batch],
                      documents=[item[0] for item in records], metadatas=[item[1] for item in records])
    stale = sorted(set(fingerprints) - {chunk.chunk_id for chunk in chunks})
    for start in range(0, len(stale), batch_size):
        target.delete(ids=stale[start:start + batch_size])
    return {"count": target.count(), "upserted": len(changed), "deleted": len(stale)}


def _bm25_top_ids(chunks: list[Chunk], query: str, k: int, doc_id: str) -> list[str]:
    """Return a deterministic BM25 top-k over the exact processed corpus."""
    query = re.sub(r"(?<=\d)\s+\(", "(", query.casefold())

    def tokenize(text: str) -> list[str]:
        return re.findall(r"[a-z0-9]+(?:[.][a-z0-9]+)*(?:\([a-z0-9]+\))*", text.casefold())

    selected = [chunk for chunk in chunks if chunk.doc_id == doc_id]
    documents = [tokenize(" ".join([chunk.clause_id, *chunk.heading_path, chunk.text]))
                 for chunk in selected]
    if not documents:
        return []
    query_tokens = tokenize(query)
    frequencies = [Counter(tokens) for tokens in documents]
    document_frequency: Counter[str] = Counter()
    for term_counts in frequencies:
        document_frequency.update(term_counts.keys())
    average_length = sum(map(len, documents)) / len(documents)
    scores = []
    for chunk, tokens, term_counts in zip(selected, documents, frequencies, strict=True):
        length_norm = 1.5 * (1 - 0.75 + 0.75 * len(tokens) / max(average_length, 1))
        score = 0.0
        for token in query_tokens:
            frequency = term_counts.get(token, 0)
            if frequency:
                df = document_frequency[token]
                idf = math.log(1 + (len(documents) - df + 0.5) / (df + 0.5))
                score += idf * frequency * 2.5 / (frequency + length_norm)
        scores.append((score, chunk.chunk_id))
    return [chunk_id for score, chunk_id in sorted(scores, key=lambda item: (-item[0], item[1]))
            if score > 0][:k]


def _rrf_top_ids(rankings: list[list[str]], k: int, *, rank_constant: int = 60) -> list[str]:
    """Fuse ranked candidate lists deterministically with reciprocal-rank fusion."""
    scores: dict[str, float] = {}
    for ranking in rankings:
        for rank, chunk_id in enumerate(ranking, start=1):
            scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (rank_constant + rank)
    return sorted(scores, key=lambda chunk_id: (-scores[chunk_id], chunk_id))[:k]


def verify_cancellation_retrieval(chunks: list[Chunk], target) -> dict:
    expected_id = "CAR-S3-M-IV-R4:3.3.2:0001"
    if expected_id not in {chunk.chunk_id for chunk in chunks}:
        raise ValueError(f"Known cancellation compensation chunk absent: {expected_id}")
    doc_filter = {"doc_id": "CAR-S3-M-IV-R4"}
    vector_result = target.query(
        query_texts=["cancellation compensation"], n_results=min(20, len(chunks)),
        where=doc_filter,
    )
    vector_ids = (vector_result.get("ids") or [[]])[0]
    clause_result = target.query(
        query_texts=["cancellation compensation clause 3.3.2"],
        n_results=min(10, len(chunks)), where=doc_filter,
    )
    clause_ids = (clause_result.get("ids") or [[]])[0]
    bm25_ids = _bm25_top_ids(chunks, "cancellation compensation", 20, "CAR-S3-M-IV-R4")
    vector_top = _rrf_top_ids([vector_ids, clause_ids, bm25_ids], 3)
    bm25_top = bm25_ids[:3]
    if expected_id not in vector_top:
        raise ValueError(f"Cancellation clause 3.3.2 absent from fused real-corpus top 3: {vector_top}")
    if expected_id not in bm25_top:
        raise ValueError(f"Cancellation clause 3.3.2 is not in BM25 top 3: {bm25_top}")
    return {"cancellation_compensation_vector_reranked_top3": vector_top,
            "cancellation_compensation_bm25_top3": bm25_top}


def verify(chunks: list[Chunk], target, *, require_retrieval_smoke: bool = True) -> dict:
    validate_collection(target)
    stored = target.get(include=["documents", "metadatas", "embeddings"])
    expected = {chunk.chunk_id: record(chunk) for chunk in chunks}
    if set(stored["ids"]) != set(expected):
        raise ValueError("Collection IDs differ from processed corpus; re-run indexing")
    for i, key in enumerate(stored["ids"]):
        document, metadata = expected[key]
        if stored["documents"][i] != document or stored["metadatas"][i] != metadata:
            raise ValueError(f"Collection record differs from processed corpus: {key}")
        if stored["embeddings"] is None or len(stored["embeddings"][i]) != 384:
            raise ValueError(f"Missing or invalid MiniLM embedding: {key}")
    known = "CAR-S3-M-IV-R4:3.8.1(a):0001"
    if known not in expected:
        raise ValueError(f"Known smoke-check chunk absent: {known}")
    matches = target.query(query_texts=["Meals and refreshments in relation to waiting time"],
                           n_results=min(5, len(chunks)), where={"doc_id": "CAR-S3-M-IV-R4"})
    if known not in matches["ids"][0]:
        raise ValueError(f"Known facilities passage was not retrieved: {matches['ids'][0]}")
    result = {"count": len(expected), "retrieved": matches["ids"][0]}
    if require_retrieval_smoke:
        result.update(verify_cancellation_retrieval(chunks, target))
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chunks", type=Path, default=Path("corpus/processed/chunks.jsonl"))
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--verify", action="store_true", help="check contents/retrieval without writing")
    mode.add_argument("--force", action="store_true", help="re-embed all records to repair content/vector drift")
    args = parser.parse_args()
    try:
        chunks = load_chunks(args.chunks)
        client = chromadb.HttpClient(host=settings.chroma_url)
        if args.verify:
            target = client.get_collection(settings.chroma_collection, embedding_function=DefaultEmbeddingFunction())
            result = verify(chunks, target)
        else:
            result = sync(chunks, collection(client, settings.chroma_collection), force=args.force)
        print(json.dumps({"collection": settings.chroma_collection, **result}, sort_keys=True))
    except Exception as exc:
        parser.exit(1, f"index: {exc}\n")


if __name__ == "__main__":
    main()
