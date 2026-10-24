"""Build clause chunks and reference/definition sidecars; embedding is a separate stage."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
from pathlib import Path

import yaml
from triage_common.contracts import Chunk

CAR_DOC = "CAR-S3-M-IV-R4"
NUMBER = r"\d+(?:\.\d+)*(?:\s*\([a-z]\))?(?:\s*\([ivx]+\))?"
REF = re.compile(rf"\b(?:para(?:graph)?s?|clauses?)\s+({NUMBER})", re.I)
NEXT_REF = re.compile(rf"\s*(?:,\s*(?:and\s+)?|/|and\s+|or\s+)({NUMBER})", re.I)
SUB_REF = re.compile(r"\bsub[- ]para(?:graph)?s?\s+\(([a-z])\)((?:\s*(?:and|or|,)\s*\([a-z]\))*)", re.I)
CAR_NUMBER = re.compile(r"^(\d+(?:\.\d+)*)(?:\.\s+|\s+)(\S.*)$")
LETTER = re.compile(r"^([a-z])\)\s+(.+)$")


def normalized(text: str) -> str:
    return " ".join(text.split())


def canonical_ref(value: str) -> str:
    return re.sub(r"\s+", "", value).lower()


def reference_targets(text: str, clause_id: str, doc_id: str) -> list[tuple[str, str]]:
    """Keep unresolved references, and never guess letter labels from Markdown lists."""
    text = normalized(text)
    targets: set[tuple[str, str]] = set()
    for match in REF.finditer(text):
        values = [canonical_ref(match[1])]
        end = match.end()
        while following := NEXT_REF.match(text, end):
            values.append(canonical_ref(following[1]))
            end = following.end()
        # CoC explicitly cites CAR: do not confuse its 3.2 with CoC section 3.2.
        external_car = re.match(
            r"\s+of\s+(?:DGCA\s+)?CAR\s*[-–]?\s*Section\s+3,\s*Series\s+M,\s*Part\s+IV\b",
            text[end:], re.I,
        )
        target_doc = CAR_DOC if external_car else doc_id
        targets.update((target_doc, value) for value in values)
    for match in SUB_REF.finditer(text):
        base = clause_id.split("(")[0]
        letters = [match[1], *re.findall(r"\(([a-z])\)", match[2], re.I)]
        targets.update((doc_id, f"{base}({letter.lower()})") for letter in letters)
    return sorted(targets)


def make_chunk(doc_id: str, clause_id: str, headings: list[str], text: str, page: int | None) -> Chunk:
    text = normalized(text)
    return Chunk(
        chunk_id=f"{doc_id}:{clause_id}:0001", clause_id=clause_id, doc_id=doc_id,
        heading_path=headings, text=text, page=page,
        refs=sorted({target for _, target in reference_targets(text, clause_id, doc_id)}),
        hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
    )


def parse_car(text: str, doc_id: str = CAR_DOC) -> list[Chunk]:
    """Retain complete numbered clauses, plus addressable views of lettered children."""
    chunks: list[Chunk] = []
    headings: dict[int, str] = {}
    current: tuple[str, list[str], int] | None = None
    lines: list[tuple[str, int]] = []

    def flush() -> None:
        if current is None:
            return
        clause, path, page = current
        chunks.append(make_chunk(doc_id, clause, path, " ".join(line for line, _ in lines), page))
        child: str | None = None
        child_lines: list[str] = []
        child_page = page

        def flush_child() -> None:
            if child:
                chunks.append(make_chunk(
                    doc_id, f"{clause}({child})", [*path, f"{clause}({child})"],
                    " ".join(child_lines), child_page,
                ))

        for line, line_page in lines[1:]:
            if match := LETTER.match(line):
                flush_child()
                child, child_page, child_lines = match[1], line_page, [line]
            elif child:
                child_lines.append(line)
        flush_child()

    for page, page_text in enumerate(text.split("\f"), 1):
        for raw in page_text.splitlines():
            line = raw.strip()
            if not line or re.fullmatch(r"\d+", line):
                continue
            if line.startswith(("CIVIL AVIATION REQUIREMENTS", "SERIES", "Rev.")):
                continue
            if line.startswith("(Arun Kumar)") or line == "Director General of Civil Aviation":
                continue
            # Wrapped references can start with a number too (e.g. 3.8.1 (a)).
            # Actual CAR clause labels occupy the leftmost three columns.
            match = CAR_NUMBER.match(line) if len(raw) - len(raw.lstrip()) <= 2 else None
            if match:
                flush()
                clause, title = match.groups()
                depth = clause.count(".") + 1
                headings = {level: name for level, name in headings.items() if level < depth}
                is_heading = depth == 1 or (depth == 2 and clause.startswith("3."))
                label = f"{clause} {title}" if is_heading else clause
                path = [*headings.values(), label]
                if is_heading:
                    headings[depth] = label
                current, lines = (clause, path, page), [(line, page)]
            elif current:
                lines.append((line, page))
    flush()
    return chunks


def parse_markdown(text: str, doc_id: str) -> list[Chunk]:
    """Snapshot headings have escaped dots; plain numbered lists are body text."""
    chunks: list[Chunk] = []
    major = 0
    major_heading = ""
    current: tuple[str, list[str]] | None = None
    lines: list[str] = []

    def flush() -> None:
        if current:
            chunks.append(make_chunk(doc_id, current[0], current[1], "\n".join(lines), None))

    for raw in text.splitlines():
        # Escaped one-level numbers also appear in 7.3's lists. Only the next
        # major section can start a top-level heading in these ordered snapshots.
        line = raw.strip()
        unstyled = line.replace("**", "")
        match = re.match(r"^(\d+)\\\.(\d+(?:\.\d+)*|)\.?\s*(.*)$", unstyled)
        is_heading = match and (bool(match[2]) or int(match[1]) == major + 1)
        if is_heading:
            flush()
            clause = match[1] + (f".{match[2]}" if match[2] else "")
            plain = unstyled.replace("\\.", ".")
            # Codeshare headings (18.x) close bold after the number; the prose
            # following that marker is passage text, not a heading title.
            label = plain
            if "**" in line:
                label = line.lstrip("*").split("**", 1)[0].replace("\\.", ".").strip().rstrip(".")
            if not match[2]:
                major, major_heading = int(match[1]), label
                path = [label]
            else:
                if int(match[1]) != major:
                    raise ValueError(f"{doc_id}: heading {clause} outside section {major}")
                path = [major_heading, label]
            current, lines = (clause, path), [plain]
        elif current:
            lines.append(line.replace("\\.", ".").replace("**", ""))
    flush()
    return chunks


def definition_table(chunks: list[Chunk]) -> dict[str, list[dict]]:
    definitions: dict[str, list[dict]] = {}

    def add(term: str, chunk: Chunk, text: str) -> None:
        key = normalized(term).strip(" .:").casefold()
        entry = {"doc_id": chunk.doc_id, "clause_id": chunk.clause_id,
                 "chunk_id": chunk.chunk_id, "text": text}
        if entry not in definitions.setdefault(key, []):
            definitions[key].append(entry)

    for chunk in chunks:
        if chunk.doc_id == CAR_DOC:
            if re.fullmatch(r"2\.\d+", chunk.clause_id):
                match = re.match(r"2\.\d+\s+(.+?)\s+(?:means|is|shall be)\b", chunk.text)
                if match:
                    add(match[1], chunk, chunk.text)
            elif chunk.clause_id == "1.4":
                # This definition is in the introduction, not Section 2.
                for term in ("extraordinary circumstances", "force majeure"):
                    add(term, chunk, chunk.text)
        elif chunk.clause_id == "1.1":
            # Each entry starts with a bullet or list number and a quoted term.
            entries = re.split(r"(?:\*|\b\d+\.)\s*(?=[“\"])", chunk.text)[1:]
            for entry in entries:
                # Quoted aliases such as “Aviation Security Fees” or “ASF” share a definition.
                head = re.match(r'(?:[“\"][^”\"]+[”\"]\s*(?:or\s*)?)+', entry)
                if head:
                    for term in re.findall(r'[“\"]([^”\"]+)[”\"]', head[0]):
                        add(term, chunk, entry.strip())
    return {term: sorted(entries, key=lambda entry: entry["chunk_id"])
            for term, entries in sorted(definitions.items())}


def reference_graph(chunks: list[Chunk]) -> dict:
    keys = {f"{chunk.doc_id}:{chunk.clause_id}" for chunk in chunks}
    refs: dict[str, list[str]] = {}
    referrers: dict[str, list[str]] = {key: [] for key in sorted(keys)}
    unresolved: dict[str, list[str]] = {}
    for chunk in chunks:
        key = f"{chunk.doc_id}:{chunk.clause_id}"
        refs[key] = [f"{doc}:{clause}" for doc, clause in
                     reference_targets(chunk.text, chunk.clause_id, chunk.doc_id)]
        for target in refs[key]:
            referrers.setdefault(target, []).append(key)
            if target not in keys:
                unresolved.setdefault(key, []).append(target)
    return {"refs": refs, "referrers": referrers, "unresolved": unresolved}


def build(manifest: Path, output: Path | None = None, *, force: bool = False) -> list[Chunk]:
    documents = yaml.safe_load(manifest.read_text(encoding="utf-8"))["documents"]
    chunks: list[Chunk] = []
    doc_ids: set[str] = set()
    for doc in documents:
        doc_id = doc["doc_id"]
        if doc_id in doc_ids:
            raise ValueError(f"Duplicate document ID: {doc_id}")
        doc_ids.add(doc_id)
        path = manifest.parent / doc["file"]
        if not path.exists():
            raise ValueError(f"Missing source: {path}; run make corpus first")
        if hashlib.sha256(path.read_bytes()).hexdigest() != doc["sha256"]:
            raise ValueError(f"Source hash mismatch: {path}; verify the snapshot and manifest")
        if doc["format"] == "pdf":
            if doc_id != CAR_DOC:
                raise ValueError(f"Unsupported PDF document: {doc_id}")
            try:
                result = subprocess.run(["pdftotext", "-layout", str(path), "-"],
                                        check=True, capture_output=True, encoding="utf-8")
            except FileNotFoundError as exc:
                raise ValueError("pdftotext is required; install poppler-utils") from exc
            parsed = parse_car(result.stdout, doc_id)
        elif doc["format"] == "markdown":
            parsed = parse_markdown(path.read_text(encoding="utf-8"), doc_id)
        else:
            raise ValueError(f"Unsupported source format: {doc['format']}")
        if not parsed:
            raise ValueError(f"No clauses found in {path}")
        chunks.extend(parsed)
    chunks.sort(key=lambda chunk: chunk.chunk_id)
    if len({chunk.chunk_id for chunk in chunks}) != len(chunks):
        raise ValueError("Duplicate chunk IDs; refusing to overwrite processed corpus")
    graph = reference_graph(chunks)
    artifacts = {
        "chunks.jsonl": "".join(json.dumps(chunk.model_dump(), ensure_ascii=False, sort_keys=True) + "\n"
                                for chunk in chunks),
        "refs.json": json.dumps(graph, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        "definitions.json": json.dumps(definition_table(chunks), ensure_ascii=False, sort_keys=True, indent=2) + "\n",
    }
    output = output if output is not None else manifest.parent / "processed"
    output.mkdir(parents=True, exist_ok=True)
    for name, content in artifacts.items():
        dest = output / name
        if force or not dest.exists() or dest.read_bytes() != content.encode("utf-8"):
            temporary = dest.with_suffix(dest.suffix + ".tmp")
            temporary.write_text(content, encoding="utf-8")
            temporary.replace(dest)
    return chunks


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=Path("corpus/manifest.yaml"))
    parser.add_argument("--output", type=Path, help="default: processed/ next to the manifest")
    parser.add_argument("--force", action="store_true", help="rewrite outputs even when unchanged")
    args = parser.parse_args()
    try:
        chunks = build(args.manifest, args.output, force=args.force)
    except (ValueError, OSError, subprocess.CalledProcessError) as exc:
        parser.exit(1, f"ingest: {exc}\n")
    print(f"ingest: wrote/verified {len(chunks)} chunks in {args.output or args.manifest.parent / 'processed'}")


if __name__ == "__main__":
    main()
