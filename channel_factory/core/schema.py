from __future__ import annotations

from copy import deepcopy
import json
import math
import os
from pathlib import Path
import tempfile
from typing import Any

from channel_factory.config import Profile


LICENSES = {"public_domain", "cc_by", "cc_by_sa", "owned", "ai_generated"}
NARRATION_MODES = {"recorded", "tts"}
BASE_REQUIRED = {
    "title",
    "channel",
    "content_type",
    "language",
    "topic_id",
    "summary",
    "scenes",
    "safety_flags",
    "disclosure_required",
    "narration_mode",
}
MAX_SCRIPT_BYTES = 1_000_000
MAX_SCENES = 60
MAX_TOTAL_DURATION_SECONDS = 3_600
STRING_LIMITS = {
    "title": 300,
    "channel": 64,
    "content_type": 100,
    "language": 32,
    "topic_id": 160,
    "summary": 8_000,
}
SCENE_STRING_LIMITS = {
    "scene_id": 120,
    "narration": 12_000,
    "on_screen_text": 1_000,
    "image_prompt": 8_000,
    "image_file": 240,
}
RESERVED_ASSET_IDS = {"music", "narration"}


class ScriptValidationError(ValueError):
    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        super().__init__("Script validation failed:\n- " + "\n- ".join(errors))


def _required_string(data: dict[str, Any], key: str, errors: list[str]) -> None:
    if not isinstance(data.get(key), str) or not data[key].strip():
        errors.append(f"{key} must be a non-empty string")
    elif len(data[key]) > STRING_LIMITS.get(key, 8_000):
        errors.append(f"{key} must be {STRING_LIMITS.get(key, 8_000)} characters or fewer")


def validate_script(data: dict[str, Any], profile: Profile) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise ScriptValidationError(["root must be a JSON object"])
    script = deepcopy(data)
    errors: list[str] = []
    missing = sorted((BASE_REQUIRED | set(profile.required_extra_schema_fields)) - script.keys())
    errors.extend(f"missing required field: {key}" for key in missing)
    for key in ("title", "channel", "content_type", "language", "topic_id", "summary"):
        _required_string(script, key, errors)
    if script.get("channel") != profile.profile_id:
        errors.append(f"channel must be {profile.profile_id!r}")
    if script.get("content_type") != profile.content_type:
        errors.append(f"content_type must be {profile.content_type!r}")
    if script.get("language") != profile.language:
        errors.append(f"language must be {profile.language!r}")
    mode = script.get("narration_mode")
    if mode not in NARRATION_MODES:
        errors.append("narration_mode must be recorded or tts")
    if not isinstance(script.get("safety_flags"), list):
        errors.append("safety_flags must be a list")
    elif any(not isinstance(item, str) or not item.strip() for item in script["safety_flags"]):
        errors.append("safety_flags must contain only non-empty strings")
    if not isinstance(script.get("disclosure_required"), bool):
        errors.append("disclosure_required must be boolean")
    scenes = script.get("scenes")
    if not isinstance(scenes, list) or not scenes:
        errors.append("scenes must be a non-empty list")
        scenes = []
    elif len(scenes) > MAX_SCENES:
        errors.append(f"scenes must contain no more than {MAX_SCENES} items")
    scene_ids: set[str] = set()
    generated_image = False
    total_duration = 0.0
    for index, scene in enumerate(scenes):
        prefix = f"scenes[{index}]"
        if not isinstance(scene, dict):
            errors.append(f"{prefix} must be an object")
            continue
        for key in ("scene_id", "narration", "on_screen_text", "image_prompt", "image_file"):
            if not isinstance(scene.get(key), str):
                errors.append(f"{prefix}.{key} must be a string")
            elif len(scene[key]) > SCENE_STRING_LIMITS[key]:
                errors.append(
                    f"{prefix}.{key} must be {SCENE_STRING_LIMITS[key]} characters or fewer"
                )
        for key in ("scene_id", "narration", "image_file"):
            if isinstance(scene.get(key), str) and not scene[key].strip():
                errors.append(f"{prefix}.{key} must be non-empty")
        scene_id = scene.get("scene_id")
        if scene_id in scene_ids:
            errors.append(f"{prefix}.scene_id must be unique")
        if scene_id in RESERVED_ASSET_IDS:
            errors.append(f"{prefix}.scene_id is reserved for pipeline assets")
        if isinstance(scene_id, str):
            scene_ids.add(scene_id)
        duration = scene.get("duration_sec")
        if (
            not isinstance(duration, (int, float))
            or isinstance(duration, bool)
            or not math.isfinite(duration)
            or duration <= 0
        ):
            errors.append(f"{prefix}.duration_sec must be positive")
        else:
            total_duration += float(duration)
        credit = scene.get("image_credit")
        if not isinstance(credit, dict):
            errors.append(f"{prefix}.image_credit must be an object")
            continue
        for key in ("source", "license", "attribution"):
            if not isinstance(credit.get(key), str) or not credit[key].strip():
                errors.append(f"{prefix}.image_credit.{key} must be a non-empty string")
        license_name = credit.get("license")
        if license_name not in LICENSES:
            errors.append(f"{prefix}.image_credit.license must be one of {sorted(LICENSES)}")
        generated_image = generated_image or license_name == "ai_generated"
    if total_duration > MAX_TOTAL_DURATION_SECONDS:
        errors.append(
            f"total scene duration must be {MAX_TOTAL_DURATION_SECONDS} seconds or fewer"
        )
    if profile.profile_id == "history_en":
        for key in ("sources", "claims_to_verify"):
            if not isinstance(script.get(key), list):
                errors.append(f"{key} must be a list")
            elif any(not isinstance(item, str) or not item.strip() for item in script[key]):
                errors.append(f"{key} must contain only non-empty strings")
    if "tags" in script:
        if not isinstance(script["tags"], list) or any(
            not isinstance(item, str) or not item.strip() for item in script["tags"]
        ):
            errors.append("tags must be a list of non-empty strings")
    if profile.made_for_kids:
        if not script.get("lesson"):
            errors.append("lesson is required for made-for-kids profiles")
        if not isinstance(script.get("characters"), list) or not script.get("characters"):
            errors.append("characters must be a non-empty list for made-for-kids profiles")
    required_disclosure = mode == "tts" or generated_image
    if required_disclosure and script.get("disclosure_required") is not True:
        errors.append("disclosure_required must be true for TTS or AI-generated images")
    try:
        encoded_size = len(
            json.dumps(script, ensure_ascii=False, allow_nan=False).encode("utf-8")
        )
        if encoded_size > MAX_SCRIPT_BYTES:
            errors.append(f"script JSON must be {MAX_SCRIPT_BYTES} bytes or fewer")
    except (TypeError, ValueError) as exc:
        errors.append(f"script must contain only finite JSON values: {exc}")
    if errors:
        raise ScriptValidationError(errors)
    return script


def load_and_validate_script(path: Path, profile: Profile) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ScriptValidationError([f"cannot read valid JSON from {path}: {exc}"]) from exc
    return validate_script(data, profile)


def save_script(script: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(script, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
    handle = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        newline="\n",
        prefix=f".{path.name}-",
        suffix=".tmp",
        dir=path.parent,
        delete=False,
    )
    temporary = Path(handle.name)
    try:
        with handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise

