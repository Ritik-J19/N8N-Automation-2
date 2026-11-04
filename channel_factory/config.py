from __future__ import annotations

from dataclasses import dataclass, field
import math
import os
from pathlib import Path
import re
from typing import Any

import yaml


PACKAGE_ROOT = Path(__file__).resolve().parent
DEFAULT_PROFILES_ROOT = PACKAGE_ROOT / "profiles"
PROFILE_ID_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")


class ConfigError(ValueError):
    """Raised when a profile or environment setting is invalid."""


@dataclass(frozen=True)
class Profile:
    profile_id: str
    name: str
    language: str = "en"
    enabled: bool = True
    audience: str = ""
    tone: str = ""
    content_type: str = ""
    posting_cap_per_week: int = 3
    narration_mode_default: str = "recorded"
    visual_style: str = ""
    music_folder: str = ""
    safety_ruleset_id: str = "default"
    output_formats: tuple[str, ...] = ("16:9",)
    made_for_kids: bool = False
    disclosure_settings: dict[str, Any] = field(default_factory=dict)
    required_extra_schema_fields: tuple[str, ...] = ()
    duplicate_thresholds: dict[str, float] = field(default_factory=dict)
    structure_window: int = 4
    profile_dir: Path = Path(".")

    @classmethod
    def from_mapping(cls, profile_id: str, profile_dir: Path, raw: dict[str, Any]) -> "Profile":
        try:
            cap = int(raw.get("posting_cap_per_week", 3))
            structure_window = int(raw.get("structure_window", 4))
            thresholds = {
                str(k): float(v) for k, v in dict(raw.get("duplicate_thresholds", {})).items()
            }
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"Profile contains an invalid numeric setting: {exc}") from exc
        if cap < 1 or cap > 1_000:
            raise ConfigError("posting_cap_per_week must be between 1 and 1000")
        if structure_window < 1 or structure_window > 100:
            raise ConfigError("structure_window must be between 1 and 100")
        allowed_thresholds = {"title", "narration", "lesson_character"}
        if set(thresholds) - allowed_thresholds or any(
            not math.isfinite(value) or not 0 <= value <= 1 for value in thresholds.values()
        ):
            raise ConfigError(
                "duplicate_thresholds may contain title, narration, and lesson_character values from 0 to 1"
            )
        raw_formats = raw.get("output_formats", ["16:9"])
        if not isinstance(raw_formats, list):
            raise ConfigError("output_formats must be a list")
        formats = tuple(raw_formats)
        if not formats or any(item not in {"16:9", "9:16"} for item in formats):
            raise ConfigError("output_formats may contain only 16:9 and 9:16")
        mode = str(raw.get("narration_mode_default", "recorded"))
        if mode not in {"recorded", "tts"}:
            raise ConfigError("narration_mode_default must be recorded or tts")
        return cls(
            profile_id=profile_id,
            profile_dir=profile_dir,
            name=str(raw.get("name", profile_id)),
            language=str(raw.get("language", "en")),
            enabled=bool(raw.get("enabled", True)),
            audience=str(raw.get("audience", "")),
            tone=str(raw.get("tone", "")),
            content_type=str(raw.get("content_type", "")),
            posting_cap_per_week=cap,
            narration_mode_default=mode,
            visual_style=str(raw.get("visual_style", "")),
            music_folder=str(raw.get("music_folder", "")),
            safety_ruleset_id=str(raw.get("safety_ruleset_id", "default")),
            output_formats=formats,
            made_for_kids=bool(raw.get("made_for_kids", False)),
            disclosure_settings=dict(raw.get("disclosure_settings", {})),
            required_extra_schema_fields=tuple(raw.get("required_extra_schema_fields", [])),
            duplicate_thresholds=thresholds,
            structure_window=structure_window,
        )


def load_profile(profile_id: str, profiles_root: Path | None = None) -> Profile:
    if PROFILE_ID_RE.fullmatch(profile_id) is None:
        raise ConfigError("Profile ID may contain only lowercase letters, numbers, hyphens, and underscores")
    root = (profiles_root or DEFAULT_PROFILES_ROOT).resolve()
    profile_dir = root / profile_id
    path = profile_dir / "profile.yaml"
    if not path.is_file():
        raise ConfigError(f"Profile not found: {path}")
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ConfigError(f"Profile must be a YAML mapping: {path}")
    return Profile.from_mapping(profile_id, profile_dir, raw)


def load_topics(profile: Profile) -> list[dict[str, Any]]:
    path = profile.profile_dir / "topics.yaml"
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    topics = raw.get("topics") if isinstance(raw, dict) else None
    if not isinstance(topics, list) or not topics:
        raise ConfigError(f"topics.yaml must contain a non-empty topics list: {path}")
    result: list[dict[str, Any]] = []
    identifiers: set[str] = set()
    for item in topics:
        if not isinstance(item, dict) or not item.get("id") or not item.get("title"):
            raise ConfigError("Each topic needs id and title")
        topic_id = str(item["id"])
        if topic_id in identifiers:
            raise ConfigError(f"Duplicate topic ID: {topic_id}")
        identifiers.add(topic_id)
        result.append(dict(item))
    return result


def load_prompt(profile: Profile, name: str) -> str:
    if name not in {"idea", "script", "safety"}:
        raise ConfigError(f"Unknown prompt: {name}")
    path = profile.profile_dir / "prompts" / f"{name}.md"
    if not path.is_file():
        raise ConfigError(f"Prompt not found: {path}")
    return path.read_text(encoding="utf-8")


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}

