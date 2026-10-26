import json

from triage_common.cache import allow_run, cache_get, cache_set, retrieval_cache_key, run_cache_key


def test_run_cache_key_normalizes_statement_and_includes_versions():
    claim = {"pnr": "ABC123", "statement": "  My   Flight was cancelled. "}
    normalized = {"pnr": "ABC123", "statement": "my flight was cancelled."}
    assert run_cache_key(claim, "v1", "corpus-a") == run_cache_key(normalized, "v1", "corpus-a")
    assert run_cache_key(claim, "v1", "corpus-a") != run_cache_key(claim, "v2", "corpus-a")
    assert run_cache_key(claim, "v1", "corpus-a") != run_cache_key(claim, "v1", "corpus-b")
    assert run_cache_key(claim, "v1", "corpus-a", "client-a") != run_cache_key(
        claim, "v1", "corpus-a", "client-b"
    )


def test_retrieval_cache_key_covers_query_mode_k_and_corpus():
    base = retrieval_cache_key(" Meals  & refreshment ", "semantic", 5, "corpus-a")
    assert base == retrieval_cache_key("meals & refreshment", "semantic", 5, "corpus-a")
    assert base != retrieval_cache_key("meals & refreshment", "keyword", 5, "corpus-a")
    assert base != retrieval_cache_key("meals & refreshment", "semantic", 3, "corpus-a")
    assert base != retrieval_cache_key("meals & refreshment", "semantic", 5, "corpus-b")


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
