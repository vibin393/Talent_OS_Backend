"""Skills graph events (Epic 37).

Epic 36 contract layer (AD-36/AD-39): frozen, slotted dataclasses; ``tenant_id`` first; ids only (no PII,
money as decimal ``str``); dispatched after commit. The events below are frozen
(``tests/contracts/test_events_catalogue.py``); the owner may add domain-local events to this file.
"""

import dataclasses
import uuid

__all__ = ["SkillGraphChanged"]


@dataclasses.dataclass(frozen=True, slots=True)
class SkillGraphChanged:
    tenant_id: uuid.UUID
    scope: str  # tenant | platform
    skill_keys: tuple[str, ...]  # "global:<uuid>" / "tenant:<uuid>"
