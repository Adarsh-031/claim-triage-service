"""search_corpus / get_clause over chunks.jsonl, BM25, and a fake Chroma collection."""
import json
import time

import pytest
from langchain_core.tools import BaseTool
from triage_common.settings import settings
from worker.agent.research import REGISTRY, TOOLS, execute_tool
from worker.tools import corpus
from worker.tools.retrieval import LANGCHAIN_TOOLS

pytestmark = pytest.mark.usefixtures("research_env")


class FakeCollection:
    def __init__(self, ids, delay=0.0):
        self.ids, self.delay, self.calls = ids, delay, []
        self.metadata = {"embedding_model": settings.embedding_model}

    def query(self, query_texts, n_results, where, include):
        self.calls.append({"query": query_texts[0], "n": n_results, "where": where})
        time.sleep(self.delay)
        chunks = corpus.get_corpus().chunks
        ids = self.ids[:n_results]
        return {"ids": [ids], "distances": [[0.1 * i for i, _ in enumerate(ids)]],
                "metadatas": [[{"hash": chunks[i]["hash"] if i in chunks else "x"} for i in ids]]}


@pytest.fixture
def chroma(monkeypatch):
    def install(collection):
        monkeypatch.setattr(settings, "chroma_url", "http://chroma.test")
        monkeypatch.setattr(corpus.VECTOR, "_collection", lambda: collection)
        return collection
    yield install
    corpus.VECTOR.reset()


def chunk_id(clause, doc=corpus.CAR_DOC):
    return corpus.get_clause(clause, doc)["chunk_id"]


def test_cancellation_compensation_finds_3_3_2_in_top_3_without_chroma():
    results = corpus.search_corpus("cancellation compensation", k=3)
    assert "3.3.2" in [c["clause_id"] for c in results]
    assert {c["retrieval"] for c in results} == {"bm25_fallback"}


def test_get_clause_returns_exact_chunks_jsonl_text():
    path = corpus.get_corpus_dir() / "chunks.jsonl"
    lines = [json.loads(line) for line in path.read_text().splitlines()]
    expected = next(c for c in lines if c["doc_id"] == corpus.CAR_DOC and c["clause_id"] == "3.8.1(a)")
    assert corpus.get_clause("3.8.1(a)")["text"] == expected["text"]
    assert LANGCHAIN_TOOLS["get_clause"].invoke({"clause_id": "3.8.1(a)"}) == corpus.get_clause("3.8.1(a)")


def test_exact_clause_number_in_query_ranks_first():
    assert corpus.search_corpus("3.8.1(a)", k=3)[0]["clause_id"] == "3.8.1(a)"
    assert corpus.search_corpus("3.8.1(a)", k=3, mode="bm25")[0]["clause_id"] == "3.8.1(a)"


def test_bm25_mode_filters_and_limits():
    results = corpus.search_corpus("refund", k=4, doc_id="INDIGO-COC-DOM", mode="bm25")
    assert 0 < len(results) <= 4
    assert all(c["doc_id"] == "INDIGO-COC-DOM" and c["retrieval"] == "bm25" for c in results)
    assert corpus.search_corpus("zzzzzz", mode="bm25") == []
    assert corpus.search_corpus("   ") == []
    assert len(corpus.search_corpus("flight", k=50)) == corpus.MAX_K


def test_vector_results_use_local_text_and_pass_doc_filter(chroma):
    fake = chroma(FakeCollection([chunk_id("3.3.4"), chunk_id("1.4")]))
    results = corpus.search_corpus("cancellation", k=2, doc_id=corpus.CAR_DOC, mode="vector")
    assert [c["clause_id"] for c in results] == ["3.3.4", "1.4"]
    assert results[0]["text"] == corpus.get_clause("3.3.4")["text"]
    assert results[0]["score"] > results[1]["score"]
    assert fake.calls[0]["where"] == {"doc_id": corpus.CAR_DOC}


def test_hybrid_rolls_subclause_hits_up_to_their_parent(chroma):
    tiers = [chunk_id(f"3.3.2({x})") for x in "abc"]
    chroma(FakeCollection(tiers))
    results = corpus.search_corpus("INR 5,000 block time", k=3)
    assert results[0]["clause_id"] == "3.3.2"
    assert {c["retrieval"] for c in results} == {"hybrid"}


def test_stale_index_is_an_error_for_vector_and_a_fallback_for_hybrid(chroma):
    chroma(FakeCollection(["CAR-S3-M-IV-R4:9.9.9:0001"]))
    with pytest.raises(corpus.RetrievalUnavailable, match="out of sync"):
        corpus.search_corpus("cancellation", mode="vector")
    corpus.VECTOR.reset()
    assert {c["retrieval"] for c in corpus.search_corpus("cancellation")} == {"bm25_fallback"}


def test_slow_chroma_times_out_then_cools_down(chroma, monkeypatch):
    monkeypatch.setattr(settings, "retrieval_timeout_s", 0.05)
    fake = chroma(FakeCollection([chunk_id("3.3.2")], delay=0.5))
    started = time.monotonic()
    assert {c["retrieval"] for c in corpus.search_corpus("cancellation")} == {"bm25_fallback"}
    assert time.monotonic() - started < 0.4
    corpus.search_corpus("cancellation")
    assert len(fake.calls) == 1  # cooling down: Chroma is not asked again


def test_vector_failure_reaches_the_agent_as_a_tool_error(chroma):
    chroma(FakeCollection(["missing"]))
    with pytest.raises(OSError):
        execute_tool("search_corpus", {"query": "cancellation", "mode": "vector"}, {})


def test_retrieval_tools_are_langchain_tools_with_docstring_descriptions():
    for name in ("search_corpus", "get_clause"):
        tool = LANGCHAIN_TOOLS[name]
        assert isinstance(tool, BaseTool)
        assert REGISTRY[name][2] == tool.description
        assert tool.description.split("\n")[0] in tool.func.__doc__
    schema = next(t for t in TOOLS if t["function"]["name"] == "search_corpus")["function"]["parameters"]
    assert schema["properties"]["mode"]["enum"] == ["hybrid", "vector", "bm25"]
    assert execute_tool("search_corpus", {"query": "cancellation", "k": 2}, {})
    with pytest.raises(ValueError):
        execute_tool("search_corpus", {"query": "cancellation", "mode": "magic"}, {})
