import hashlib
import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from triage_common.contracts import Chunk

from cli.ingest import (
    CAR_DOC,
    build,
    definition_table,
    parse_car,
    parse_markdown,
    reference_graph,
    reference_targets,
)

ROOT = Path(__file__).resolve().parents[1]
LAYOUT = (ROOT / "tests/fixtures/car-s3-m-iv-r4.layout.txt").read_text()


@pytest.fixture(scope="module")
def car():
    return {chunk.clause_id: chunk for chunk in parse_car(LAYOUT)}


@pytest.mark.parametrize(("source", "targets"), [
    ("3.2.2", {"3.2.1"}),
    ("3.2.3", {"3.2.2"}),
    ("3.3.2", {"3.3.1", "3.8.1(a)"}),
    ("3.3.3", {"3.8.1(a)"}),
    ("3.3.4", {"1.4", "1.5"}),
    ("3.4.1", {"3.8.1(a)", "3.4.1(a)", "3.4.1(b)"}),
    ("3.4.3", {"3.8.1(b)"}),
    ("3.4.4", {"3.8", "1.4", "1.5"}),
    ("3.6.1", {"3.2.2", "3.3.2", "3.5.1"}),
    ("3.7.1", {"3.2.2", "3.3.2"}),
])
def test_known_car_references(car, source, targets):
    assert targets <= set(car[source].refs)


def test_car_boundaries_pages_and_letter_views(car):
    assert len(car) == len(parse_car(LAYOUT))
    clause = car["3.3.2"]
    assert "a) INR 5,000" in clause.text
    assert "b) INR 7,500" in clause.text
    assert "c) INR 10,000" in clause.text
    assert clause.text.endswith("waiting for the alternate flight.")
    assert clause.page == 5
    assert clause.heading_path == ["3 REQUIREMENTS", "3.3 Cancellation of Flight", "3.3.2"]
    assert car["3.8.1(a)"].text == "a) Meals and refreshments in relation to waiting time."
    assert car["3.8.1(a)"].page == 7
    assert car["1.4"].page == 1
    assert "of force majeure" in car["1.4"].text  # continuation from page 2
    assert "control of the airline" in car["3.3.4"].text  # continuation from page 6
    assert "Rev. 4" not in car["3.3.4"].text
    assert "SECTION 3" not in car["1.4"].text
    assert "Director General" not in car["3.10.6"].text


def test_relative_references_in_child_use_parent():
    assert reference_targets("See sub-para (a) and (b).", "3.4.1(c)", CAR_DOC) == [
        (CAR_DOC, "3.4.1(a)"), (CAR_DOC, "3.4.1(b)"),
    ]


@pytest.mark.parametrize(("name", "doc_id", "count"), [
    ("domestic", "INDIGO-COC-DOM", 84),
    ("international", "INDIGO-COC-INTL", 81),
])
def test_snapshot_headings_and_lists(name, doc_id, count):
    chunks = parse_markdown((ROOT / f"corpus/raw/indigo-coc-{name}.md").read_text(), doc_id)
    by_clause = {chunk.clause_id: chunk for chunk in chunks}
    assert len(chunks) == len(by_clause) == count
    assert {str(number) for number in range(1, 24)} <= by_clause.keys()
    assert all(chunk.page is None for chunk in chunks)
    assert "Conditions of Carriage" in by_clause["1.1"].text
    assert by_clause["18.2"].heading_path[-1] == "18.2"
    assert by_clause["23"].text
    if name == "international":
        # The escaped 1..7 list within 7.3 must not become top-level sections.
        assert "7. In the event of ticket cancellations due to a medical emergency" in by_clause["7.3"].text
    graph = reference_graph(chunks)
    assert f"{doc_id}:10.2(c)(i)" in graph["unresolved"][f"{doc_id}:10.2"]
    assert f"{CAR_DOC}:3.2" in graph["refs"][f"{doc_id}:10.2"]
    assert f"{doc_id}:3.2" not in graph["refs"][f"{doc_id}:10.2"]


def test_graph_reverse_edges_and_no_dangling_car_refs(car):
    graph = reference_graph(list(car.values()))
    assert graph["unresolved"] == {}
    assert f"{CAR_DOC}:3.3.2" in graph["referrers"][f"{CAR_DOC}:3.8.1(a)"]
    for source, targets in graph["refs"].items():
        for target in targets:
            assert source in graph["referrers"][target]


def test_definitions_keep_provenance_and_aliases(car):
    chunks = list(car.values())
    for name, doc_id in [("domestic", "INDIGO-COC-DOM"), ("international", "INDIGO-COC-INTL")]:
        chunks += parse_markdown((ROOT / f"corpus/raw/indigo-coc-{name}.md").read_text(), doc_id)
    definitions = definition_table(chunks)
    assert definitions["block time"][0]["clause_id"] == "2.9"
    assert definitions["extraordinary circumstances"][0]["clause_id"] == "1.4"
    assert {entry["doc_id"] for entry in definitions["force majeure"]} == {
        CAR_DOC, "INDIGO-COC-DOM", "INDIGO-COC-INTL",
    }
    assert definitions["asf"] == definitions["aviation security fees"]
    assert "Airport Charges" in definitions["airport charges"][0]["text"]
    assert "Baggage Identification Tag" not in definitions["airport charges"][0]["text"]
    by_id = {chunk.chunk_id: chunk for chunk in chunks}
    for entries in definitions.values():
        for entry in entries:
            assert entry["text"] in by_id[entry["chunk_id"]].text


@pytest.fixture
def manifest(tmp_path, monkeypatch):
    """Run the complete pipeline offline using the real PDF's extracted text."""
    (tmp_path / "raw").mkdir()
    documents = yaml.safe_load((ROOT / "corpus/manifest.yaml").read_text())["documents"]
    for doc in documents:
        dest = tmp_path / doc["file"]
        if doc["format"] == "pdf":
            dest.write_bytes(b"PDF stand-in: text extraction is replaced with the layout fixture")
            doc["sha256"] = hashlib.sha256(dest.read_bytes()).hexdigest()
        else:
            shutil.copyfile(ROOT / "corpus" / doc["file"], dest)
    path = tmp_path / "manifest.yaml"
    path.write_text(yaml.safe_dump({"documents": documents}))

    def extract(command, **kwargs):
        assert command == ["pdftotext", "-layout", str(tmp_path / documents[0]["file"]), "-"]
        assert kwargs["check"]
        return SimpleNamespace(stdout=LAYOUT)

    monkeypatch.setattr("cli.ingest.subprocess.run", extract)
    return path


def test_build_valid_unique_hashed_and_byte_identical(manifest):
    output = manifest.parent / "processed"
    chunks = build(manifest)
    assert len(chunks) == 246
    assert len({chunk.chunk_id for chunk in chunks}) == len(chunks)
    assert chunks == sorted(chunks, key=lambda chunk: chunk.chunk_id)
    for line in (output / "chunks.jsonl").read_text().splitlines():
        chunk = Chunk.model_validate_json(line)
        assert chunk.hash == hashlib.sha256(chunk.text.encode("utf-8")).hexdigest()
    before = {path.name: (path.read_bytes(), path.stat().st_mtime_ns) for path in output.iterdir()}
    build(manifest)
    assert before == {path.name: (path.read_bytes(), path.stat().st_mtime_ns) for path in output.iterdir()}
    build(manifest, force=True)
    assert {name: content for name, (content, _) in before.items()} == {
        path.name: path.read_bytes() for path in output.iterdir()
    }
    graph = json.loads((output / "refs.json").read_text())
    assert "INDIGO-COC-DOM:10.2" in graph["referrers"][f"{CAR_DOC}:3.2"]


def test_bad_source_does_not_overwrite_existing_outputs(manifest):
    build(manifest)
    output = manifest.parent / "processed/chunks.jsonl"
    before = output.read_bytes()
    (manifest.parent / "raw/indigo-coc-domestic.md").write_text("tampered")
    with pytest.raises(ValueError, match="Source hash mismatch"):
        build(manifest)
    assert output.read_bytes() == before


def test_duplicate_clauses_rejected_before_writing(manifest):
    snapshot = manifest.parent / "raw/indigo-coc-domestic.md"
    snapshot.write_text(snapshot.read_text() + "\n**23\\.1 Repeated**\nbody\n**23\\.1 Repeated**\nbody\n")
    data = yaml.safe_load(manifest.read_text())
    data["documents"][1]["sha256"] = hashlib.sha256(snapshot.read_bytes()).hexdigest()
    manifest.write_text(yaml.safe_dump(data))
    with pytest.raises(ValueError, match="Duplicate chunk IDs"):
        build(manifest)
    assert not (manifest.parent / "processed").exists()


def test_missing_pdf_tool_has_actionable_error(manifest, monkeypatch):
    def missing(*args, **kwargs):
        raise FileNotFoundError("pdftotext")

    monkeypatch.setattr("cli.ingest.subprocess.run", missing)
    with pytest.raises(ValueError, match="install poppler-utils"):
        build(manifest)


def test_missing_source_has_actionable_error(tmp_path):
    path = tmp_path / "manifest.yaml"
    path.write_text(yaml.safe_dump({"documents": [{
        "doc_id": CAR_DOC, "file": "missing.pdf", "format": "pdf", "sha256": "0" * 64,
    }]}))
    with pytest.raises(ValueError, match="make corpus"):
        build(path)


@pytest.mark.skipif(not shutil.which("pdftotext") or not (ROOT / "corpus/raw/car-s3-m-iv-r4.pdf").exists(),
                    reason="optional local PDF integration; offline layout fixture is always tested")
def test_real_pdf_matches_layout_fixture():
    text = subprocess.check_output([
        "pdftotext", "-layout", str(ROOT / "corpus/raw/car-s3-m-iv-r4.pdf"), "-",
    ], encoding="utf-8")
    assert [chunk.model_dump() for chunk in parse_car(text)] == [chunk.model_dump() for chunk in parse_car(LAYOUT)]
