"""Idempotent corpus ingestion (L1).

Steps (P0 TODO):
  1. Read corpus/manifest.yaml; sha256 each file; skip if hash unchanged since last build.
  2. Parse into clause-level chunks (one clause/sub-clause each) with clause_id, parent heading,
     doc_id, revision, page.
  3. Extract cross-references ("para 3.3.1", "sub-para (a)") -> clause graph edges.
  4. Embed changed chunks only; upsert to Chroma; write graph + freshness record
     (built_at, doc versions, chunk counts) to data/index_meta.json.
"""
import argparse
import logging

log = logging.getLogger(__name__)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--manifest", default="corpus/manifest.yaml")
    p.add_argument("--force", action="store_true", help="rebuild even if hashes are unchanged")
    p.parse_args()
    log.warning("ingest not implemented yet (#9); skipping")
    return


if __name__ == "__main__":
    main()
