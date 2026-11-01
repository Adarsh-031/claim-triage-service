"""Hit@k of search_corpus over eval/retrieval/queries.yaml for each retrieval mode.

Vector and hybrid need a reachable Chroma (CHROMA_URL); without one they are reported
as unavailable rather than silently scored as BM25.
"""
from __future__ import annotations

import argparse
from pathlib import Path

import yaml
from worker.tools import corpus

ROOT = Path(__file__).resolve().parents[1]


def evaluate(cases: list[dict], mode: str, k: int, doc_id: str | None) -> tuple[int, list[str]]:
    hits, misses = 0, []
    for case in cases:
        results = corpus.search_corpus(case["query"], k=k, doc_id=doc_id, mode=mode)
        if any(c["retrieval"] == "bm25_fallback" for c in results):
            raise corpus.RetrievalUnavailable("hybrid fell back to BM25")
        found = [c["clause_id"] for c in results]
        if set(case["clauses"]) & set(found):
            hits += 1
        else:
            misses.append(f"{case['query']!r} -> {found}")
    return hits, misses


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--queries", type=Path, default=ROOT / "eval/retrieval/queries.yaml")
    parser.add_argument("-k", type=int, default=3)
    parser.add_argument("--doc-id", default=None, help="restrict to one document, e.g. CAR-S3-M-IV-R4")
    args = parser.parse_args()
    cases = yaml.safe_load(args.queries.read_text())
    for mode in ("bm25", "vector", "hybrid"):
        try:
            hits, misses = evaluate(cases, mode, args.k, args.doc_id)
        except corpus.RetrievalUnavailable as exc:
            print(f"{mode:6}  unavailable ({exc})")
            continue
        print(f"{mode:6}  hit@{args.k} = {hits}/{len(cases)}")
        for miss in misses:
            print(f"        miss {miss}")


if __name__ == "__main__":
    main()
