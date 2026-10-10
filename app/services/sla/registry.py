"""SLA milestone registry (Epic 13, AD-31 / R10): Phase 2 domains declare the milestones they time."""

import dataclasses
from collections.abc import Callable, Mapping
from typing import Literal

from app.core.registry import Registry


@dataclasses.dataclass(frozen=True)
class SlaMilestoneDef:
    label: str
    scope_keys: tuple[str, ...] = ()
    calendar: Literal["business", "calendar", "deadline"] = "business"
    recipients: Callable[..., object] | None = None
    link: Callable[..., object] | None = None
    deadline: Callable[..., object] | None = None


_milestones: Registry[tuple[str, str], SlaMilestoneDef] = Registry("SLA milestone")


def register_sla_milestone(subject_type: str, milestone: str, definition: SlaMilestoneDef) -> None:
    _milestones.register((subject_type, milestone), definition)


def milestone(subject_type: str, milestone_key: str) -> SlaMilestoneDef | None:
    return _milestones.find((subject_type, milestone_key))


def milestones() -> Mapping[tuple[str, str], SlaMilestoneDef]:
    return _milestones.view()
