import json

from triage_common.cache import (
    RUN_CACHE_VERSION,
    allow_run,
    cache_get,
    cache_set,
    cacheable_result,
    run_cache_key,
)


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
    assert not cacheable_result({"verified": False, "route": "pay_as_computed"})


def test_cache_get_and_set_serialize_json():
    values = {}

    class Client:
        def set(self, key, value, ex):
            values[key] = (value, ex)

        def get(self, key):
            return values.get(key, (None, None))[0]

    cache_set(Client(), "key", {"route": "escalate"}, 300)
    assert cache_get(Client(), "key") == {"route": "escalate"}
    assert values["key"][1] == 300
    assert json.loads(values["key"][0]) == {"route": "escalate"}


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
