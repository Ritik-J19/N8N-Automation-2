from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import subprocess
from typing import Any, Callable

from channel_factory.config import Profile
from channel_factory.core.schema import load_and_validate_script, save_script
from channel_factory.core.state import PipelineStatus
from channel_factory.history.dedupe import DuplicateReport, check_duplicate
from channel_factory.history.store import HistoryStore


class ApprovalError(RuntimeError):
    pass


def format_review(script: dict[str, Any], report: DuplicateReport) -> str:
    lines = [
        f"Title: {script['title']}",
        f"Summary: {script['summary']}",
        f"Narration mode: {script['narration_mode']}",
        f"Safety flags: {json.dumps(script.get('safety_flags', []))}",
        f"Claims to verify: {json.dumps(script.get('claims_to_verify', []))}",
        f"Sources: {json.dumps(script.get('sources', []))}",
        (
            "Closest past video: "
            f"{report.closest_title or 'none'} "
            f"(title {report.title_score:.3f}, narration {report.narration_score:.3f}, "
            f"lesson+characters {report.lesson_character_score:.3f})"
        ),
    ]
    if report.structure_warning:
        lines.append(f"Structure warning: {report.structure_warning}")
    lines.append("\nScenes:")
    for scene in script["scenes"]:
        lines.append(f"[{scene['scene_id']}] {scene['narration']}")
    return "\n".join(lines)


def approve_video(store: HistoryStore, video_id: str) -> None:
    video = store.get_video(video_id)
    if video["status"] != PipelineStatus.AWAITING_REVIEW.value:
        raise ApprovalError("Only a video awaiting review can be approved")
    store.transition(video_id, PipelineStatus.APPROVED)


def _open_editor(path: Path) -> None:
    editor = os.getenv("EDITOR", "notepad.exe")
    command = [*shlex.split(editor, posix=False), str(path)]
    result = subprocess.run(command, check=False, timeout=3600)
    if result.returncode:
        raise ApprovalError(f"Editor exited with code {result.returncode}")


def interactive_review(
    store: HistoryStore,
    video_id: str,
    script_path: Path,
    script: dict[str, Any],
    profile: Profile,
    report: DuplicateReport,
    input_fn: Callable[[str], str] = input,
) -> str:
    print(format_review(script, report))
    while True:
        choice = input_fn("\nChoose approve / edit / reject: ").strip().lower()
        if choice in {"approve", "a"}:
            if report.rejected:
                print("This edit exceeds duplicate thresholds. Edit again or reject it for redrafting.")
                continue
            approve_video(store, video_id)
            return "approved"
        if choice in {"edit", "e"}:
            save_script(script, script_path)
            _open_editor(script_path)
            updated = load_and_validate_script(script_path, profile)
            store.update_script(video_id, updated)
            script.clear()
            script.update(updated)
            report = check_duplicate(
                updated,
                store.list_recent(profile.profile_id, 30, exclude_id=video_id),
                profile,
            )
            print(format_review(script, report))
            continue
        if choice in {"reject", "r"}:
            reason = input_fn("Reason for rejection: ").strip()
            if not reason:
                print("A rejection reason is required.")
                continue
            store.transition(video_id, PipelineStatus.REJECTED, reason=reason)
            return "rejected"
        print("Enter approve, edit, or reject.")

