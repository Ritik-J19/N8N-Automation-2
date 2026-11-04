from __future__ import annotations

from pathlib import Path
import wave

from channel_factory.services.tts import RecordedNarration


def _wav(path: Path, frames: int) -> Path:
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(8_000)
        output.writeframes(b"\x00\x00" * frames)
    return path


def test_recorded_narration_accepts_clips_with_different_lengths(tmp_path: Path) -> None:
    first = _wav(tmp_path / "first.wav", 800)
    second = _wav(tmp_path / "second.wav", 1_600)
    output = RecordedNarration().create({"scenes": []}, tmp_path / "joined.wav", [first, second])
    with wave.open(str(output), "rb") as joined:
        assert joined.getnframes() == 2_400

