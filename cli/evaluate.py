"""Run the fixed evaluation set (L3) against a running API and report metrics.

Metrics (P1 TODO): route accuracy, citation resolvability, groundedness, context relevance,
faithfulness, hallucination rate. Each scenario file holds the claim, expected route and the
clause(s) the answer must rest on.
"""
import argparse
import glob
import json
import time

import httpx


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--api", default="http://localhost:8000")
    p.add_argument("--scenarios", default="eval/scenarios/*.json")
    a = p.parse_args()
    results = []
    for path in sorted(glob.glob(a.scenarios)):
        with open(path) as fh:
            s = json.load(fh)
        run = httpx.post(f"{a.api}/v1/claims", json=s["claim"]).json()
        while True:
            out = httpx.get(f"{a.api}/v1/runs/{run['run_id']}").json()
            if out["status"] not in ("queued", "running"):
                break
            time.sleep(1)
        got = (out.get("result") or {}).get("route")
        results.append({"scenario": s["id"], "expected": s["expected"]["route"], "got": got,
                        "pass": got == s["expected"]["route"]})
    for r in results:
        print(f"{'PASS' if r['pass'] else 'FAIL'}  {r['scenario']:45} expected={r['expected']:26} got={r['got']}")
    print(f"\n{sum(r['pass'] for r in results)}/{len(results)} routes correct")


if __name__ == "__main__":
    main()
