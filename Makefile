setup:     ; uv sync
up:        ; docker compose up --build
down:      ; docker compose down
scale:     ; docker compose up --build --scale worker=3
ingest:    ; uv run python cli/ingest.py
index:     ; uv run python cli/index_corpus.py
index-check: ; uv run python cli/index_corpus.py --verify
eval:      ; uv run python cli/evaluate.py
test:      ; uv run pytest -q
lint:      ; uv run ruff check .
corpus:    ; uv run python scripts/fetch_corpus.py
fresh:     ; docker compose down -v && docker compose up --build -d && docker compose ps
