"""Read-only retrieval over the ingestion artifacts, with document-qualified edges."""
from __future__ import annotations

import json
import re
from functools import lru_cache
from hashlib import sha256
from pathlib import Path

from triage_common.contracts import Chunk
from triage_common.settings import settings

CAR_DOC = "CAR-S3-M-IV-R4"
DEFAULT_CORPUS_DIR = Path(__file__).resolve().parents[4] / "corpus" / "processed"
ARTIFACTS = ("chunks.jsonl", "refs.json", "definitions.json")


def qualified(clause_id: str, doc_id: str = CAR_DOC) -> str:
    return clause_id if ":" in clause_id else f"{doc_id}:{clause_id}"


class Corpus:
    def __init__(self, directory: Path):
        contents = {name: (directory / name).read_bytes() for name in ARTIFACTS}
        chunks = [Chunk.model_validate_json(line) for line in contents["chunks.jsonl"].splitlines() if line.strip()]
        if not chunks or len({c.chunk_id for c in chunks}) != len(chunks):
            raise ValueError("Corpus must contain nonempty, unique chunk IDs")
        if any(sha256(c.text.encode()).hexdigest() != c.hash for c in chunks):
            raise ValueError("Corpus chunk hash mismatch")
        self.chunks = {c.chunk_id: c.model_dump(mode="json") for c in chunks}
        self.clauses = {qualified(c.clause_id, c.doc_id): self.chunks[c.chunk_id] for c in chunks}
        self.graph = json.loads(contents["refs.json"])
        self.definitions = json.loads(contents["definitions.json"])
        self.version = "processed-" + sha256(b"".join(contents.values())).hexdigest()[:16]

    def clause(self, clause_id: str, doc_id: str = CAR_DOC) -> dict | None:
        return self.clauses.get(qualified(clause_id, doc_id))


@lru_cache(maxsize=2)
def _load(directory: Path, signature: tuple) -> Corpus:
    return Corpus(directory)


def get_corpus() -> Corpus:
    directory = Path(settings.corpus_dir) if settings.corpus_dir else DEFAULT_CORPUS_DIR
    signature = tuple((p.stat().st_mtime_ns, p.stat().st_size) for p in (directory / n for n in ARTIFACTS))
    return _load(directory, signature)


def search_corpus(query: str, k: int = 5, doc_id: str | None = None) -> list[dict]:
    """Deterministic lexical retrieval; graph expansion supplies modifying clauses.

    Uses the same complete chunks as Chroma, without needing a vector service for
    exact/graph research. Semantic/hybrid ranking can replace this baseline later.
    """
    if k <= 0:
        return []
    terms = set(re.findall(r"[\w.]+", query.casefold()))
    ranked = []
    for chunk in get_corpus().chunks.values():
        if doc_id and chunk["doc_id"] != doc_id:
            continue
        words = set(re.findall(r"[\w.]+", chunk["text"].casefold()))
        score = len(terms & words) / max(1, len(terms))
        if score:
            ranked.append({**chunk, "score": score})
    return sorted(ranked, key=lambda c: (-c["score"], len(c["text"]), c["chunk_id"]))[:min(k, 10)]


def get_clause(clause_id: str, doc_id: str = CAR_DOC) -> dict | None:
    return get_corpus().clause(clause_id, doc_id)


def get_references(clause_id: str, doc_id: str = CAR_DOC) -> list[str]:
    """Return qualified targets, preserving unresolved edges without guessing."""
    return get_corpus().graph["refs"].get(qualified(clause_id, doc_id), [])


def get_referrers(clause_id: str, doc_id: str = CAR_DOC) -> list[str]:
    return get_corpus().graph["referrers"].get(qualified(clause_id, doc_id), [])


def get_definition(term: str, doc_id: str | None = None) -> list[dict]:
    """Resolve definition records to their full, citable source chunks."""
    corpus = get_corpus()
    found = {}
    for entry in corpus.definitions.get(term.strip().casefold(), []):
        if doc_id and entry["doc_id"] != doc_id:
            continue
        chunk = corpus.chunks.get(entry["chunk_id"])
        if chunk is None or entry["text"] not in chunk["text"]:
            raise ValueError("Definition does not resolve to its source chunk")
        found[chunk["chunk_id"]] = chunk
    return list(found.values())
