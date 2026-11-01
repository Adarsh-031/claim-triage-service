"""Read-only retrieval over the ingestion artifacts, with document-qualified edges."""
from __future__ import annotations

import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from functools import lru_cache
from hashlib import sha256
from pathlib import Path
from typing import Literal

from rank_bm25 import BM25Okapi
from triage_common.contracts import Chunk
from triage_common.corpus_version import CORPUS_ARTIFACTS, corpus_version_from_contents
from triage_common.settings import settings

CAR_DOC = "CAR-S3-M-IV-R4"
DEFAULT_CORPUS_DIR = Path(__file__).resolve().parents[4] / "corpus" / "processed"
ARTIFACTS = CORPUS_ARTIFACTS
SearchMode = Literal["hybrid", "vector", "bm25"]
MAX_K = 10
RRF_K = 60  # standard reciprocal-rank-fusion constant
# Clause numbers such as 3.8.1(a) stay one token so "3.8.1(a)" matches exactly.
TOKEN = re.compile(r"\d+(?:\.\d+)*(?:\([a-z0-9]+\))*|[a-z]+")


class RetrievalUnavailable(OSError):
    """Vector index unreachable, slow, or out of sync with chunks.jsonl.

    An OSError so the research loop turns it into a tool error instead of a crash.
    """


def tokenize(text: str) -> list[str]:
    return TOKEN.findall(text.casefold())


SUBCLAUSE = re.compile(r"^(?P<parent>.+)\([a-z0-9]+\)$")


def parent_clause(clause_id: str) -> str | None:
    """3.3.2(a) -> 3.3.2; 3.2.2(c)(i) -> 3.2.2(c); top-level clauses have no parent."""
    match = SUBCLAUSE.match(clause_id)
    return match["parent"] if match else None


def index_text(chunk: dict, parent: dict | None = None) -> str:
    """Headings + body, as Chroma embeds them (cli/index_corpus.record).

    For BM25 a subclause also carries its parent's lead-in sentence, so "a) INR 5,000 ..."
    is findable by the word "compensation" that only its parent 3.3.2 states.
    """
    text = " > ".join([chunk["doc_id"], *chunk.get("heading_path", [])]) + "\n" + chunk["text"]
    if parent:
        text += "\n" + re.split(r"\s[a-z]\)\s", parent["text"], maxsplit=1)[0]
    return text


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
        self.version = corpus_version_from_contents(contents)
        # BM25 is built once per corpus version (get_corpus caches the instance).
        self.order = sorted(self.chunks)
        self.bm25 = BM25Okapi([tokenize(index_text(self.chunks[key], self.parent(self.chunks[key])))
                               for key in self.order])

    def clause(self, clause_id: str, doc_id: str = CAR_DOC) -> dict | None:
        return self.clauses.get(qualified(clause_id, doc_id))

    def parent(self, chunk: dict) -> dict | None:
        parent = parent_clause(chunk["clause_id"])
        return self.clause(parent, chunk["doc_id"]) if parent else None


@lru_cache(maxsize=2)
def _load(directory: Path, signature: tuple) -> Corpus:
    return Corpus(directory)


def get_corpus_dir() -> Path:
    return Path(settings.corpus_dir) if settings.corpus_dir else DEFAULT_CORPUS_DIR


def get_corpus() -> Corpus:
    directory = get_corpus_dir()
    signature = tuple((p.stat().st_mtime_ns, p.stat().st_size) for p in (directory / n for n in ARTIFACTS))
    return _load(directory, signature)


def bm25_search(query: str, k: int, doc_id: str | None = None) -> list[dict]:
    corpus = get_corpus()
    terms = tokenize(query)
    if not terms:
        return []
    scores = corpus.bm25.get_scores(terms)
    ranked = sorted(
        ((float(score), key) for score, key in zip(scores, corpus.order, strict=True)
         if score > 0 and (not doc_id or corpus.chunks[key]["doc_id"] == doc_id)),
        key=lambda item: (-item[0], item[1]),
    )
    return [{**corpus.chunks[key], "score": round(score, 4), "retrieval": "bm25"} for score, key in ranked[:k]]


class _Vector:
    """Lazy Chroma client with a hard per-call timeout and a short failure cooldown."""

    def __init__(self):
        self.lock = threading.Lock()
        self.pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="chroma")
        self.collection = None
        self.down_until = 0.0

    def enabled(self) -> bool:
        return bool(settings.chroma_url) and time.monotonic() >= self.down_until

    def _collection(self):
        with self.lock:
            if self.collection is None:
                import chromadb
                from chromadb.utils.embedding_functions import DefaultEmbeddingFunction

                client = chromadb.HttpClient(host=settings.chroma_url)
                collection = client.get_collection(settings.chroma_collection,
                                                   embedding_function=DefaultEmbeddingFunction())
                if (collection.metadata or {}).get("embedding_model") != settings.embedding_model:
                    raise RetrievalUnavailable("Chroma collection was built with a different embedding model")
                self.collection = collection
            return self.collection

    def _query(self, query: str, k: int, doc_id: str | None) -> list[tuple[str, float, str | None]]:
        result = self._collection().query(query_texts=[query], n_results=k,
                                          where={"doc_id": doc_id} if doc_id else None,
                                          include=["distances", "metadatas"])
        hits = []
        for key, distance, meta in zip(result["ids"][0], result["distances"][0], result["metadatas"][0],
                                       strict=True):
            hits.append((key, 1.0 - float(distance), (meta or {}).get("hash")))
        return hits

    def search(self, query: str, k: int, doc_id: str | None = None) -> list[dict]:
        if not self.enabled():
            raise RetrievalUnavailable("Vector search disabled or cooling down after a failure")
        corpus = get_corpus()
        try:
            hits = self.pool.submit(self._query, query, k, doc_id).result(timeout=settings.retrieval_timeout_s)
        except FutureTimeout as exc:
            self._fail()
            raise RetrievalUnavailable(f"Chroma query exceeded {settings.retrieval_timeout_s}s") from exc
        except RetrievalUnavailable:
            self._fail()
            raise
        except Exception as exc:  # network, missing collection, server errors
            self._fail()
            raise RetrievalUnavailable(f"Chroma unavailable: {type(exc).__name__}") from exc
        results = []
        for key, score, digest in hits:
            chunk = corpus.chunks.get(key)
            # Text always comes from chunks.jsonl; Chroma only ranks. A stale index is an error.
            if chunk is None or digest != chunk["hash"]:
                self._fail()
                raise RetrievalUnavailable("Chroma index is out of sync with chunks.jsonl; re-run ingest")
            results.append({**chunk, "score": round(score, 4), "retrieval": "vector"})
        return results

    def _fail(self) -> None:
        with self.lock:
            self.collection = None
            self.down_until = time.monotonic() + settings.retrieval_cooldown_s

    def reset(self) -> None:
        with self.lock:
            self.collection = None
            self.down_until = 0.0


VECTOR = _Vector()


def fuse(rankings: list[list[dict]], k: int, query: str = "") -> list[dict]:
    """Reciprocal rank fusion, then two clause-aware adjustments.

    RRF uses rank, not raw score, so BM25 and cosine scores are comparable.
    Parent roll-up: a subclause hit also credits its parent, so three tier hits
    (3.3.2(a), (b), (c)) surface the governing clause 3.3.2 itself.
    Exact clause numbers named in the query (e.g. "3.8.1(a)") go first.
    """
    corpus = get_corpus()
    scores: dict[str, float] = {}
    chunks: dict[str, dict] = {}
    for ranking in rankings:
        for rank, chunk in enumerate(ranking, start=1):
            scores[chunk["chunk_id"]] = scores.get(chunk["chunk_id"], 0.0) + 1.0 / (RRF_K + rank)
            chunks.setdefault(chunk["chunk_id"], chunk)
    children: dict[str, list[float]] = {}
    for key, score in list(scores.items()):
        parent = corpus.parent(chunks[key])
        if parent and parent["chunk_id"] != key:
            children.setdefault(parent["chunk_id"], []).append(score)
            chunks.setdefault(parent["chunk_id"], parent)
    for key, child_scores in children.items():
        # Best three children at half weight: big families (3.4.1 has many) cannot swamp the list.
        scores[key] = scores.get(key, 0.0) + sum(sorted(child_scores, reverse=True)[:3]) / 2
    named = {token for token in tokenize(query) if "." in token}  # "3.8.1(a)", never a bare "3"
    ordered = sorted(scores, key=lambda key: (chunks[key]["clause_id"].casefold() not in named,
                                              -scores[key], key))[:k]
    return [{**chunks[key], "score": round(scores[key], 6), "retrieval": "hybrid"} for key in ordered]


def search_corpus(query: str, k: int = 5, doc_id: str | None = None, mode: SearchMode = "hybrid") -> list[dict]:
    """Rank source chunks; returned text is always the exact chunks.jsonl text.

    hybrid: Chroma (MiniLM) + BM25 fused by reciprocal rank; falls back to BM25 alone
            when Chroma is unavailable (each result says which retrieval produced it).
    vector: Chroma only; raises RetrievalUnavailable (a tool error) if it cannot answer.
    bm25:   in-memory keyword ranking; best for exact clause numbers and terms.
    """
    k = min(k, MAX_K)
    if k <= 0 or not query.strip():
        return []
    if mode == "bm25":
        return bm25_search(query, k, doc_id)
    if mode == "vector":
        return VECTOR.search(query, k, doc_id)
    pool = 3 * MAX_K  # fuse over a wider pool than we return, so roll-up sees whole clause families
    lexical = bm25_search(query, pool, doc_id)
    try:
        semantic = VECTOR.search(query, pool, doc_id)
    except RetrievalUnavailable:
        return [{**chunk, "retrieval": "bm25_fallback"} for chunk in fuse([lexical], k, query)]
    return fuse([semantic, lexical], k, query)


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
