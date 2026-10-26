"""Evaluate both prompt versions against a running API, with optional TruLens feedback."""
from __future__ import annotations

import argparse
import glob
import hashlib
import json
import math
import time
from datetime import UTC, datetime
from pathlib import Path
from statistics import mean
from typing import Any
from urllib.parse import urljoin

import httpx

try:
    from cli.semantic_feedback import METRICS, SemanticFeedback
except ModuleNotFoundError:  # python cli/evaluate.py
    from semantic_feedback import METRICS, SemanticFeedback

ROOT = Path(__file__).resolve().parents[1]


def _load_chunks(directory: Path | None = None) -> dict[str, dict[str, Any]]:
    path = (directory or ROOT / "corpus/processed") / "chunks.jsonl"
    if not path.exists():
        return {}
    return {item["chunk_id"]: item for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip() for item in [json.loads(line)]}


def _corpus_hash(directory: Path | None = None) -> str | None:
    path = (directory or ROOT / "corpus/processed") / "chunks.jsonl"
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


def _citation_metrics(result, expected, chunks=None) -> dict:
    citations = (result or {}).get("citations") or []
    required = expected.get("must_cite", [])
    resolved_clauses = []
    resolved = 0
    for citation in citations:
        if not isinstance(citation, dict):
            continue
        chunk = (chunks or {}).get(citation.get("chunk_id"))
        quote = citation.get("quote")
        if (chunk and chunk.get("doc_id") == citation.get("doc_id")
                and chunk.get("clause_id") == citation.get("clause_id")
                and isinstance(quote, str) and quote.strip() and quote in chunk.get("text", "")):
            resolved += 1
            resolved_clauses.append(citation["clause_id"])
    return {
        "citation_count": len(citations), "citations_resolved": resolved,
        "citation_resolution_rate": resolved / len(citations) if citations and chunks else None,
        "required_clause_recall": (sum(c in resolved_clauses for c in required) / len(required)
                                  if required and chunks else (None if required else 1.0)),
        "citation_resolution_basis": "local corpus chunk verification" if chunks else "unavailable: no local corpus",
        "required_clauses": required,
        "missing_required_clauses": [c for c in required if c not in resolved_clauses],
    }


def _request(client, method, url, deadline, **kwargs):
    """Respect API throttling, without retrying ambiguous POST transport failures."""
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Evaluation deadline exhausted")
        response = client.request(method, url, timeout=min(30, remaining), **kwargs)
        if response.status_code != 429:
            response.raise_for_status() if response.status_code != 422 else None
            return response
        delay = response.headers.get("Retry-After", "6")
        try:
            delay = max(0.1, float(delay))
        except ValueError:
            delay = 6.0
        if not math.isfinite(delay) or delay >= deadline - time.monotonic():
            raise TimeoutError("Rate limit retry would exceed evaluation deadline")
        time.sleep(delay)


def run_scenario(client, api, path, prompt_version, poll_interval, chunks=None, *,
                 feedback=None, timeout=None, corpus_version=None):
    case = {"scenario": Path(path).stem, "prompt_version": prompt_version, "expected_route": None}
    expected = {}
    try:
        scenario = json.loads(Path(path).read_text(encoding="utf-8"))
        expected = scenario["expected"]
        case.update(scenario=scenario["id"], expected_route=expected["route"],
                    expected_escalation_reason=expected.get("escalation_reason"))
        budget = timeout if timeout is not None else (client.timeout.read or 65)
        deadline = time.monotonic() + budget
        base = api.rstrip("/") + "/"
        response = _request(client, "POST", urljoin(base, "v1/claims"), deadline,
                            headers={"x-prompt-version": prompt_version}, json=scenario["claim"])
        if response.status_code == 422:
            quarantine = response.json()
            if expected["route"] != "quarantine" or not quarantine.get("quarantine_id"):
                raise ValueError("Unexpected validation failure or quarantine was not persisted")
            return {**case, "actual_route": "quarantine", "route_correct": True,
                    "citation": _citation_metrics(None, expected, chunks), "status": "completed",
                    "payload": quarantine}
        run_id = response.json()["run_id"]
        while True:
            payload = _request(client, "GET", urljoin(base, f"v1/runs/{run_id}"), deadline).json()
            status = payload["status"]
            if status in {"done", "completed", "escalated"}:
                break
            if status == "failed":
                raise ValueError("Worker failed; this is not a completed recommendation")
            if status not in {"queued", "running"}:
                raise ValueError("Unknown run status")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Run did not finish before evaluation deadline")
            # Leave time for a final fetch, even if the configured interval exceeds the budget.
            time.sleep(min(poll_interval, remaining / 2))
        result = payload.get("result")
        if not isinstance(result, dict) or not result.get("route"):
            raise ValueError("Terminal run has no recommendation")
        trace_payload = _request(client, "GET", urljoin(base, f"v1/runs/{run_id}/trace"), deadline).json()
        steps = trace_payload.get("steps", [])
        intake = next((s for s in steps if s.get("step") == "intake"), {})
        executed = intake.get("prompt_version")
        if executed != prompt_version:
            raise ValueError("Run trace does not confirm requested prompt version; update API and worker")
        if corpus_version is not None and intake.get("corpus_version") != corpus_version:
            raise ValueError("Local corpus differs from the worker corpus; citation scores would be invalid")
        correct = result["route"] == expected["route"]
        if expected.get("escalation_reason"):
            correct = correct and result.get("escalation_reason") == expected["escalation_reason"]
        item = {**case, "actual_route": result["route"], "actual_escalation_reason": result.get("escalation_reason"),
                "route_correct": correct, "citation": _citation_metrics(result, expected, chunks),
                "status": status, "result": result, "payload": payload, "trace": steps,
                "executed_prompt_version": executed, "runtime_corpus_version": intake.get("corpus_version")}
        if "compensation_inr" in expected:
            # Consequential amounts are intentionally withheld from the public result
            # until independent grounding verification exists. Score the deterministic
            # calculator from its private worker trace instead.
            draft = next((s for s in reversed(steps) if s.get("step") == "draft"), {})
            computation = draft.get("compensation") or {}
            actual_amount = computation.get("amount") if computation.get("status") == "computed" else None
            item["calculator_amount_inr"] = actual_amount
            item["amount_correct"] = actual_amount == expected["compensation_inr"]
            item["expected_compensation_inr"] = expected["compensation_inr"]
        if feedback:
            ids = dict.fromkeys(cid for s in steps if s.get("step") == "tool" for cid in s.get("chunk_ids", []))
            contexts = [chunks[cid]["text"] for cid in ids if cid in (chunks or {})]
            # Only structured, non-identifying eligibility facts are sent to the external judge.
            from worker.agent.research import research_facts
            question = json.dumps(research_facts(scenario["claim"]), sort_keys=True)
            item["semantic_feedback"] = feedback.evaluate(question, result.get("summary", ""), contexts)
        return item
    except (httpx.HTTPError, OSError, ValueError, KeyError, TypeError, TimeoutError) as exc:
        return {**case, "actual_route": None, "route_correct": False,
                "citation": _citation_metrics(None, expected, chunks), "status": "error",
                "error": f"{type(exc).__name__}: {exc}"}


def summarize(results):
    def average(values):
        values = [v for v in values if v is not None]
        return mean(values) if values else None
    semantic = {name: average([r.get("semantic_feedback", {}).get(name) for r in results]) for name in METRICS}
    semantic["measured_scenarios"] = sum(r.get("semantic_feedback", {}).get("status") == "measured" for r in results)
    semantic["errors"] = sum(r.get("semantic_feedback", {}).get("status") == "error" for r in results)
    return {"scenario_count": len(results), "route_accuracy": average([r["route_correct"] for r in results]),
            "citation_resolution_rate": average([r["citation"]["citation_resolution_rate"] for r in results]),
            "required_clause_recall": average([r["citation"]["required_clause_recall"] for r in results]),
            "amount_accuracy": average([r.get("amount_correct") for r in results]),
            "amount_cases": sum("expected_compensation_inr" in r for r in results),
            "errors": sum(r["status"] == "error" for r in results), "semantic_feedback": semantic}


def write_report(output_dir, results_by_version, *, report_path=None, corpus_dir=None):
    output_dir.mkdir(parents=True, exist_ok=True)
    summary = {v: summarize(rows) for v, rows in results_by_version.items()}
    artifact = {"generated_at": datetime.now(UTC).isoformat(), "corpus_chunks_sha256": _corpus_hash(corpus_dir),
                "prompt_sha256": {v: hashlib.sha256((ROOT / f"prompts/{v}.md").read_bytes()).hexdigest()
                                  for v in results_by_version}, "summary": summary, "results": results_by_version}
    (output_dir / "eval-results.json").write_text(json.dumps(artifact, indent=2) + "\n", encoding="utf-8")
    def pct(value):
        return "N/A" if value is None else f"{value:.1%}"
    lines = ["# Evaluation report", "", f"Generated: {artifact['generated_at']}", "",
             "v1 and v2 select different research instructions. "
             "Deterministic decision and verification gates remain active.",
             "The HTTP stub verifies plumbing only; its output does not demonstrate model-quality improvement.", "",
             "| Prompt | Scenarios | Route accuracy | Citation accuracy | Clause recall | "
             "Calculator amount accuracy | Errors |",
             "| --- | ---: | ---: | ---: | ---: | ---: | ---: |"]
    for v, m in summary.items():
        lines.append(f"| {v} | {m['scenario_count']} | {pct(m['route_accuracy'])} | "
                     f"{pct(m['citation_resolution_rate'])} | {pct(m['required_clause_recall'])} | "
                     f"{pct(m['amount_accuracy'])} ({m['amount_cases']} gold cases) | {m['errors']} |")
    lines += ["", "Calculator amount accuracy is scored from the internal deterministic calculator trace; "
              "the final API deliberately withholds unverified consequential amounts.", "",
              "## TruLens feedback", "",
              "Hallucination rate is defined as 1 − groundedness (a judge-score proxy).",
              "N/A means unavailable or not evaluated; no substitute score is fabricated.", "",
              "| Prompt | Groundedness | Context relevance | Answer relevance | Hallucination rate | "
              "Measured | Errors |",
              "| --- | ---: | ---: | ---: | ---: | ---: | ---: |"]
    for v, m in summary.items():
        s = m['semantic_feedback']
        lines.append(f"| {v} | " + " | ".join(pct(s[k]) for k in METRICS) +
                     f" | {s['measured_scenarios']} | {s['errors']} |")
    lines += ["", "## Scenario results", "", "| Prompt | Scenario | Expected | Actual | Route | Status |",
              "| --- | --- | --- | --- | --- | --- |"]
    for v, rows in results_by_version.items():
        for r in rows:
            lines.append(f"| {v} | {r['scenario']} | {r['expected_route']} | {r['actual_route'] or '—'} | "
                         f"{'PASS' if r['route_correct'] else 'FAIL'} | {r['status']} |")
            if r.get("error"):
                lines.append(f"\n{r['scenario']}: {r['error']}\n")
    report = "\n".join(lines) + "\n"
    (output_dir / "eval-report.md").write_text(report, encoding="utf-8")
    if report_path:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(report, encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api", default="http://localhost:8000")
    parser.add_argument("--scenarios", default="eval/scenarios/*.json")
    parser.add_argument("--output-dir", type=Path, default=Path("eval/results"))
    parser.add_argument("--report", type=Path, default=Path("docs/eval-report.md"))
    parser.add_argument("--corpus-dir", type=Path, default=ROOT / "corpus/processed")
    parser.add_argument("--prompt-versions", default="v1,v2")
    parser.add_argument("--timeout", type=float, default=180)
    parser.add_argument("--poll-interval", type=float, default=1)
    parser.add_argument("--semantic", choices=["trulens", "off"], default="trulens")
    args = parser.parse_args()
    versions = list(dict.fromkeys(v.strip() for v in args.prompt_versions.split(",") if v.strip()))
    if not versions or any(v not in {"v1", "v2"} for v in versions):
        parser.error("--prompt-versions accepts v1 and/or v2")
    if not math.isfinite(args.timeout) or args.timeout <= 0 or not math.isfinite(args.poll_interval) \
            or args.poll_interval < 0:
        parser.error("timeout must be positive and poll interval non-negative and finite")
    paths = sorted(glob.glob(args.scenarios))
    if not paths:
        parser.error(f"no scenarios matched {args.scenarios!r}")
    chunks = _load_chunks(args.corpus_dir)
    if not chunks:
        parser.error("No processed corpus: ingest it before evaluating citation accuracy")
    from worker.tools.corpus import Corpus
    corpus_version = Corpus(args.corpus_dir).version
    feedback = None
    if args.semantic == "trulens":
        try:
            feedback = SemanticFeedback.from_environment()
        except (ImportError, ValueError) as exc:
            parser.error(str(exc) + "; install the evaluation dependency group, or use --semantic off for smoke checks")
    with httpx.Client(timeout=args.timeout) as client:
        results = {v: [run_scenario(client, args.api, p, v, args.poll_interval, chunks,
                                   feedback=feedback, timeout=args.timeout, corpus_version=corpus_version)
                       for p in paths] for v in versions}
    write_report(args.output_dir, results, report_path=args.report, corpus_dir=args.corpus_dir)
    print(f"Report: {args.report}; raw results: {args.output_dir / 'eval-results.json'}")
    if any(r["status"] == "error" or (feedback and r.get("actual_route") != "quarantine"
                                      and r.get("semantic_feedback", {}).get("status") != "measured")
           for rows in results.values() for r in rows):
        raise SystemExit(1)


if __name__ == "__main__":
    main()

