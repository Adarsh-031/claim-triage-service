import json

import httpx

from cli.evaluate import _citation_metrics, run_scenario, summarize, write_report


def test_citation_metrics_check_presence_and_required_clause_coverage():
    metrics = _citation_metrics(
        {"citations": [{"chunk_id": "chunk-1", "doc_id": "CAR", "clause_id": "3.3.1", "quote": "text"}]},
        {"must_cite": ["3.3.1", "3.3.2"]},
    )
    assert metrics["citation_resolution_rate"] == 1
    assert metrics["required_clause_recall"] == 0.5
    assert metrics["missing_required_clauses"] == ["3.3.2"]


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
    transport = httpx.MockTransport(lambda request: httpx.Response(422, json={"errors": []}))
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
