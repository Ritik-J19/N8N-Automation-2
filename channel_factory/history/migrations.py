from __future__ import annotations

from dataclasses import dataclass
import sqlite3
import time


@dataclass(frozen=True)
class Migration:
    version: int
    description: str
    statements: tuple[str, ...]


MIGRATIONS: tuple[Migration, ...] = (
    Migration(
        1,
        "baseline workflow storage",
        (
            """CREATE TABLE IF NOT EXISTS videos (
                id TEXT PRIMARY KEY,
                channel TEXT NOT NULL,
                topic_id TEXT NOT NULL,
                title TEXT NOT NULL,
                summary TEXT NOT NULL,
                lesson TEXT,
                characters_json TEXT NOT NULL DEFAULT '[]',
                key_facts_json TEXT NOT NULL DEFAULT '[]',
                narration TEXT NOT NULL,
                script_json TEXT NOT NULL,
                status TEXT NOT NULL,
                hook_type TEXT,
                length_bucket TEXT NOT NULL,
                structure_fingerprint TEXT NOT NULL,
                rejection_reason TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                approved_at TEXT,
                exported_at TEXT
            )""",
            "CREATE INDEX IF NOT EXISTS idx_videos_channel_created ON videos(channel, created_at DESC)",
            """CREATE TABLE IF NOT EXISTS topic_usage (
                channel TEXT NOT NULL,
                topic_id TEXT NOT NULL,
                used_at TEXT NOT NULL,
                PRIMARY KEY(channel, topic_id)
            )""",
            """CREATE TABLE IF NOT EXISTS llm_calls (
                day TEXT PRIMARY KEY,
                call_count INTEGER NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS assets (
                video_id TEXT NOT NULL,
                scene_id TEXT NOT NULL,
                path TEXT NOT NULL,
                source TEXT NOT NULL,
                license TEXT NOT NULL,
                attribution TEXT NOT NULL,
                PRIMARY KEY(video_id, scene_id),
                FOREIGN KEY(video_id) REFERENCES videos(id)
            )""",
        ),
    ),
    Migration(
        2,
        "workflow event audit log",
        (
            """CREATE TABLE IF NOT EXISTS workflow_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                video_id TEXT NOT NULL,
                from_status TEXT,
                to_status TEXT NOT NULL,
                detail TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY(video_id) REFERENCES videos(id)
            )""",
            "CREATE INDEX IF NOT EXISTS idx_workflow_events_video ON workflow_events(video_id, id)",
        ),
    ),
    Migration(
        3,
        "concurrent topic reservations",
        (
            """CREATE TABLE IF NOT EXISTS topic_reservations (
                channel TEXT NOT NULL,
                topic_id TEXT NOT NULL,
                reservation_token TEXT NOT NULL UNIQUE,
                reserved_at TEXT NOT NULL,
                PRIMARY KEY(channel, topic_id)
            )""",
        ),
    ),
    Migration(
        4,
        "recoverable workflow failures",
        (
            """CREATE TABLE IF NOT EXISTS workflow_failures (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                video_id TEXT NOT NULL,
                operation TEXT NOT NULL,
                error_type TEXT NOT NULL,
                message TEXT NOT NULL,
                created_at TEXT NOT NULL,
                resolved_at TEXT,
                FOREIGN KEY(video_id) REFERENCES videos(id)
            )""",
            """CREATE INDEX IF NOT EXISTS idx_workflow_failures_active
                ON workflow_failures(video_id, resolved_at, id)""",
        ),
    ),
    Migration(
        5,
        "system operations audit log",
        (
            """CREATE TABLE IF NOT EXISTS system_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_type TEXT NOT NULL,
                severity TEXT NOT NULL,
                detail TEXT NOT NULL,
                created_at TEXT NOT NULL
            )""",
            "CREATE INDEX IF NOT EXISTS idx_system_events_created ON system_events(created_at DESC, id DESC)",
        ),
    ),
    Migration(
        6,
        "scalable queries and cross-process operation leases",
        (
            "CREATE INDEX IF NOT EXISTS idx_videos_status_updated ON videos(status, updated_at DESC)",
            "CREATE INDEX IF NOT EXISTS idx_videos_updated ON videos(updated_at DESC, id)",
            "CREATE INDEX IF NOT EXISTS idx_videos_channel_exported ON videos(channel, exported_at)",
            "CREATE INDEX IF NOT EXISTS idx_workflow_failures_resolution ON workflow_failures(resolved_at, video_id)",
            """CREATE TABLE IF NOT EXISTS operation_leases (
                operation_key TEXT PRIMARY KEY,
                owner_token TEXT NOT NULL UNIQUE,
                acquired_at TEXT NOT NULL,
                expires_at TEXT NOT NULL
            )""",
            "CREATE INDEX IF NOT EXISTS idx_operation_leases_expiry ON operation_leases(expires_at)",
        ),
    ),
)


LATEST_SCHEMA_VERSION = MIGRATIONS[-1].version


def apply_migrations(connection: sqlite3.Connection) -> None:
    """Apply every pending additive migration in its own transaction."""
    for attempt in range(8):
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            break
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).lower() or attempt == 7:
                raise
            time.sleep(min(1.0, 0.05 * (2**attempt)))
    connection.execute(
        """CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            description TEXT NOT NULL,
            applied_at TEXT NOT NULL
        )"""
    )
    applied = {
        int(row[0])
        for row in connection.execute("SELECT version FROM schema_migrations").fetchall()
    }
    for migration in MIGRATIONS:
        if migration.version in applied:
            continue
        try:
            connection.execute("BEGIN IMMEDIATE")
            # Another process may have completed this migration after our
            # initial snapshot but before this writer acquired SQLite's lock.
            already_applied = connection.execute(
                "SELECT 1 FROM schema_migrations WHERE version=?",
                (migration.version,),
            ).fetchone()
            if already_applied is not None:
                connection.commit()
                applied.add(migration.version)
                continue
            for statement in migration.statements:
                connection.execute(statement)
            connection.execute(
                """INSERT INTO schema_migrations(version, description, applied_at)
                VALUES (?, ?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))""",
                (migration.version, migration.description),
            )
            connection.commit()
            applied.add(migration.version)
        except Exception:
            connection.rollback()
            raise
