import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
import redis
from pydantic import ValidationError
from triage_common.cache import (
    RUN_CACHE_VERSION,
    allow_run,
    cache_get,
    cache_set,
    cacheable_result,
    run_cache_key,
)
from triage_common.corpus_version import corpus_version_from_directory
from triage_common.settings import Settings
from worker.tools.corpus import Corpus


def test_run_cache_key_normalizes_statement_and_includes_versions():
    claim = {"pnr": "ABC123", "statement": "  My   Flight was cancelled. "}
    normalized = {"pnr": "ABC123", "statement": "my flight was cancelled."}
    assert run_cache_key(claim, "v1", "corpus-a") == run_cache_key(normalized, "v1", "corpus-a")
    assert run_cache_key(claim, "v1", "corpus-a") != run_cache_key(claim, "v2", "corpus-a")
    assert run_cache_key(claim, "v1", "corpus-a") != run_cache_key(claim, "v1", "corpus-b")
    assert run_cache_key(claim, "v1", "corpus-a", "client-a") != run_cache_key(
        claim, "v1", "corpus-a", "client-b"
    )


def test_run_cache_key_ignores_claim_metadata_and_includes_graph_version(monkeypatch):
    import triage_common.cache as cache

    claim = {"pnr": "ABC123", "statement": "cancelled"}
    with_metadata = {**claim, "client_id": "ignored", "prompt_version": "ignored"}
    base = run_cache_key(claim, "v1", "corpus-a", "client-a")
    assert base == run_cache_key(with_metadata, "v1", "corpus-a", "client-a")
    monkeypatch.setattr(cache, "RUN_CACHE_VERSION", f"{RUN_CACHE_VERSION}-next")
    assert base != run_cache_key(claim, "v1", "corpus-a", "client-a")


def test_only_verified_or_allowlisted_deterministic_results_are_cached():
    assert cacheable_result({"verified": True, "route": "pay_as_computed"})
    assert cacheable_result({"verified": False, "route": "escalate", "escalation_reason": "OUT_OF_CORPUS"})
    assert not cacheable_result({"verified": False, "route": "escalate", "escalation_reason": "PROVIDER_DEGRADED"})
    assert not cacheable_result({"verified": False, "route": "escalate", "escalation_reason": "NEW_REASON"})
    assert not cacheable_result({"verified": True, "route": "escalate", "escalation_reason": "NEW_REASON"})
    assert not cacheable_result({"verified": False, "route": "pay_as_computed"})
    assert not cacheable_result({"verified": True, "route": "NEW_ROUTE"})


def test_cache_get_and_set_serialize_json():
    values = {}

    class Client:
        def set(self, key, value, ex):
            values[key] = (value, ex)

        def get(self, key):
            return values.get(key, (None, None))[0]

    value = {
        "status": "escalated",
        "result": {
            "route": "escalate", "summary": "Needs human review", "entitlements": [],
            "compensation_inr": None, "citations": [], "escalation_reason": "VERIFICATION_FAILED",
            "verified": False, "requires_human_approval": True,
        },
    }
    cache_set(Client(), "key", value, 300)
    assert cache_get(Client(), "key") == value
    assert values["key"][1] == 300
    assert json.loads(values["key"][0]) == value


@pytest.mark.parametrize("value", ["{invalid", "[]", '{"status":"queued","result":{"route":"escalate"}}',
                                    '{"status":"completed","result":{"route":"escalate"}}',
                                    '{"status":[],"result":{}}', '{"status":"completed","result":{"route":[]}}'])
def test_corrupt_or_nonterminal_cache_entries_are_deleted_and_treated_as_misses(value):
    class Client:
        values = {"key": value}

        def get(self, key):
            return self.values.get(key)

        def delete(self, key):
            self.values.pop(key, None)

    client = Client()

    assert cache_get(client, "key") is None
    assert "key" not in client.values


def test_cache_read_failure_is_a_miss():
    class UnavailableRedis:
        def get(self, _key):
            raise redis.RedisError("redis unavailable")

    assert cache_get(UnavailableRedis(), "key") is None


def test_token_bucket_returns_allow_and_retry_after_values():
    class Client:
        def __init__(self):
            self.args = None

        def eval(self, *args):
            self.args = args
            return [0, 6]

    client = Client()
    assert allow_run(client, "customer-7", 10) == (False, 6)
    assert client.args[1] == 1
    assert "customer-7" not in client.args[2]


@pytest.mark.parametrize("setting", ["rate_limit_per_min", "cache_ttl_s"])
def test_cache_and_rate_limit_settings_must_be_positive(setting):
    with pytest.raises(ValidationError):
        Settings(**{setting: 0})


def test_corpus_version_matches_worker_and_covers_sidecar_artifacts(tmp_path):
    from triage_common.contracts import Chunk

    text = "Example clause text."
    chunk = Chunk(chunk_id="doc:1:0001", doc_id="doc", clause_id="1", text=text,
                  hash=hashlib.sha256(text.encode()).hexdigest())
    (tmp_path / "chunks.jsonl").write_text(chunk.model_dump_json() + "\n", encoding="utf-8")
    (tmp_path / "refs.json").write_text('{"refs":{}}', encoding="utf-8")
    (tmp_path / "definitions.json").write_text('{"terms":{}}', encoding="utf-8")

    version = corpus_version_from_directory(tmp_path)
    assert version == Corpus(tmp_path).version
    (tmp_path / "definitions.json").write_text('{"terms":{"new":"definition"}}', encoding="utf-8")
    assert corpus_version_from_directory(tmp_path) != version


@pytest.fixture
def live_redis():
    import os

    url = os.getenv("TRIAGE_TEST_REDIS_URL")
    if not url:
        pytest.skip("real Redis integration runs in CI when TRIAGE_TEST_REDIS_URL is set")
    client = redis.Redis.from_url(url, decode_responses=True)
    try:
        client.ping()
    except redis.RedisError as exc:
        pytest.skip(f"test Redis is unavailable: {exc}")
    yield client
    client.close()


def test_token_bucket_is_atomic_under_concurrent_requests(live_redis):
    client_id = f"concurrent-{uuid4()}"
    with ThreadPoolExecutor(max_workers=20) as pool:
        decisions = list(pool.map(lambda _: allow_run(live_redis, client_id, 10), range(20)))

    assert sum(allowed for allowed, _ in decisions) == 10
    assert all(retry_after >= 1 for allowed, retry_after in decisions if not allowed)


def test_token_bucket_refills_and_expires(live_redis):
    client_id = f"refill-{uuid4()}"
    key = f"triage:rl:{hashlib.sha256(client_id.encode()).hexdigest()}"
    server_time = live_redis.time()
    now = server_time[0] + server_time[1] / 1_000_000
    live_redis.hset(key, mapping={"tokens": "0", "updated": str(now - 6.1)})

    allowed, retry_after = allow_run(live_redis, client_id, 10)

    assert allowed is True
    assert retry_after == 0
    assert 0 < live_redis.ttl(key) <= 120
