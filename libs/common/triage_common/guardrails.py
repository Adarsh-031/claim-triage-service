"""Input and output guardrails (L4): one module, one list of named rules.

Input (API before queueing, and again in the worker's intake node):
  * prompt injection in ``statement``   -> WITHHOLD: the narrative never reaches a model;
    the run continues on the structured form fields only.
  * prompt injection in ``stated_cause`` -> BLOCK: that field feeds research, the
    compensation checklist and the verifier, so it cannot simply be dropped.
  * toxicity (threats, abuse) in either -> BLOCK.
Output (finalize, before anything is published):
  * route outside the allowed set, authorisation language, toxicity, or an amount
    that ``compute_compensation`` did not produce -> BLOCK.

A BLOCK becomes an ESCALATE result with ``GUARDRAIL_BLOCKED`` and the rule names in
the run trace. Rules are deliberately simple regexes: they are cheap, explainable in
the viva, and each one has its own test in ``tests/test_guardrails.py``.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import asdict, dataclass
from enum import StrEnum

from .contracts import Route


class Action(StrEnum):
    WITHHOLD = "withhold"
    BLOCK = "block"


@dataclass(frozen=True)
class Hit:
    stage: str  # "input" or "output"
    field: str
    rule: str
    kind: str  # "injection", "toxicity", "authorisation", "route", "amount"
    action: Action
    excerpt: str = ""

    def as_dict(self) -> dict:
        return {**asdict(self), "action": self.action.value}


# --- Normalisation -------------------------------------------------------------------

_ZERO_WIDTH = re.compile("[\u200b-\u200f\u202a-\u202e\u2060-\u2064\ufeff]")


def normalise(text: str) -> str:
    """NFKC, strip zero-width/bidi characters, collapse whitespace, casefold.

    Defeats the cheap evasions: full-width letters, 'ig\u200bnore', odd spacing, case.
    """
    text = unicodedata.normalize("NFKC", text or "")
    text = _ZERO_WIDTH.sub("", text)
    return re.sub(r"\s+", " ", text).strip().casefold()


# --- Rules ---------------------------------------------------------------------------

INJECTION_RULES: dict[str, re.Pattern[str]] = {
    "ignore_instructions": re.compile(
        r"\b(?:ignore|disregard|forget|override|bypass)\b(?:\W+\w+){0,3}?\W+"
        r"(?:instructions?|rules?|prompts?|polic(?:y|ies)|guidelines?|regulations?)\b"),
    "role_override": re.compile(
        r"\byou are now\b|\bact as (?:an?|the)\b|\bpretend (?:to be|you are)\b|"
        r"\bnew instructions?\b|\bdeveloper mode\b|\bjailbreak\b|\bdo anything now\b"),
    "prompt_markup": re.compile(
        r"<\|?(?:im_start|im_end|system|assistant)\|?>|\[/?(?:inst|system)\]|^#{2,}\s*system\b|"
        r"\bsystem prompt\b|\bsystem:\s"),
    "prompt_exfiltration": re.compile(
        r"\b(?:reveal|print|show|repeat|output)\b(?:\W+\w+){0,3}?\W+"
        r"(?:system prompt|prompt|instructions|api key|secret)s?\b"),
    "decision_instruction": re.compile(
        r"\b(?:approve|authori[sz]e|grant|release)\b(?:\W+\w+){0,3}?\W+"
        r"(?:claim|payment|compensation|refund|payout)\b"
        r"|\b\d{3,}\s?%\s*(?:compensation|refund|payout)\b"
        r"|\b(?:set|change|make)\b(?:\W+\w+){0,2}?\W+(?:amount|compensation|route)\b(?:\W+\w+){0,2}?\W+(?:to|=)\b"),
    "tool_invocation": re.compile(r"\b(?:compute_compensation|search_clauses|get_clause|tool_call)\b"),
}

TOXICITY_RULES: dict[str, re.Pattern[str]] = {
    "threat": re.compile(
        r"\b(?:i(?:'ll| will| am going to| gonna)|we(?:'ll| will))\s+(?:kill|hurt|attack|beat|stab|shoot|burn)\b"
        r"|\b(?:kill|hurt|shoot|stab) (?:you|your staff|the staff|everyone)\b"
        r"|\byou(?:'ll| will) (?:regret|pay for) (?:this|it)\b"),
    "abuse": re.compile(
        r"\b(?:fuck\w*|motherfuck\w*|bitch\w*|bastards?|assholes?|dickheads?|"
        r"chutiy\w*|madarchod\w*|bhenchod\w*|behenchod\w*)\b"),
}

# Outputs never claim a payment decision; a claims officer always approves.
AUTHORISATION_RULES: dict[str, re.Pattern[str]] = {
    "payment_decision": re.compile(
        r"payment (?:is |has been )?(?:approved|authori[sz]ed|released|processed)"
        r"|(?:approve|authori[sz]e|release)[sd]? (?:the |this |your )?(?:payment|claim|compensation)"
        r"|(?:has|have) been paid|will be paid|we (?:have )?paid|we will pay|refund (?:is |has been )?issued"
        r"|\bclaim (?:is |has been )?approved\b|\byou are entitled to receive\b"),
}

# Determinations the system may publish. ESCALATE is a hand-off, not a determination,
# so finalize screens only these routes.
ALLOWED_ROUTES = frozenset({Route.PAY, Route.REQUEST_EVIDENCE, Route.DECLINE})
INPUT_FIELDS = ("statement", "stated_cause")


def _scan(text: str, rules: dict[str, re.Pattern[str]]) -> list[tuple[str, str]]:
    found = []
    for name, pattern in rules.items():
        if match := pattern.search(text):
            found.append((name, match.group(0)[:80]))
    return found


# --- Input ---------------------------------------------------------------------------

def screen_input(claim: dict) -> list[Hit]:
    """Screen every free-text field that can reach a model."""
    hits: list[Hit] = []
    for field in INPUT_FIELDS:
        value = claim.get(field)
        if not isinstance(value, str) or not value.strip():
            continue
        text = normalise(value)
        injection_action = Action.WITHHOLD if field == "statement" else Action.BLOCK
        hits += [Hit("input", field, rule, "injection", injection_action, excerpt)
                 for rule, excerpt in _scan(text, INJECTION_RULES)]
        hits += [Hit("input", field, rule, "toxicity", Action.BLOCK, excerpt)
                 for rule, excerpt in _scan(text, TOXICITY_RULES)]
    return hits


def blocked(hits: list[Hit]) -> bool:
    return any(h.action is Action.BLOCK for h in hits)


def withheld_fields(hits: list[Hit]) -> set[str]:
    return {h.field for h in hits if h.action is Action.WITHHOLD}


def block_summary(hits: list[Hit]) -> str:
    rules = sorted({f"{h.kind}:{h.rule} in {h.field}" for h in hits if h.action is Action.BLOCK})
    return ("Guardrail blocked this claim before any model saw it (" + ", ".join(rules) +
            "). Routed to a human officer.") if hits and hits[0].stage == "input" else (
            "Guardrail blocked the drafted recommendation (" + ", ".join(rules) +
            "). Routed to a human officer.")


# --- Output --------------------------------------------------------------------------

def screen_output(draft: dict, computed_amount: int | None) -> list[Hit]:
    """Check a non-escalation draft before publication.

    ``computed_amount`` is the amount the deterministic calculator produced for this
    run (``None`` when it produced none). PAY and DECLINE may only carry that amount;
    every other route carries none.
    """
    hits: list[Hit] = []
    route = draft.get("route")
    try:
        route = Route(route)
    except ValueError:
        hits.append(Hit("output", "route", "route_not_allowed", "route", Action.BLOCK, str(route)[:80]))
    else:
        if route not in ALLOWED_ROUTES:
            hits.append(Hit("output", "route", "route_not_allowed", "route", Action.BLOCK, route.value))
    text = normalise(" ".join([str(draft.get("summary") or ""), *map(str, draft.get("entitlements") or [])]))
    hits += [Hit("output", "summary", rule, "authorisation", Action.BLOCK, excerpt)
             for rule, excerpt in _scan(text, AUTHORISATION_RULES)]
    hits += [Hit("output", "summary", rule, "toxicity", Action.BLOCK, excerpt)
             for rule, excerpt in _scan(text, TOXICITY_RULES)]
    amount = draft.get("amount")
    if route in {Route.PAY, Route.DECLINE}:
        if amount != computed_amount:
            hits.append(Hit("output", "amount", "amount_not_computed", "amount", Action.BLOCK,
                            f"draft {amount!r} != computed {computed_amount!r}"))
    elif amount is not None:
        hits.append(Hit("output", "amount", "amount_on_non_payment_route", "amount", Action.BLOCK, repr(amount)))
    # Any rupee figure in the prose must be the computed one.
    for figure in re.findall(r"(?:inr|rs\.?|₹)\s?([\d,]+)", text):
        value = int(figure.replace(",", ""))
        if value != computed_amount:
            hits.append(Hit("output", "summary", "amount_in_text_not_computed", "amount", Action.BLOCK,
                            f"text {value} != computed {computed_amount!r}"))
    return hits
