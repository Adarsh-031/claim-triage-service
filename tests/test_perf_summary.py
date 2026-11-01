import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("perf_summary", ROOT / "scripts/perf_summary.py")
perf_summary = importlib.util.module_from_spec(spec)
spec.loader.exec_module(perf_summary)

HEADER = "Type,Name,Request Count,Failure Count,Requests/s,50%,95%\n"


def write(path, e2e_p95):
    path.write_text(HEADER + "POST,POST /v1/claims,10,0,1.0,20,40\n"
                    f"E2E,claim end-to-end,10,0,0.5,8000,{e2e_p95}\n")


def test_summary_marks_where_the_slo_breaks(tmp_path, monkeypatch):
    monkeypatch.setattr(perf_summary, "RESULTS", tmp_path)
    write(tmp_path / "w1-u5_stats.csv", 12000)
    write(tmp_path / "w1-u10_stats.csv", 41000)
    write(tmp_path / "w3-u10_stats.csv", 15000)
    text = perf_summary.render(perf_summary.collect())
    assert "| 1 | 10 | 10 | 0 | 0.50 | 40 ms | 8.0 s | 41.0 s | FAIL |" in text
    assert "| 3 | 10 |" in text and "1 worker(s): SLO first breaks at 10" in text
    assert "3 worker(s): SLO first breaks at none" in text
