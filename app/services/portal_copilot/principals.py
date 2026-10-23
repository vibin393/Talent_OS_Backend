# ruff: file-ignore[line-too-long, non-imperative-mood]
"""Portal principals of the portal Copilot (Epic 77 A2, FR-396, NFR-60).

``actor_for(principal)`` turns the signed-in portal principal (``ClientDep`` / ``VendorDep`` ``Principal`` or the
``CandidatePrincipal``) into a ``PortalActor``: the foundation ``OwnerRef`` of its runs and its effective portal
permissions. Client / Vendor Users hold their own ``portal_permissions`` (``portal_copilot:use`` required); a
Candidate has the implicit ``CANDIDATE_COPILOT_PERMISSIONS`` only while the tenant enabled the candidate Copilot and
the Candidate's ``ai_agent_communication`` consent is active (``candidate_rights``, registered with the runtime so the
next step of a run fails closed when consent is withdrawn).
"""

import dataclasses
import uuid
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import tenant_session
from app.core.errors import Forbidden
from app.core.permissions import Principal

PortalKind = Literal["client", "vendor", "candidate"]
USE = "portal_copilot:use"
CANDIDATE_COPILOT_PERMISSIONS: frozenset[str] = frozenset({USE, "self:read", "self:draft"})
CANDIDATE_CONSENT_PURPOSE = "ai_agent_communication"
AGENT_KEYS: dict[str, str] = {
    "client": "copilot_client",
    "vendor": "copilot_vendor",
    "candidate": "copilot_candidate",
}
PRINCIPAL_TYPES: dict[str, str] = {"client": "client_user", "vendor": "vendor_user", "candidate": "candidate"}
ORG_KINDS: dict[str, str] = {"client": "client", "vendor": "vendor", "candidate": "worker"}
KIND_OF_TYPE: dict[str, str] = {v: k for k, v in PRINCIPAL_TYPES.items()}


@dataclasses.dataclass(frozen=True, slots=True)
class PortalActor:
    kind: str  # client | vendor | candidate
    tenant_id: uuid.UUID
    principal_id: uuid.UUID
    org_id: uuid.UUID
    portal_permissions: frozenset[str]
    session_id: uuid.UUID | None = None

    @property
    def principal_type(self) -> str:
        return PRINCIPAL_TYPES[self.kind]

    @property
    def org_kind(self) -> str:
        return ORG_KINDS[self.kind]

    @property
    def agent_key(self) -> str:
        return AGENT_KEYS[self.kind]

    def org_columns(self) -> dict[str, uuid.UUID | None]:
        return {
            "client_id": self.org_id if self.kind == "client" else None,
            "vendor_id": self.org_id if self.kind == "vendor" else None,
            "candidate_id": self.org_id if self.kind == "candidate" else None,
        }

    def core_principal(self) -> Principal:
        return Principal(
            type=self.principal_type, id=self.principal_id, tenant_id=self.tenant_id, org_id=self.org_id,  # type: ignore[arg-type]
            portal_permissions=self.portal_permissions, session_id=self.session_id,
        )  # fmt: skip


def actor_for(kind: str, principal: Any) -> PortalActor:
    """The portal actor of a portal request (403 for any other principal; API keys and Apps never reach here)."""
    if kind == "candidate":
        cid = getattr(principal, "candidate_id", None)
        if cid is None or getattr(principal, "type", None) != "candidate":
            raise Forbidden("Candidate portal only")
        return PortalActor(
            "candidate",
            principal.tenant_id,
            cid,
            cid,
            frozenset(),
            session_id=getattr(principal, "session_id", None),
        )
    expected = PRINCIPAL_TYPES[kind]
    if not isinstance(principal, Principal) or principal.type != expected or principal.org_id is None:
        raise Forbidden("Portal only")
    assert principal.tenant_id is not None
    return PortalActor(
        kind, principal.tenant_id, principal.id, principal.org_id, frozenset(principal.portal_permissions or ()),
        session_id=principal.session_id,
    )  # fmt: skip


def owner_ref(actor: PortalActor) -> Any:
    from app.services.agent_runtime_service import OwnerRef

    return OwnerRef(actor.principal_type, actor.principal_id, actor.org_kind, actor.org_id)  # type: ignore[arg-type]


async def candidate_enabled(db: AsyncSession) -> bool:
    from app.models.portal_copilot import PortalCopilotSettings

    row = (await db.execute(select(PortalCopilotSettings.candidate_enabled))).scalar_one_or_none()
    return bool(row)


async def candidate_consented(db: AsyncSession, candidate_id: uuid.UUID) -> bool:
    from app.services.privacy.consent import has_active_consent

    return await has_active_consent(db, candidate_id, CANDIDATE_CONSENT_PURPOSE)


async def candidate_rights(db: AsyncSession, tenant_id: uuid.UUID, candidate_id: uuid.UUID) -> frozenset[str]:  # ruff: ignore[unused-function-argument]
    """Implicit rights of a Candidate owner (runtime hook): enablement + active consent, read on an internal
    session of its own (the caller's session may be org-scoped)."""
    async with tenant_session(tenant_id) as idb:
        if await candidate_enabled(idb) and await candidate_consented(idb, candidate_id):
            return CANDIDATE_COPILOT_PERMISSIONS
    return frozenset()


async def with_rights(actor: PortalActor) -> PortalActor:
    """The actor with its live effective portal permissions (candidates: implicit rights)."""
    if actor.kind != "candidate":
        return actor
    rights = await candidate_rights(None, actor.tenant_id, actor.principal_id)  # type: ignore[arg-type]
    return dataclasses.replace(actor, portal_permissions=rights)


__all__ = [
    "AGENT_KEYS",
    "CANDIDATE_CONSENT_PURPOSE",
    "CANDIDATE_COPILOT_PERMISSIONS",
    "KIND_OF_TYPE",
    "PRINCIPAL_TYPES",
    "USE",
    "PortalActor",
    "actor_for",
    "candidate_consented",
    "candidate_enabled",
    "candidate_rights",
    "owner_ref",
    "with_rights",
]
