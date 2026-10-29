from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    llm_provider: str = "stub"
    llm_base_url: str = "http://llm-stub:8001/v1"
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    openrouter_api_key: str = ""
    llm_model_primary: str = ""
    llm_model_fallback: str = ""
    llm_timeout_s: float = 20
    tool_timeout_s: float = 5

    agent_max_steps: int = 12
    agent_max_tool_calls: int = 15
    agent_run_timeout_s: float = 60

    retry_max: int = 3
    breaker_fail_threshold: int = 5
    breaker_window_s: float = 30
    breaker_reset_s: float = 30

    airline_code: str = ""
    corpus_dir: str = ""  # Defaults to <repo>/corpus/processed; mounted read-only in workers.
    research_prompt_path: str = ""  # Container image sets an absolute path for its installed package.
    intake_prompt_path: str = ""  # Installed workers cannot rely on repository-relative paths.
    embedding_model: str = "sentence-transformers/all-MiniLM-L6-v2"

    redis_url: str = "redis://redis:6379/0"
    database_url: str = "postgresql+psycopg://triage:triage@postgres:5432/triage"
    chroma_url: str = "http://chroma:8000"
    chroma_collection: str = "triage-clauses-v1"
    worker_metrics_port: int = 8002
    otel_exporter_otlp_endpoint: str = "http://jaeger:4317"
    rate_limit_per_min: int = Field(default=10, ge=1)
    cache_enabled: bool = True
    cache_ttl_s: int = Field(default=3600, ge=1)
    prompt_version: str = "v1"

    queue_name: str = "triage:runs"


settings = Settings()
