from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path

import pytest

from channel_factory.cli import main, run_demo
from channel_factory.core.pipeline import Pipeline, PipelineError
from channel_factory.config import load_profile
from channel_factory.services.export import ExportError, export_package
from channel_factory.services.assemble import AssemblyError, RenderSettings
from channel_factory.services.llm import MockLLM
from channel_factory.services.publish_youtube import build_request_body
from channel_factory.services.tts import EspeakNarration, RecordedNarration


SAMPLE = Path(__file__).resolve().parents[1] / "samples" / "history_script.json"


def sample() -> dict:
    return json.loads(SAMPLE.read_text(encoding="utf-8"))


def test_approval_gate_blocks_narration(tmp_path: Path) -> None:
    pipeline = Pipeline(tmp_path)
    result = pipeline.draft("history_en", MockLLM(sample()))
    with pytest.raises(PipelineError, match="approved"):
        pipeline.narration(result.video_id, RecordedNarration(), [])


def test_youtube_body_records_audience_and_disclosure() -> None:
    script = sample()
    script["disclosure_required"] = True
    body = build_request_body(script, load_profile("history_en"))
    assert body["status"]["privacyStatus"] == "private"
    assert body["status"]["containsSyntheticMedia"] is True
    assert body["status"]["selfDeclaredMadeForKids"] is False


def test_espeak_creates_local_wav(tmp_path: Path) -> None:
    output = EspeakNarration().create(sample(), tmp_path / "speech.wav")
    assert output.stat().st_size > 10_000
    assert output.read_bytes()[:4] == b"RIFF"


def test_export_refuses_weekly_posting_cap(tmp_path: Path) -> None:
    pipeline = Pipeline(tmp_path)
    first_id = pipeline.store.create_draft(sample())
    for status in ("safety_checked", "awaiting_review", "approved", "narrated", "rendered", "exporting", "exported"):
        pipeline.store.transition(first_id, status)
    second = sample()
    second["topic_id"] = "hist-postal-roads"
    second["title"] = "A Different Draft"
    second_id = pipeline.store.create_draft(second)
    for status in ("safety_checked", "awaiting_review", "approved", "narrated", "rendered"):
        pipeline.store.transition(second_id, status)
    video = tmp_path / "video.mp4"
    video.write_bytes(b"sample")
    profile = replace(load_profile("history_en"), posting_cap_per_week=1)
    with pytest.raises(ExportError, match="posting cap"):
        export_package(pipeline.store, second_id, second, video, profile, tmp_path / "outputs")


def test_mock_end_to_end_exports_video_and_metadata(tmp_path: Path) -> None:
    package = run_demo(tmp_path / "demo", approve=True)
    assert package is not None
    assert (package / "video.mp4").stat().st_size > 1_000
    metadata = json.loads((package / "metadata.json").read_text(encoding="utf-8"))
    assert metadata["manual_upload_required"] is True
    assert (package / "credits.csv").is_file()
    assert (package / "manifest.json").is_file()
    pipeline = Pipeline(tmp_path / "demo")
    video = pipeline.store.list_videos(limit=1)[0]
    assets = pipeline.store.list_assets(video["id"])
    assert {asset["asset_id"] for asset in assets} == {"narration", "scene-1", "scene-2"}
    assert pipeline.export(video["id"], package / "video.mp4") == package
    assert pipeline.export(video["id"], tmp_path / "missing-intermediate.mp4") == package
    (package / "metadata.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ExportError, match="matching manifest"):
        pipeline.export(video["id"], package / "video.mp4")


def test_profile_blocks_unconfigured_output_format(tmp_path: Path) -> None:
    pipeline = Pipeline(tmp_path)
    video_id = pipeline.store.create_draft(sample())
    for status in ("safety_checked", "awaiting_review", "approved", "narrated"):
        pipeline.store.transition(video_id, status)
    with pytest.raises(PipelineError, match="not enabled"):
        pipeline.render(
            video_id,
            tmp_path / "images",
            tmp_path / "narration.wav",
            settings=RenderSettings(output_format="9:16"),
        )


def test_list_and_status_commands_are_machine_readable(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    pipeline = Pipeline(tmp_path)
    video_id = pipeline.store.create_draft(sample())
    assert main(["list", "--workspace", str(tmp_path), "--json"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert listed[0]["id"] == video_id
    assert main(["status", video_id, "--workspace", str(tmp_path)]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["events"][0]["to_status"] == "drafted"
    assert status["next_actions"] == ["internal safety pass"]


def test_music_requires_local_license_sidecar(tmp_path: Path) -> None:
    pipeline = Pipeline(tmp_path)
    video_id = pipeline.store.create_draft(sample())
    for status in ("safety_checked", "awaiting_review", "approved", "narrated"):
        pipeline.store.transition(video_id, status)
    music = tmp_path / "music.mp3"
    music.write_bytes(b"not needed because license validation runs first")
    with pytest.raises(AssemblyError, match="license sidecar"):
        pipeline.render(video_id, tmp_path / "images", tmp_path / "narration.wav", music_path=music)


def test_failed_export_releases_reservation(tmp_path: Path) -> None:
    pipeline = Pipeline(tmp_path)
    video_id = pipeline.store.create_draft(sample())
    for status in ("safety_checked", "awaiting_review", "approved", "narrated", "rendered"):
        pipeline.store.transition(video_id, status)
    with pytest.raises(ExportError, match="not found"):
        pipeline.export(video_id, tmp_path / "missing.mp4")
    assert pipeline.store.get_video(video_id)["status"] == "rendered"
    failures = pipeline.store.list_failures(video_id, active_only=True)
    assert failures[0]["operation"] == "export"
    assert failures[0]["error_type"] == "ExportError"


def test_export_uses_full_uuid_when_a_short_prefix_folder_is_already_claimed(
    tmp_path: Path,
) -> None:
    pipeline = Pipeline(tmp_path)
    current = sample()
    video_id = pipeline.store.create_draft(current)
    for state in ("safety_checked", "awaiting_review", "approved", "narrated", "rendered"):
        pipeline.store.transition(video_id, state)
    video = tmp_path / "video.mp4"
    video.write_bytes(b"rendered-video")
    short_folder = tmp_path / "outputs" / f"the-clockwork-sky-beneath-the-sea-{video_id[:8]}"
    short_folder.mkdir(parents=True)
    (short_folder / "manifest.json").write_text(
        json.dumps({"package_version": 1, "video_id": "different-job", "files": {}}),
        encoding="utf-8",
    )

    package = pipeline.export(video_id, video)
    assert package.name.endswith(video_id)
    assert short_folder.is_dir()

