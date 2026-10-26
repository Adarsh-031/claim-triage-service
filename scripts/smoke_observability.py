"""Submit a fixture claim and assert API/worker/node spans share a Jaeger trace."""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from triage_common.contracts import TERMINAL_STATUSES


def fetch(url: str, body: dict | None = None):
    request = Request(url, data=json.dumps(body).encode() if body is not None else None,
                      headers={"Content-Type": "application/json"})
    with urlopen(request, timeout=5) as response:
        return json.load(response)


def check(api: str, jaeger: str, timeout: float = 60) -> dict:
    scenario = Path(__file__).resolve().parents[1] / "eval/scenarios/s01_demo_cancellation_alternate_taken.json"
    run = fetch(api + "/v1/claims", json.loads(scenario.read_text())["claim"])
    run_id = run["run_id"]
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = fetch(api + f"/v1/runs/{run_id}")["status"]
        if status in TERMINAL_STATUSES:
            break
        time.sleep(1)
    else:
        raise RuntimeError(f"Run did not finish: {run_id}")
    query = urlencode({"service": "triage-worker", "tags": json.dumps({"run_id": run_id}), "lookback": "1h"})
    required = {"queue.publish", "queue.process", "agent.intake", "agent.research", "agent.verify",
                "agent.finalize", "tool.search_corpus"}
    while time.monotonic() < deadline:
        for item in fetch(jaeger + "/api/traces?" + query)["data"]:
            names = {span["operationName"] for span in item["spans"]}
            services = {process["serviceName"] for process in item["processes"].values()}
            if required <= names and {"triage-api", "triage-worker"} <= services:
                return {"run_id": run_id, "status": status, "trace_id": item["traceID"],
                        "trace_url": jaeger + "/trace/" + item["traceID"], "spans": sorted(names)}
        time.sleep(1)
    raise RuntimeError(f"Correlated API/worker/graph trace did not reach Jaeger for {run_id}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api", default="http://localhost:8000")
    parser.add_argument("--jaeger", default="http://localhost:16686")
    args = parser.parse_args()
    print(json.dumps(check(args.api.rstrip("/"), args.jaeger.rstrip("/")), indent=2))


if __name__ == "__main__":
    main()
