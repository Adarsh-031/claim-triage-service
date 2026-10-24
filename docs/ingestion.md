# Clause corpus ingestion

Issue #9 builds the source passages and graph consumed by the embedding stage. It does not embed,
contact an LLM, or write a vector index.

## Run

Install Poppler (`sudo apt install poppler-utils` on Debian/Ubuntu), then:

```sh
uv sync --frozen
make corpus
make ingest
# Optional destination; paths in the manifest are relative to the manifest directory.
uv run python cli/ingest.py --manifest corpus/manifest.yaml --output /tmp/triage-corpus
```

Every source is checked against its manifest SHA-256 before parsing. A missing source or changed
snapshot fails the build before replacing any existing artifacts. Refresh the source/manifest
deliberately rather than bypassing the hash check. `pdftotext -layout` reads the CAR text layer;
there is no OCR or network access during ingestion.

## Parsing decisions

CAR's numbered clauses begin in its leftmost three columns. Wrapped references, including
`3.8.1 (a)` at the beginning of an indented continuation line, are body text. Form-feed page
boundaries supply the starting page number, while repeated headers, footers, and the final signature
are excluded. Clauses crossing pages remain intact.

Each complete numbered clause gets one chunk, including all of `3.3.2`. Lettered CAR children also
get explicit views such as `3.8.1(a)` so graph edges have precise targets. Parent/child passages
overlap intentionally; consumers should account for this when assembling context. Long clauses
are not split into arbitrary token windows. Heading-only sections are retained because they can
also be referenced (for example, `3.8`).

The two IndiGo snapshots use escaped Markdown numbers, bold headings, and ordinary numbered lists.
The parser recognizes their ordered top-level sections and dotted subsections, retaining lists
inside the passage. It does not interpret each list item as a new document clause. Markdown emphasis
and escaped heading dots are removed; other source wording is retained. All passages have whitespace
collapsed to single spaces before hashing. Markdown snapshots have `page: null` rather than fabricated
PDF page numbers. These parsers target the pinned source layouts; new document layouts need explicit
parser support and fixtures.

## Artifacts

Generated files under `corpus/processed/` are ignored by Git and can be regenerated with `make ingest`.

- `chunks.jsonl`: one JSON object per line, validated using `triage_common.contracts.Chunk`. IDs are
  `doc_id:clause_id:0001`; the suffix is fixed because full clauses are never split. `hash` is SHA-256
  of the UTF-8 normalized `text`. `refs` contains deduplicated clause IDs as required by the contract.
- `refs.json`: `refs` maps a document-qualified clause key (for example,
  `CAR-S3-M-IV-R4:3.3.2`) to its qualified targets. `referrers` maps targets to source keys, including
  empty lists for clauses without incoming edges. `unresolved` maps source keys to targets absent
  from the processed corpus. Keys omit the chunk suffix because these are clause relationships.
- `definitions.json`: case-folded term to a list of records containing `doc_id`, `clause_id`,
  `chunk_id`, and the definition's source `text`. A term can have several definitions across documents.
  CAR Section 2 supplies formal definitions; `extraordinary circumstances` and `force majeure`
  explicitly point to CAR `1.4`, where that concept is described. CoC entries and quoted aliases
  are extracted from their definitions section (`1.1`).

Reference extraction handles `Para`/`Paragraph`/`Clause`, case differences, spaces before `(a)`,
comma/conjunction/slash-separated targets, nested qualifiers such as `(c)(i)`, and relative
`sub-para (a) and (b)`. Relative letters resolve against the current numbered clause. Explicit
CoC references to `Para 3.2 of DGCA CAR - Section 3, Series M, Part IV` target the CAR document.
Other references stay within their source document. `refs.json` is authoritative for target document
identity because the existing `Chunk.refs` contract carries only clause IDs.

The IndiGo snapshots refer to lettered provisions such as `10.2(c)(i)` while rendering their bodies
as numbered lists. These targets remain **unresolved**, rather than guessing how the original letters
correspond to snapshot list items. Consumers can retrieve the enclosing numbered clause, but should
not treat that as an exact resolution of the missing child label.

Chunks sort by `chunk_id`; JSON keys, reference lists, and definition records have deterministic
ordering. There are no generated timestamps. Rebuilding identical sources produces identical bytes
for all three files and leaves unchanged outputs untouched. `--force` rewrites them with the same
content. Each changed artifact is staged to a temporary file and atomically replaced; the three
files are not a transactional snapshot for concurrent readers, so run ingestion before consumers.

## Verification

```sh
uv run ruff check .
uv run pytest -q
```

`tests/fixtures/car-s3-m-iv-r4.layout.txt` is the text extracted with
`pdftotext -layout corpus/raw/car-s3-m-iv-r4.pdf -` from the manifest-pinned PDF (SHA-256
`3b4b50844edc6a46099cce3b94626d29b03f90ccc61318aa1aa2db6d0fa3ff4a`). It preserves page breaks and
indentation so CI can test the real layout without downloading a PDF or requiring Poppler. When the
PDF and `pdftotext` are available, an additional integration test checks extraction against the fixture.

Tests cover ten known CAR reference cases, page continuations, parent/child boundaries, both CoC
snapshots, reverse graph edges, explicit cross-document references, unresolved targets, definitions
and aliases, schema validation, unique IDs, text hashes, and byte-identical rebuilds. Hash mismatches,
duplicate clauses, missing sources, and a missing PDF tool must fail clearly. The current pinned
sources produce 246 chunks: CAR 81, domestic CoC 84, international CoC 81.
