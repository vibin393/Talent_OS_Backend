"""Burn-in override and checklist acknowledgement (Epic 65 story 65.3, FR-325, FR-330). Both change autonomy
settings, so both are ★ human only and need a reason of at least 10 characters (audited with the reason)."""

from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.actors import actor_from_ctx, require_human
from app.core.context import RequestContext
from app.core.errors import ConflictError, ValidationFailed
from app.schemas.agentic import AgentCatalogOut
from app.services.agent_runtime import admin, state

MIN_REASON = 10


def _reason(reason: str | None) -> str:
    text = (reason or "").strip()
    if len(text) < MIN_REASON:
        raise ValidationFailed("Give a reason of at least 10 characters", code="reason_required")
    return text[:500]


async def end_early(
    ctx: RequestContext, db: AsyncSession, agent_key: str, reason: str | None
) -> AgentCatalogOut:
    require_human(actor_from_ctx(ctx))
    spec = admin._spec(agent_key)
    text = _reason(reason)
    config = await state.config_for(db, agent_key)
    now = datetime.now(UTC)
    if config is None or config.burn_in_until is None or config.burn_in_until <= now:
        raise ConflictError("This agent is not in its burn-in period", code="burn_in_not_active")
    before = config.burn_in_until
    config.burn_in_until = now
    audit.record(
        "agent.burn_in_ended", "agent_config", config.id, session=db, agent_key=agent_key, reason=text,
        burn_in_until_before=before.isoformat(),
    )  # fmt: skip
    await db.flush()
    return await admin.catalog_entry(ctx, db, spec)


async def acknowledge_checklist(
    ctx: RequestContext, db: AsyncSession, agent_key: str, reason: str | None
) -> None:
    """Record that an agent was enabled although blocking checklist items were missing ("Enable anyway")."""
    require_human(actor_from_ctx(ctx))
    admin._spec(agent_key)
    text = _reason(reason)
    assert ctx.tenant_id is not None
    config = await admin._config(db, ctx.tenant_id, agent_key)
    audit.record(
        "agent.checklist_overridden", "agent_config", config.id, session=db, agent_key=agent_key, reason=text
    )
    await db.flush()


__all__ = ["acknowledge_checklist", "end_early"]
