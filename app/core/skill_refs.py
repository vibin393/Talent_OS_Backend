"""Skill references shared by the skills graph, the semantic layer and matching (Epic 36 contract, AD-41).

``SkillKey`` names one catalogue skill: a platform skill (``kind="global"`` -> ``skills.id``) or a tenant skill
(``kind="tenant"`` -> ``tenant_skills.id``); the kinds equal ``demand_skills.skill_ref_kind``. Its string form
(``"global:<uuid>"`` / ``"tenant:<uuid>"``) is the key used in events and caches. ``ExpandedSkill`` is one row of
``skills_service.expand``. Re-exported by ``app.schemas.skills``.
"""

import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

__all__ = ["EdgeKind", "ExpandedSkill", "SkillKey", "SkillKind"]

SkillKind = Literal["global", "tenant"]
EdgeKind = Literal["parent_of", "related_to", "implies"]
_KINDS: frozenset[str] = frozenset({"global", "tenant"})


@dataclass(frozen=True, slots=True)
class SkillKey:
    kind: SkillKind
    id: uuid.UUID

    def as_str(self) -> str:
        return f"{self.kind}:{self.id}"

    @classmethod
    def parse(cls, s: str) -> "SkillKey":
        if not isinstance(s, str) or ":" not in s:
            raise ValueError(f"invalid skill key: {s!r}")
        kind, _, raw = s.partition(":")
        if kind not in _KINDS:
            raise ValueError(f"invalid skill key kind: {kind!r}")
        try:
            return cls(kind=kind, id=uuid.UUID(raw))  # type: ignore[arg-type]
        except (ValueError, AttributeError) as exc:
            raise ValueError(f"invalid skill key id: {raw!r}") from exc


@dataclass(frozen=True, slots=True)
class ExpandedSkill:
    key: SkillKey
    root: SkillKey
    weight: Decimal
    depth: int
    via: SkillKey | None
