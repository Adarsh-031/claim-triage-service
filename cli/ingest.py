"""Build clause chunks and reference/definition sidecars; embedding is a separate stage."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
from itertools import groupby
from pathlib import Path

import yaml
from triage_common.contracts import Chunk

CAR_DOC = "CAR-S3-M-IV-R4"
LABEL = r"(?:[a-z]|[ivxlcdm]+|\d+)"
QUALIFIERS = rf"(?:\s*\({LABEL}\))+"
NUMBER = rf"\d+(?:\.\d+)*(?:{QUALIFIERS})?"
REF = re.compile(rf"\b(?:para(?:graph)?s?|clauses?)\s+({NUMBER})", re.I)
NEXT_REF = re.compile(rf"\s*(?:,\s*(?:and\s+)?|/|and\s+|or\s+)({NUMBER})", re.I)
SUB_REF = re.compile(
    rf"\bsub[- ]para(?:graph)?s?\s+({QUALIFIERS})"
    rf"((?:\s*(?:and|or|,)\s*(?:{QUALIFIERS}))*)", re.I,
)
CAR_NUMBER = re.compile(r"^(\d+(?:\.\d+)*)(?:\.\s+|\s+)(\S.*)$")
SUBCLAUSE = re.compile(rf"^(?:\(({LABEL})\)|({LABEL})[.)])(?:\s+(.*)|$)", re.I)
QUALIFIED_CLAUSE = re.compile(rf"^(\d+(?:\.\d+)*(?:\s*\({LABEL}\))+)[.:]?\s+(.*)$", re.I)
WRAPPED_REFERENCE = re.compile(r"\b(?:sub[- ]para(?:graph)?s?|para(?:graph)?s?|clauses?)\s*$", re.I)


def normalized(text: str) -> str:
    return " ".join(text.split())


def canonical_ref(value: str) -> str:
    return re.sub(r"\s+", "", value).lower()


def relative_target(clause_id: str, qualifier: str, known_ids: set[str] | None = None) -> str:
    """Resolve siblings at their qualifier level, retaining enclosing qualifiers."""
    base = clause_id.split("(", 1)[0]
    parts = re.findall(r"\(([^)]+)\)", clause_id)
    requested = re.findall(r"\(([^)]+)\)", qualifier)
    # A compound (a)(i) is rooted at the numbered clause. A single roman
    # qualifier is below the alphabetic level; a single letter names that level.
    if len(requested) > 1:
        return base + qualifier
    label = requested[0]
    roman = bool(re.fullmatch(r"[ivx]+", label))
    kind = "number" if label.isdigit() else "roman" if roman else "letter"
    matching_levels = []
    for index, part in enumerate(parts):
        part_kind = ("number" if part.isdigit() else
                     "roman" if index > 0 and re.fullmatch(r"[ivx]+", part) else "letter")
        if part_kind == kind:
            matching_levels.append(index)
    level = matching_levels[-1] if matching_levels else len(parts) if parts else 0
    candidate = base + "".join(f"({part})" for part in parts[:level]) + qualifier
    if known_ids is not None and candidate not in known_ids:
        # E.g. alphabetic (i) at the same level as (h), rather than roman (i).
        alternatives = {base + "".join(f"({part})" for part in parts[:i]) + qualifier
                        for i in range(len(parts) + 1)} & known_ids
        if len(alternatives) == 1:
            return alternatives.pop()
    return candidate


def reference_targets(
    text: str, clause_id: str, doc_id: str, known_ids: set[str] | None = None,
) -> list[tuple[str, str]]:
    """Keep unresolved references, and never guess letter labels from Markdown lists."""
    text = normalized(text)
    targets: set[tuple[str, str]] = set()
    for match in REF.finditer(text):
        values = [canonical_ref(match[1])]
        end = match.end()
        while following := NEXT_REF.match(text, end):
            values.append(canonical_ref(following[1]))
            end = following.end()
        external_car = re.match(
            r"\s+of\s+(?:DGCA\s+)?CAR\s*[-–]?\s*Section\s+3,\s*Series\s+M,\s*Part\s+IV\b",
            text[end:], re.I,
        )
        target_doc = CAR_DOC if external_car else doc_id
        targets.update((target_doc, value) for value in values)
    for match in SUB_REF.finditer(text):
        explicit = re.match(rf"\s+of\s+(?:para(?:graph)?|clause)\s+({NUMBER})", text[match.end():], re.I)
        context = canonical_ref(explicit[1]) if explicit else clause_id
        qualifiers = [match[1], *re.findall(QUALIFIERS, match[2], re.I)]
        for qualifier in qualifiers:
            targets.add((doc_id, relative_target(context, canonical_ref(qualifier), known_ids)))
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
    """Retain numbered clauses and nested subclause views using layout indentation."""
    chunks: list[Chunk] = []
    headings: dict[int, str] = {}
    current: tuple[str, list[str], int] | None = None
    lines: list[tuple[str, int, int]] = []

    def flush() -> None:
        if current is None:
            return
        clause, path, page = current
        # Views retain their parent's complete passage, while each reference is
        # interpreted in the innermost subclause containing its source text.
        views = [(clause, path, page, 0, len(lines))]
        nodes: list[dict] = []
        active: list[dict] = []
        contexts = [clause]
        for index, (line, line_page, indent) in enumerate(lines[1:], 1):
            marker = SUBCLAUSE.match(line)
            qualified = QUALIFIED_CLAUSE.match(line)
            if WRAPPED_REFERENCE.search(lines[index - 1][0]):
                # A reference can wrap immediately before '(i)', even across a
                # page boundary; that continuation is not a new child clause.
                marker = qualified = None
            if marker or qualified:
                if qualified:
                    child_id = canonical_ref(qualified[1])
                    while active and not child_id.startswith(active[-1]["id"] + "("):
                        active.pop()["end"] = index
                else:
                    while active and indent <= active[-1]["indent"]:
                        active.pop()["end"] = index
                parent = active[-1]["id"] if active else clause
                child_id = canonical_ref(qualified[1]) if qualified else parent + f"({marker[1] or marker[2]})".lower()
                child_path = [*path, *(node["id"] for node in active), child_id]
                node = {"id": child_id, "path": child_path, "page": line_page,
                        "start": index, "end": len(lines), "indent": indent}
                nodes.append(node)
                active.append(node)
            contexts.append(active[-1]["id"] if active else clause)
        views.extend((node["id"], node["path"], node["page"], node["start"], node["end"]) for node in nodes)
        known_ids = {view[0] for view in views}
        for view_id, view_path, view_page, start, end in views:
            chunk = make_chunk(doc_id, view_id, view_path, " ".join(line for line, _, _ in lines[start:end]), view_page)
            targets: set[str] = set()
            indices = range(start, end)
            for context, group in groupby(indices, key=lambda i: contexts[i]):
                passage = " ".join(lines[i][0] for i in group)
                targets.update(target for _, target in reference_targets(passage, context, doc_id, known_ids))
            chunk.refs = sorted(targets)
            chunks.append(chunk)

    for page, page_text in enumerate(text.split("\f"), 1):
        for raw in page_text.expandtabs(8).splitlines():
            line = raw.strip()
            indent = len(raw) - len(raw.lstrip())
            if not line or re.fullmatch(r"\d+", line):
                continue
            if line.startswith(("CIVIL AVIATION REQUIREMENTS", "SERIES", "Rev.")):
                continue
            if line.startswith("(Arun Kumar)") or line == "Director General of Civil Aviation":
                continue
            # Indented wrapped references are body text, not new clause labels.
            match = CAR_NUMBER.match(line) if indent <= 2 and not QUALIFIED_CLAUSE.match(line) else None
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
                current, lines = (clause, path, page), [(line, page, indent)]
            elif current:
                lines.append((line, page, indent))
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
        unstyled = line.replace("**", "").replace("__", "")
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
            lines.append(line.replace("\\.", ".").replace("**", "").replace("__", ""))
    flush()
    return chunks


# The pinned snapshots sometimes mix a curly opening quote with a straight
# closing quote (or vice versa). Apostrophes inside double-quoted terms are text.
QUOTED_TERM = r'''(?:[“"][^”"]+[”"]|[‘'][^’']+[’'])'''
DEFINITION_VERB = r"(?:shall\s+mean|means?|refers?\s+to|is\s+defined\s+as|shall\s+be|is|are)\b"
DEFINITION_HEAD = re.compile(
    rf"^(?:{QUOTED_TERM}(?:\s*(?:or|/|,)\s*{QUOTED_TERM})*|"
    rf"[A-Za-z][A-Za-z0-9 /&()’'-]{{0,100}}?)\s*(?:{DEFINITION_VERB}|[:–—-])", re.I,
)
DEFINITION_ENTRY = re.compile(
    rf"(?<!\S)(?:[-+*•]|\d+[.)]|\(\d+\))\s+(?="
    rf"[“\"‘']|[A-Za-z][A-Za-z0-9 /&()’'-]{{0,100}}?\s*(?:{DEFINITION_VERB}|[:–—]))", re.I,
)


def definition_terms(text: str) -> list[str]:
    """Accept quoted aliases or a bare term followed by a definition verb/delimiter."""
    quoted = re.match(rf"^{QUOTED_TERM}(?:\s*(?:or|/|,)\s*{QUOTED_TERM})*", text)
    if quoted:
        return [term[1:-1] for term in re.findall(QUOTED_TERM, quoted[0])]
    head = DEFINITION_HEAD.match(text)
    if not head:
        return []
    term = re.split(rf"\s+(?:{DEFINITION_VERB})|\s*[:–—]", head[0], maxsplit=1, flags=re.I)[0]
    return [term.rstrip(" -")]


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
                body = re.sub(r"^2\.\d+\.?\s+", "", chunk.text)
                for term in definition_terms(body):
                    add(term, chunk, chunk.text)
            elif chunk.clause_id == "1.4":
                for term in ("extraordinary circumstances", "force majeure"):
                    add(term, chunk, chunk.text)
        elif chunk.clause_id == "1.1":
            markers = list(DEFINITION_ENTRY.finditer(chunk.text))
            for index, marker in enumerate(markers):
                end = markers[index + 1].start() if index + 1 < len(markers) else len(chunk.text)
                entry = chunk.text[marker.end():end].strip()
                for term in definition_terms(entry):
                    add(term, chunk, entry)
    return {term: sorted(entries, key=lambda entry: entry["chunk_id"])
            for term, entries in sorted(definitions.items())}


def reference_graph(chunks: list[Chunk]) -> dict:
    keys = {f"{chunk.doc_id}:{chunk.clause_id}" for chunk in chunks}
    refs: dict[str, list[str]] = {}
    referrers: dict[str, list[str]] = {key: [] for key in sorted(keys)}
    unresolved: dict[str, list[str]] = {}
    for chunk in chunks:
        key = f"{chunk.doc_id}:{chunk.clause_id}"
        external = {clause: doc for doc, clause in reference_targets(chunk.text, chunk.clause_id, chunk.doc_id)
                    if doc != chunk.doc_id}
        refs[key] = sorted(f"{external.get(clause, chunk.doc_id)}:{clause}" for clause in chunk.refs)
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
