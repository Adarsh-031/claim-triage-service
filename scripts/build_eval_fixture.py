"""Build the offline CI corpus from committed fixtures; never overwrite deployment data."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from cli.ingest import definition_table, parse_car, parse_markdown, reference_graph


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    chunks = parse_car((root / "tests/fixtures/car-s3-m-iv-r4.layout.txt").read_text(encoding="utf-8"))
    for suffix, doc in (("domestic", "DOM"), ("international", "INTL")):
        chunks += parse_markdown((root / f"corpus/raw/indigo-coc-{suffix}.md").read_text(encoding="utf-8"),
                                 f"INDIGO-COC-{doc}")
    chunks.sort(key=lambda c: c.chunk_id)
    args.output.mkdir(parents=True, exist_ok=False)
    (args.output / "chunks.jsonl").write_text("".join(c.model_dump_json() + "\n" for c in chunks), encoding="utf-8")
    (args.output / "refs.json").write_text(json.dumps(reference_graph(chunks)), encoding="utf-8")
    (args.output / "definitions.json").write_text(json.dumps(definition_table(chunks)), encoding="utf-8")


if __name__ == "__main__":
    main()
