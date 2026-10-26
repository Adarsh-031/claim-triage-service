FROM python:3.11.17-slim-bookworm
COPY --from=ghcr.io/astral-sh/uv:0.13.5 /uv /uvx /bin/
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/opt/venv PATH="/opt/venv/bin:$PATH"
WORKDIR /src

# 1) dependencies only (cached layer; reruns only when the lockfile changes)
COPY pyproject.toml uv.lock ./
COPY libs/common/pyproject.toml libs/common/
COPY services/api/pyproject.toml services/api/
COPY services/worker/pyproject.toml services/worker/
COPY services/ingest/pyproject.toml services/ingest/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --package triage-worker --no-install-workspace

# 2) source
COPY libs/common libs/common
COPY services/worker services/worker
COPY prompts /src/prompts
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --package triage-worker --no-editable

WORKDIR /src/services/worker
ENV CORPUS_DIR=/src/corpus/processed RESEARCH_PROMPT_PATH=/src/prompts/research_v1.md

CMD ["python", "-m", "worker.main"]
