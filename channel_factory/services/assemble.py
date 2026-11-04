from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import json
import logging
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import textwrap
from typing import Any, Iterator, Sequence

from PIL import Image, ImageDraw, ImageFont, ImageOps


LOGGER = logging.getLogger(__name__)


class AssemblyError(RuntimeError):
    pass


MUSIC_LICENSES = {"royalty_free", "public_domain", "cc_by", "cc_by_sa", "owned"}


@dataclass(frozen=True)
class RenderSettings:
    output_format: str = "16:9"
    threads: int = 2
    fps: int = 30

    @property
    def size(self) -> tuple[int, int]:
        if self.output_format == "16:9":
            return 1280, 720
        if self.output_format == "9:16":
            return 720, 1280
        raise AssemblyError("Output format must be 16:9 or 9:16")


def find_ffmpeg() -> Path:
    configured = os.getenv("CHANNEL_FACTORY_FFMPEG")
    if configured:
        path = Path(configured)
        if path.is_file():
            return path
        raise AssemblyError(f"CHANNEL_FACTORY_FFMPEG does not exist: {path}")
    system = shutil.which("ffmpeg")
    if system:
        return Path(system)
    try:
        import imageio_ffmpeg

        path = Path(imageio_ffmpeg.get_ffmpeg_exe())
    except (ImportError, RuntimeError) as exc:
        raise AssemblyError("FFmpeg is not on PATH and imageio-ffmpeg is unavailable") from exc
    if not path.is_file():
        raise AssemblyError(f"Bundled FFmpeg was not found: {path}")
    return path


def validate_manual_images(script: dict[str, Any], image_dir: Path) -> list[Path]:
    if not image_dir.is_dir():
        raise AssemblyError(f"Image folder not found: {image_dir}")
    resolved_image_dir = image_dir.resolve()
    paths: list[Path] = []
    for scene in script["scenes"]:
        relative = Path(scene["image_file"])
        if relative.is_absolute() or ".." in relative.parts:
            raise AssemblyError(f"Unsafe image_file path: {relative}")
        path = (resolved_image_dir / relative).resolve()
        try:
            path.relative_to(resolved_image_dir)
        except ValueError as exc:
            raise AssemblyError(f"Image path escapes the selected folder: {relative}") from exc
        if not path.is_file():
            raise AssemblyError(f"Image missing for {scene['scene_id']}: {path}")
        try:
            with Image.open(path) as image:
                image.verify()
        except Exception as exc:
            raise AssemblyError(f"Invalid image for {scene['scene_id']}: {path}: {exc}") from exc
        paths.append(path)
    return paths


def load_music_credit(music_path: Path) -> dict[str, str]:
    sidecar = Path(f"{music_path}.license.json")
    if not sidecar.is_file():
        raise AssemblyError(f"Music license sidecar not found: {sidecar}")
    try:
        credit = json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AssemblyError(f"Music license sidecar is invalid: {sidecar}: {exc}") from exc
    if not isinstance(credit, dict):
        raise AssemblyError(f"Music license sidecar must be a JSON object: {sidecar}")
    for key in ("source", "license", "attribution"):
        if not isinstance(credit.get(key), str) or not credit[key].strip():
            raise AssemblyError(f"Music license sidecar requires a non-empty {key}: {sidecar}")
    if credit["license"] not in MUSIC_LICENSES:
        raise AssemblyError(f"Music license must be one of {sorted(MUSIC_LICENSES)}")
    return {key: credit[key] for key in ("source", "license", "attribution")}


def _font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = [
        Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / "arial.ttf",
        Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / "segoeui.ttf",
    ]
    for path in candidates:
        if path.is_file():
            return ImageFont.truetype(str(path), size=size)
    return ImageFont.load_default()


def _prepare_frame(source: Path, target: Path, text: str, size: tuple[int, int]) -> None:
    with Image.open(source) as opened:
        frame = ImageOps.fit(opened.convert("RGB"), size, method=Image.Resampling.LANCZOS)
    if text.strip():
        draw = ImageDraw.Draw(frame, "RGBA")
        font_size = 40 if size[0] > size[1] else 34
        font = _font(font_size)
        max_chars = 48 if size[0] > size[1] else 28
        lines = textwrap.wrap(text.strip(), width=max_chars)[:3]
        rendered = "\n".join(lines)
        box = draw.multiline_textbbox((0, 0), rendered, font=font, spacing=8, align="center")
        width = box[2] - box[0]
        height = box[3] - box[1]
        x = (size[0] - width) // 2
        y = size[1] - height - 52
        padding = 18
        draw.rounded_rectangle(
            (x - padding, y - padding, x + width + padding, y + height + padding),
            radius=8,
            fill=(0, 0, 0, 178),
        )
        draw.multiline_text((x, y), rendered, font=font, fill="white", spacing=8, align="center")
    frame.save(target, format="PNG", optimize=True)


@contextmanager
def _render_lock(lock_path: Path) -> Iterator[None]:
    """Hold a kernel-backed, crash-released lock for the workspace renderer."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+b")
    handle.seek(0, os.SEEK_END)
    if handle.tell() == 0:
        handle.write(b"\0")
        handle.flush()
    handle.seek(0)
    try:
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        handle.close()
        raise AssemblyError(f"Another render is active ({lock_path})") from exc
    try:
        yield
    finally:
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def _video_filters(scene_count: int, durations: Sequence[float], settings: RenderSettings) -> str:
    width, height = settings.size
    filters: list[str] = []
    labels: list[str] = []
    for index, duration in enumerate(durations):
        frames = max(1, round(duration * settings.fps))
        label = f"v{index}"
        filters.append(
            f"[{index}:v]zoompan=z='min(zoom+0.00035,1.045)':x='iw/2-(iw/zoom/2)':"
            f"y='ih/2-(ih/zoom/2)':d={frames}:s={width}x{height}:fps={settings.fps},setsar=1[{label}]"
        )
        labels.append(f"[{label}]")
    filters.append(f"{''.join(labels)}concat=n={scene_count}:v=1:a=0,format=yuv420p[vout]")
    return ";".join(filters)


def render_slideshow(
    script: dict[str, Any],
    image_dir: Path,
    narration_path: Path,
    output_path: Path,
    *,
    music_path: Path | None = None,
    settings: RenderSettings = RenderSettings(),
    timeout_seconds: int = 300,
    lock_path: Path | None = None,
) -> Path:
    images = validate_manual_images(script, image_dir)
    if not narration_path.is_file():
        raise AssemblyError(f"Narration file not found: {narration_path}")
    if music_path is not None and not music_path.is_file():
        raise AssemblyError(f"Music file not found: {music_path}")
    if settings.threads < 1:
        raise AssemblyError("threads must be at least 1")
    durations = [float(scene["duration_sec"]) for scene in script["scenes"]]
    total_duration = sum(durations)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    render_lock_path = lock_path or output_path.parent / ".render.lock"
    ffmpeg = find_ffmpeg()
    with _render_lock(render_lock_path), tempfile.TemporaryDirectory(prefix="channel-factory-") as temp_name:
        temp_dir = Path(temp_name)
        prepared: list[Path] = []
        for index, (scene, source) in enumerate(zip(script["scenes"], images, strict=True)):
            target = temp_dir / f"scene-{index:03d}.png"
            _prepare_frame(source, target, str(scene.get("on_screen_text", "")), settings.size)
            prepared.append(target)
        command: list[str] = [str(ffmpeg), "-hide_banner", "-loglevel", "error", "-y"]
        for image, duration in zip(prepared, durations, strict=True):
            command.extend(["-loop", "1", "-t", str(duration), "-i", str(image)])
        narration_index = len(prepared)
        command.extend(["-i", str(narration_path)])
        music_index: int | None = None
        if music_path is not None:
            music_index = narration_index + 1
            command.extend(["-stream_loop", "-1", "-i", str(music_path)])
        filters = _video_filters(len(prepared), durations, settings)
        if music_index is None:
            filters += f";[{narration_index}:a]apad,atrim=0:{total_duration}[aout]"
        else:
            filters += (
                f";[{narration_index}:a]apad,atrim=0:{total_duration},asplit=2[voice][side]"
                f";[{music_index}:a]volume=0.20,atrim=0:{total_duration}[music]"
                ";[music][side]sidechaincompress=threshold=0.03:ratio=10:attack=20:release=400[ducked]"
                ";[voice][ducked]amix=inputs=2:duration=first:dropout_transition=0[aout]"
            )
        command.extend(
            [
                "-filter_complex", filters,
                "-map", "[vout]", "-map", "[aout]",
                "-c:v", "libx264", "-preset", "medium", "-crf", "21",
                "-c:a", "aac", "-b:a", "160k", "-threads", str(settings.threads),
                "-movflags", "+faststart", "-t", str(total_duration), str(output_path),
            ]
        )
        LOGGER.info("Rendering %s seconds to %s", total_duration, output_path)
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=timeout_seconds, check=False)
        except subprocess.TimeoutExpired as exc:
            output_path.unlink(missing_ok=True)
            raise AssemblyError(f"FFmpeg render timed out after {timeout_seconds} seconds") from exc
        if result.returncode or not output_path.is_file() or output_path.stat().st_size == 0:
            output_path.unlink(missing_ok=True)
            raise AssemblyError(f"FFmpeg render failed: {(result.stderr or result.stdout).strip()}")
    return output_path

