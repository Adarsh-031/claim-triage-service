import hashlib

import chromadb
import pytest
from chromadb.api.types import EmbeddingFunction
from triage_common.contracts import Chunk

from cli.index_corpus import INDEX_VERSION, MODEL, load_chunks, sync, validate_collection, verify


class OfflineEmbedding(EmbeddingFunction):
    def __init__(self):
        pass

    def __call__(self, input):
        return [[1.0] * 384 for _ in input]

    @staticmethod
    def name():
        return "offline-test"

    def get_config(self):
        return {}

    @staticmethod
    def build_from_config(config):
        return OfflineEmbedding()


def chunk(clause="3.8.1(a)", text="Meals and refreshments in relation to waiting time."):
    return Chunk(chunk_id=f"CAR-S3-M-IV-R4:{clause}:0001", doc_id="CAR-S3-M-IV-R4",
                 clause_id=clause, text=text, hash=hashlib.sha256(text.encode()).hexdigest())


@pytest.fixture
def target(tmp_path):
    return chromadb.PersistentClient(path=str(tmp_path)).create_collection(
        "test-clauses", embedding_function=OfflineEmbedding(),
        metadata={"embedding_model": MODEL, "index_version": INDEX_VERSION},
    )


def test_sync_idempotent_updates_metadata_and_prunes_stale(target):
    first, stale = chunk(), chunk("3.8.2", "Hotel accommodation")
    assert sync([first, stale], target) == {"count": 2, "upserted": 2, "deleted": 0}
    assert sync([first, stale], target) == {"count": 2, "upserted": 0, "deleted": 0}
    first.page = 7  # Provenance changes also invalidate the cached record.
    assert sync([first], target) == {"count": 1, "upserted": 1, "deleted": 1}
    assert verify([first], target, require_retrieval_smoke=False)["count"] == 1
    assert sync([first], target, force=True)["upserted"] == 1
    first = chunk(text="Updated meals provision")
    assert sync([first], target)["upserted"] == 1
    assert verify([first], target, require_retrieval_smoke=False)["count"] == 1


def test_incompatible_collection_is_rejected(target):
    target.modify(metadata={"embedding_model": "different-model"})
    with pytest.raises(ValueError, match="model/index version differs"):
        validate_collection(target)


def test_verify_detects_stale_records_and_missing_ids(target):
    first = chunk()
    sync([first], target)
    with pytest.raises(ValueError, match="record differs"):
        verify([chunk(text="changed")], target, require_retrieval_smoke=False)
    with pytest.raises(ValueError, match="IDs differ"):
        verify([first, chunk("3.8.2")], target, require_retrieval_smoke=False)


def test_failed_upsert_does_not_prune_previous_records(target, monkeypatch):
    first = chunk()
    sync([first], target)

    def fail(**kwargs):
        raise RuntimeError("embedding unavailable")

    monkeypatch.setattr(type(target), "upsert", lambda self, **kwargs: fail(**kwargs))
    with pytest.raises(RuntimeError, match="embedding unavailable"):
        sync([chunk("3.8.2")], target)
    assert target.get()["ids"] == [first.chunk_id]


def test_load_rejects_empty_duplicate_and_tampered_chunks(tmp_path):
    path = tmp_path / "chunks.jsonl"
    first = chunk()
    for content, error in [("", "nonempty"), (first.model_dump_json() + "\n" + first.model_dump_json(), "unique")]:
        path.write_text(content)
        with pytest.raises(ValueError, match=error):
            load_chunks(path)
    first.text = "tampered"
    path.write_text(first.model_dump_json())
    with pytest.raises(ValueError, match="hash mismatch"):
        load_chunks(path)



def test_cancellation_compensation_bm25_real_chunks_contract():
    from cli.index_corpus import _bm25_top_ids

    chunks = [
        chunk("3.3.1", "Cancellation permits an alternate flight or refund."),
        chunk("3.3.2", "Cancellation compensation and full refund apply to eligible passengers."),
        chunk("3.3.2(a)", "Cancellation compensation may be payable in the stated circumstances."),
        chunk("3.8.1(a)", "Meals and refreshments in relation to waiting time."),
        chunk("4.1", "Delay rules use a different calculation."),
    ]
    top = _bm25_top_ids(chunks, "cancellation compensation", 3, "CAR-S3-M-IV-R4")
    assert "CAR-S3-M-IV-R4:3.3.2:0001" in top


def test_verify_cancellation_retrieval_checks_vector_and_bm25_top_three():
    from cli.index_corpus import verify_cancellation_retrieval

    chunks = [
        chunk("3.3.1", "Cancellation permits an alternate flight or refund."),
        chunk("3.3.2", "Cancellation compensation and full refund apply to eligible passengers."),
        chunk("3.3.2(a)", "Cancellation compensation may be payable in the stated circumstances."),
        chunk("3.8.1(a)", "Meals and refreshments in relation to waiting time."),
    ]

    class FakeTarget:
        def query(self, **kwargs):
            assert kwargs["query_texts"] == ["cancellation compensation"]
            assert kwargs["where"] == {"doc_id": "CAR-S3-M-IV-R4"}
            if kwargs["query_texts"] == ["cancellation compensation"]:
                return {"ids": [[
                    "CAR-S3-M-IV-R4:3.3.4:0001",
                    "CAR-S3-M-IV-R4:1.4:0001",
                    "CAR-S3-M-IV-R4:2.8:0001",
                    "CAR-S3-M-IV-R4:3.3.2:0001",
                ]]}
            assert kwargs["query_texts"] == ["cancellation compensation clause 3.3.2"]
            return {"ids": [[
                "CAR-S3-M-IV-R4:3.3.2:0001",
                "CAR-S3-M-IV-R4:3.3.2(a):0001",
                "CAR-S3-M-IV-R4:3.3.1:0001",
            ]]}

    result = verify_cancellation_retrieval(chunks, FakeTarget())
    assert "CAR-S3-M-IV-R4:3.3.2:0001" in result["cancellation_compensation_vector_reranked_top3"]
    assert "CAR-S3-M-IV-R4:3.3.2:0001" in result["cancellation_compensation_bm25_top3"]
