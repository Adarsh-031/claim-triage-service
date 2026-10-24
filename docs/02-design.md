# 02 - System design (draft)

Architecture: see `img/architecture.png`. Web client wireframe: `img/web_client_wireframe.png`.

## To decide / document
- Agent topology: classifier, retrieval/reasoning agent, verifier; loop limits.
- Tool contracts: search_corpus, get_clause, get_references, get_referrers, get_claim_field, compute_compensation.
- Chunking + clause graph schema.
- Precedence: CAR = floor; airline doc may add, never reduce; conflicts escalate.
- Fallback path: primary model -> fallback model -> escalate PROVIDER_DEGRADED.
- Rejected alternatives and why.
