# LLM provider reliability

The OpenAI-compatible provider client enforces `LLM_TIMEOUT_S` (20 seconds by
default), bounded further by the remaining agent run deadline. `ResilientLLM` retries
network/response failures and HTTP 429/5xx up to `RETRY_MAX` with exponential
backoff and jitter. It does not retry permanent HTTP 4xx responses; it gives the
configured fallback model one attempt instead. When fallback is absent or fails,
callers must escalate; provider failures must never be turned into confident claim
answers.

Worst-case primary latency ≈ `RETRY_MAX` × `LLM_TIMEOUT_S` + backoff (0.5, 1, 2… s, max 8 s + jitter), then one fallback call; all calls share the 60 s run deadline.

The primary provider circuit breaker is shared across worker replicas through Redis.
It opens after `BREAKER_FAIL_THRESHOLD` failures within `BREAKER_WINDOW_S`, even if
successful calls occur between failures. While open it rejects primary calls and
permits one half-open probe after `BREAKER_RESET_S`; only that successful probe
clears the failure window and closes the breaker. The
`llm_circuit_breaker_state` gauge reports 0 (closed), 1 (open), or 2 (half-open) for
the provider label.

For local fault-injection demos, set `STUB_MODE=error` to return HTTP 503 or
`STUB_MODE=slow` with `STUB_SLOW_DELAY` longer than `LLM_TIMEOUT_S` to trigger a
timeout. Restore `STUB_MODE=normal` for ordinary runs. The default delay is
`STUB_DELAY`.

`TOOL_TIMEOUT_S` is the configured timeout for tools that perform remote calls; each
tool integration must apply it when creating its own HTTP client. The run-level
timeout remains the outer wall-clock limit.
