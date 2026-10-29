"""Shared fingerprinting for the processed corpus artifacts."""
from __future__ import annotations

import hashlib
from collections.abc import Mapping
from pathlib import Path

CORPUS_ARTIFACTS = ("chunks.jsonl", "refs.json", "definitions.json")


def corpus_version_from_contents(contents: Mapping[str, bytes]) -> str:
    """Return the same corpus version for both the indexer and research worker."""
    missing = [name for name in CORPUS_ARTIFACTS if name not in contents]
    if missing:
        raise ValueError(f"Missing processed corpus artifacts: {', '.join(missing)}")
    digest = hashlib.sha256()
    for name in CORPUS_ARTIFACTS:
        content = contents[name]
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return f"processed-{digest.hexdigest()[:16]}"


def corpus_version_from_directory(directory: Path) -> str:
    contents = {name: (directory / name).read_bytes() for name in CORPUS_ARTIFACTS}
    return corpus_version_from_contents(contents)
