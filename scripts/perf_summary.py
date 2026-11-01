"""Summarise Locust CSVs (perf/results/w<workers>-u<users>_stats.csv) into a Markdown table.

Writes perf/results/summary.md and prints it; paste or keep it next to docs/perf-report.md.
The SLO (docs/02-design.md): end-to-end p95 <= 25 s at 10 concurrent claims.
"""
from __future__ import annotations

import csv
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / "perf" / "results"
SLO_P95_MS = 25_000


def _row(path: Path, name: str) -> dict | None:
    with path.open(newline="", encoding="utf-8") as fh:
        return next((r for r in csv.DictReader(fh) if r["Name"] == name), None)


def _num(row: dict | None, key: str) -> float | None:
    try:
        return float(row[key]) if row and row.get(key) not in (None, "", "N/A") else None
    except ValueError:
        return None


def collect() -> list[dict]:
    rows = []
    for path in RESULTS.glob("w*-u*_stats.csv"):
        m = re.fullmatch(r"w(\d+)-u(\d+)_stats\.csv", path.name)
        if not m:
            continue
        e2e, submit = _row(path, "claim end-to-end"), _row(path, "POST /v1/claims")
        rows.append({
            "workers": int(m[1]), "users": int(m[2]),
            "claims": int(_num(e2e, "Request Count") or 0), "failed": int(_num(e2e, "Failure Count") or 0),
            "throughput": _num(e2e, "Requests/s"),
            "submit_p95": _num(submit, "95%"), "e2e_p50": _num(e2e, "50%"), "e2e_p95": _num(e2e, "95%"),
        })
    return sorted(rows, key=lambda r: (r["workers"], r["users"]))


def render(rows: list[dict]) -> str:
    def s(ms):
        return "-" if ms is None else f"{ms / 1000:.1f} s"
    lines = ["| Workers | Users | Claims | Failed | Claims/s | Submit p95 | E2E p50 | E2E p95 | SLO |",
             "| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | :---: |"]
    for r in rows:
        submit = "-" if r["submit_p95"] is None else f"{r['submit_p95']:.0f} ms"
        ok = r["e2e_p95"] is not None and r["e2e_p95"] <= SLO_P95_MS and r["failed"] == 0
        lines.append(f"| {r['workers']} | {r['users']} | {r['claims']} | {r['failed']} | "
                     f"{(r['throughput'] or 0):.2f} | {submit} | "
                     f"{s(r['e2e_p50'])} | {s(r['e2e_p95'])} | {'pass' if ok else 'FAIL'} |")
    for workers in sorted({r["workers"] for r in rows}):
        broke = next((r["users"] for r in rows if r["workers"] == workers
                      and (r["e2e_p95"] or 0) > SLO_P95_MS), None)
        lines.append(f"\n{workers} worker(s): SLO first breaks at "
                     f"{broke if broke else 'none of the tested'} concurrent users.")
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    text = render(collect())
    (RESULTS / "summary.md").write_text(text, encoding="utf-8")
    print(text)
