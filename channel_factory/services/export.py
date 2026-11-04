from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path
import re
import shutil
import tempfile
from typing import Any

from channel_factory.config import Profile
from channel_factory.core.state import PipelineStatus
from channel_factory.history.store import HistoryStore


class ExportError(RuntimeError):
    pass


def _slug(value: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return cleaned[:64] or "video"


def _description(script: dict[str, Any], profile: Profile) -> str:
    lines = [script["summary"], "", "Sources and credits are included in credits.csv."]
    if script.get("disclosure_required"):
        lines.extend(["", "Disclosure: This video includes AI-assisted drafting or synthetic media."])
    if profile.made_for_kids:
        lines.extend(["", "Audience setting reminder: made for kids."])
    return "\n".join(lines)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_manifest(directory: Path, video_id: str) -> None:
    files = {
        path.name: {"sha256": _sha256(path), "size": path.stat().st_size}
        for path in sorted(directory.iterdir())
        if path.is_file() and path.name != "manifest.json"
    }
    manifest = {"package_version": 1, "video_id": video_id, "files": files}
    (directory / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


def verify_export_package(directory: Path, video_id: str) -> bool:
    manifest_path = directory / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if manifest.get("video_id") != video_id or manifest.get("package_version") != 1:
        return False
    files = manifest.get("files")
    if not isinstance(files, dict) or not files:
        return False
    actual_files = {
        path.name for path in directory.iterdir() if path.is_file() and path.name != "manifest.json"
    }
    if actual_files != set(files):
        return False
    for name, expected in files.items():
        if not isinstance(name, str) or Path(name).name != name or not isinstance(expected, dict):
            return False
        path = directory / name
        if not path.is_file():
            return False
        if path.stat().st_size != expected.get("size") or _sha256(path) != expected.get("sha256"):
            return False
    return True


def _manifest_video_id(directory: Path) -> str | None:
    try:
        manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    value = manifest.get("video_id") if isinstance(manifest, dict) else None
    return value if isinstance(value, str) else None


def export_package(
    store: HistoryStore,
    video_id: str,
    script: dict[str, Any],
    video_path: Path,
    profile: Profile,
    output_root: Path,
) -> Path:
    video = store.get_video(video_id)
    if video["channel"] != profile.profile_id:
        raise ExportError("Profile does not match the video's channel")
    output_root.mkdir(parents=True, exist_ok=True)
    short_dir = output_root / f"{_slug(script['title'])}-{video_id[:8]}"
    final_dir = short_dir
    try:
        status, acquired = store.begin_export(video_id, profile.posting_cap_per_week)
    except RuntimeError as exc:
        raise ExportError(str(exc)) from exc
    if short_dir.exists() and _manifest_video_id(short_dir) not in {None, video_id}:
        # Eight-character UUID prefixes are convenient but can eventually collide.
        # Preserve existing package names while using the full UUID for the colliding job.
        final_dir = output_root / f"{_slug(script['title'])}-{video_id}"
    if final_dir.exists():
        if not verify_export_package(final_dir, video_id):
            if acquired:
                store.abort_export(video_id, "existing export package failed validation")
            raise ExportError(f"Export folder exists without a matching manifest: {final_dir}")
        if status is PipelineStatus.EXPORTING:
            store.complete_export(video_id)
        return final_dir
    if status is PipelineStatus.EXPORTED:
        raise ExportError(f"Database says exported but package is missing: {final_dir}")
    if not acquired:
        raise ExportError(f"An export is already in progress for {video_id}")
    if not video_path.is_file():
        store.abort_export(video_id, "rendered video was missing")
        raise ExportError(f"Rendered video not found: {video_path}")
    try:
        with tempfile.TemporaryDirectory(prefix="export-", dir=output_root) as temp_name:
            temp_dir = Path(temp_name)
            shutil.copy2(video_path, temp_dir / "video.mp4")
            title = script["title"]
            description = _description(script, profile)
            tags = list(dict.fromkeys([profile.profile_id, profile.content_type, *script.get("tags", [])]))
            hashtags = [f"#{re.sub(r'[^A-Za-z0-9]', '', tag)}" for tag in tags if tag]
            metadata = {
                "title": title,
                "description": description,
                "tags": tags,
                "caption": f"{script['summary']} {' '.join(hashtags)}".strip(),
                "made_for_kids": profile.made_for_kids,
                "disclosure_required": bool(script.get("disclosure_required")),
                "manual_upload_required": True,
                "reminders": [
                    "Confirm audience and synthetic-content fields during upload.",
                    "Upload only cleared images, narration, and royalty-free music.",
                ],
                "sources": script.get("sources", []),
            }
            (temp_dir / "title.txt").write_text(title + "\n", encoding="utf-8")
            (temp_dir / "description.txt").write_text(description + "\n", encoding="utf-8")
            (temp_dir / "tags.txt").write_text(", ".join(tags) + "\n", encoding="utf-8")
            (temp_dir / "caption.txt").write_text(metadata["caption"] + "\n", encoding="utf-8")
            (temp_dir / "metadata.json").write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
            assets = store.list_assets(video_id)
            if not assets:
                assets = [
                    {
                        "asset_id": scene["scene_id"],
                        "path": scene["image_file"],
                        **scene["image_credit"],
                    }
                    for scene in script["scenes"]
                ]
            with (temp_dir / "credits.csv").open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(
                    handle, fieldnames=["asset_id", "path", "source", "license", "attribution"]
                )
                writer.writeheader()
                writer.writerows(assets)
            _write_manifest(temp_dir, video_id)
            shutil.move(str(temp_dir), str(final_dir))
        store.complete_export(video_id)
        return final_dir
    except Exception as exc:
        store.abort_export(video_id, f"export failed: {type(exc).__name__}")
        if isinstance(exc, ExportError):
            raise
        raise ExportError(f"Export failed: {exc}") from exc

