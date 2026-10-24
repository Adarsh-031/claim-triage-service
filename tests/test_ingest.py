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


@pytest.mark.parametrize(("first", "second", "suffix"), [
    ("a)", "b)", "a"),
    ("(a)", "(b)", "a"),
    ("a.", "b.", "a"),
    ("(A)", "(B)", "a"),
    ("1)", "2)", "1"),
    ("(1)", "(2)", "1"),
])
@pytest.mark.parametrize("indent", ["", "    ", "\t", "             "])
def test_car_subclause_marker_formats_and_indentation(first, second, suffix, indent):
    text = (f"3.8.0 See Para 3.8.1({suffix}).\n3.8.1 Facilities\n"
            f"{indent}{first} Meals and refreshments.\n{indent}{second} Hotel accommodation.")
    chunks = {chunk.clause_id: chunk for chunk in parse_car(text)}
    assert chunks[f"3.8.1({suffix})"].text.endswith("Meals and refreshments.")
    assert "Hotel accommodation" not in chunks[f"3.8.1({suffix})"].text
    assert "Hotel accommodation" in chunks["3.8.1"].text
    assert reference_graph(list(chunks.values()))["unresolved"] == {}


def test_nested_subclauses_keep_context_in_parent_graph():
    text = """3.8.1 Facilities
    (a) Main provision.
        (i) First option.
        ii) Second option; see sub-para (i) and sub-para (b).
    b. Other provision.
3.8.2 Next provision.
"""
    chunks = {chunk.clause_id: chunk for chunk in parse_car(text)}
    assert "3.8.1(a)(i)" in chunks
    assert "3.8.1(a)(ii)" in chunks
    expected = ["3.8.1(a)(i)", "3.8.1(b)"]
    assert chunks["3.8.1(a)(ii)"].refs == expected
    assert chunks["3.8.1(a)"].refs == expected
    assert chunks["3.8.1"].refs == expected
    assert "Other provision" not in chunks["3.8.1(a)"].text
    assert chunks["3.8.1(a)(ii)"].heading_path[-2:] == ["3.8.1(a)", "3.8.1(a)(ii)"]
    graph = reference_graph(list(chunks.values()))
    assert graph["unresolved"] == {}
    assert f"{CAR_DOC}:3.8.1(a)(i)" in graph["refs"][f"{CAR_DOC}:3.8.1"]


def test_nested_subclause_across_page_and_wrapped_reference():
    text = """3.8.1 Facilities
    (a) Options:
        (i) First.
        (ii) Second; see sub-para
\fCIVIL AVIATION REQUIREMENTS SECTION 3
             (i) above.
        (iii) Third option.
    (b) Third.
"""
    chunks = {chunk.clause_id: chunk for chunk in parse_car(text)}
    assert "3.8.1(a)(ii)(i)" not in chunks
    assert chunks["3.8.1(a)(ii)"].refs == ["3.8.1(a)(i)"]
    assert chunks["3.8.1(a)(iii)"].page == 2


@pytest.mark.parametrize(("context", "relative", "target"), [
    ("3.4.1", "(a)", "3.4.1(a)"),
    ("3.4.1(c)", "(a)", "3.4.1(a)"),
    ("3.4.1(c)", "(i)", "3.4.1(c)(i)"),
    ("3.4.1(c)(ii)", "(i)", "3.4.1(c)(i)"),
    ("3.4.1(c)(ii)", "(a)", "3.4.1(a)"),
    ("3.4.1(a)(ii)", "(c)", "3.4.1(c)"),
    ("3.4.1(c)(ii)(b)", "(a)", "3.4.1(c)(ii)(a)"),
    ("3.4.1(c)(ii)(b)", "(i)", "3.4.1(c)(i)"),
    ("3.4.1(c)(ii)", "(a)(i)", "3.4.1(a)(i)"),
    ("3.4.1(a)(2)", "(1)", "3.4.1(a)(1)"),
])
def test_nested_relative_reference_levels(context, relative, target):
    assert reference_targets(f"See sub-para {relative}.", context, CAR_DOC) == [(CAR_DOC, target)]


def test_relative_reference_explicit_base_and_alphabetic_i():
    targets = reference_targets("See sub-para (i) of Para 3.4.2(b).", "3.4.1(a)(ii)", CAR_DOC)
    assert (CAR_DOC, "3.4.2(b)(i)") in targets
    assert (CAR_DOC, "3.4.1(a)(i)") not in targets
    assert reference_targets("See sub-para (i).", "3.4.1(h)", CAR_DOC,
                             {"3.4.1(h)", "3.4.1(i)"}) == [(CAR_DOC, "3.4.1(i)")]


@pytest.mark.parametrize("spacing", ["", " "])
def test_fully_qualified_subclauses_and_numbered_children(spacing):
    text = f"""3.8.1 Facilities
    3.8.1{spacing}(a) Meals.
        1. First option.
        2. Second option; see sub-para (1).
    3.8.1{spacing}(b) Accommodation.
"""
    chunks = {chunk.clause_id: chunk for chunk in parse_car(text)}
    assert chunks["3.8.1(a)(2)"].refs == ["3.8.1(a)(1)"]
    assert "Accommodation" not in chunks["3.8.1(a)"].text
    assert reference_graph(list(chunks.values()))["unresolved"] == {}


@pytest.mark.parametrize(("definition", "term"), [
    ('“Block time” means elapsed flight time.', "block time"),
    ('"Block time" SHALL MEAN elapsed flight time.', "block time"),
    ("Block time refers to elapsed flight time.", "block time"),
    ("Block time is defined as elapsed flight time.", "block time"),
    ("Block time: elapsed flight time.", "block time"),
    ("Block time — elapsed flight time.", "block time"),
    ("Block time shall be elapsed flight time.", "block time"),
])
def test_car_definition_wording_variations(definition, term):
    chunks = parse_car(f"2. DEFINITIONS\n2.9 {definition}")
    entries = definition_table(chunks)[term]
    assert entries[0]["clause_id"] == "2.9"
    assert entries[0]["text"] == chunks[-1].text


@pytest.mark.parametrize("doc_id", ["INDIGO-COC-DOM", "INDIGO-COC-INTL"])
@pytest.mark.parametrize(("entry", "term"), [
    ('- **“Block time”** means elapsed flight time.', "block time"),
    ('+ "Block time" SHALL MEAN elapsed flight time.', "block time"),
    ("• ‘Block time’ refers to elapsed flight time.", "block time"),
    ("1) Block time is defined as elapsed flight time.", "block time"),
    ("(1) __Block time__: elapsed flight time.", "block time"),
    ("1. Block time — elapsed flight time.", "block time"),
    ('* “Customer’s ticket” means a booking record.', "customer’s ticket"),
    ('* “Flight Segment" or “Route ” means a flight segment.', "flight segment"),
    ('* "Unconnected Segments” means separate segments.', "unconnected segments"),
])
def test_indigo_definition_formatting_variations(doc_id, entry, term):
    text = (f"1\\. Definitions\n**1\\.1 Definitions**\n{entry}\n"
            "    Continued definition text.\n- “Next term” means something else.")
    chunks = parse_markdown(text, doc_id)
    definitions = definition_table(chunks)
    assert term in definitions
    assert definitions[term][0]["doc_id"] == doc_id
    assert "Continued definition text." in definitions[term][0]["text"]
    assert "Next term" not in definitions[term][0]["text"]
    assert definitions[term][0]["text"] in chunks[-1].text
    assert "next term" in definitions


def test_expected_formal_definitions_in_each_source(car):
    definitions = definition_table(list(car.values()))
    for number, term in enumerate([
        "airline", "operating airline", "reservation", "final destination",
        "a person with reduced mobility", "denied boarding", "volunteer",
        "cancellation", "block time", "downgrading",
    ], 1):
        assert definitions[term][0]["clause_id"] == f"2.{number}"
    for name, doc_id in [("domestic", "INDIGO-COC-DOM"), ("international", "INDIGO-COC-INTL")]:
        chunks = parse_markdown((ROOT / f"corpus/raw/indigo-coc-{name}.md").read_text(), doc_id)
        definitions = definition_table(chunks)
        for term in ["booking", "baggage", "airport charges", "force majeure", "asf"]:
            assert definitions[term][0]["doc_id"] == doc_id
            assert definitions[term][0]["clause_id"] == "1.1"
        if name == "domestic":
            assert definitions["route"] == definitions["flight segment"]
        else:
            for term in ["unconnected segments", "marketing carrier", "operating carrier"]:
                assert definitions[term][0]["doc_id"] == doc_id
