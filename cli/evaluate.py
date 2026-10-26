"""Run scenario evaluations against a live API and write JSON/Markdown reports."""
from __future__ import annotations

import argparse
import glob
import hashlib
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import httpx

ROOT = Path(__file__).resolve().parents[1]


def _load_chunks() -> dict[str, dict[str, Any]]:
    """Read indexed chunks when present; return empty if the corpus is not indexed."""
    chunks_path = ROOT / "corpus" / "processed" / "chunks.jsonl"
    if not chunks_path.exists():
        return {}
    chunks = {}
    for line in chunks_path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            item = json.loads(line)
            chunks[item["chunk_id"]] = item
    return chunks


def _corpus_hash() -> str | None:
    manifest = ROOT / "corpus" / "manifest.yaml"
    return hashlib.sha256(manifest.read_bytes()).hexdigest() if manifest.exists() else None


def _citation_metrics(
    result: dict[str, Any] | None, expected: dict[str, Any], chunks: dict[str, dict[str, Any]] | None = None
) -> dict[str, Any]:
    citations = (result or {}).get("citations") or []
    clauses = [citation.get("clause_id") for citation in citations]
    required = expected.get("must_cite", [])
    resolved = []
    for citation in citations:
        chunk = (chunks or {}).get(citation.get("chunk_id"))
        source_matches = not chunks or (
            chunk is not None
            and chunk.get("doc_id") == citation.get("doc_id")
            and citation.get("quote") in chunk.get("text", "")
        )
        resolved.append(bool(citation.get("chunk_id") and citation.get("doc_id") and citation.get("quote")
                             and source_matches))
    found = sum(clause in clauses for clause in required)
    return {
        "citation_count": len(citations),
        "citations_resolved": sum(resolved),
        "citation_resolution_rate": sum(resolved) / len(resolved) if resolved else None,
        "required_clause_recall": found / len(required) if required else 1.0,
        "citation_resolution_basis": (
            "local corpus chunk verification" if chunks else "citation fields only; corpus not indexed"
        ),
        "required_clauses": required,
        "missing_required_clauses": [clause for clause in required if clause not in clauses],
    }


def run_scenario(
    client: httpx.Client,
    api: str,
    path: str,
    prompt_version: str,
    poll_interval: float,
    chunks: dict[str, dict[str, Any]] | None = None,
) -> dict:
    scenario = json.loads(Path(path).read_text(encoding="utf-8"))
    expected = scenario["expected"]
    case = {"scenario": scenario["id"], "prompt_version": prompt_version,
            "expected_route": expected["route"], "expected_escalation_reason": expected.get("escalation_reason")}
    try:
        response = client.post(urljoin(api.rstrip("/") + "/", "v1/claims"),
                               headers={"x-prompt-version": prompt_version}, json=scenario["claim"])
        if response.status_code == 422 and expected["route"] == "quarantine":
            return {**case, "actual_route": "quarantine", "route_correct": True,
                    "citation": _citation_metrics(None, expected, chunks), "status": "completed"}
        response.raise_for_status()
        run_id = response.json()["run_id"]
        deadline = time.monotonic() + client.timeout.read
        while time.monotonic() < deadline:
            polled = client.get(urljoin(api.rstrip("/") + "/", f"v1/runs/{run_id}"))
            polled.raise_for_status()
            payload = polled.json()
            if payload["status"] not in {"queued", "running"}:
                result = payload.get("result") or {}
                citation = _citation_metrics(result, expected, chunks)
                route = result.get("route")
                route_correct = route == expected["route"]
                if expected.get("escalation_reason"):
                    route_correct = route_correct and result.get("escalation_reason") == expected["escalation_reason"]
                return {**case, "actual_route": route, "actual_escalation_reason": result.get("escalation_reason"),
                        "route_correct": route_correct, "citation": citation,
                        "status": payload["status"], "result": result}
            time.sleep(poll_interval)
        raise TimeoutError(f"run {run_id} did not finish before deadline")
    except (httpx.HTTPError, ValueError, KeyError, TimeoutError) as exc:
        return {**case, "actual_route": None, "route_correct": False,
                "citation": _citation_metrics(None, expected, chunks), "status": "error",
                "error": f"{type(exc).__name__}: {exc}"}


def summarize(results: list[dict]) -> dict[str, Any]:
    count = len(results)
    route_correct = sum(item["route_correct"] for item in results)
    resolved = [item["citation"]["citation_resolution_rate"] for item in results
                if item["citation"]["citation_resolution_rate"] is not None]
    recall = [item["citation"]["required_clause_recall"] for item in results]
    return {"scenario_count": count, "route_accuracy": route_correct / count if count else None,
            "citation_resolution_rate": sum(resolved) / len(resolved) if resolved else None,
            "required_clause_recall": sum(recall) / len(recall) if recall else None,
            "errors": sum(item["status"] == "error" for item in results),
            "semantic_feedback": {"groundedness": None, "context_relevance": None,
                                  "answer_relevance": None, "hallucination_rate": None,
                                  "availability": "not implemented: no semantic feedback provider configured"}}


def write_report(output_dir: Path, results_by_version: dict[str, list[dict]]) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    summary = {version: summarize(results) for version, results in results_by_version.items()}
    artifact = {
        "generated_at": datetime.now(UTC).isoformat(),
        "corpus_chunks_sha256": _corpus_hash(),
        "summary": summary,
        "results": results_by_version,
    }
    (output_dir / "eval-results.json").write_text(json.dumps(artifact, indent=2) + "\n", encoding="utf-8")
    lines = ["# Evaluation report", "", f"Generated: {artifact['generated_at']}", "",
             "| Prompt | Scenarios | Route accuracy | Citation resolution | Required clause recall | Errors |",
             "| --- | ---: | ---: | ---: | ---: | ---: |"]
    def pct(value: float | None) -> str:
        return "N/A" if value is None else f"{value:.1%}"
    for version, metrics in summary.items():
        lines.append(f"| {version} | {metrics['scenario_count']} | {pct(metrics['route_accuracy'])} | "
                     f"{pct(metrics['citation_resolution_rate'])} | {pct(metrics['required_clause_recall'])} | "
                     f"{metrics['errors']} |")
    lines += ["", "## Semantic feedback", "", "Groundedness, context relevance, answer relevance, and "
              "hallucination rate are unavailable until a semantic feedback provider is configured. "
              "They are stored as null in JSON.", "", "## Scenario results", ""]
    for version, results in results_by_version.items():
        lines += [
            f"### {version}",
            "",
            "| Scenario | Expected | Actual | Route | Citation resolution | Clause recall | Status |",
                  "| --- | --- | --- | --- | ---: | ---: | --- |"]
        for item in results:
            citation = item["citation"]
            lines.append(f"| {item['scenario']} | {item['expected_route']} | {item['actual_route'] or '—'} | "
                         f"{'PASS' if item['route_correct'] else 'FAIL'} | "
                         f"{pct(citation['citation_resolution_rate'])} | "
                         f"{citation['required_clause_recall']:.1%} | {item['status']} |")
        lines.append("")
    (output_dir / "eval-report.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api", default="http://localhost:8000")
    parser.add_argument("--scenarios", default="eval/scenarios/*.json")
    parser.add_argument("--output-dir", default="eval/results")
    parser.add_argument("--prompt-versions", default="v1,v2")
    parser.add_argument("--timeout", type=float, default=65)
    parser.add_argument("--poll-interval", type=float, default=1)
    args = parser.parse_args()
    versions = [item.strip() for item in args.prompt_versions.split(",") if item.strip()]
    if not versions or any(item not in {"v1", "v2"} for item in versions):
        parser.error("--prompt-versions accepts v1 and/or v2")
    paths = sorted(glob.glob(args.scenarios))
    if not paths:
        parser.error(f"no scenarios matched {args.scenarios!r}")
    results = {}
    chunks = _load_chunks()
    with httpx.Client(timeout=args.timeout) as client:
        for version in versions:
            results[version] = [
                run_scenario(client, args.api, path, version, args.poll_interval, chunks) for path in paths
            ]
    write_report(Path(args.output_dir), results)
    for version, items in results.items():
        metrics = summarize(items)
        accuracy = metrics["route_accuracy"]
        print(f"{version}: {sum(item['route_correct'] for item in items)}/{len(items)} routes correct "
              f"({accuracy:.1%}); report: {args.output_dir}")
    if any(item["status"] == "error" for items in results.values() for item in items):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
