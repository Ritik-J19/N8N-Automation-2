from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from channel_factory.config import load_profile
from channel_factory.history.store import HistoryStore
from channel_factory.services.llm import GeminiAdapter, MockLLM, draft_with_dedupe, repair_json, run_safety_pass


SAMPLE = Path(__file__).resolve().parents[1] / "samples" / "history_script.json"


def sample() -> dict:
    return json.loads(SAMPLE.read_text(encoding="utf-8"))


def test_google_genai_sdk_imports() -> None:
    from google import genai

    assert genai.Client is not None


def test_json_repair_handles_fence_and_trailing_comma() -> None:
    assert repair_json('```json\n{"ok": true,}\n```') == {"ok": True}


def test_mock_llm_draft_and_safety() -> None:
    llm = MockLLM(sample())
    topic = {"id": "hist-antikythera", "title": "Antikythera"}
    drafted, report = draft_with_dedupe(llm, load_profile("history_en"), topic, [])
    checked = run_safety_pass(llm, load_profile("history_en"), drafted)
    assert not report.rejected
    assert checked["safety_flags"] == []
    assert len(llm.calls) == 2


def test_gemini_retry_budget_counts_each_network_attempt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class TransientError(RuntimeError):
        status_code = 503

    class Models:
        def __init__(self) -> None:
            self.calls = 0

        def generate_content(self, **_: object) -> object:
            self.calls += 1
            if self.calls == 1:
                raise TransientError("temporarily unavailable")
            return type("Response", (), {"text": '{"ok": true}'})()

    adapter = GeminiAdapter.__new__(GeminiAdapter)
    adapter.store = HistoryStore(tmp_path / "retry-budget.sqlite3")
    adapter.daily_limit = 4
    adapter.max_attempts = 3
    adapter.model = "configured-by-test"
    models = Models()
    adapter.client = type("Client", (), {"models": models})()
    monkeypatch.setattr("channel_factory.services.llm.time.sleep", lambda _: None)

    assert adapter.generate_json("return JSON") == '{"ok": true}'
    with adapter.store.connection() as connection:
        count = connection.execute("SELECT call_count FROM llm_calls").fetchone()[0]
    assert count == 2


@pytest.mark.live
@pytest.mark.skipif(
    not (os.getenv("GEMINI_API_KEY") and os.getenv("GEMINI_MODEL")),
    reason="GEMINI_API_KEY and GEMINI_MODEL are not set; live Gemini test is opt-in",
)
def test_gemini_live_tiny_request(tmp_path: Path) -> None:
    adapter = GeminiAdapter(HistoryStore(tmp_path / "live.sqlite3"), daily_limit=2)
    response = repair_json(adapter.generate_json('Return exactly {"ok": true} as JSON.'))
    assert response.get("ok") is True

