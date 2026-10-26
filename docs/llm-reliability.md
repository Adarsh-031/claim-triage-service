# LLM provider reliability

The OpenAI-compatible provider client enforces `LLM_TIMEOUT_S` (20 seconds by
default). `ResilientLLM` retries transient HTTP/response failures up to `RETRY_MAX`
with exponential backoff and jitter, then uses the configured fallback model. When
fallback is absent or fails, callers must escalate; provider failures must never be
turned into confident claim answers.

The primary provider circuit breaker is shared across worker replicas through Redis.
It opens after `BREAKER_FAIL_THRESHOLD` failures within `BREAKER_WINDOW_S`, rejects
calls while open, and permits one half-open probe after `BREAKER_RESET_S`. The
`llm_circuit_breaker_state` gauge reports 0 (closed), 1 (open), or 2 (half-open) for
the provider label.

For local fault-injection demos, set `STUB_MODE=error` to return HTTP 503 or
`STUB_MODE=slow` with `STUB_SLOW_DELAY` longer than `LLM_TIMEOUT_S` to trigger a
timeout. Restore `STUB_MODE=normal` for ordinary runs. The default delay is
`STUB_DELAY`.

`TOOL_TIMEOUT_S` is the configured timeout for tools that perform remote calls; each
tool integration must apply it when creating its own HTTP client. The run-level
timeout remains the outer wall-clock limit.
