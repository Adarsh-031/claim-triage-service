import json

import httpx

from cli.evaluate import _citation_metrics, run_scenario, summarize, write_report


def test_citation_metrics_check_presence_and_required_clause_coverage():
    metrics = _citation_metrics(
        {"citations": [{"chunk_id": "chunk-1", "doc_id": "CAR", "clause_id": "3.3.1", "quote": "text"}]},
        {"must_cite": ["3.3.1", "3.3.2"]},
    )
    assert metrics["citation_resolution_rate"] is None
    assert metrics["required_clause_recall"] is None
    assert metrics["missing_required_clauses"] == ["3.3.1", "3.3.2"]


def test_citation_resolution_checks_source_chunk_and_quote():
    chunks = {"chunk-1": {"doc_id": "CAR", "text": "exact source wording"}}
    metrics = _citation_metrics(
        {"citations": [{"chunk_id": "chunk-1", "doc_id": "CAR", "clause_id": "3.3.1",
                        "quote": "fabricated wording"}]},
        {"must_cite": ["3.3.1"]},
        chunks,
    )
    assert metrics["citation_resolution_rate"] == 0


def test_quarantine_scenario_is_scored_without_creating_a_run(tmp_path):
    transport = httpx.MockTransport(
        lambda request: httpx.Response(422, json={"errors": [], "quarantine_id": "persisted-id"}))
    with httpx.Client(transport=transport) as client:
        result = run_scenario(client, "http://api", "eval/scenarios/s02_malformed_quarantine.json", "v1", 0)
    assert result["actual_route"] == "quarantine"
    assert result["route_correct"] is True


def test_report_writes_json_and_markdown_with_unavailable_semantic_metrics(tmp_path):
    results = [{"scenario": "case", "expected_route": "escalate", "actual_route": "escalate",
                "route_correct": True, "status": "completed",
                "citation": {"citation_resolution_rate": None, "required_clause_recall": 1.0}}]
    write_report(tmp_path, {"v1": results, "v2": results})
    artifact = json.loads((tmp_path / "eval-results.json").read_text())
    markdown = (tmp_path / "eval-report.md").read_text()
    assert artifact["summary"]["v1"]["route_accuracy"] == 1
    assert artifact["summary"]["v2"]["semantic_feedback"]["groundedness"] is None
    assert "unavailable" in markdown


def test_summary_empty_results_has_null_rates():
    summary = summarize([])
    assert summary["route_accuracy"] is None
    assert summary["citation_resolution_rate"] is None


def test_citations_require_matching_clause_and_nonempty_exact_quote():
    chunks = {"c": {"doc_id": "CAR", "clause_id": "3.3.2", "text": "source text"}}
    for quote, clause in [("", "3.3.2"), ("source", "wrong"), ("fabricated", "3.3.2")]:
        result = {"citations": [{"chunk_id": "c", "doc_id": "CAR", "clause_id": clause, "quote": quote}]}
        metrics = _citation_metrics(result, {"must_cite": ["3.3.2"]}, chunks)
        assert metrics["citation_resolution_rate"] == 0
        assert metrics["required_clause_recall"] == 0


def test_failed_worker_is_an_error_even_if_result_route_matches(tmp_path):
    path = tmp_path / "case.json"
    path.write_text(json.dumps({"id": "case", "claim": {}, "expected": {"route": "escalate"}}))

    def handle(request):
        return httpx.Response(202, json={"run_id": "id"}) if request.method == "POST" else httpx.Response(
            200, json={"status": "failed", "result": {"route": "escalate"}})

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        result = run_scenario(client, "http://api", str(path), "v1", 0)
    assert result["status"] == "error"
    assert result["route_correct"] is False


def test_rate_limit_retries_then_checks_executed_version(monkeypatch, tmp_path):
    from cli import evaluate
    sleeps = []
    monkeypatch.setattr(evaluate.time, "sleep", sleeps.append)
    path = tmp_path / "case.json"
    path.write_text(json.dumps({"id": "case", "claim": {}, "expected": {"route": "escalate"}}))
    posts = []

    def handle(request):
        if request.method == "POST":
            posts.append(request.headers["x-prompt-version"])
            if len(posts) == 1:
                return httpx.Response(429, headers={"Retry-After": "2"})
            return httpx.Response(202, json={"run_id": "id"})
        if request.url.path.endswith("/trace"):
            return httpx.Response(200, json={"steps": [{"step": "intake", "prompt_version": "v2"}]})
        return httpx.Response(200, json={"status": "done", "result": {"route": "escalate"}})

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        result = run_scenario(client, "http://api", str(path), "v2", 0)
    assert result["status"] == "done"
    assert result["executed_prompt_version"] == "v2"
    assert posts == ["v2", "v2"] and sleeps == [2]


def test_wrong_executed_version_is_not_a_comparison_success(tmp_path):
    path = tmp_path / "case.json"
    path.write_text(json.dumps({"id": "case", "claim": {}, "expected": {"route": "escalate"}}))

    def handle(request):
        if request.method == "POST":
            return httpx.Response(202, json={"run_id": "id"})
        if request.url.path.endswith("/trace"):
            return httpx.Response(200, json={"steps": [{"step": "intake", "prompt_version": "v1"}]})
        return httpx.Response(200, json={"status": "escalated", "result": {"route": "escalate"}})

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        result = run_scenario(client, "http://api", str(path), "v2", 0)
    assert result["status"] == "error"
    assert "prompt version" in result["error"]


def test_trulens_adapter_uses_context_and_aggregates_scores():
    from cli.semantic_feedback import SemanticFeedback

    class Judge:
        def groundedness_measure_with_cot_reasons(self, *, source, statement):
            assert source == "source one\n\nsource two" and statement == "answer"
            return 0.75, {"reason": "evidence"}

        def context_relevance_with_cot_reasons(self, *, question, context):
            assert question == "facts"
            return (0.5 if context == "source one" else 1), {}

        def relevance_with_cot_reasons(self, *, prompt, response):
            assert prompt == "facts" and response == "answer"
            return 1, {}

    scores = SemanticFeedback(Judge(), "test").evaluate("facts", "answer", ["source one", "source two"])
    assert scores["groundedness"] == scores["context_relevance"] == 0.75
    assert scores["hallucination_rate"] == 0.25 and scores["answer_relevance"] == 1
    assert scores["status"] == "measured"


def test_judge_failures_are_explicit_and_do_not_leak_credentials():
    from cli.semantic_feedback import SemanticFeedback

    class Judge:
        def groundedness_measure_with_cot_reasons(self, **kwargs):
            raise RuntimeError("secret-key-and-private-request")

    scores = SemanticFeedback(Judge(), "test").evaluate("facts", "answer", ["source"])
    assert scores["status"] == "error" and scores["groundedness"] is None
    assert "secret" not in json.dumps(scores)


def test_installed_trulens_provider_contract():
    import inspect

    import pytest
    provider = pytest.importorskip("trulens.providers.openai").OpenAI
    for method, parameters in [("groundedness_measure_with_cot_reasons", {"source", "statement"}),
                               ("context_relevance_with_cot_reasons", {"question", "context"}),
                               ("relevance_with_cot_reasons", {"prompt", "response"})]:
        assert parameters <= set(inspect.signature(getattr(provider, method)).parameters)
