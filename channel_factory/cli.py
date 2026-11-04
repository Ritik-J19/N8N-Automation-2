from __future__ import annotations

import argparse
from importlib.metadata import PackageNotFoundError, version
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import shutil
import sys
import wave
from typing import Any, Sequence

from PIL import Image, ImageDraw

from channel_factory.config import load_profile
from channel_factory.core.pipeline import Pipeline
from channel_factory.core.schema import ScriptValidationError, load_and_validate_script
from channel_factory.core.state import PipelineStatus
from channel_factory.review.approval import format_review, interactive_review
from channel_factory.services.assemble import AssemblyError, RenderSettings, find_ffmpeg
from channel_factory.services.llm import GeminiAdapter, LLMError, MockLLM
from channel_factory.services.tts import EspeakNarration, RecordedNarration


PACKAGE_ROOT = Path(__file__).resolve().parent


def configure_logging(workspace: Path | None = None) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if workspace is not None:
        log_dir = workspace / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        handlers.append(
            RotatingFileHandler(
                log_dir / "channel-factory.log",
                maxBytes=2 * 1024 * 1024,
                backupCount=3,
                encoding="utf-8",
            )
        )
    logging.basicConfig(
        level=os.getenv("CHANNEL_FACTORY_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers,
        force=True,
    )


def _load_sample(name: str = "history_script.json") -> dict[str, Any]:
    return json.loads((PACKAGE_ROOT / "samples" / name).read_text(encoding="utf-8"))


def phase0_report() -> dict[str, Any]:
    try:
        sdk_version = version("google-genai")
        from google import genai  # noqa: F401

        sdk_import = "ok"
    except (PackageNotFoundError, ImportError) as exc:
        sdk_version = None
        sdk_import = f"failed: {exc}"
    system_ffmpeg = shutil.which("ffmpeg")
    try:
        ffmpeg = str(find_ffmpeg())
        ffmpeg_status = "ok"
    except AssemblyError as exc:
        ffmpeg = None
        ffmpeg_status = f"failed: {exc}"
    return {
        "python": sys.version.split()[0],
        "system_ffmpeg": system_ffmpeg,
        "usable_ffmpeg": ffmpeg,
        "ffmpeg_status": ffmpeg_status,
        "google_genai_version": sdk_version,
        "google_genai_import": sdk_import,
        "gemini_live_test": "SKIPPED: Phase 0 never makes a live call; GEMINI_API_KEY is not required",
        "tts_evaluation": {
            "windows_sapi": {
                "install_friction": "low: built into Windows and needs no voice-model download",
                "sound_quality": "not measurable here: the installed SAPI voice failed to initialize",
                "selected": False,
            },
            "piper": {
                "install_friction": "medium: executable/runtime plus a separate voice model; Windows setup varies",
                "sound_quality": "not measured: runtime installed, but the official voice host returned HTTP 403",
                "selected": False,
            },
            "espeak_ng": {
                "install_friction": "low: a 9.4 MB Windows wheel includes the DLL and voice data",
                "sound_quality": "intelligible but robotic; verified by generating a valid local WAV",
                "selected": True,
            },
        },
    }


def _sample_images(script: dict[str, Any], image_dir: Path) -> None:
    image_dir.mkdir(parents=True, exist_ok=True)
    palette = [(23, 74, 92), (116, 69, 45), (45, 89, 61)]
    for index, scene in enumerate(script["scenes"]):
        image = Image.new("RGB", (1280, 720), palette[index % len(palette)])
        draw = ImageDraw.Draw(image)
        draw.rectangle((80, 80, 1200, 640), outline=(240, 230, 195), width=8)
        draw.text((120, 120), f"Owned sample image\n{scene['scene_id']}", fill=(255, 255, 255))
        image.save(image_dir / scene["image_file"], format="PNG")


def _silent_wav(path: Path, seconds: float, sample_rate: int = 16_000) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    frames = int(seconds * sample_rate)
    chunk = b"\x00\x00" * min(frames, sample_rate)
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(sample_rate)
        remaining = frames
        while remaining:
            count = min(remaining, sample_rate)
            output.writeframes(chunk[: count * 2])
            remaining -= count
    return path


def _llm_for(name: str, pipeline: Pipeline) -> MockLLM | GeminiAdapter:
    return MockLLM(_load_sample()) if name == "mock" else GeminiAdapter(pipeline.store)


def _video_summary(video: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": video["id"],
        "channel": video["channel"],
        "topic_id": video["topic_id"],
        "title": video["title"],
        "status": video["status"],
        "created_at": video["created_at"],
        "updated_at": video["updated_at"],
        "approved_at": video["approved_at"],
        "exported_at": video["exported_at"],
        "rejection_reason": video["rejection_reason"],
    }


def _status_payload(pipeline: Pipeline, video_id: str) -> dict[str, Any]:
    video = pipeline.store.get_video(video_id)
    next_actions = {
        PipelineStatus.DRAFTED.value: ["internal safety pass"],
        PipelineStatus.SAFETY_CHECKED.value: ["internal review preparation"],
        PipelineStatus.AWAITING_REVIEW.value: ["review"],
        PipelineStatus.APPROVED.value: ["narrate"],
        PipelineStatus.NARRATED.value: ["render"],
        PipelineStatus.RENDERED.value: ["export"],
        PipelineStatus.EXPORTING.value: ["export (retry after recovery window)"],
        PipelineStatus.EXPORTED.value: [],
        PipelineStatus.REJECTED.value: ["redraft"],
    }
    run_dir = pipeline.runs_dir / video_id
    export_matches = sorted(pipeline.outputs_dir.glob(f"*-{video_id[:8]}")) if pipeline.outputs_dir.exists() else []
    return {
        **_video_summary(video),
        "next_actions": next_actions[video["status"]],
        "events": pipeline.store.list_events(video_id),
        "failures": pipeline.store.list_failures(video_id),
        "schema_version": pipeline.store.schema_version(),
        "artifacts": {
            "script": str(run_dir / "script.json") if (run_dir / "script.json").is_file() else None,
            "narration": [str(path) for path in sorted(run_dir.glob("narration.*"))],
            "video": str(run_dir / "video.mp4") if (run_dir / "video.mp4").is_file() else None,
            "exports": [str(path) for path in export_matches],
        },
    }


def _default_narration_path(pipeline: Pipeline, video_id: str) -> Path:
    matches = sorted((pipeline.runs_dir / video_id).glob("narration.*"))
    if len(matches) != 1:
        raise RuntimeError("Specify --narration because exactly one prepared narration file was not found")
    return matches[0]


def run_demo(workspace: Path, *, approve: bool, use_tts: bool = False) -> Path | None:
    configure_logging(workspace)
    pipeline = Pipeline(workspace)
    sample = _load_sample()
    if use_tts:
        sample["narration_mode"] = "tts"
        sample["disclosure_required"] = True
    result = pipeline.draft("history_en", MockLLM(sample))
    print(format_review(result.script, result.duplicate_report))
    if not approve:
        print(f"\nDraft saved at {result.script_path}. Re-run with --approve to continue.")
        return None
    pipeline.approve(result.video_id)
    run_dir = workspace / "runs" / result.video_id
    image_dir = run_dir / "images"
    _sample_images(result.script, image_dir)
    if use_tts:
        narration = pipeline.narration(result.video_id, EspeakNarration())
    else:
        seconds = sum(float(scene["duration_sec"]) for scene in result.script["scenes"])
        recorded = _silent_wav(run_dir / "sample-recording.wav", seconds)
        narration = pipeline.narration(result.video_id, RecordedNarration(), [recorded])
    video = pipeline.render(
        result.video_id,
        image_dir,
        narration,
        settings=RenderSettings(output_format="16:9", threads=2),
    )
    package = pipeline.export(result.video_id, video)
    print(f"\nExported MockLLM sample package: {package}")
    return package


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="channel-factory", description="Human-reviewed video drafting pipeline")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("phase0-check", help="Check local dependencies without a live Gemini call")

    validate = subparsers.add_parser("validate", help="Validate a script JSON file")
    validate.add_argument("script", type=Path)
    validate.add_argument("--profile", default="history_en")

    demo = subparsers.add_parser("demo", help="Run the sample workflow through MockLLM")
    demo.add_argument("--workspace", type=Path, default=Path("demo-workspace"))
    demo.add_argument("--approve", action="store_true", help="Explicitly approve the sample before rendering")
    demo.add_argument("--tts", action="store_true", help="Use local eSpeak NG instead of sample recorded audio")

    draft = subparsers.add_parser("draft", help="Create a draft and safety pass")
    draft.add_argument("--workspace", type=Path, default=Path("channel_factory/data"))
    draft.add_argument("--profile", default="history_en")
    draft.add_argument("--llm", choices=("mock", "gemini"), default="mock")

    review = subparsers.add_parser("review", help="Review an awaiting-review draft")
    review.add_argument("video_id")
    review.add_argument("--workspace", type=Path, default=Path("channel_factory/data"))

    redraft = subparsers.add_parser("redraft", help="Regenerate a rejected draft using its rejection feedback")
    redraft.add_argument("video_id")
    redraft.add_argument("--workspace", type=Path, default=Path("channel_factory/data"))
    redraft.add_argument("--llm", choices=("mock", "gemini"), default="mock")

    list_command = subparsers.add_parser("list", help="List persisted workflow jobs")
    list_command.add_argument("--workspace", type=Path, default=Path("channel_factory/data"))
    list_command.add_argument("--status", choices=tuple(status.value for status in PipelineStatus))
    list_command.add_argument("--limit", type=int, default=50)
    list_command.add_argument("--json", action="store_true", dest="as_json")

    status_command = subparsers.add_parser("status", help="Show one job, its audit events, and artifacts as JSON")
    status_command.add_argument("video_id")
    status_command.add_argument("--workspace", type=Path, default=Path("channel_factory/data"))

    narrate = subparsers.add_parser("narrate", help="Prepare narration for an approved job")
    narrate.add_argument("video_id")
    narrate.add_argument("--workspace", type=Path, default=Path("channel_factory/data"))
    narrate.add_argument("--mode", choices=("recorded", "tts"), required=True)
    narrate.add_argument("--audio", type=Path, action="append", default=[])

    render = subparsers.add_parser("render", help="Render a narrated job with manual images")
    render.add_argument("video_id")
    render.add_argument("--workspace", type=Path, default=Path("channel_factory/data"))
    render.add_argument("--images", type=Path, required=True)
    render.add_argument("--narration", type=Path)
    render.add_argument("--music", type=Path)
    render.add_argument("--format", choices=("16:9", "9:16"), default="16:9", dest="output_format")
    render.add_argument("--threads", type=int, default=2)

    export = subparsers.add_parser("export", help="Create or recover an idempotent manual upload package")
    export.add_argument("video_id")
    export.add_argument("--workspace", type=Path, default=Path("channel_factory/data"))
    export.add_argument("--video", type=Path)

    serve = subparsers.add_parser("serve", help="Run the authenticated local operations console")
    serve.add_argument("--workspace", type=Path, default=Path("channel_factory/data"))
    serve.add_argument("--port", type=int, default=8765)

    backup = subparsers.add_parser("backup", help="Create, list, or verify database backups")
    backup.add_argument("--workspace", type=Path, default=Path("channel_factory/data"))
    backup_commands = backup.add_subparsers(dest="backup_command", required=True)
    backup_commands.add_parser("create", help="Create and verify a new online backup")
    backup_commands.add_parser("list", help="List verified backup files")
    verify = backup_commands.add_parser("verify", help="Verify one backup file")
    verify.add_argument("path", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "phase0-check":
            print(json.dumps(phase0_report(), indent=2))
            return 0
        if args.command == "validate":
            profile = load_profile(args.profile)
            load_and_validate_script(args.script, profile)
            print(f"Valid: {args.script}")
            return 0
        if args.command == "demo":
            package = run_demo(args.workspace, approve=args.approve, use_tts=args.tts)
            return 0 if package else 2
        if args.command == "draft":
            configure_logging(args.workspace)
            pipeline = Pipeline(args.workspace)
            result = pipeline.draft(args.profile, _llm_for(args.llm, pipeline))
            print(format_review(result.script, result.duplicate_report))
            print(f"\nDraft ID: {result.video_id}\nScript: {result.script_path}")
            return 0
        if args.command == "review":
            configure_logging(args.workspace)
            pipeline = Pipeline(args.workspace)
            video = pipeline.store.get_video(args.video_id)
            profile = pipeline.profile(video["channel"])
            script = json.loads(video["script_json"])
            path = pipeline.runs_dir / args.video_id / "script.json"
            result = interactive_review(
                pipeline.store, args.video_id, path, script, profile, pipeline.duplicate_report(args.video_id)
            )
            print(result)
            return 0
        if args.command == "redraft":
            configure_logging(args.workspace)
            pipeline = Pipeline(args.workspace)
            result = pipeline.redraft_rejected(args.video_id, _llm_for(args.llm, pipeline))
            print(format_review(result.script, result.duplicate_report))
            print(f"\nRedrafted ID: {result.video_id}\nScript: {result.script_path}")
            return 0
        if args.command == "list":
            pipeline = Pipeline(args.workspace)
            videos = [_video_summary(video) for video in pipeline.store.list_videos(status=args.status, limit=args.limit)]
            if args.as_json:
                print(json.dumps(videos, indent=2))
            elif not videos:
                print("No workflow jobs found.")
            else:
                for video in videos:
                    print(f"{video['id']}  {video['status']:<16}  {video['channel']:<20}  {video['title']}")
            return 0
        if args.command == "status":
            pipeline = Pipeline(args.workspace)
            print(json.dumps(_status_payload(pipeline, args.video_id), indent=2))
            return 0
        if args.command == "narrate":
            configure_logging(args.workspace)
            pipeline = Pipeline(args.workspace)
            adapter = RecordedNarration() if args.mode == "recorded" else EspeakNarration()
            narration = pipeline.narration(args.video_id, adapter, args.audio)
            print(narration)
            return 0
        if args.command == "render":
            configure_logging(args.workspace)
            pipeline = Pipeline(args.workspace)
            narration = args.narration or _default_narration_path(pipeline, args.video_id)
            video = pipeline.render(
                args.video_id,
                args.images,
                narration,
                music_path=args.music,
                settings=RenderSettings(output_format=args.output_format, threads=args.threads),
            )
            print(video)
            return 0
        if args.command == "export":
            configure_logging(args.workspace)
            pipeline = Pipeline(args.workspace)
            video = args.video or pipeline.runs_dir / args.video_id / "video.mp4"
            print(pipeline.export(args.video_id, video))
            return 0
        if args.command == "serve":
            if not 1 <= args.port <= 65535:
                raise ValueError("port must be between 1 and 65535")
            configure_logging(args.workspace)
            from channel_factory.web import create_app
            import uvicorn

            app = create_app(args.workspace)
            print(f"Channel Factory console: http://127.0.0.1:{args.port}")
            uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="info")
            return 0
        if args.command == "backup":
            pipeline = Pipeline(args.workspace)
            backup_dir = args.workspace.resolve() / "backups"
            if args.backup_command == "create":
                from datetime import UTC, datetime

                stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S-%f")
                path = pipeline.store.backup(backup_dir / f"channel-factory-{stamp}.sqlite3")
                print(json.dumps(pipeline.store.verify_backup(path), indent=2))
                return 0
            if args.backup_command == "list":
                backups = [
                    pipeline.store.verify_backup(path)
                    for path in sorted(backup_dir.glob("channel-factory-*.sqlite3"), reverse=True)
                ] if backup_dir.exists() else []
                print(json.dumps(backups, indent=2))
                return 0
            if args.backup_command == "verify":
                print(json.dumps(pipeline.store.verify_backup(args.path), indent=2))
                return 0
    except (AssemblyError, LLMError, ScriptValidationError, RuntimeError, ValueError) as exc:
        logging.getLogger(__name__).error("%s", exc)
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

