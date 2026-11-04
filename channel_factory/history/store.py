from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
import json
from pathlib import Path
import sqlite3
import tempfile
from typing import Any, Iterator
from uuid import uuid4

from channel_factory.core.state import PipelineStatus, require_transition
from channel_factory.history.migrations import LATEST_SCHEMA_VERSION, apply_migrations


class StoreError(RuntimeError):
    pass


class HistoryStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=10000")
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self.connection() as db:
            apply_migrations(db)

    def schema_version(self) -> int:
        with self.connection() as db:
            row = db.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
        return int(row[0] or 0)

    def backup(self, destination: Path) -> Path:
        with self.operation_lease("backup:create", ttl=timedelta(hours=1)) as acquired:
            if not acquired:
                raise StoreError("Another database backup is already running")
            return self._create_backup(destination)

    def _create_backup(self, destination: Path) -> Path:
        if destination.exists():
            raise StoreError(f"Backup already exists: {destination}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary_handle = tempfile.NamedTemporaryFile(
            prefix=f".{destination.stem}-",
            suffix=".tmp",
            dir=destination.parent,
            delete=False,
        )
        temporary = Path(temporary_handle.name)
        temporary_handle.close()
        try:
            with self.connection() as source:
                source.execute("PRAGMA wal_checkpoint(PASSIVE)")
                target = sqlite3.connect(temporary)
                try:
                    source.backup(target)
                    # Leases describe in-flight work in the source process and
                    # must never become active work after a future restore.
                    target.execute("DELETE FROM operation_leases")
                    target.commit()
                finally:
                    target.close()
            self.verify_backup(temporary)
            temporary.replace(destination)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
        return destination

    @staticmethod
    def verify_backup(path: Path) -> dict[str, Any]:
        if not path.is_file():
            raise StoreError(f"Backup not found: {path}")
        try:
            connection = sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True)
            result = str(connection.execute("PRAGMA quick_check").fetchone()[0])
            row = connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
            version = int(row[0] or 0)
        except sqlite3.Error as exc:
            raise StoreError(f"Invalid SQLite backup: {path}: {exc}") from exc
        finally:
            if "connection" in locals():
                connection.close()
        if result != "ok":
            raise StoreError(f"Backup integrity check failed: {result}")
        if version != LATEST_SCHEMA_VERSION:
            raise StoreError(
                f"Backup schema version is {version}; expected {LATEST_SCHEMA_VERSION}"
            )
        return {"path": str(path), "integrity": result, "schema_version": version}

    @staticmethod
    def _now() -> str:
        return datetime.now(UTC).isoformat()

    @staticmethod
    def _narration(script: dict[str, Any]) -> str:
        return "\n".join(str(scene.get("narration", "")) for scene in script.get("scenes", []))

    @staticmethod
    def _length_bucket(script: dict[str, Any]) -> str:
        duration = sum(float(scene.get("duration_sec", 0)) for scene in script.get("scenes", []))
        return "short" if duration <= 60 else "medium" if duration <= 180 else "long"

    @classmethod
    def structure_fingerprint(cls, script: dict[str, Any]) -> str:
        return ":".join(
            [str(len(script.get("scenes", []))), str(script.get("hook_type", "unspecified")), cls._length_bucket(script)]
        )

    def create_draft(
        self,
        script: dict[str, Any],
        key_facts: list[str] | None = None,
        topic_reservation: str | None = None,
    ) -> str:
        video_id = str(uuid4())
        now = self._now()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if topic_reservation is not None:
                reservation = db.execute(
                    """SELECT 1 FROM topic_reservations
                    WHERE channel=? AND topic_id=? AND reservation_token=?""",
                    (script["channel"], script["topic_id"], topic_reservation),
                ).fetchone()
                if reservation is None:
                    raise StoreError("Topic reservation is missing or no longer valid")
            db.execute(
                """INSERT INTO videos (
                    id, channel, topic_id, title, summary, lesson, characters_json,
                    key_facts_json, narration, script_json, status, hook_type,
                    length_bucket, structure_fingerprint, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    video_id,
                    script["channel"],
                    script["topic_id"],
                    script["title"],
                    script["summary"],
                    script.get("lesson"),
                    json.dumps(script.get("characters", [])),
                    json.dumps(key_facts or []),
                    self._narration(script),
                    json.dumps(script),
                    PipelineStatus.DRAFTED.value,
                    script.get("hook_type"),
                    self._length_bucket(script),
                    self.structure_fingerprint(script),
                    now,
                    now,
                ),
            )
            db.execute(
                "INSERT OR IGNORE INTO topic_usage(channel, topic_id, used_at) VALUES (?, ?, ?)",
                (script["channel"], script["topic_id"], now),
            )
            if topic_reservation is not None:
                db.execute(
                    "DELETE FROM topic_reservations WHERE reservation_token=?",
                    (topic_reservation,),
                )
            self._insert_event(db, video_id, None, PipelineStatus.DRAFTED, "draft created", now)
        return video_id

    @staticmethod
    def _insert_event(
        db: sqlite3.Connection,
        video_id: str,
        from_status: str | PipelineStatus | None,
        to_status: str | PipelineStatus,
        detail: str | None,
        created_at: str,
    ) -> None:
        source = from_status.value if isinstance(from_status, PipelineStatus) else from_status
        target = to_status.value if isinstance(to_status, PipelineStatus) else to_status
        db.execute(
            """INSERT INTO workflow_events(video_id, from_status, to_status, detail, created_at)
            VALUES (?, ?, ?, ?, ?)""",
            (video_id, source, target, detail, created_at),
        )

    @staticmethod
    def _video_in_connection(db: sqlite3.Connection, video_id: str) -> sqlite3.Row:
        row = db.execute("SELECT * FROM videos WHERE id = ?", (video_id,)).fetchone()
        if row is None:
            raise StoreError(f"Video not found: {video_id}")
        return row

    def get_video(self, video_id: str) -> dict[str, Any]:
        with self.connection() as db:
            row = db.execute("SELECT * FROM videos WHERE id = ?", (video_id,)).fetchone()
        if row is None:
            raise StoreError(f"Video not found: {video_id}")
        return dict(row)

    def update_script(self, video_id: str, script: dict[str, Any]) -> None:
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            video = self._video_in_connection(db, video_id)
            mutable_statuses = {
                PipelineStatus.DRAFTED,
                PipelineStatus.SAFETY_CHECKED,
                PipelineStatus.AWAITING_REVIEW,
                PipelineStatus.REJECTED,
            }
            if PipelineStatus(video["status"]) not in mutable_statuses:
                raise StoreError("A script cannot be changed after human approval")
            if script.get("channel") != video["channel"]:
                raise StoreError("A script update cannot change the video's channel")
            if script.get("topic_id") != video["topic_id"]:
                raise StoreError("A script update cannot change the video's topic")
            result = db.execute(
                """UPDATE videos SET title=?, summary=?, lesson=?, characters_json=?,
                narration=?, script_json=?, hook_type=?, length_bucket=?,
                structure_fingerprint=?, updated_at=? WHERE id=?""",
                (
                    script["title"], script["summary"], script.get("lesson"),
                    json.dumps(script.get("characters", [])), self._narration(script),
                    json.dumps(script), script.get("hook_type"), self._length_bucket(script),
                    self.structure_fingerprint(script), self._now(), video_id,
                ),
            )
            if result.rowcount != 1:
                raise StoreError(f"Video not found: {video_id}")
            timestamp = self._now()
            self._insert_event(
                db,
                video_id,
                video["status"],
                video["status"],
                "script updated",
                timestamp,
            )

    def transition(self, video_id: str, target: str | PipelineStatus, reason: str | None = None) -> None:
        target = PipelineStatus(target)
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            video = self._video_in_connection(db, video_id)
            source = PipelineStatus(video["status"])
            require_transition(source, target)
            now = self._now()
            approved_at = now if target is PipelineStatus.APPROVED else video["approved_at"]
            exported_at = now if target is PipelineStatus.EXPORTED else video["exported_at"]
            if target is PipelineStatus.REJECTED:
                rejection_reason = reason
            elif source is PipelineStatus.REJECTED and target is PipelineStatus.DRAFTED:
                rejection_reason = None
            else:
                rejection_reason = video["rejection_reason"]
            result = db.execute(
                """UPDATE videos SET status=?, rejection_reason=?, approved_at=?,
                exported_at=?, updated_at=? WHERE id=? AND status=?""",
                (target.value, rejection_reason, approved_at, exported_at, now, video_id, source.value),
            )
            if result.rowcount != 1:
                raise StoreError(f"Concurrent status change detected for {video_id}")
            self._insert_event(db, video_id, source, target, reason, now)

    def begin_export(
        self,
        video_id: str,
        weekly_cap: int,
        now: datetime | None = None,
    ) -> tuple[PipelineStatus, bool]:
        current = now or datetime.now(UTC)
        start = (current - timedelta(days=current.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            video = self._video_in_connection(db, video_id)
            status = PipelineStatus(video["status"])
            if status is PipelineStatus.EXPORTED:
                return status, False
            if status is PipelineStatus.EXPORTING:
                last_update = datetime.fromisoformat(video["updated_at"])
                if current - last_update < timedelta(minutes=15):
                    return status, False
                timestamp = self._now()
                db.execute("UPDATE videos SET updated_at=? WHERE id=?", (timestamp, video_id))
                self._insert_event(
                    db,
                    video_id,
                    PipelineStatus.EXPORTING,
                    PipelineStatus.EXPORTING,
                    "stale export reservation recovered",
                    timestamp,
                )
                return status, True
            if status is not PipelineStatus.RENDERED or not video["approved_at"]:
                raise StoreError("Only an approved and rendered video can be exported")
            count = db.execute(
                """SELECT COUNT(*) FROM videos WHERE channel=? AND (
                    (status=? AND exported_at>=?) OR (status=? AND updated_at>=?)
                )""",
                (
                    video["channel"], PipelineStatus.EXPORTED.value, start.isoformat(),
                    PipelineStatus.EXPORTING.value, start.isoformat(),
                ),
            ).fetchone()[0]
            if int(count) >= weekly_cap:
                raise StoreError(f"Weekly posting cap reached for {video['channel']}: {weekly_cap}")
            timestamp = self._now()
            result = db.execute(
                "UPDATE videos SET status=?, updated_at=? WHERE id=? AND status=?",
                (PipelineStatus.EXPORTING.value, timestamp, video_id, PipelineStatus.RENDERED.value),
            )
            if result.rowcount != 1:
                raise StoreError(f"Concurrent export reservation detected for {video_id}")
            self._insert_event(
                db, video_id, PipelineStatus.RENDERED, PipelineStatus.EXPORTING, "export reserved", timestamp
            )
        return PipelineStatus.EXPORTING, True

    def complete_export(self, video_id: str) -> None:
        self.transition(video_id, PipelineStatus.EXPORTED, reason="export package complete")

    def abort_export(self, video_id: str, reason: str) -> None:
        video = self.get_video(video_id)
        if video["status"] == PipelineStatus.EXPORTING.value:
            self.transition(video_id, PipelineStatus.RENDERED, reason=reason)

    def list_recent(self, channel: str, limit: int = 30, exclude_id: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM videos WHERE channel=?"
        params: list[Any] = [channel]
        if exclude_id:
            query += " AND id<>?"
            params.append(exclude_id)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        with self.connection() as db:
            return [dict(row) for row in db.execute(query, params).fetchall()]

    def list_videos(
        self,
        *,
        status: str | PipelineStatus | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        if limit < 1 or limit > 500:
            raise StoreError("limit must be between 1 and 500")
        query = "SELECT * FROM videos"
        params: list[Any] = []
        if status is not None:
            query += " WHERE status=?"
            params.append(PipelineStatus(status).value)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        with self.connection() as db:
            return [dict(row) for row in db.execute(query, params).fetchall()]

    def count_videos_by_status(self) -> dict[str, int]:
        with self.connection() as db:
            rows = db.execute(
                "SELECT status, COUNT(*) AS total FROM videos GROUP BY status"
            ).fetchall()
        return {str(row["status"]): int(row["total"]) for row in rows}

    def list_attention_videos(self, limit: int = 100) -> list[dict[str, Any]]:
        if limit < 1 or limit > 500:
            raise StoreError("limit must be between 1 and 500")
        with self.connection() as db:
            rows = db.execute(
                """SELECT * FROM videos
                WHERE status IN (?, ?)
                ORDER BY updated_at DESC, id ASC LIMIT ?""",
                (
                    PipelineStatus.AWAITING_REVIEW.value,
                    PipelineStatus.EXPORTING.value,
                    limit,
                ),
            ).fetchall()
        return [dict(row) for row in rows]

    def search_videos(
        self,
        *,
        query_text: str | None = None,
        status: str | PipelineStatus | None = None,
        channel: str | None = None,
        sort_by: str = "updated_at",
        descending: bool = True,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[list[dict[str, Any]], int]:
        if limit < 1 or limit > 200:
            raise StoreError("limit must be between 1 and 200")
        if offset < 0:
            raise StoreError("offset must be non-negative")
        if query_text is not None and len(query_text) > 200:
            raise StoreError("query text must be 200 characters or fewer")
        allowed_sort = {"title", "channel", "status", "created_at", "updated_at"}
        if sort_by not in allowed_sort:
            raise StoreError(f"Unsupported sort field: {sort_by}")
        clauses: list[str] = []
        params: list[Any] = []
        if query_text:
            escaped = query_text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            clauses.append(
                "(title LIKE ? ESCAPE '\\' OR id LIKE ? ESCAPE '\\' OR "
                "topic_id LIKE ? ESCAPE '\\' OR channel LIKE ? ESCAPE '\\')"
            )
            term = f"%{escaped}%"
            params.extend([term, term, term, term])
        if status:
            clauses.append("status=?")
            params.append(PipelineStatus(status).value)
        if channel:
            clauses.append("channel=?")
            params.append(channel)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        direction = "DESC" if descending else "ASC"
        with self.connection() as db:
            total = int(db.execute(f"SELECT COUNT(*) FROM videos{where}", params).fetchone()[0])
            rows = db.execute(
                f"SELECT * FROM videos{where} ORDER BY {sort_by} {direction}, id ASC LIMIT ? OFFSET ?",
                [*params, limit, offset],
            ).fetchall()
        return [dict(row) for row in rows], total

    def list_events(self, video_id: str) -> list[dict[str, Any]]:
        self.get_video(video_id)
        with self.connection() as db:
            rows = db.execute(
                "SELECT id, from_status, to_status, detail, created_at FROM workflow_events WHERE video_id=? ORDER BY id",
                (video_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_recent_events(self, limit: int = 20) -> list[dict[str, Any]]:
        if limit < 1 or limit > 100:
            raise StoreError("limit must be between 1 and 100")
        with self.connection() as db:
            rows = db.execute(
                """SELECT e.id, e.video_id, e.from_status, e.to_status, e.detail,
                e.created_at, v.title, v.channel
                FROM workflow_events AS e
                JOIN videos AS v ON v.id=e.video_id
                ORDER BY e.id DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def record_system_event(self, event_type: str, detail: str, severity: str = "info") -> int:
        if severity not in {"info", "warning", "error"}:
            raise StoreError(f"Unsupported system event severity: {severity}")
        with self.connection() as db:
            cursor = db.execute(
                """INSERT INTO system_events(event_type, severity, detail, created_at)
                VALUES (?, ?, ?, ?)""",
                (event_type[:80], severity, detail[:1000], self._now()),
            )
        return int(cursor.lastrowid)

    def list_system_events(self, limit: int = 20) -> list[dict[str, Any]]:
        if limit < 1 or limit > 100:
            raise StoreError("limit must be between 1 and 100")
        with self.connection() as db:
            rows = db.execute(
                """SELECT id, event_type, severity, detail, created_at
                FROM system_events ORDER BY id DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def record_failure(self, video_id: str, operation: str, error: BaseException) -> int:
        self.get_video(video_id)
        with self.connection() as db:
            cursor = db.execute(
                """INSERT INTO workflow_failures(
                    video_id, operation, error_type, message, created_at
                ) VALUES (?, ?, ?, ?, ?)""",
                (video_id, operation, type(error).__name__, str(error)[:4000], self._now()),
            )
        return int(cursor.lastrowid)

    def resolve_failures(self, video_id: str, operation: str) -> int:
        with self.connection() as db:
            result = db.execute(
                """UPDATE workflow_failures SET resolved_at=?
                WHERE video_id=? AND operation=? AND resolved_at IS NULL""",
                (self._now(), video_id, operation),
            )
        return int(result.rowcount)

    def list_failures(
        self,
        video_id: str | None = None,
        *,
        active_only: bool = False,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        if limit is not None and (limit < 1 or limit > 1000):
            raise StoreError("limit must be between 1 and 1000")
        query = "SELECT * FROM workflow_failures"
        clauses: list[str] = []
        params: list[Any] = []
        if video_id is not None:
            clauses.append("video_id=?")
            params.append(video_id)
        if active_only:
            clauses.append("resolved_at IS NULL")
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY id DESC"
        if limit is not None:
            query += " LIMIT ?"
            params.append(limit)
        with self.connection() as db:
            rows = db.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def count_active_failures(self) -> int:
        with self.connection() as db:
            row = db.execute(
                "SELECT COUNT(*) FROM workflow_failures WHERE resolved_at IS NULL"
            ).fetchone()
        return int(row[0])

    def active_failure_video_ids(self, video_ids: list[str]) -> set[str]:
        if not video_ids:
            return set()
        if len(video_ids) > 200:
            raise StoreError("At most 200 video IDs may be checked at once")
        placeholders = ",".join("?" for _ in video_ids)
        with self.connection() as db:
            rows = db.execute(
                f"""SELECT DISTINCT video_id FROM workflow_failures
                WHERE resolved_at IS NULL AND video_id IN ({placeholders})""",
                video_ids,
            ).fetchall()
        return {str(row[0]) for row in rows}

    @contextmanager
    def operation_lease(
        self,
        operation_key: str,
        *,
        ttl: timedelta = timedelta(hours=4),
        now: datetime | None = None,
    ) -> Iterator[bool]:
        if not operation_key or len(operation_key) > 200:
            raise StoreError("operation key must contain between 1 and 200 characters")
        if ttl <= timedelta(0):
            raise StoreError("operation lease TTL must be positive")
        current = now or datetime.now(UTC)
        token = str(uuid4())
        acquired = False
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "DELETE FROM operation_leases WHERE expires_at<=?",
                (current.isoformat(),),
            )
            result = db.execute(
                """INSERT OR IGNORE INTO operation_leases(
                    operation_key, owner_token, acquired_at, expires_at
                ) VALUES (?, ?, ?, ?)""",
                (operation_key, token, current.isoformat(), (current + ttl).isoformat()),
            )
            acquired = result.rowcount == 1
        try:
            yield acquired
        finally:
            if acquired:
                with self.connection() as db:
                    db.execute(
                        "DELETE FROM operation_leases WHERE operation_key=? AND owner_token=?",
                        (operation_key, token),
                    )

    def next_unused_topic(self, channel: str, topics: list[dict[str, Any]]) -> dict[str, Any]:
        with self.connection() as db:
            used = {row[0] for row in db.execute("SELECT topic_id FROM topic_usage WHERE channel=?", (channel,))}
            used.update(
                row[0] for row in db.execute("SELECT topic_id FROM topic_reservations WHERE channel=?", (channel,))
            )
        for topic in topics:
            if topic["id"] not in used:
                return topic
        raise StoreError(f"No unused topics remain for {channel}")

    def reserve_next_topic(
        self,
        channel: str,
        topics: list[dict[str, Any]],
        now: datetime | None = None,
    ) -> tuple[dict[str, Any], str]:
        current = now or datetime.now(UTC)
        stale_before = current - timedelta(hours=1)
        token = str(uuid4())
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM topic_reservations WHERE reserved_at<?", (stale_before.isoformat(),))
            used = {row[0] for row in db.execute("SELECT topic_id FROM topic_usage WHERE channel=?", (channel,))}
            used.update(
                row[0] for row in db.execute("SELECT topic_id FROM topic_reservations WHERE channel=?", (channel,))
            )
            topic = next((item for item in topics if item["id"] not in used), None)
            if topic is None:
                raise StoreError(f"No unused topics remain for {channel}")
            db.execute(
                """INSERT INTO topic_reservations(channel, topic_id, reservation_token, reserved_at)
                VALUES (?, ?, ?, ?)""",
                (channel, topic["id"], token, current.isoformat()),
            )
        return topic, token

    def release_topic_reservation(self, reservation_token: str) -> None:
        with self.connection() as db:
            db.execute("DELETE FROM topic_reservations WHERE reservation_token=?", (reservation_token,))

    def increment_daily_llm_calls(self, maximum: int) -> int:
        today = date.today().isoformat()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT call_count FROM llm_calls WHERE day=?", (today,)).fetchone()
            count = int(row[0]) if row else 0
            if count >= maximum:
                raise StoreError(f"Daily LLM call limit reached ({maximum})")
            count += 1
            db.execute(
                "INSERT INTO llm_calls(day, call_count) VALUES (?, ?) ON CONFLICT(day) DO UPDATE SET call_count=excluded.call_count",
                (today, count),
            )
        return count

    def count_weekly_exports(self, channel: str, now: datetime | None = None) -> int:
        current = now or datetime.now(UTC)
        start = (current - timedelta(days=current.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
        with self.connection() as db:
            row = db.execute(
                "SELECT COUNT(*) FROM videos WHERE channel=? AND exported_at>=?",
                (channel, start.isoformat()),
            ).fetchone()
        return int(row[0])

    def count_all_weekly_exports(self, now: datetime | None = None) -> int:
        current = now or datetime.now(UTC)
        start = (current - timedelta(days=current.weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        with self.connection() as db:
            row = db.execute(
                "SELECT COUNT(*) FROM videos WHERE exported_at>=?",
                (start.isoformat(),),
            ).fetchone()
        return int(row[0])

    def record_asset(self, video_id: str, scene: dict[str, Any], path: Path) -> None:
        credit = scene["image_credit"]
        self.record_asset_credit(
            video_id,
            scene["scene_id"],
            path,
            source=credit["source"],
            license_name=credit["license"],
            attribution=credit["attribution"],
        )

    def record_asset_credit(
        self,
        video_id: str,
        asset_id: str,
        path: Path,
        *,
        source: str,
        license_name: str,
        attribution: str,
    ) -> None:
        with self.connection() as db:
            video = db.execute(
                "SELECT status FROM videos WHERE id=?", (video_id,)
            ).fetchone()
            if video is not None and PipelineStatus(video["status"]) in {
                PipelineStatus.RENDERED,
                PipelineStatus.EXPORTING,
                PipelineStatus.EXPORTED,
            }:
                raise StoreError("Asset provenance is immutable after rendering")
            db.execute(
                """INSERT OR REPLACE INTO assets(video_id, scene_id, path, source, license, attribution)
                VALUES (?, ?, ?, ?, ?, ?)""",
                (video_id, asset_id, str(path), source, license_name, attribution),
            )

    def list_assets(self, video_id: str) -> list[dict[str, Any]]:
        self.get_video(video_id)
        with self.connection() as db:
            rows = db.execute(
                """SELECT scene_id AS asset_id, path, source, license, attribution
                FROM assets WHERE video_id=? ORDER BY scene_id""",
                (video_id,),
            ).fetchall()
        return [dict(row) for row in rows]

