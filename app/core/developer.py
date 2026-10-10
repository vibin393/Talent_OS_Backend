# ruff: noqa: E501
"""Developer identity contract (Epic 66, D-25(c); used by Epics 69 and 74).

A ``DeveloperContext`` is the authenticated caller of the developer portal (``tos_developer`` session, Epic 74). It is a
platform-side identity: it is never a tenant ``Principal``, never an approver, never an actor of tenant data. Epic 74's
``dependencies/developer.py`` builds it; Epic 69's submission / review flows and Epic 74's App credentials and sandbox
self-serve receive it. Pure: imports nothing from ``app.services``.
"""

import dataclasses
import uuid
from typing import Literal

# Epic 74 adds ``viewer`` (partner org roles owner | developer | viewer; ``admin`` is kept for 69 compatibility).
DeveloperRole = Literal["owner", "admin", "developer", "viewer"]


@dataclasses.dataclass(frozen=True, slots=True)
class DeveloperContext:
    developer_id: uuid.UUID
    partner_org_id: uuid.UUID | None
    # The developer's role in ``partner_org_id`` (None when the developer has no organisation yet).
    role: DeveloperRole | None = None
    # ``partner_orgs.verification_status`` at session resolution (only ``verified`` partners publish).
    partner_verified: bool = False
    mfa_verified: bool = False
    session_id: uuid.UUID | None = None
    # Sandbox Tenants owned by the partner org (``tenants.sandbox_owner_kind = 'partner_org'``).
    sandbox_tenant_ids: frozenset[uuid.UUID] = frozenset()
    request_id: str | None = None

    def can_manage(self) -> bool:
        return self.role in ("owner", "admin")

    def can_write(self) -> bool:
        """Epic 74 (spec 0.1): owners and developers change Apps, sandboxes, runs and submissions; viewers only read."""
        return self.role in ("owner", "admin", "developer")


__all__ = ["DeveloperContext", "DeveloperRole"]
