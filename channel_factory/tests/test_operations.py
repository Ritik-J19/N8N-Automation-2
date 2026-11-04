from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sqlite3

import pytest
from starlette.testclient import TestClient

from channel_factory.history.migrations import LATEST_SCHEMA_VERSION
from channel_factory.history.store import HistoryStore, StoreError
from channel_factory.web import create_app
from channel_factory.web.security import COOKIE_NAME


SAMPLE = Path(__file__).resolve().parents[1] / "samples" / "history_script.json"


def script() -> dict:
    return json.loads(SAMPLE.read_text(encoding="utf-8"))


def test_numbered_migrations_upgrade_an_existing_database(tmp_path: Path) -> None:
    database = tmp_path / "existing.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE legacy_marker(value TEXT)")
    store = HistoryStore(database)
    assert store.schema_version() == LATEST_SCHEMA_VERSION
    with store.connection() as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    assert {"schema_migrations", "workflow_events", "workflow_failures"} <= tables
    assert "legacy_marker" in tables


def test_concurrent_store_startup_applies_migrations_once(tmp_path: Path) -> None:
    database = tmp_path / "concurrent-startup.sqlite3"
    with ThreadPoolExecutor(max_workers=8) as executor:
        stores = list(executor.map(lambda _: HistoryStore(database), range(8)))
    assert {store.schema_version() for store in stores} == {LATEST_SCHEMA_VERSION}
    with stores[0].connection() as connection:
        versions = connection.execute(
            "SELECT version, COUNT(*) FROM schema_migrations GROUP BY version"
        ).fetchall()
    assert len(versions) == LATEST_SCHEMA_VERSION
    assert all(row[1] == 1 for row in versions)


def test_verified_backup_and_failure_lifecycle(tmp_path: Path) -> None:
    store = HistoryStore(tmp_path / "channel_factory.sqlite3")
    video_id = store.create_draft(script())
    failure_id = store.record_failure(video_id, "render", ValueError("missing image"))
    active = store.list_failures(video_id, active_only=True)
    assert active[0]["id"] == failure_id
    assert active[0]["error_type"] == "ValueError"
    assert store.resolve_failures(video_id, "render") == 1
    assert store.list_failures(video_id, active_only=True) == []

    backup = store.backup(tmp_path / "backups" / "snapshot.sqlite3")
    report = store.verify_backup(backup)
    assert report["integrity"] == "ok"
    assert report["schema_version"] == LATEST_SCHEMA_VERSION
    with pytest.raises(StoreError, match="already exists"):
        store.backup(backup)
    corrupt = tmp_path / "corrupt.sqlite3"
    corrupt.write_bytes(b"not a database")
    with pytest.raises(StoreError, match="Invalid SQLite backup"):
        store.verify_backup(corrupt)


def test_failed_backup_does_not_leave_a_visible_or_temporary_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = HistoryStore(tmp_path / "channel_factory.sqlite3")
    destination = tmp_path / "backups" / "snapshot.sqlite3"

    def reject(_: Path) -> dict:
        raise StoreError("simulated verification failure")

    monkeypatch.setattr(HistoryStore, "verify_backup", staticmethod(reject))
    with pytest.raises(StoreError, match="simulated verification failure"):
        store.backup(destination)
    assert not destination.exists()
    assert list(destination.parent.glob(".snapshot-*.tmp")) == []


def test_operations_console_authentication_csrf_and_governance(tmp_path: Path) -> None:
    token = "test-console-token-123456"
    app = create_app(tmp_path, token)
    video_id = app.state.pipeline.store.create_draft(script())

    with TestClient(app) as client:
        health = client.get("/api/health")
        assert health.status_code == 200
        assert health.headers["x-frame-options"] == "DENY"
        assert "frame-ancestors 'none'" in health.headers["content-security-policy"]
        assert "style-src-attr 'none'" in health.headers["content-security-policy"]
        assert client.get("/api/health", headers={"Host": "attacker.example"}).status_code == 400
        assert client.get("/api/summary").status_code == 401
        assert client.post("/api/session", json={"token": "incorrect-token-value"}).status_code == 401

        login = client.post("/api/session", json={"token": token})
        assert login.status_code == 200
        csrf = login.json()["csrf_token"]
        assert "HttpOnly" in login.headers["set-cookie"]
        assert "SameSite=strict" in login.headers["set-cookie"]

        summary = client.get("/api/summary")
        assert summary.json()["total_jobs"] == 1
        detail = client.get(f"/api/jobs/{video_id}")
        assert detail.json()["events"][0]["to_status"] == "drafted"
        assert "readiness" in detail.json()
        assert client.get("/api/jobs/not-a-uuid").status_code == 422
        assert client.get("/api/system").json()["database"] == "channel_factory.sqlite3"
        page = client.get("/api/jobs-page?sort_by=title&descending=false")
        assert page.json()["total"] == 1

        assert client.post("/api/backups").status_code == 403
        assert client.post(
            "/api/backups",
            headers={"X-CSRF-Token": csrf, "Origin": "http://attacker.example"},
        ).status_code == 403
        backup = client.post("/api/backups", headers={"X-CSRF-Token": csrf})
        assert backup.status_code == 201
        assert backup.json()["valid"] is True
        assert "path" not in backup.json()
        assert client.post("/api/backups", headers={"X-CSRF-Token": csrf}).status_code == 429

        # Approval intentionally has no web route; it remains an interactive terminal decision.
        assert client.post(
            f"/api/jobs/{video_id}/approve", headers={"X-CSRF-Token": csrf}
        ).status_code == 404

        replay_cookie = client.cookies.get(COOKIE_NAME)
        assert client.delete("/api/session", headers={"X-CSRF-Token": csrf}).status_code == 200
        client.cookies.set(COOKIE_NAME, replay_cookie)
        replay = client.get("/api/session")
        assert replay.status_code == 200
        assert replay.json()["authenticated"] is False


def test_console_rejects_weak_access_token(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="at least 24"):
        create_app(tmp_path, "too-short")
    with pytest.raises(ValueError, match="varied characters"):
        create_app(tmp_path, "a" * 32)


def test_console_throttles_repeated_login_failures(tmp_path: Path) -> None:
    app = create_app(tmp_path, "strong-test-console-token-123456")
    with TestClient(app) as client:
        for _ in range(5):
            assert client.post("/api/session", json={"token": "wrong"}).status_code == 401
        limited = client.post("/api/session", json={"token": "wrong"})
        assert limited.status_code == 429
        assert int(limited.headers["Retry-After"]) > 0
        events = app.state.pipeline.store.list_system_events()
        assert events[0]["event_type"] == "login_throttled"


def test_operation_leases_serialize_work_across_store_instances(tmp_path: Path) -> None:
    first_store = HistoryStore(tmp_path / "leases.sqlite3")
    second_store = HistoryStore(tmp_path / "leases.sqlite3")
    with first_store.operation_lease("recover:job") as first:
        assert first is True
        with second_store.operation_lease("recover:job") as duplicate:
            assert duplicate is False
    with second_store.operation_lease("recover:job") as retry:
        assert retry is True


def test_summary_uses_database_aggregates_instead_of_a_500_job_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = create_app(tmp_path, "strong-test-console-token-123456")
    app.state.pipeline.store.create_draft(script())
    original = app.state.pipeline.store.list_videos
    requested_limits: list[int] = []

    def tracked_list_videos(*, status: str | None = None, limit: int = 50) -> list[dict]:
        requested_limits.append(limit)
        return original(status=status, limit=limit)

    monkeypatch.setattr(app.state.pipeline.store, "list_videos", tracked_list_videos)
    with TestClient(app) as client:
        login = client.post("/api/session", json={"token": "strong-test-console-token-123456"})
        assert login.status_code == 200
        summary = client.get("/api/summary")
        assert summary.status_code == 200
        assert summary.json()["total_jobs"] == 1
    assert requested_limits == [8]
