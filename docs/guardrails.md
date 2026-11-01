# Guardrails (L4, issue #32)

All rules live in `libs/common/triage_common/guardrails.py`, as named regexes, and
each one has its own test in `tests/test_guardrails.py`.

## Where each check runs

| Stage | Where | Fields | Hit | Response |
|---|---|---|---|---|
| Input | API `POST /v1/claims`, before queueing | `statement`, `stated_cause` | toxicity, or injection in `stated_cause` | Run created as `escalated` / `GUARDRAIL_BLOCKED`, never queued, no model call |
| Input | worker `intake` node (defence in depth) | same | injection in `statement` | Narrative **withheld**: intake extracts from structured form fields only; run continues |
| Input | worker `intake` node | same | any BLOCK hit | Escalate `GUARDRAIL_BLOCKED` before any model call |
| Output | worker `finalize`, after the verifier | `route`, `summary`, `entitlements`, `amount` | route not in {pay, request evidence, decline}; authorisation wording; toxicity; amount not from `compute_compensation` | Draft replaced by `GUARDRAIL_BLOCKED` escalation |

Why `statement` is withheld but `stated_cause` is blocked: the narrative is only a
source for intake, and the form fields are canonical anyway, so dropping it loses
nothing. `stated_cause` feeds research, the extraordinary-circumstances checklist and
the verifier's CAR/CoC comparison, so a poisoned cause cannot simply be removed.

Escalations are hand-offs, not determinations, so the output filter screens only the
three determination routes.

## Rules

- Injection: `ignore_instructions`, `role_override`, `prompt_markup`, `prompt_exfiltration`,
  `decision_instruction`, `tool_invocation`.
- Toxicity: `threat`, `abuse` (including common Hindi abuse). Frustration, "the airline
  ignored the rules" and "bomb threat" as a cause all pass (tested).
- Output: `route_not_allowed`, `payment_decision`, `amount_not_computed`,
  `amount_on_non_payment_route`, `amount_in_text_not_computed`, plus toxicity.

Text is NFKC-normalised, stripped of zero-width/bidi characters, whitespace-collapsed and
case-folded first, so full-width letters and `ig\u200bnore` still match.

## Evidence in the trace (demo item 7)

Every hit is logged as a `guardrail` step: `stage`, `blocked`, `withheld`, and per hit
`field`, `rule`, `kind`, `action`, `excerpt`. API blocks add `service: "api"` and count in
the Prometheus metric `guardrail_blocks_total{stage,kind,rule}`.

```bash
curl -s localhost:8000/v1/runs/$RUN/trace | jq '.steps[] | select(.step=="guardrail")'
```

Scenarios: `s23` (statement injection, withheld, still declines on form facts),
`s30` (injection in `stated_cause`, blocked), `s31` (threat in statement, blocked).
