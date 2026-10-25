import hashlib
import json
import time

import pytest
from langchain_core.tools import BaseTool
from triage_common.contracts import Chunk
from worker.tools import corpus


def make_chunk(clause_id: str, text: str, doc_id: str = "CAR-S3-M-IV-R4") -> dict:
    chunk = Chunk(
        chunk_id=f"{doc_id}:{clause_id}:0001",
        doc_id=doc_id,
        clause_id=clause_id,
        heading_path=["3 Cancellation"],
        text=text,
        hash=hashlib.sha256(text.encode()).hexdigest(),
    )
    return chunk.model_dump(mode="json")


@pytest.fixture
def chunks_file(tmp_path, monkeypatch):
    rows = [
        make_chunk("3.3.1", "Cancellation permits an alternate flight or refund."),
        make_chunk("3.3.2", "Cancellation compensation and full refund apply to eligible passengers."),
        make_chunk("3.8.1(a)", "Meals and refreshments in relation to waiting time."),
        make_chunk("4.1", "Delay rules use a different calculation.", "CAR-OTHER"),
    ]
    path = tmp_path / "chunks.jsonl"
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    monkeypatch.setattr(corpus, "DEFAULT_CHUNKS_PATH", path)
    corpus._load_chunks_cached.cache_clear()
    corpus._bm25_index.cache_clear()
    return rows, path


def test_exports_are_langchain_tools():
    assert isinstance(corpus.search_corpus, BaseTool)
    assert isinstance(corpus.get_clause, BaseTool)
    assert "BM25" in corpus.search_corpus.description
    assert "exact normalized chunk" in corpus.get_clause.description


def test_bm25_ranks_cancellation_compensation_332_top_three(chunks_file):
    rows, _ = chunks_file
    result = corpus._search_impl("cancellation compensation", mode="bm25", k=3)
    assert result["retrieval_error"] is None
    assert result["chunks"][0]["clause_id"] == "3.3.2"
    assert len(result["chunks"]) == 3
    assert all("heading_path" in chunk and "score" in chunk for chunk in result["chunks"])


def test_bm25_filters_document(chunks_file):
    result = corpus._search_impl("delay rules", mode="bm25", doc_id="CAR-OTHER")
    assert [chunk["clause_id"] for chunk in result["chunks"]] == ["4.1"]


def test_get_clause_returns_exact_processed_chunk(chunks_file):
    rows, _ = chunks_file
    result = corpus._get_clause_impl("3.8.1(a)")
    assert result["retrieval_error"] is None
    assert result["chunk"] == rows[2]


def test_get_clause_missing_is_not_an_exception(chunks_file):
    result = corpus._get_clause_impl("9.9.9")
    assert result == {"chunk": None, "retrieval_error": "not found"}


def test_clause_lookup_normalizes_whitespace_and_returns_a_copy(chunks_file):
    result = corpus._get_clause_impl(" 3.8.1 (a) ")
    assert result["retrieval_error"] is None
    result["chunk"]["heading_path"].append("mutated")
    again = corpus._get_clause_impl("3.8.1(a)")
    assert "mutated" not in again["chunk"]["heading_path"]


def test_vector_search_uses_configured_collection_and_metadata(monkeypatch):
    seen = {}
    class FakeCollection:
        def query(self, **kwargs):
            seen.update(kwargs)
            return {
                "ids": [["CAR-S3-M-IV-R4:3.3.2:0001"]],
                "documents": [["CAR-S3-M-IV-R4 > 3 Cancellation\nCancellation compensation."]],
                "metadatas": [[{
                    "doc_id": "CAR-S3-M-IV-R4", "clause_id": "3.3.2",
                    "heading_path": json.dumps(["3 Cancellation"]),
                    "hash": hashlib.sha256(b"Cancellation compensation.").hexdigest(), "refs": "[]",
                }]],
                "distances": [[0.1]],
            }
    class FakeClient:
        def get_collection(self, name, **kwargs):
            assert name == corpus.settings.chroma_collection
            return FakeCollection()
    monkeypatch.setattr(corpus, "_VECTOR_BACKEND", None)
    monkeypatch.setattr(corpus, "DefaultEmbeddingFunction", lambda: object())
    monkeypatch.setattr(corpus.chromadb, "HttpClient", lambda **kwargs: FakeClient())
    result = corpus._vector_search("cancellation compensation", 5, "CAR-S3-M-IV-R4")
    assert seen["where"] == {"doc_id": "CAR-S3-M-IV-R4"}
    assert result[0]["clause_id"] == "3.3.2"
    assert result[0]["text"] == "Cancellation compensation."
    assert result[0]["score"] == pytest.approx(0.9)


@pytest.mark.parametrize(("mode", "message"), [("vector", "unavailable"), ("bm25", "timeout")])
def test_failures_return_empty_chunks_and_error_flag(monkeypatch, mode, message):
    def fail(*args, **kwargs):
        if message == "timeout":
            raise TimeoutError("slow backend")
        raise OSError("index down")
    monkeypatch.setattr(corpus, "_vector_search" if mode == "vector" else "_bm25_search", fail)
    result = corpus._search_impl("cancellation", mode=mode)
    assert result == {"chunks": [], "retrieval_error": message}


def test_invalid_search_arguments_are_reported_without_exception():
    assert corpus._search_impl(" ", mode="vector")["retrieval_error"] == "empty query"
    assert corpus._search_impl("cancellation", k=0)["retrieval_error"] == "k must be greater than zero"



def test_vector_backend_is_created_once_and_reused(monkeypatch):
    calls = {"client": 0, "embedding": 0}

    class FakeCollection:
        def query(self, **kwargs):
            text = "Cancellation compensation."
            return {
                "ids": [["CAR-S3-M-IV-R4:3.3.2:0001"]],
                "documents": [[f"CAR-S3-M-IV-R4 > 3 Cancellation\\n{text}"]],
                "metadatas": [[{
                    "doc_id": "CAR-S3-M-IV-R4", "clause_id": "3.3.2",
                    "heading_path": ["3 Cancellation"],
                    "hash": hashlib.sha256(text.encode()).hexdigest(), "refs": [], "page": -1,
                }]],
                "distances": [[0.1]],
            }

    class FakeClient:
        def get_collection(self, name, **kwargs):
            return FakeCollection()

    def client_factory(**kwargs):
        calls["client"] += 1
        return FakeClient()

    def embedding_factory():
        calls["embedding"] += 1
        return object()

    monkeypatch.setattr(corpus, "_VECTOR_BACKEND", None)
    monkeypatch.setattr(corpus.chromadb, "HttpClient", client_factory)
    monkeypatch.setattr(corpus, "DefaultEmbeddingFunction", embedding_factory)
    assert corpus._vector_search("cancellation", 3, None)
    assert corpus._vector_search("refund", 3, None)
    assert calls == {"client": 1, "embedding": 1}


def test_vector_normalization_drops_source_hash_mismatch():
    result = corpus._normalize_chroma(
        ["bad-id"],
        ["CAR-S3-M-IV-R4 > 3 Cancellation\\nnot the indexed source text"],
        [{"doc_id": "CAR-S3-M-IV-R4", "clause_id": "3.3.2", "hash": "0" * 64}],
    )
    assert result == []


def test_timeout_returns_control_for_a_blocked_operation(monkeypatch):
    monkeypatch.setattr(corpus.settings, "tool_timeout_s", 0.005)

    def slow():
        time.sleep(0.2)
        return "late"

    started = time.monotonic()
    with pytest.raises(TimeoutError, match="retrieval exceeded"):
        corpus._within_timeout(slow)
    assert time.monotonic() - started < 0.15
    time.sleep(0.21)


def test_search_tool_schema_accepts_optional_doc_id_and_literal_mode(chunks_file, monkeypatch):
    chunk = make_chunk("3.3.2", "Cancellation compensation.")
    monkeypatch.setattr(corpus, "_bm25_search", lambda query, k, doc_id: [{**chunk, "score": 1.0}])
    result = corpus.search_corpus.invoke({
        "query": "cancellation compensation", "doc_id": None, "mode": "bm25",
    })
    assert result["retrieval_error"] is None
    assert result["chunks"][0]["clause_id"] == "3.3.2"
    with pytest.raises(Exception):
        corpus.search_corpus.invoke({
            "query": "cancellation compensation", "doc_id": "UNKNOWN-DOC", "mode": "bm25",
        })
