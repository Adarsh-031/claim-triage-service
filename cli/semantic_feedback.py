"""Optional TruLens semantic feedback for the evaluation CLI."""
from __future__ import annotations

import math
import os
from statistics import mean

METRICS = ("groundedness", "context_relevance", "answer_relevance", "hallucination_rate")


class SemanticFeedback:
    def __init__(self, provider, model: str):
        self.provider = provider
        self.model = model

    @classmethod
    def from_environment(cls):
        model = os.environ.get("EVAL_JUDGE_MODEL")
        key = os.environ.get("EVAL_JUDGE_API_KEY")
        if not model or not key:
            raise ValueError("Set EVAL_JUDGE_MODEL and EVAL_JUDGE_API_KEY to run TruLens feedback")
        from trulens.providers.openai import OpenAI
        provider = OpenAI(model_engine=model, api_key=key,
                          base_url=os.environ.get("EVAL_JUDGE_BASE_URL", "https://api.openai.com/v1"),
                          timeout=30, max_retries=1)
        return cls(provider, model)

    @staticmethod
    def _score(value):
        score = value[0] if isinstance(value, tuple) else value
        if isinstance(score, bool) or not isinstance(score, (float, int)) or not math.isfinite(score):
            raise ValueError("Judge returned a non-finite score")
        if not 0 <= score <= 1:
            raise ValueError("Judge returned a score outside [0, 1]")
        return float(score)

    def evaluate(self, question: str, answer: str, contexts: list[str]) -> dict:
        if not contexts or not answer.strip():
            return {**dict.fromkeys(METRICS), "status": "unavailable", "reason": "No source context or answer"}
        try:
            grounded = self._score(self.provider.groundedness_measure_with_cot_reasons(
                source="\n\n".join(contexts), statement=answer))
            relevance = mean(self._score(self.provider.context_relevance_with_cot_reasons(
                question=question, context=context)) for context in contexts)
            answer_relevance = self._score(self.provider.relevance_with_cot_reasons(prompt=question, response=answer))
            return {"groundedness": grounded, "context_relevance": relevance, "answer_relevance": answer_relevance,
                    "hallucination_rate": 1 - grounded, "status": "measured", "judge_model": self.model}
        except Exception as exc:
            # Provider exception messages may contain credentials or request payloads.
            return {**dict.fromkeys(METRICS), "status": "error", "error": type(exc).__name__}
