# ruff: file-ignore[line-too-long]
"""Erasure handler ``collection_rules.log`` (Epic 63, order 67; FR-326).

``collection_agent_log`` stores no amounts and no Contact PII by design; on a Contact subject's erasure, the log rows
that reference the Contact (the rule's escalation Contact on an escalation message, or a hold / summary naming them)
get ``summary`` nulled, and the Contact is removed as the escalation Contact of any collection rule. Candidate subjects
hold nothing here. A top-level module, so ``privacy`` never imports the Phase 4 domain.
"""

from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.collection_rules import CollectionAgentLog, CollectionRule
from app.services.privacy.registry import ErasureContext, register_erasure_handler


class CollectionLogHandler:
    name = "collection_rules.log"
    order = 67
    tables = ("collection_agent_log", "collection_rules")

    def _contact(self, ctx: ErasureContext) -> Any:
        return ctx.subject_id or ctx.candidate_id

    async def erase(self, db: AsyncSession, ctx: ErasureContext) -> dict[str, int]:
        if ctx.subject_kind != "contact":
            return {"collection_log_redacted": 0}
        contact_id = self._contact(ctx)
        rules = list(
            (await db.execute(select(CollectionRule.id, CollectionRule.client_id).where(CollectionRule.escalation_contact_id == contact_id))).all()
        )
        clients = {r[1] for r in rules if r[1] is not None}
        redacted = 0
        if clients:
            res = await db.execute(
                update(CollectionAgentLog)
                .where(CollectionAgentLog.client_id.in_(clients), CollectionAgentLog.tone == "escalation")
                .values(summary=None)
            )
            redacted = int(getattr(res, "rowcount", 0) or 0)
        await db.execute(
            update(CollectionRule).where(CollectionRule.escalation_contact_id == contact_id).values(escalation_contact_id=None)
        )
        await db.flush()
        return {"collection_log_redacted": redacted, "collection_rules_unlinked": len(rules)}

    async def export(self, db: AsyncSession, ctx: ErasureContext) -> dict[str, Any] | None:
        if ctx.subject_kind != "contact":
            return None
        rows = (await db.execute(select(CollectionRule.name).where(CollectionRule.escalation_contact_id == self._contact(ctx)))).scalars()
        names = list(rows)
        return {"collection_rules_escalation_contact": names} if names else None

    async def verify(self, db: AsyncSession, ctx: ErasureContext) -> list[str]:
        if ctx.subject_kind != "contact":
            return []
        left = (
            await db.execute(select(CollectionRule.id).where(CollectionRule.escalation_contact_id == self._contact(ctx)).limit(1))
        ).first()
        return ["collection rules still name the contact"] if left else []


register_erasure_handler(CollectionLogHandler())
