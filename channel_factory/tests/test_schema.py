from __future__ import annotations

from copy import deepcopy
import json
import math
from pathlib import Path

import pytest

from channel_factory.config import ConfigError, load_profile
from channel_factory.core.schema import ScriptValidationError, validate_script


SAMPLES = Path(__file__).resolve().parents[1] / "samples"


def sample(name: str) -> dict:
    return json.loads((SAMPLES / name).read_text(encoding="utf-8"))


def test_history_sample_is_valid() -> None:
    validated = validate_script(sample("history_script.json"), load_profile("history_en"))
    assert validated["topic_id"] == "hist-antikythera"


def test_kids_sample_is_valid() -> None:
    validated = validate_script(sample("kids_script.json"), load_profile("kids_stories_en"))
    assert validated["lesson"]


def test_missing_image_license_is_rejected() -> None:
    script = sample("history_script.json")
    del script["scenes"][0]["image_credit"]["license"]
    with pytest.raises(ScriptValidationError, match="license"):
        validate_script(script, load_profile("history_en"))


def test_tts_requires_disclosure() -> None:
    script = deepcopy(sample("history_script.json"))
    script["narration_mode"] = "tts"
    with pytest.raises(ScriptValidationError, match="disclosure_required"):
        validate_script(script, load_profile("history_en"))


def test_empty_credit_and_malformed_source_are_rejected() -> None:
    script = sample("history_script.json")
    script["scenes"][0]["image_credit"]["source"] = ""
    script["sources"] = [""]
    with pytest.raises(ScriptValidationError) as error:
        validate_script(script, load_profile("history_en"))
    assert "image_credit.source" in str(error.value)
    assert "sources must contain" in str(error.value)


def test_kids_profile_requires_lesson_and_named_characters() -> None:
    script = sample("kids_script.json")
    script["lesson"] = ""
    script["characters"] = []
    with pytest.raises(ScriptValidationError) as error:
        validate_script(script, load_profile("kids_stories_en"))
    assert "lesson" in str(error.value)
    assert "characters" in str(error.value)


def test_script_resource_limits_reject_non_finite_duration_and_scene_flood() -> None:
    non_finite = sample("history_script.json")
    non_finite["scenes"][0]["duration_sec"] = math.nan
    with pytest.raises(ScriptValidationError, match="finite|positive"):
        validate_script(non_finite, load_profile("history_en"))

    oversized = sample("history_script.json")
    oversized["scenes"] = [deepcopy(oversized["scenes"][0]) for _ in range(61)]
    for index, scene in enumerate(oversized["scenes"]):
        scene["scene_id"] = f"scene-{index}"
    with pytest.raises(ScriptValidationError, match="no more than 60"):
        validate_script(oversized, load_profile("history_en"))


def test_profile_id_cannot_escape_the_profiles_root() -> None:
    with pytest.raises(ConfigError, match="Profile ID"):
        load_profile("../history_en")


def test_scene_ids_cannot_overwrite_narration_or_music_provenance() -> None:
    value = sample("history_script.json")
    value["scenes"][0]["scene_id"] = "narration"
    with pytest.raises(ScriptValidationError, match="reserved for pipeline assets"):
        validate_script(value, load_profile("history_en"))


