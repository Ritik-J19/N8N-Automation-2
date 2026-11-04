from __future__ import annotations

from datetime import UTC, datetime, timedelta
from importlib.metadata import PackageNotFoundError, version
import json
import os
from pathlib import Path
import shutil
import sys
import threading
import time
from typing import Annotated, Any
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from channel_factory.config import DEFAULT_PROFILES_ROOT, ConfigError, load_profile
from channel_factory.core.pipeline import Pipeline
from channel_factory.core.state import PipelineStatus
from channel_factory.history.store import HistoryStore, StoreError
from channel_factory.services.assemble import AssemblyError, RenderSettings, find_ffmpeg
from channel_factory.services.export import verify_export_package
from channel_factory.web.security import (
    COOKIE_NAME,
    SESSION_HOURS,
    LoginThrottle,
    Session,
    SessionSigner,
    authenticated_session,
    csrf_protected,
)


STATIC_ROOT = Path(__file__).resolve().parent / "static"


class LoginRequest(BaseModel):
    token: str = Field(min_length=1, max_length=256)


class BackupGuard:
    def __init__(self, minimum_interval_seconds: int = 5, maximum_backups: int = 20) -> None:
        self.minimum_interval_seconds = minimum_interval_seconds
        self.maximum_backups = maximum_backups
        self.last_created = 0.0
        self.lock = threading.Lock()


class BackupCatalog:
    """Cache immutable backup verification by file size and modification time."""

    def __init__(self) -> None:
        self._cache: dict[Path, tuple[tuple[int, int], dict[str, Any]]] = {}
        self._lock = threading.Lock()

    def records(self, backups_dir: Path) -> list[dict[str, Any]]:
        if not backups_dir.exists():
            return []
        paths = sorted(backups_dir.glob("channel-factory-*.sqlite3"), reverse=True)
        current_paths = set(paths)
        result: list[dict[str, Any]] = []
        with self._lock:
            self._cache = {
                path: cached for path, cached in self._cache.items() if path in current_paths
            }
            for path in paths:
                try:
                    metadata = path.stat()
                except OSError:
                    continue
                signature = (metadata.st_size, metadata.st_mtime_ns)
                cached = self._cache.get(path)
                if cached is not None and cached[0] == signature:
                    result.append(dict(cached[1]))
                    continue
                item: dict[str, Any] = {
                    "name": path.name,
                    "size": metadata.st_size,
                    "created_at": datetime.fromtimestamp(metadata.st_mtime, UTC).isoformat(),
                }
                try:
                    report = HistoryStore.verify_backup(path)
                    item.update({key: value for key, value in report.items() if key != "path"})
                    item["valid"] = True
                except StoreError as exc:
                    item.update({"valid": False, "error": str(exc)})
                self._cache[path] = (signature, dict(item))
                result.append(item)
        return result


def _relative_path(workspace: Path, path: Path) -> str:
    resolved_workspace = workspace.resolve()
    resolved_path = path.resolve()
    try:
        return resolved_path.relative_to(resolved_workspace).as_posix()
    except ValueError:
        return path.name


def _safe_job_directory(root: Path, video_id: str) -> Path:
    resolved_root = root.resolve()
    candidate = (resolved_root / video_id).resolve()
    try:
        candidate.relative_to(resolved_root)
    except ValueError as exc:
        raise StoreError("Invalid job path") from exc
    return candidate


def _workspace_storage_report(workspace: Path) -> dict[str, Any]:
    resolved = workspace.resolve()
    configured_roots = [
        Path(value).resolve()
        for name in ("OneDrive", "OneDriveCommercial", "OneDriveConsumer")
        if (value := os.getenv(name))
    ]
    in_sync_root = any(
        resolved == root or root in resolved.parents for root in configured_roots
    ) or any(part.casefold().startswith("onedrive") for part in resolved.parts)
    return {
        "sync_folder_detected": in_sync_root,
        "mode": "synced_folder" if in_sync_root else "local_disk",
        "recommendation": (
            "Keep the active SQLite workspace on a local non-synced folder; copy verified backups after shutdown."
            if in_sync_root
            else "Workspace is on a local non-synced path."
        ),
    }


def _video_summary(video: dict[str, Any]) -> dict[str, Any]:
    return {
        key: video[key]
        for key in (
            "id", "channel", "topic_id", "title", "summary", "status", "created_at",
            "updated_at", "approved_at", "exported_at", "rejection_reason",
        )
    }


def _artifacts(pipeline: Pipeline, video_id: str) -> dict[str, Any]:
    run_dir = _safe_job_directory(pipeline.runs_dir, video_id)
    export_matches = (
        [
            path
            for path in sorted(pipeline.outputs_dir.glob(f"*-{video_id[:8]}*"))
            if path.is_dir() and verify_export_package(path, video_id)
        ]
        if pipeline.outputs_dir.exists()
        else []
    )
    return {
        "script": _relative_path(pipeline.workspace, run_dir / "script.json")
        if (run_dir / "script.json").is_file() else None,
        "narration": [
            _relative_path(pipeline.workspace, path) for path in sorted(run_dir.glob("narration.*"))
        ],
        "video": _relative_path(pipeline.workspace, run_dir / "video.mp4")
        if (run_dir / "video.mp4").is_file() else None,
        "exports": [_relative_path(pipeline.workspace, path) for path in export_matches],
    }


def _readiness(pipeline: Pipeline, video: dict[str, Any]) -> dict[str, Any]:
    video_id = video["id"]
    script = json.loads(video["script_json"])
    artifacts = _artifacts(pipeline, video_id)
    assets = pipeline.store.list_assets(video_id)
    asset_ids = {asset["asset_id"] for asset in assets}
    expected_scenes = {str(scene["scene_id"]) for scene in script.get("scenes", [])}
    unresolved = pipeline.store.list_failures(video_id, active_only=True, limit=1)
    exported = bool(artifacts["exports"])
    checks = [
        {"id": "approval", "label": "Human approval recorded", "passed": bool(video["approved_at"])},
        {
            "id": "images",
            "label": "Every scene has a registered image",
            "passed": bool(expected_scenes) and expected_scenes.issubset(asset_ids),
        },
        {
            "id": "licenses",
            "label": "Asset provenance and licenses recorded",
            "passed": bool(assets) and all(asset["source"] and asset["license"] for asset in assets),
        },
        {"id": "narration", "label": "Narration artifact prepared", "passed": bool(artifacts["narration"])},
        {"id": "render", "label": "Rendered video available", "passed": bool(artifacts["video"])},
        {
            "id": "export",
            "label": "Verified export package available",
            "passed": exported if video["status"] == PipelineStatus.EXPORTED.value else None,
        },
        {"id": "incidents", "label": "No unresolved incidents", "passed": not unresolved},
    ]
    applicable = [check for check in checks if check["passed"] is not None]
    passed = sum(1 for check in applicable if check["passed"])
    return {
        "score": round((passed / len(applicable)) * 100) if applicable else 0,
        "passed": passed,
        "total": len(applicable),
        "checks": checks,
    }


def _attention_items(
    pipeline: Pipeline,
    videos: list[dict[str, Any]],
    backups: list[dict[str, Any]],
    storage_report: dict[str, Any],
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    failures = pipeline.store.list_failures(active_only=True, limit=6)
    videos_by_id = {video["id"]: video for video in videos}
    for failure in failures[:6]:
        video = videos_by_id.get(failure["video_id"])
        items.append(
            {
                "kind": "incident", "severity": "error", "video_id": failure["video_id"],
                "title": f"{failure['operation'].title()} failed",
                "detail": video["title"] if video else failure["message"],
                "created_at": failure["created_at"],
            }
        )
    for video in videos:
        if video["status"] == PipelineStatus.AWAITING_REVIEW.value:
            items.append(
                {
                    "kind": "review", "severity": "warning", "video_id": video["id"],
                    "title": "Human review required", "detail": video["title"],
                    "created_at": video["updated_at"],
                }
            )
        if video["status"] == PipelineStatus.EXPORTING.value:
            updated = datetime.fromisoformat(video["updated_at"])
            if datetime.now(UTC) - updated > timedelta(minutes=15):
                items.append(
                    {
                        "kind": "stale_export", "severity": "warning", "video_id": video["id"],
                        "title": "Stale export can be recovered", "detail": video["title"],
                        "created_at": video["updated_at"],
                    }
                )
    valid_backups = [backup for backup in backups if backup.get("valid")]
    if not valid_backups:
        items.append(
            {
                "kind": "backup", "severity": "warning", "video_id": None,
                "title": "No verified backup", "detail": "Create the first database snapshot.",
                "created_at": None,
            }
        )
    else:
        newest_backup = valid_backups[0]
        newest = datetime.fromisoformat(newest_backup["created_at"])
        if datetime.now(UTC) - newest > timedelta(days=7):
            items.append(
                {
                    "kind": "backup", "severity": "warning", "video_id": None,
                    "title": "Backup is older than seven days", "detail": newest_backup["name"],
                    "created_at": newest_backup["created_at"],
                }
            )
    if storage_report["sync_folder_detected"]:
        items.append(
            {
                "kind": "storage", "severity": "warning", "video_id": None,
                "title": "Synced workspace detected",
                "detail": "Use a local non-synced workspace for concurrent or sustained runs.",
                "created_at": None,
            }
        )
    return sorted(items, key=lambda item: item["created_at"] or "", reverse=True)[:12]


def _system_report() -> dict[str, Any]:
    try:
        sdk_version = version("google-genai")
        from google import genai  # noqa: F401

        sdk_status = "ready"
    except (PackageNotFoundError, ImportError):
        sdk_version = None
        sdk_status = "unavailable"
    try:
        ffmpeg_path = find_ffmpeg()
        ffmpeg = ffmpeg_path.name
        ffmpeg_status = "ready"
    except AssemblyError as exc:
        ffmpeg = str(exc)
        ffmpeg_status = "unavailable"
    return {
        "python": sys.version.split()[0],
        "ffmpeg": {"status": ffmpeg_status, "path": ffmpeg},
        "gemini_sdk": {"status": sdk_status, "version": sdk_version},
        "gemini_live": "deferred",
        "gemini_credentials_present": bool(os.getenv("GEMINI_API_KEY") and os.getenv("GEMINI_MODEL")),
        "ffmpeg_provider": "system" if shutil.which("ffmpeg") else "bundled",
    }


def create_app(workspace: Path, access_token: str | None = None) -> FastAPI:
    resolved_workspace = workspace.resolve()
    token = access_token or os.getenv("CHANNEL_FACTORY_DASHBOARD_TOKEN", "")
    signer = SessionSigner(token)
    pipeline = Pipeline(resolved_workspace)
    backups_dir = resolved_workspace / "backups"
    login_throttle = LoginThrottle()
    backup_guard = BackupGuard()
    backup_catalog = BackupCatalog()
    storage_report = _workspace_storage_report(resolved_workspace)

    app = FastAPI(
        title="Channel Factory Operations Console",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.pipeline = pipeline
    app.state.session_signer = signer
    app.state.backups_dir = backups_dir
    app.state.login_throttle = login_throttle
    app.state.backup_guard = backup_guard
    app.state.backup_catalog = backup_catalog
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=["127.0.0.1", "localhost", "[::1]", "testserver"],
    )

    @app.middleware("http")
    async def security_headers(request: Request, call_next: Any) -> Response:
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; script-src-attr 'none'; style-src 'self'; "
            "style-src-attr 'none'; img-src 'self' data:; "
            "connect-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'none'; "
            "form-action 'self'; worker-src 'none'; manifest-src 'none'"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "service": "channel-factory-console",
            "schema_version": pipeline.store.schema_version(),
        }

    @app.post("/api/session")
    def login(payload: LoginRequest, request: Request, response: Response) -> dict[str, Any]:
        client_key = request.client.host if request.client else "unknown"
        retry_after = login_throttle.retry_after(client_key)
        if retry_after:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Too many failed login attempts",
                headers={"Retry-After": str(retry_after)},
            )
        if not signer.verify_access_token(payload.token):
            locked = login_throttle.record_failure(client_key)
            if locked:
                pipeline.store.record_system_event(
                    "login_throttled", "Dashboard login throttled for the local client", "warning"
                )
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid access token")
        login_throttle.clear(client_key)
        cookie, session = signer.create()
        response.set_cookie(
            COOKIE_NAME,
            cookie,
            max_age=SESSION_HOURS * 60 * 60,
            httponly=True,
            samesite="strict",
            secure=False,
            path="/",
        )
        return {"authenticated": True, "csrf_token": session.csrf_token}

    @app.get("/api/session")
    def session_info(request: Request) -> dict[str, Any]:
        session = signer.verify(request.cookies.get(COOKIE_NAME))
        if session is None:
            return {"authenticated": False, "csrf_token": None, "expires_at": None}
        return {"authenticated": True, "csrf_token": session.csrf_token, "expires_at": session.expires_at}

    @app.delete("/api/session")
    def logout(
        response: Response,
        session: Annotated[Session, Depends(csrf_protected)],
    ) -> dict[str, bool]:
        signer.revoke(session)
        response.delete_cookie(COOKIE_NAME, path="/")
        return {"authenticated": False}

    @app.get("/api/summary")
    def summary(_session: Annotated[Session, Depends(authenticated_session)]) -> dict[str, Any]:
        status_counts = pipeline.store.count_videos_by_status()
        active_failures = pipeline.store.count_active_failures()
        attention_videos = pipeline.store.list_attention_videos(limit=100)
        recent_videos = pipeline.store.list_videos(limit=8)
        backup_records = backup_catalog.records(backups_dir)
        valid_backups = [backup for backup in backup_records if backup.get("valid")]
        exports_this_week = pipeline.store.count_all_weekly_exports()
        stages = {
            "draft": status_counts.get(PipelineStatus.DRAFTED.value, 0)
            + status_counts.get(PipelineStatus.SAFETY_CHECKED.value, 0),
            "review": status_counts.get(PipelineStatus.AWAITING_REVIEW.value, 0),
            "approved": status_counts.get(PipelineStatus.APPROVED.value, 0),
            "narrated": status_counts.get(PipelineStatus.NARRATED.value, 0),
            "rendered": status_counts.get(PipelineStatus.RENDERED.value, 0)
            + status_counts.get(PipelineStatus.EXPORTING.value, 0),
            "exported": status_counts.get(PipelineStatus.EXPORTED.value, 0),
        }
        workflow_activity = [
            {**event, "kind": "workflow"} for event in pipeline.store.list_recent_events(12)
        ]
        system_activity = [
            {**event, "kind": "system"} for event in pipeline.store.list_system_events(8)
        ]
        activity = sorted(
            [*workflow_activity, *system_activity],
            key=lambda item: item["created_at"],
            reverse=True,
        )[:12]
        return {
            "total_jobs": sum(status_counts.values()),
            "status_counts": dict(status_counts),
            "stages": stages,
            "active_failures": active_failures,
            "awaiting_review": status_counts.get(PipelineStatus.AWAITING_REVIEW.value, 0),
            "ready_to_export": status_counts.get(PipelineStatus.RENDERED.value, 0),
            "exports_this_week": exports_this_week,
            "attention": _attention_items(
                pipeline, attention_videos, backup_records, storage_report
            ),
            "activity": activity,
            "backup_health": valid_backups[0] if valid_backups else None,
            "backup_count": len(backup_records),
            "backup_limit": backup_guard.maximum_backups,
            "recent_jobs": [_video_summary(video) for video in recent_videos],
            "schema_version": pipeline.store.schema_version(),
        }

    @app.get("/api/jobs")
    def jobs(
        _session: Annotated[Session, Depends(authenticated_session)],
        job_status: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        try:
            return [
                _video_summary(video)
                for video in pipeline.store.list_videos(status=job_status, limit=limit)
            ]
        except (StoreError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/jobs-page")
    def jobs_page(
        _session: Annotated[Session, Depends(authenticated_session)],
        q: str | None = None,
        job_status: str | None = None,
        channel: str | None = None,
        sort_by: str = "updated_at",
        descending: bool = True,
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        try:
            videos, total = pipeline.store.search_videos(
                query_text=q,
                status=job_status,
                channel=channel,
                sort_by=sort_by,
                descending=descending,
                limit=limit,
                offset=offset,
            )
        except (StoreError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        incident_ids = pipeline.store.active_failure_video_ids(
            [str(video["id"]) for video in videos]
        )
        return {
            "items": [
                {**_video_summary(video), "has_incident": video["id"] in incident_ids}
                for video in videos
            ],
            "total": total,
            "limit": limit,
            "offset": offset,
        }

    @app.get("/api/jobs/{video_id}")
    def job(video_id: UUID, _session: Annotated[Session, Depends(authenticated_session)]) -> dict[str, Any]:
        video_key = str(video_id)
        try:
            video = pipeline.store.get_video(video_key)
        except StoreError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        assets = pipeline.store.list_assets(video_key)
        for asset in assets:
            asset["path"] = _relative_path(pipeline.workspace, Path(asset["path"]))
        return {
            **_video_summary(video),
            "script": json.loads(video["script_json"]),
            "events": pipeline.store.list_events(video_key),
            "assets": assets,
            "failures": pipeline.store.list_failures(video_key, limit=200),
            "artifacts": _artifacts(pipeline, video_key),
            "readiness": _readiness(pipeline, video),
        }

    @app.post("/api/jobs/{video_id}/recover")
    def recover(
        video_id: UUID,
        _session: Annotated[Session, Depends(csrf_protected)],
    ) -> dict[str, Any]:
        video_key = str(video_id)
        with pipeline.store.operation_lease(
            f"recover:{video_key}", ttl=timedelta(hours=1)
        ) as acquired:
            if not acquired:
                raise HTTPException(status_code=409, detail="A recovery is already running for this job")
            try:
                video = pipeline.store.get_video(video_key)
                current = PipelineStatus(video["status"])
                run_dir = _safe_job_directory(pipeline.runs_dir, video_key)
                if current is PipelineStatus.NARRATED:
                    narration = sorted(run_dir.glob("narration.*"))
                    if len(narration) != 1:
                        raise StoreError("Recovery needs exactly one prepared narration file")
                    profile = pipeline.profile(video["channel"])
                    output = pipeline.render(
                        video_key,
                        run_dir / "images",
                        narration[0],
                        settings=RenderSettings(output_format=profile.output_formats[0], threads=2),
                    )
                    operation = "render"
                elif current in {PipelineStatus.RENDERED, PipelineStatus.EXPORTING}:
                    output = pipeline.export(video_key, run_dir / "video.mp4")
                    operation = "export"
                else:
                    raise StoreError(
                        "This stage cannot be recovered automatically. Use the terminal review or narration command."
                    )
                pipeline.store.record_system_event(
                    "job_recovered", f"{operation} recovery completed for job {video_key}"
                )
                return {"operation": operation, "output": _relative_path(pipeline.workspace, output)}
            except StoreError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            except (RuntimeError, ValueError) as exc:
                raise HTTPException(status_code=500, detail=str(exc)) from exc

    @app.get("/api/profiles")
    def profiles(_session: Annotated[Session, Depends(authenticated_session)]) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for directory in sorted(DEFAULT_PROFILES_ROOT.iterdir()):
            if not (directory / "profile.yaml").is_file():
                continue
            try:
                profile = load_profile(directory.name)
            except ConfigError:
                continue
            exports_this_week = pipeline.store.count_weekly_exports(profile.profile_id)
            result.append(
                {
                    "id": profile.profile_id,
                    "name": profile.name,
                    "enabled": profile.enabled,
                    "audience": profile.audience,
                    "content_type": profile.content_type,
                    "posting_cap_per_week": profile.posting_cap_per_week,
                    "narration_mode": profile.narration_mode_default,
                    "output_formats": profile.output_formats,
                    "made_for_kids": profile.made_for_kids,
                    "exports_this_week": exports_this_week,
                    "remaining_capacity": max(
                        0,
                        profile.posting_cap_per_week - exports_this_week,
                    ),
                }
            )
        return result

    @app.get("/api/system")
    def system(_session: Annotated[Session, Depends(authenticated_session)]) -> dict[str, Any]:
        disk = shutil.disk_usage(resolved_workspace)
        return {
            **_system_report(),
            "workspace": resolved_workspace.name,
            "database": _relative_path(resolved_workspace, pipeline.store.path),
            "schema_version": pipeline.store.schema_version(),
            "disk": {"free": disk.free, "total": disk.total, "used": disk.used},
            "workspace_storage": storage_report,
            "security": {
                "trusted_hosts": True,
                "login_throttling": True,
                "revocable_sessions": True,
                "csrf_and_origin": True,
            },
        }

    @app.get("/api/backups")
    def backups(_session: Annotated[Session, Depends(authenticated_session)]) -> list[dict[str, Any]]:
        return backup_catalog.records(backups_dir)

    @app.post("/api/backups", status_code=201)
    def create_backup(
        _session: Annotated[Session, Depends(csrf_protected)],
    ) -> dict[str, Any]:
        if not backup_guard.lock.acquire(blocking=False):
            raise HTTPException(status_code=409, detail="A backup is already running")
        try:
            since_last = time.monotonic() - backup_guard.last_created
            if backup_guard.last_created and since_last < backup_guard.minimum_interval_seconds:
                retry = max(1, int(backup_guard.minimum_interval_seconds - since_last))
                raise HTTPException(
                    status_code=429,
                    detail="Backups are temporarily rate limited",
                    headers={"Retry-After": str(retry)},
                )
            existing = backup_catalog.records(backups_dir)
            if len(existing) >= backup_guard.maximum_backups:
                raise HTTPException(
                    status_code=409,
                    detail=f"Backup retention limit reached ({backup_guard.maximum_backups})",
                )
            database_size = pipeline.store.path.stat().st_size if pipeline.store.path.exists() else 0
            required_free = max(50 * 1024 * 1024, database_size * 3)
            if shutil.disk_usage(resolved_workspace).free < required_free:
                raise HTTPException(status_code=507, detail="Insufficient free space for a safe backup")
            stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S-%f")
            destination = backups_dir / f"channel-factory-{stamp}.sqlite3"
            try:
                pipeline.store.backup(destination)
            except StoreError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            backup_guard.last_created = time.monotonic()
            pipeline.store.record_system_event("backup_created", f"Verified backup created: {destination.name}")
            report = HistoryStore.verify_backup(destination)
            return {
                **{key: value for key, value in report.items() if key != "path"},
                "name": destination.name,
                "created_at": datetime.fromtimestamp(destination.stat().st_mtime, UTC).isoformat(),
                "size": destination.stat().st_size,
                "valid": True,
            }
        finally:
            backup_guard.lock.release()

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC_ROOT / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC_ROOT), name="static")
    return app
