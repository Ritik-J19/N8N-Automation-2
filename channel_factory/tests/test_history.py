from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
import json
from pathlib import Path
import sqlite3

import pytest

from channel_factory.config import load_profile
from channel_factory.core.state import InvalidTransition, PipelineStatus
from channel_factory.history.dedupe import check_duplicate, tfidf_cosine
from channel_factory.history.store import HistoryStore, StoreError


SAMPLE = Path(__file__).resolve().parents[1] / "samples" / "history_script.json"


def script() -> dict:
    return json.loads(SAMPLE.read_text(encoding="utf-8"))


def test_store_tracks_topic_state_and_weekly_exports(tmp_path: Path) -> None:
    store = HistoryStore(tmp_path / "history.sqlite3")
    video_id = store.create_draft(script())
    with pytest.raises(InvalidTransition):
        store.transition(video_id, PipelineStatus.RENDERED)
    for status in (
        PipelineStatus.SAFETY_CHECKED,
        PipelineStatus.AWAITING_REVIEW,
        PipelineStatus.APPROVED,
        PipelineStatus.NARRATED,
        PipelineStatus.RENDERED,
        PipelineStatus.EXPORTING,
        PipelineStatus.EXPORTED,
    ):
        store.transition(video_id, status)
    assert store.get_video(video_id)["approved_at"]
    assert store.count_weekly_exports("history_en", datetime.now(UTC)) == 1
    events = store.list_events(video_id)
    assert events[0]["to_status"] == PipelineStatus.DRAFTED.value
    assert events[-1]["to_status"] == PipelineStatus.EXPORTED.value


def test_duplicate_detection_rejects_near_copy() -> None:
    current = script()
    past = {
        "title": current["title"],
        "narration": " ".join(scene["narration"] for scene in current["scenes"]),
        "lesson": "",
        "characters_json": "[]",
        "structure_fingerprint": HistoryStore.structure_fingerprint(current),
    }
    report = check_duplicate(current, [past], load_profile("history_en"))
    assert report.rejected
    assert report.title_score == pytest.approx(1.0)
    assert tfidf_cosine("clockwork sky", "clockwork sky") == pytest.approx(1.0)


def test_next_unused_topic(tmp_path: Path) -> None:
    store = HistoryStore(tmp_path / "history.sqlite3")
    first = script()
    store.create_draft(first)
    topics = [
        {"id": first["topic_id"], "title": "used"},
        {"id": "new-topic", "title": "unused"},
    ]
    assert store.next_unused_topic("history_en", topics)["id"] == "new-topic"


def test_topic_reservations_prevent_two_workers_selecting_same_topic(tmp_path: Path) -> None:
    first_store = HistoryStore(tmp_path / "history.sqlite3")
    second_store = HistoryStore(tmp_path / "history.sqlite3")
    topics = [{"id": "one", "title": "One"}, {"id": "two", "title": "Two"}]
    first, first_token = first_store.reserve_next_topic("history_en", topics)
    second, _ = second_store.reserve_next_topic("history_en", topics)
    assert first["id"] == "one"
    assert second["id"] == "two"
    first_store.release_topic_reservation(first_token)
    assert first_store.next_unused_topic("history_en", topics)["id"] == "one"


def test_foreign_keys_are_enabled_on_every_connection(tmp_path: Path) -> None:
    store = HistoryStore(tmp_path / "history.sqlite3")
    scene = script()["scenes"][0]
    with pytest.raises(sqlite3.IntegrityError):
        store.record_asset("missing-video", scene, tmp_path / "image.png")


def test_export_reservation_is_serialized(tmp_path: Path) -> None:
    store = HistoryStore(tmp_path / "history.sqlite3")
    video_id = store.create_draft(script())
    for status in (
        PipelineStatus.SAFETY_CHECKED,
        PipelineStatus.AWAITING_REVIEW,
        PipelineStatus.APPROVED,
        PipelineStatus.NARRATED,
        PipelineStatus.RENDERED,
    ):
        store.transition(video_id, status)
    status, acquired = store.begin_export(video_id, weekly_cap=3)
    assert (status, acquired) == (PipelineStatus.EXPORTING, True)
    status, acquired = store.begin_export(video_id, weekly_cap=3)
    assert (status, acquired) == (PipelineStatus.EXPORTING, False)


def test_approval_freezes_script_identity_and_render_freezes_provenance(tmp_path: Path) -> None:
    store = HistoryStore(tmp_path / "history.sqlite3")
    original = script()
    video_id = store.create_draft(original)

    changed_identity = deepcopy(original)
    changed_identity["topic_id"] = "different-topic"
    with pytest.raises(StoreError, match="cannot change.*topic"):
        store.update_script(video_id, changed_identity)

    for state in (
        PipelineStatus.SAFETY_CHECKED,
        PipelineStatus.AWAITING_REVIEW,
        PipelineStatus.APPROVED,
    ):
        store.transition(video_id, state)
    edited = deepcopy(original)
    edited["title"] = "Changed after approval"
    with pytest.raises(StoreError, match="after human approval"):
        store.update_script(video_id, edited)

    store.transition(video_id, PipelineStatus.NARRATED)
    store.transition(video_id, PipelineStatus.RENDERED)
    with pytest.raises(StoreError, match="immutable after rendering"):
        store.record_asset(video_id, original["scenes"][0], tmp_path / "image.png")

