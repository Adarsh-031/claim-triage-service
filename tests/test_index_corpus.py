import hashlib

import chromadb
import pytest
from chromadb.api.types import EmbeddingFunction
from triage_common.contracts import Chunk

from cli.index_corpus import INDEX_VERSION, MODEL, load_chunks, record, sync, validate_collection, verify


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
    assert sync([first, stale], target) == {
        "count": 2, "upserted": 2, "deleted": 0, "embedding_cache_hits": 0
    }
    assert sync([first, stale], target) == {
        "count": 2, "upserted": 0, "deleted": 0, "embedding_cache_hits": 0
    }
    first.page = 7  # Provenance changes also invalidate the cached record.
    assert sync([first], target) == {
        "count": 1, "upserted": 1, "deleted": 1, "embedding_cache_hits": 1
    }
    assert verify([first], target)["count"] == 1
    forced = sync([first], target, force=True)
    assert forced["upserted"] == 1
    assert forced["embedding_cache_hits"] == 0
    first = chunk(text="Updated meals provision")
    assert sync([first], target)["upserted"] == 1
    assert verify([first], target)["count"] == 1


def test_sync_reuses_embedding_when_metadata_changes_for_same_chunk_hash(target):
    first = chunk()
    sync([first], target)
    updated = chunk()
    updated.page = 9
    result = sync([updated], target)
    assert result["embedding_cache_hits"] == 1
    assert target.get(ids=[updated.chunk_id], include=["metadatas"])["metadatas"][0]["page"] == 9


def test_sync_upgrades_legacy_index_metadata_once(target):
    first = chunk()
    document, metadata = record(first)
    metadata.pop("embedding_hash")
    metadata["fingerprint"] = hashlib.sha256(first.model_dump_json().encode()).hexdigest()
    target.upsert(ids=[first.chunk_id], documents=[document], metadatas=[metadata])

    result = sync([first], target)

    assert result["upserted"] == 1
    assert result["embedding_cache_hits"] == 0
    stored = target.get(ids=[first.chunk_id], include=["metadatas"])["metadatas"][0]
    assert stored["embedding_hash"] == record(first)[1]["embedding_hash"]
    assert verify([first], target)["count"] == 1


def test_incompatible_collection_is_rejected(target):
    target.modify(metadata={"embedding_model": "different-model"})
    with pytest.raises(ValueError, match="model/index version differs"):
        validate_collection(target)


def test_verify_detects_stale_records_and_missing_ids(target):
    first = chunk()
    sync([first], target)
    with pytest.raises(ValueError, match="record differs"):
        verify([chunk(text="changed")], target)
    with pytest.raises(ValueError, match="IDs differ"):
        verify([first, chunk("3.8.2")], target)


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
