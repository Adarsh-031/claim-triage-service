"""Load test (#34): each simulated user submits a claim, polls it to completion, repeats.

So N users = N claims in flight. Locust reports two numbers the SLO is about:
  * ``POST /v1/claims``         submit latency (API only)
  * ``E2E claim end-to-end``    submit -> terminal status, measured by this file

Claims are the valid eval scenarios (no quarantine or guardrail cases) with a unique PNR,
so the result cache never short-circuits a run.
"""
from __future__ import annotations

import json
import os
import random
import string
import time
from pathlib import Path

from locust import HttpUser, between, events, task

SCENARIO_DIR = Path(os.getenv("SCENARIO_DIR", "/mnt/scenarios"))
E2E_TIMEOUT_S = float(os.getenv("E2E_TIMEOUT_S", "180"))
POLL_S = float(os.getenv("POLL_S", "0.5"))
TERMINAL = {"done", "completed", "escalated", "failed"}


def _claims() -> list[dict]:
    claims = []
    for path in sorted(SCENARIO_DIR.glob("s*.json")):
        scenario = json.loads(path.read_text(encoding="utf-8"))
        expected = scenario["expected"]
        if expected["route"] == "quarantine" or expected.get("escalation_reason") == "GUARDRAIL_BLOCKED":
            continue
        claims.append(scenario["claim"])
    if not claims:
        raise RuntimeError(f"no scenarios found in {SCENARIO_DIR}")
    return claims


CLAIMS = _claims()


def _pnr() -> str:
    return "".join(random.choices(string.ascii_uppercase + string.digits, k=6))


class ClaimUser(HttpUser):
    wait_time = between(0.5, 1.5)

    @task
    def submit_and_wait(self) -> None:
        claim = {**random.choice(CLAIMS), "pnr": _pnr()}
        started = time.perf_counter()
        with self.client.post("/v1/claims", json=claim, name="POST /v1/claims", catch_response=True) as resp:
            if resp.status_code != 202:
                resp.failure(f"HTTP {resp.status_code}: {resp.text[:120]}")
                return
            run_id = resp.json()["run_id"]
        status, exception = None, None
        while True:
            with self.client.get(f"/v1/runs/{run_id}", name="GET /v1/runs/[id]", catch_response=True) as poll:
                if poll.status_code != 200:
                    poll.failure(f"HTTP {poll.status_code}")
                else:
                    status = poll.json()["status"]
            if status in TERMINAL:
                break
            if time.perf_counter() - started > E2E_TIMEOUT_S:
                exception = TimeoutError(f"run {run_id} not terminal after {E2E_TIMEOUT_S}s")
                break
            time.sleep(POLL_S)
        if status == "failed":
            exception = RuntimeError(f"run {run_id} failed")
        events.request.fire(request_type="E2E", name="claim end-to-end",
                            response_time=(time.perf_counter() - started) * 1000,
                            response_length=0, exception=exception, context={})
