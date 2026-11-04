from __future__ import annotations

from enum import StrEnum


class PipelineStatus(StrEnum):
    DRAFTED = "drafted"
    SAFETY_CHECKED = "safety_checked"
    AWAITING_REVIEW = "awaiting_review"
    APPROVED = "approved"
    NARRATED = "narrated"
    RENDERED = "rendered"
    EXPORTING = "exporting"
    EXPORTED = "exported"
    REJECTED = "rejected"


TRANSITIONS: dict[PipelineStatus, set[PipelineStatus]] = {
    PipelineStatus.DRAFTED: {PipelineStatus.SAFETY_CHECKED, PipelineStatus.REJECTED},
    PipelineStatus.SAFETY_CHECKED: {PipelineStatus.AWAITING_REVIEW, PipelineStatus.REJECTED},
    PipelineStatus.AWAITING_REVIEW: {PipelineStatus.APPROVED, PipelineStatus.REJECTED},
    PipelineStatus.APPROVED: {PipelineStatus.NARRATED},
    PipelineStatus.NARRATED: {PipelineStatus.RENDERED},
    PipelineStatus.RENDERED: {PipelineStatus.EXPORTING},
    PipelineStatus.EXPORTING: {PipelineStatus.RENDERED, PipelineStatus.EXPORTED},
    PipelineStatus.EXPORTED: set(),
    PipelineStatus.REJECTED: {PipelineStatus.DRAFTED},
}


class InvalidTransition(ValueError):
    pass


def require_transition(current: str | PipelineStatus, target: str | PipelineStatus) -> None:
    source = PipelineStatus(current)
    destination = PipelineStatus(target)
    if destination not in TRANSITIONS[source]:
        raise InvalidTransition(f"Cannot move pipeline from {source.value} to {destination.value}")

