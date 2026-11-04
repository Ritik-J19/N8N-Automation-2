from __future__ import annotations

from copy import deepcopy
import json
import logging
import os
import random
import re
import time
from typing import Any, Protocol

from channel_factory.config import Profile, load_prompt
from channel_factory.history.dedupe import DuplicateReport, check_duplicate
from channel_factory.history.store import HistoryStore


LOGGER = logging.getLogger(__name__)


class LLMError(RuntimeError):
    pass


class DuplicateDraftError(LLMError):
    pass


class LLM(Protocol):
    def generate_json(self, prompt: str) -> str:
        """Return a JSON document as text."""


def repair_json(text: str) -> dict[str, Any]:
    candidate = text.strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)\s*```", candidate, re.DOTALL | re.IGNORECASE)
    if fenced:
        candidate = fenced.group(1)
    start = candidate.find("{")
    end = candidate.rfind("}")
    if start < 0 or end < start:
        raise LLMError("LLM response did not contain a JSON object")
    candidate = re.sub(r",\s*([}\]])", r"\1", candidate[start : end + 1])
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise LLMError(f"LLM returned invalid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise LLMError("LLM JSON response must be an object")
    return parsed


class MockLLM:
    """Deterministic offline adapter used by tests and the demo."""

    def __init__(self, sample_script: dict[str, Any]) -> None:
        self.sample_script = deepcopy(sample_script)
        self.calls: list[str] = []

    def generate_json(self, prompt: str) -> str:
        self.calls.append(prompt)
        if "TASK: SAFETY" in prompt:
            return json.dumps({"safety_flags": [], "claims_to_verify": self.sample_script.get("claims_to_verify", [])})
        match = re.search(r"TOPIC_JSON:\s*(\{.*?\})\s*RECENT_JSON:", prompt, re.DOTALL)
        script = deepcopy(self.sample_script)
        if match:
            topic = json.loads(match.group(1))
            script["topic_id"] = topic["id"]
        return json.dumps(script)


class GeminiAdapter:
    """Official Google Gen AI SDK adapter with daily limits and retry backoff."""

    def __init__(
        self,
        store: HistoryStore,
        *,
        daily_limit: int = 20,
        max_attempts: int = 4,
        timeout_seconds: float = 45,
    ) -> None:
        api_key = os.getenv("GEMINI_API_KEY")
        model = os.getenv("GEMINI_MODEL")
        if not api_key:
            raise LLMError("GEMINI_API_KEY is not set")
        if not model:
            raise LLMError("GEMINI_MODEL is not set; model names are never hardcoded")
        try:
            from google import genai
            from google.genai import types
        except ImportError as exc:
            raise LLMError("Install the official google-genai package") from exc
        self.client = genai.Client(
            api_key=api_key,
            http_options=types.HttpOptions(timeout=int(timeout_seconds * 1000)),
        )
        self.model = model
        self.store = store
        self.daily_limit = daily_limit
        self.max_attempts = max_attempts

    @staticmethod
    def _is_retryable(exc: Exception) -> bool:
        status = getattr(exc, "status_code", None) or getattr(exc, "code", None)
        text = str(exc)
        return (
            status in {408, 429, 500, 502, 503, 504}
            or isinstance(exc, TimeoutError)
            or any(marker in text for marker in ("429", "RESOURCE_EXHAUSTED", "UNAVAILABLE", "DEADLINE_EXCEEDED"))
        )

    def generate_json(self, prompt: str) -> str:
        for attempt in range(self.max_attempts):
            # Count actual network attempts, not logical generations. Retries consume
            # provider quota too and must not bypass the configured daily ceiling.
            self.store.increment_daily_llm_calls(self.daily_limit)
            try:
                response = self.client.models.generate_content(
                    model=self.model,
                    contents=prompt,
                    config={"response_mime_type": "application/json"},
                )
                if not response.text:
                    raise LLMError("Gemini returned an empty response")
                return response.text
            except Exception as exc:
                final_attempt = attempt == self.max_attempts - 1
                if not self._is_retryable(exc) or final_attempt:
                    raise LLMError(f"Gemini request failed: {exc}") from exc
                delay = min(30.0, 2**attempt + random.random())
                LOGGER.warning("Gemini request was transiently unavailable; retrying in %.1f seconds", delay)
                time.sleep(delay)
        raise AssertionError("retry loop exited unexpectedly")


def _recent_prompt_context(recent: list[dict[str, Any]]) -> list[dict[str, str]]:
    return [{"title": item["title"], "summary": item["summary"]} for item in recent[:30]]


def draft_with_dedupe(
    llm: LLM,
    profile: Profile,
    topic: dict[str, Any],
    recent: list[dict[str, Any]],
    *,
    rejection_reason: str | None = None,
    max_drafts: int = 3,
) -> tuple[dict[str, Any], DuplicateReport]:
    feedback = rejection_reason or "none"
    last_report: DuplicateReport | None = None
    for attempt in range(1, max_drafts + 1):
        prompt = "\n\n".join(
            [
                "TASK: DRAFT",
                load_prompt(profile, "idea"),
                load_prompt(profile, "script"),
                f"TOPIC_JSON: {json.dumps(topic)}",
                f"RECENT_JSON: {json.dumps(_recent_prompt_context(recent))}",
                "Do not resemble any recent title, summary, narration, lesson, or character combination.",
                f"REJECTION_FEEDBACK: {feedback}",
                f"ATTEMPT: {attempt}",
            ]
        )
        script = repair_json(llm.generate_json(prompt))
        last_report = check_duplicate(script, recent, profile)
        if not last_report.rejected:
            return script, last_report
        feedback = (
            "Regenerate with a clearly different title, narration, lesson/character combination, and structure. "
            f"Similarity scores were title={last_report.title_score:.3f}, "
            f"narration={last_report.narration_score:.3f}, "
            f"lesson_character={last_report.lesson_character_score:.3f}."
        )
    raise DuplicateDraftError(f"All {max_drafts} drafts exceeded duplicate thresholds: {last_report}")


def run_safety_pass(llm: LLM, profile: Profile, script: dict[str, Any]) -> dict[str, Any]:
    prompt = "\n\n".join(
        ["TASK: SAFETY", load_prompt(profile, "safety"), f"SCRIPT_JSON: {json.dumps(script)}"]
    )
    review = repair_json(llm.generate_json(prompt))
    updated = deepcopy(script)
    updated["safety_flags"] = list(review.get("safety_flags", []))
    if "claims_to_verify" in updated:
        combined = [*updated.get("claims_to_verify", []), *review.get("claims_to_verify", [])]
        updated["claims_to_verify"] = list(dict.fromkeys(map(str, combined)))
    return updated

