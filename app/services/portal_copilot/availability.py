# ruff: file-ignore[line-too-long]
"""Portal Copilot availability (Epic 77 A4 / A6): whether a portal principal may ask a question now, and why not.

Order: kill switches (global / platform / tenant, P4) -> agentic master switch -> portal enablement (tenant setting and
the AI feature ``agent_copilot_<kind>``) -> monthly AI budget -> the principal's ``portal_copilot:use`` (Client /
Vendor Users) -> the Candidate's ``ai_agent_communication`` consent. Reuses P4 ``copilot_store.settings_row``."""

import dataclasses
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.services.portal_copilot import settings as settings_mod
from app.services.portal_copilot.principals import USE, PortalActor, candidate_consented


@dataclasses.dataclass(frozen=True, slots=True)
class Availability:
    reason: str | None
    settings: settings_mod.Settings

    @property
    def available(self) -> bool:
        return self.reason is None


async def check(ctx: Any, idb: AsyncSession, actor: PortalActor) -> Availability:
    """``idb`` is an internal tenant session."""
    from app.services.copilot_store import settings_row

    s = await settings_mod.get(actor.tenant_id, idb)
    avail = await settings_row.availability(RequestContext(tenant_id=actor.tenant_id), idb, actor.tenant_id)
    reason: str | None = None
    if avail.global_kill or avail.tenant_kill:
        reason = "kill_switch"
    elif not avail.features.get("agentic"):
        reason = "agentic_off"
    elif not s.enabled(actor.kind) or not avail.features.get(f"agent_{actor.agent_key}"):
        reason = "not_enabled"
    elif avail.budget_exhausted:
        reason = "budget_exhausted"
    elif actor.kind != "candidate" and USE not in actor.portal_permissions:
        reason = "no_permission"
    elif actor.kind == "candidate" and not await candidate_consented(idb, actor.principal_id):
        reason = "consent_required"
    del ctx
    return Availability(reason, s)


__all__ = ["Availability", "check"]
