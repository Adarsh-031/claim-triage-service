"""Download missing corpus files and verify every sha256 in corpus/manifest.yaml.

Idempotent: files already present with the right hash are left alone, so running
it twice changes nothing. Exit code 1 on any hash mismatch or failed download.

Usage: uv run python scripts/fetch_corpus.py [--manifest corpus/manifest.yaml]
"""

from __future__ import annotations

import argparse
import hashlib
import sys
import urllib.request
from pathlib import Path

import yaml

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/129.0 Safari/537.36"


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 16), b""):
            h.update(block)
    return h.hexdigest()


def download(url: str, dest: Path) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    tmp = dest.with_suffix(dest.suffix + ".part")
    with urllib.request.urlopen(req, timeout=60) as r, tmp.open("wb") as f:
        f.write(r.read())
    tmp.replace(dest)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--manifest", default="corpus/manifest.yaml")
    args = p.parse_args()
    manifest = Path(args.manifest)
    root = manifest.parent
    ok = True

    for doc in yaml.safe_load(manifest.read_text())["documents"]:
        path = root / doc["file"]
        want = doc["sha256"]
        if path.exists() and sha256(path) == want:
            print(f"ok       {doc['doc_id']}")
            continue
        if doc.get("fetch") != "download":
            print(f"MISSING  {doc['doc_id']}: {path} absent or changed (snapshot, refresh by hand)")
            ok = False
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        for url in (doc["url"], doc.get("mirror_url")):
            if not url:
                continue
            try:
                download(url, path)
            except Exception as e:  # noqa: BLE001
                print(f"retry    {doc['doc_id']}: {url} failed ({e})")
                continue
            if sha256(path) == want:
                print(f"fetched  {doc['doc_id']} from {url}")
                break
            print(f"retry    {doc['doc_id']}: hash mismatch from {url}")
        else:
            print(f"FAIL     {doc['doc_id']}: could not get a file matching {want}")
            ok = False

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
