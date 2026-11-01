"""Offline research corpus and HTTP provider fixtures shared by research tests."""
import importlib.util
import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from triage_common.settings import settings
from worker.agent import graph
from worker.agent.llm import OpenAICompatibleLLM
from worker.tools import corpus as corpus_tools

from cli.ingest import definition_table, parse_car, parse_markdown, reference_graph

ROOT = Path(__file__).parents[1]


@pytest.fixture(autouse=True)
def no_verifier_outcome_db(monkeypatch):
    """Worker tests run without Postgres; verifier rows are covered in test_verifier.py."""
    from worker import main as worker_main
    monkeypatch.setattr(worker_main, "record_verifier_outcomes", lambda *_args, **_kwargs: None)


@pytest.fixture(scope="session")
def processed_corpus(tmp_path_factory):
    directory = tmp_path_factory.mktemp("research-corpus")
    chunks = parse_car((ROOT / "tests/fixtures/car-s3-m-iv-r4.layout.txt").read_text())
    for suffix, doc in (("domestic", "DOM"), ("international", "INTL")):
        chunks += parse_markdown((ROOT / f"corpus/raw/indigo-coc-{suffix}.md").read_text(), f"INDIGO-COC-{doc}")
    chunks.sort(key=lambda c: c.chunk_id)
    (directory / "chunks.jsonl").write_text("".join(c.model_dump_json() + "\n" for c in chunks))
    (directory / "refs.json").write_text(json.dumps(reference_graph(chunks)))
    (directory / "definitions.json").write_text(json.dumps(definition_table(chunks)))
    return directory


@pytest.fixture
def research_env(monkeypatch, processed_corpus):
    monkeypatch.setattr(settings, "corpus_dir", str(processed_corpus))
    # No live Chroma in tests: hybrid search degrades to BM25 deterministically.
    monkeypatch.setattr(settings, "chroma_url", "")
    corpus_tools.VECTOR.reset()
    spec = importlib.util.spec_from_file_location("research_http_stub", ROOT / "services/llm-stub/app.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "STUB_DELAY", 0)
    provider = OpenAICompatibleLLM("http://testserver/v1", "stub")
    provider.client.close()
    with TestClient(module.app) as client:
        def handle(request):
            response = client.post(request.url.path, content=request.content, headers=request.headers)
            return httpx.Response(response.status_code, json=response.json(), request=request)

        with httpx.Client(base_url="http://testserver/v1", transport=httpx.MockTransport(handle)) as http:
            provider.client = http
            monkeypatch.setattr(graph, "get_llm", lambda: provider)
            yield provider
