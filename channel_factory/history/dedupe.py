from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import math
import re
from typing import Any

from channel_factory.config import Profile
from channel_factory.history.store import HistoryStore


TOKEN_RE = re.compile(r"[a-z0-9']+")


def _tokens(text: str) -> list[str]:
    return TOKEN_RE.findall(text.lower())


def tfidf_cosine(left: str, right: str) -> float:
    documents = [_tokens(left), _tokens(right)]
    if not documents[0] or not documents[1]:
        return 0.0
    document_frequency = Counter(token for token in set(documents[0]))
    document_frequency.update(set(documents[1]))
    vectors: list[dict[str, float]] = []
    for document in documents:
        counts = Counter(document)
        size = len(document)
        vectors.append({
            token: (count / size) * (math.log((2 + 1) / (document_frequency[token] + 1)) + 1)
            for token, count in counts.items()
        })
    common = set(vectors[0]) & set(vectors[1])
    dot = sum(vectors[0][token] * vectors[1][token] for token in common)
    norms = [math.sqrt(sum(value * value for value in vector.values())) for vector in vectors]
    return dot / (norms[0] * norms[1]) if norms[0] and norms[1] else 0.0


@dataclass(frozen=True)
class DuplicateReport:
    rejected: bool
    closest_title: str | None
    title_score: float
    narration_score: float
    lesson_character_score: float
    structure_warning: str | None


def check_duplicate(
    script: dict[str, Any],
    recent: list[dict[str, Any]],
    profile: Profile,
) -> DuplicateReport:
    narration = " ".join(scene.get("narration", "") for scene in script.get("scenes", []))
    lesson_characters = " ".join([str(script.get("lesson", "")), *map(str, script.get("characters", []))])
    closest_title: str | None = None
    maxima = {"title": 0.0, "narration": 0.0, "lesson_character": 0.0}
    for past in recent:
        title_score = tfidf_cosine(script.get("title", ""), past.get("title", ""))
        if title_score >= maxima["title"]:
            maxima["title"] = title_score
            closest_title = past.get("title")
        maxima["narration"] = max(maxima["narration"], tfidf_cosine(narration, past.get("narration", "")))
        past_lesson_characters = " ".join([
            str(past.get("lesson") or ""),
            " ".join(__import__("json").loads(past.get("characters_json", "[]"))),
        ])
        maxima["lesson_character"] = max(
            maxima["lesson_character"], tfidf_cosine(lesson_characters, past_lesson_characters)
        )
    thresholds = {"title": 0.78, "narration": 0.82, "lesson_character": 0.90}
    thresholds.update(profile.duplicate_thresholds)
    rejected = any(maxima[key] >= thresholds[key] for key in maxima)
    fingerprint = HistoryStore.structure_fingerprint(script)
    recent_fingerprints = [item.get("structure_fingerprint") for item in recent[: profile.structure_window]]
    warning = None
    if len(recent_fingerprints) >= profile.structure_window and all(
        item == fingerprint for item in recent_fingerprints
    ):
        warning = f"Last {profile.structure_window} videos share structure {fingerprint}"
    return DuplicateReport(
        rejected=rejected,
        closest_title=closest_title,
        title_score=maxima["title"],
        narration_score=maxima["narration"],
        lesson_character_score=maxima["lesson_character"],
        structure_warning=warning,
    )

