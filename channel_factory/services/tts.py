from __future__ import annotations

from abc import abstractmethod
import base64
import ctypes
import os
from pathlib import Path
import shutil
import subprocess
from typing import Any, Protocol, Sequence


class NarrationError(RuntimeError):
    pass


class NarrationAdapter(Protocol):
    mode: str

    @abstractmethod
    def create(self, script: dict[str, Any], output_path: Path, audio_files: Sequence[Path] = ()) -> Path:
        """Create or prepare one narration audio file."""


class RecordedNarration:
    mode = "recorded"

    def create(self, script: dict[str, Any], output_path: Path, audio_files: Sequence[Path] = ()) -> Path:
        if not audio_files:
            raise NarrationError("Recorded narration requires at least one audio file")
        missing = [path for path in audio_files if not path.is_file()]
        if missing:
            raise NarrationError(f"Recorded narration files not found: {missing}")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        if len(audio_files) == 1:
            source = audio_files[0]
            if source.resolve() != output_path.resolve():
                shutil.copy2(source, output_path)
            return output_path
        if any(path.suffix.lower() != ".wav" for path in audio_files):
            raise NarrationError("Multiple recorded files must be WAV; provide one mixed file for other formats")
        import wave

        with wave.open(str(audio_files[0]), "rb") as first:
            params = first.getparams()
            frames = [first.readframes(first.getnframes())]
        for path in audio_files[1:]:
            with wave.open(str(path), "rb") as current:
                current_params = current.getparams()
                if current_params[:3] != params[:3] or current_params[4:] != params[4:]:
                    raise NarrationError("Recorded WAV files must use the same channels, width, and sample rate")
                frames.append(current.readframes(current.getnframes()))
        with wave.open(str(output_path), "wb") as target:
            target.setparams(params)
            for chunk in frames:
                target.writeframes(chunk)
        return output_path


class WindowsSapiNarration:
    """CPU-only Windows speech adapter using the built-in SAPI voices."""

    mode = "tts"

    def __init__(self, timeout_seconds: int = 180) -> None:
        self.timeout_seconds = timeout_seconds

    def create(self, script: dict[str, Any], output_path: Path, audio_files: Sequence[Path] = ()) -> Path:
        del audio_files
        executable = shutil.which("powershell.exe")
        if not executable:
            raise NarrationError("Windows PowerShell is required for the SAPI adapter")
        text = " ".join(str(scene["narration"]) for scene in script["scenes"])
        output_path.parent.mkdir(parents=True, exist_ok=True)
        ps_script = """
Add-Type -AssemblyName System.Speech
$speaker = New-Object System.Speech.Synthesis.SpeechSynthesizer
$speaker.SetOutputToWaveFile($env:CF_TTS_OUTPUT)
$speaker.Speak($env:CF_TTS_TEXT)
$speaker.Dispose()
""".strip()
        encoded = base64.b64encode(ps_script.encode("utf-16-le")).decode("ascii")
        environment = os.environ.copy()
        environment["CF_TTS_OUTPUT"] = str(output_path.resolve())
        environment["CF_TTS_TEXT"] = text
        result = subprocess.run(
            [executable, "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
            capture_output=True,
            text=True,
            timeout=self.timeout_seconds,
            env=environment,
            check=False,
        )
        if result.returncode or not output_path.is_file():
            detail = (result.stderr or result.stdout).strip()
            raise NarrationError(f"Windows SAPI failed: {detail or result.returncode}")
        return output_path


class EspeakNarration:
    """Small, deterministic CPU-only adapter backed by the eSpeak NG DLL."""

    mode = "tts"

    def __init__(self, voice: str = "en-us") -> None:
        self.voice = voice

    def create(self, script: dict[str, Any], output_path: Path, audio_files: Sequence[Path] = ()) -> Path:
        del audio_files
        try:
            import espeakng_loader
        except ImportError as exc:
            raise NarrationError("Install espeakng-loader for local TTS") from exc
        text = " ".join(str(scene["narration"]) for scene in script["scenes"])
        encoded = text.encode("utf-8")
        library = espeakng_loader.load_library()
        library.espeak_Initialize.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_char_p, ctypes.c_int]
        library.espeak_Initialize.restype = ctypes.c_int
        library.espeak_SetVoiceByName.argtypes = [ctypes.c_char_p]
        library.espeak_SetVoiceByName.restype = ctypes.c_int
        callback_type = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.POINTER(ctypes.c_short), ctypes.c_int, ctypes.c_void_p)
        chunks: list[bytes] = []

        @callback_type
        def callback(samples: ctypes.POINTER(ctypes.c_short), sample_count: int, events: ctypes.c_void_p) -> int:
            del events
            if samples and sample_count > 0:
                chunks.append(ctypes.string_at(samples, sample_count * ctypes.sizeof(ctypes.c_short)))
            return 0

        library.espeak_SetSynthCallback.argtypes = [callback_type]
        library.espeak_SetSynthCallback(callback)
        data_path = str(espeakng_loader.get_data_path()).encode("utf-8")
        sample_rate = library.espeak_Initialize(1, 0, data_path, 0)
        if sample_rate <= 0:
            raise NarrationError(f"eSpeak NG initialization failed with code {sample_rate}")
        try:
            if library.espeak_SetVoiceByName(self.voice.encode("ascii")) != 0:
                raise NarrationError(f"eSpeak NG voice is unavailable: {self.voice}")
            library.espeak_Synth.argtypes = [
                ctypes.c_void_p,
                ctypes.c_size_t,
                ctypes.c_uint,
                ctypes.c_int,
                ctypes.c_uint,
                ctypes.c_uint,
                ctypes.POINTER(ctypes.c_uint),
                ctypes.c_void_p,
            ]
            identifier = ctypes.c_uint(0)
            buffer = ctypes.create_string_buffer(encoded + b"\x00")
            result = library.espeak_Synth(
                buffer,
                len(buffer),
                0,
                0,
                0,
                1 | 0x1000,
                ctypes.byref(identifier),
                None,
            )
            if result != 0:
                raise NarrationError(f"eSpeak NG synthesis failed with code {result}")
            library.espeak_Synchronize()
        finally:
            library.espeak_Terminate()
        if not chunks:
            raise NarrationError("eSpeak NG produced no audio")
        import wave

        output_path.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(output_path), "wb") as target:
            target.setnchannels(1)
            target.setsampwidth(2)
            target.setframerate(sample_rate)
            for chunk in chunks:
                target.writeframesraw(chunk)
        return output_path


class PiperNarration:
    """Optional Piper adapter; the voice model is user-supplied."""

    mode = "tts"

    def __init__(self, model: Path) -> None:
        self.model = model

    def create(self, script: dict[str, Any], output_path: Path, audio_files: Sequence[Path] = ()) -> Path:
        del audio_files
        if not self.model.is_file():
            raise NarrationError("Piper voice model must exist")
        try:
            from piper import PiperVoice
        except ImportError as exc:
            raise NarrationError('Install Piper with pip install -e ".[piper]"') from exc
        output_path.parent.mkdir(parents=True, exist_ok=True)
        text = " ".join(str(scene["narration"]) for scene in script["scenes"])
        import wave

        try:
            voice = PiperVoice.load(str(self.model), use_cuda=False)
            with wave.open(str(output_path), "wb") as target:
                voice.synthesize_wav(text, target)
        except Exception as exc:
            output_path.unlink(missing_ok=True)
            raise NarrationError(f"Piper failed: {exc}") from exc
        return output_path

