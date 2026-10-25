"""Run the s01 graph against the real Compose corpus and print its evidence."""
from __future__ import annotations

import json
from pathlib import Path

from triage_common.contracts import EscalationReason, Route
from worker.agent.graph import run_agent

SCENARIO_PATH = Path("/src/eval/scenarios/s01_demo_cancellation_alternate_taken.json")
REQUIRED_CLAUSES = {"3.3.1", "3.3.2", "3.8.1(a)"}


def main() -> None:
    scenario = json.loads(SCENARIO_PATH.read_text(encoding="utf-8"))
    result, trace = run_agent(scenario["claim"], correlation_id="compose-s01-smoke")
    output = {
        "scenario": scenario["id"],
        "route": result.route.value,
        "escalation_reason": result.escalation_reason.value if result.escalation_reason else None,
        "corpus_version": result.corpus_version,
        "citation_clauses": sorted({citation.clause_id for citation in result.citations}),
        "tool_calls": trace[-1].get("tool_calls", 0) if trace else 0,
        "verified": result.verified,
        "summary": result.summary,
    }
    print(json.dumps(output, sort_keys=True))
    missing = REQUIRED_CLAUSES - set(output["citation_clauses"])
    if missing:
        raise SystemExit(f"s01 real-corpus smoke failed; missing clauses: {sorted(missing)}")
    if result.route is not Route.ESCALATE or result.escalation_reason is not EscalationReason.VERIFICATION_FAILED:
        raise SystemExit("s01 smoke expected ESCALATE / VERIFICATION_FAILED")
    if result.verified:
        raise SystemExit("s01 smoke expected verified=False until grounding verification is implemented")


if __name__ == "__main__":
    main()
