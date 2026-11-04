from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
import json
import logging
from pathlib import Path
from typing import Any, Sequence
from uuid import uuid4

from channel_factory.config import Profile, load_profile, load_topics
from channel_factory.core.schema import save_script, validate_script
from channel_factory.core.state import PipelineStatus
from channel_factory.history.dedupe import DuplicateReport, check_duplicate
from channel_factory.history.store import HistoryStore, StoreError
from channel_factory.review.approval import approve_video
from channel_factory.services.assemble import (
    RenderSettings,
    load_music_credit,
    render_slideshow,
    validate_manual_images,
)
from channel_factory.services.export import export_package
from channel_factory.services.llm import LLM, draft_with_dedupe, run_safety_pass
from channel_factory.services.tts import NarrationAdapter


class PipelineError(RuntimeError):
    pass


LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class DraftResult:
    video_id: str
    script: dict[str, Any]
    script_path: Path
    duplicate_report: DuplicateReport


class Pipeline:
    def __init__(self, workspace: Path, profiles_root: Path | None = None) -> None:
        self.workspace = workspace
        self.profiles_root = profiles_root
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.runs_dir = self.workspace / "runs"
        self.outputs_dir = self.workspace / "outputs"
        self.store = HistoryStore(self.workspace / "channel_factory.sqlite3")

    def _record_failure(self, video_id: str, operation: str, error: BaseException) -> None:
        try:
            self.store.record_failure(video_id, operation, error)
        except Exception:
            LOGGER.exception("Could not persist %s failure for video %s", operation, video_id)

    def profile(self, profile_id: str) -> Profile:
        profile = load_profile(profile_id, self.profiles_root)
        if not profile.enabled:
            raise PipelineError(f"Profile is disabled: {profile_id}")
        return profile

    def draft(self, profile_id: str, llm: LLM) -> DraftResult:
        profile = self.profile(profile_id)
        topics = load_topics(profile)
        topic, reservation = self.store.reserve_next_topic(profile_id, topics)
        video_id: str | None = None
        try:
            recent = self.store.list_recent(profile_id, 30)
            script, report = draft_with_dedupe(llm, profile, topic, recent)
            script = validate_script(script, profile)
            video_id = self.store.create_draft(
                script,
                list(topic.get("key_facts", [])),
                topic_reservation=reservation,
            )
            script = validate_script(run_safety_pass(llm, profile, script), profile)
            self.store.update_script(video_id, script)
            self.store.transition(video_id, PipelineStatus.SAFETY_CHECKED)
            self.store.transition(video_id, PipelineStatus.AWAITING_REVIEW)
        except Exception as exc:
            self.store.release_topic_reservation(reservation)
            if video_id is not None:
                self._record_failure(video_id, "draft", exc)
            raise
        run_dir = self.runs_dir / video_id
        script_path = run_dir / "script.json"
        save_script(script, script_path)
        return DraftResult(video_id, script, script_path, report)

    def redraft_rejected(self, video_id: str, llm: LLM) -> DraftResult:
        with self.store.operation_lease(
            f"redraft:{video_id}", ttl=timedelta(minutes=30)
        ) as acquired:
            if not acquired:
                raise PipelineError(f"A redraft is already running for {video_id}")
            try:
                video = self.store.get_video(video_id)
                if video["status"] != PipelineStatus.REJECTED.value:
                    raise PipelineError("Only rejected videos can be redrafted")
                profile = self.profile(video["channel"])
                topic = next((item for item in load_topics(profile) if item["id"] == video["topic_id"]), None)
                if topic is None:
                    raise StoreError(f"Topic no longer exists: {video['topic_id']}")
                recent = self.store.list_recent(profile.profile_id, 30, exclude_id=video_id)
                script, report = draft_with_dedupe(
                    llm, profile, topic, recent, rejection_reason=video["rejection_reason"]
                )
                script = validate_script(script, profile)
                self.store.update_script(video_id, script)
                self.store.transition(video_id, PipelineStatus.DRAFTED)
                script = validate_script(run_safety_pass(llm, profile, script), profile)
                self.store.update_script(video_id, script)
                self.store.transition(video_id, PipelineStatus.SAFETY_CHECKED)
                self.store.transition(video_id, PipelineStatus.AWAITING_REVIEW)
                script_path = self.runs_dir / video_id / "script.json"
                save_script(script, script_path)
                self.store.resolve_failures(video_id, "redraft")
                return DraftResult(video_id, script, script_path, report)
            except Exception as exc:
                self._record_failure(video_id, "redraft", exc)
                raise

    def approve(self, video_id: str) -> None:
        approve_video(self.store, video_id)

    def narration(
        self,
        video_id: str,
        adapter: NarrationAdapter,
        audio_files: Sequence[Path] = (),
    ) -> Path:
        with self.store.operation_lease(
            f"narration:{video_id}", ttl=timedelta(minutes=30)
        ) as acquired:
            if not acquired:
                raise PipelineError(f"Narration is already running for {video_id}")
            temporary_path: Path | None = None
            try:
                video = self.store.get_video(video_id)
                if video["status"] != PipelineStatus.APPROVED.value:
                    raise PipelineError("Narration requires an approved video")
                script = json.loads(video["script_json"])
                if script["narration_mode"] != adapter.mode:
                    raise PipelineError(
                        f"Script narration_mode is {script['narration_mode']!r}, but adapter mode is {adapter.mode!r}"
                    )
                suffix = audio_files[0].suffix if audio_files else ".wav"
                output_path = self.runs_dir / video_id / f"narration{suffix}"
                output_path.parent.mkdir(parents=True, exist_ok=True)
                temporary_path = output_path.with_name(
                    f".{output_path.stem}-{uuid4().hex}{output_path.suffix}"
                )
                adapter.create(script, temporary_path, audio_files)
                if not temporary_path.is_file() or temporary_path.stat().st_size == 0:
                    raise PipelineError("Narration adapter did not create a non-empty audio file")
                temporary_path.replace(output_path)
                temporary_path = None
                if adapter.mode == "recorded":
                    credit = {
                        "source": "User-supplied recording",
                        "license_name": "owned",
                        "attribution": "Narration supplied by the channel owner",
                    }
                else:
                    credit = {
                        "source": f"Local TTS adapter: {type(adapter).__name__}",
                        "license_name": "ai_generated",
                        "attribution": "Synthetic narration generated locally",
                    }
                self.store.record_asset_credit(video_id, "narration", output_path, **credit)
                self.store.transition(video_id, PipelineStatus.NARRATED)
                self.store.resolve_failures(video_id, "narration")
                return output_path
            except Exception as exc:
                self._record_failure(video_id, "narration", exc)
                raise
            finally:
                if temporary_path is not None:
                    temporary_path.unlink(missing_ok=True)

    def render(
        self,
        video_id: str,
        image_dir: Path,
        narration_path: Path,
        *,
        music_path: Path | None = None,
        settings: RenderSettings = RenderSettings(),
    ) -> Path:
        with self.store.operation_lease(
            f"render:{video_id}", ttl=timedelta(hours=1)
        ) as acquired:
            if not acquired:
                raise PipelineError(f"Rendering is already running for {video_id}")
            temporary_path: Path | None = None
            try:
                video = self.store.get_video(video_id)
                if video["status"] != PipelineStatus.NARRATED.value:
                    raise PipelineError("Rendering requires completed narration and prior approval")
                profile = self.profile(video["channel"])
                if settings.output_format not in profile.output_formats:
                    raise PipelineError(
                        f"Output format {settings.output_format} is not enabled for {profile.profile_id}: "
                        f"{', '.join(profile.output_formats)}"
                    )
                script = json.loads(video["script_json"])
                music_credit = load_music_credit(music_path) if music_path is not None else None
                image_paths = validate_manual_images(script, image_dir)
                for scene, path in zip(script["scenes"], image_paths, strict=True):
                    self.store.record_asset(video_id, scene, path)
                if music_path is not None and music_credit is not None:
                    self.store.record_asset_credit(
                        video_id,
                        "music",
                        music_path,
                        source=music_credit["source"],
                        license_name=music_credit["license"],
                        attribution=music_credit["attribution"],
                    )
                output_path = self.runs_dir / video_id / "video.mp4"
                output_path.parent.mkdir(parents=True, exist_ok=True)
                temporary_path = output_path.with_name(f".video-{uuid4().hex}.mp4")
                render_slideshow(
                    script,
                    image_dir,
                    narration_path,
                    temporary_path,
                    music_path=music_path,
                    settings=settings,
                    lock_path=self.workspace / ".render.lock",
                )
                temporary_path.replace(output_path)
                temporary_path = None
                self.store.transition(video_id, PipelineStatus.RENDERED)
                self.store.resolve_failures(video_id, "render")
                return output_path
            except Exception as exc:
                self._record_failure(video_id, "render", exc)
                raise
            finally:
                if temporary_path is not None:
                    temporary_path.unlink(missing_ok=True)

    def export(self, video_id: str, video_path: Path) -> Path:
        try:
            video = self.store.get_video(video_id)
            script = json.loads(video["script_json"])
            profile = self.profile(video["channel"])
            result = export_package(self.store, video_id, script, video_path, profile, self.outputs_dir)
            self.store.resolve_failures(video_id, "export")
            return result
        except Exception as exc:
            self._record_failure(video_id, "export", exc)
            raise

    def duplicate_report(self, video_id: str) -> DuplicateReport:
        video = self.store.get_video(video_id)
        script = json.loads(video["script_json"])
        profile = self.profile(video["channel"])
        return check_duplicate(script, self.store.list_recent(video["channel"], 30, exclude_id=video_id), profile)

