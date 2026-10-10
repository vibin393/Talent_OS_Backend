# ruff: noqa: E501
"""Public summaries (Epic 49 A7, FR-248): impact ratios and the unknown count copied from a completed snapshot
(suppressed cells stay suppressed), listed on the Career Portal (``GET /public/fairness/{slug}/summary``)."""

import uuid
from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.context import RequestContext
from app.core.database import tenant_session
from app.models.fairness import AedtInventory, FairnessPublication
from app.schemas.fairness import PublicationIn, PublicationOut, PublicPublicationOut
from app.services.fairness import inventory, reports
from app.services.fairness.common import conflict, not_found, now, tenant_of, user_id

REMINDER_AFTER = timedelta(days=335)  # ~11 months after the audit date


def summary_of(run: Any) -> dict[str, Any]:
    snap = run.snapshot or {}
    tables = []
    for t in snap.get("tables") or []:
        rows = []
        for r in t["rows"]:
            row = {
                "category": r["category"], "label": r["label"], "unknown": r["unknown"],
                "applicants": r["applicants"], "impact_ratio": r.get("impact_ratio"),
            }  # fmt: skip
            if "scoring_impact_ratio" in r:
                row["scoring_impact_ratio"] = r.get("scoring_impact_ratio")
            rows.append(row)
        tables.append({"key": t["key"], "rows": rows})
    return {
        "selection_point": run.selection_point,
        "date_from": str(run.date_from),
        "date_to": str(run.date_to),
        "total_applicants": run.total_applicants or 0,
        "unknown_counts": dict(run.unknown_counts or {}),
        "tables": tables,
    }


def _out(p: FairnessPublication, aedt_name: str) -> PublicationOut:
    return PublicationOut(
        id=p.id,
        run_id=p.run_id,
        aedt_id=p.aedt_id,
        aedt_name=aedt_name,
        audit_date=p.audit_date,
        distribution_date=p.distribution_date,
        summary=p.summary,
        published_at=p.published_at,
        unpublished_at=p.unpublished_at,
    )


async def list_publications(ctx: RequestContext, db: AsyncSession) -> list[PublicationOut]:  # noqa: ARG001
    rows = (
        await db.execute(
            select(FairnessPublication, AedtInventory.name)
            .join(AedtInventory, AedtInventory.id == FairnessPublication.aedt_id)
            .order_by(FairnessPublication.published_at.desc())
        )
    ).tuples()
    return [_out(p, name) for p, name in rows]


async def publish(ctx: RequestContext, db: AsyncSession, data: PublicationIn) -> PublicationOut:
    run = await reports.load(db, data.run_id)
    if run.status != "completed" or not run.snapshot:
        raise conflict("fairness_run_not_completed", "The report has not completed yet")
    aedt = await inventory.load(db, data.aedt_id)
    row = FairnessPublication(
        id=uuid.uuid4(),
        tenant_id=tenant_of(db),
        run_id=run.id,
        aedt_id=aedt.id,
        audit_date=data.audit_date,
        distribution_date=data.distribution_date,
        summary=summary_of(run),
        published_by=user_id(ctx),
        published_at=now(),
    )
    db.add(row)
    await db.flush()
    await db.refresh(row)
    audit.record("fairness.summary_published", "fairness_run", run.id, session=db, aedt_id=str(aedt.id))
    return _out(row, aedt.name)


async def unpublish(ctx: RequestContext, db: AsyncSession, publication_id: uuid.UUID) -> PublicationOut:  # noqa: ARG001
    row = await db.get(FairnessPublication, publication_id)
    if row is None:
        raise not_found("Publication")
    if row.unpublished_at is None:
        row.unpublished_at = now()
        await db.flush()
    aedt = await inventory.load(db, row.aedt_id)
    return _out(row, aedt.name)


async def public_summaries(slug: str) -> list[PublicPublicationOut]:
    from app.services.portal.careers import resolve_site

    site = await resolve_site(slug)
    async with tenant_session(site.tenant_id) as db:
        rows = (
            await db.execute(
                select(FairnessPublication, AedtInventory.name, AedtInventory.feature)
                .join(AedtInventory, AedtInventory.id == FairnessPublication.aedt_id)
                .where(FairnessPublication.unpublished_at.is_(None))
                .order_by(FairnessPublication.distribution_date.desc())
            )
        ).tuples()
        return [
            PublicPublicationOut(
                id=p.id,
                aedt_name=name,
                feature=feature,
                audit_date=p.audit_date,
                distribution_date=p.distribution_date,
                summary=p.summary,
            )
            for p, name, feature in rows
        ]


async def due_reminders(db: AsyncSession) -> list[FairnessPublication]:
    """Active publications whose audit date is ~11 months old and not yet reminded (``fairness.publication_due``)."""
    cutoff = (now() - REMINDER_AFTER).date()
    return list(
        (
            await db.execute(
                select(FairnessPublication).where(
                    FairnessPublication.unpublished_at.is_(None),
                    FairnessPublication.reminded_at.is_(None),
                    FairnessPublication.audit_date <= cutoff,
                )
            )
        ).scalars()
    )
