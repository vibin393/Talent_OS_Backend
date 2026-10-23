# ruff: file-ignore[line-too-long]
"""Integrations erasure handler ``integrations.links`` (70, Epic 66, spec 0.17): the external-id map
``connector_links`` rows of the erased candidate are unlinked and their ``external_url`` nulled. Receivers learn about
the erasure through the ``subject.erased`` Public Event (D-11)."""

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.integrations import ConnectorLink
from app.services.candidates_erasure import subject_ids
from app.services.integrations.links import unlink_candidate
from app.services.privacy.registry import ErasureContext, register_erasure_handler


class IntegrationLinksHandler:
    name = "integrations.links"
    order = 70
    tables = ("connector_links",)

    async def erase(self, db: AsyncSession, ctx: ErasureContext) -> dict[str, int]:
        ids = await subject_ids(db, ctx.candidate_id)
        return {"connector_links": await unlink_candidate(db, ids)}

    async def export(self, db: AsyncSession, ctx: ErasureContext) -> dict[str, Any] | None:
        rows = (
            await db.execute(select(ConnectorLink).where(ConnectorLink.candidate_id == ctx.candidate_id))
        ).scalars()
        links = [{"entity_type": r.entity_type, "status": r.status} for r in rows]
        return {"connector_links": links} if links else None

    async def verify(self, db: AsyncSession, ctx: ErasureContext) -> list[str]:
        ids = await subject_ids(db, ctx.candidate_id)
        rows = (
            await db.execute(
                select(ConnectorLink).where(
                    ConnectorLink.candidate_id.in_(ids), ConnectorLink.status != "unlinked"
                )
            )
        ).scalars()
        return [f"connector_link {r.id} still linked" for r in rows]


register_erasure_handler(IntegrationLinksHandler())
