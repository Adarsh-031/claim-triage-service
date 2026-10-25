"""Synchronize processed chunks with Chroma, or verify contents and known retrieval."""
from __future__ import annotations

import argparse
import hashlib
import json
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


def verify(chunks: list[Chunk], target) -> dict:
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
    return {"count": len(expected), "retrieved": matches["ids"][0]}


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
