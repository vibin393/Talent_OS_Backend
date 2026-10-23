"""Vendor Releases (Epic 17 A4, FR-94): a Demand released to Active Vendors with commercial limits.

* The demand must be *Open* (409 ``demand_not_open``); only *Active* vendors are released, the others are
  reported per item. A second open release of the same (demand, vendor) is refused by the partial unique
  index.
* The release carries a snapshot of the demand (title, positions, skills, location, ...) -- the Vendor Portal
  reads it instead of ``demands`` (AD-26). The client name is "Confidential client" unless ``reveal_client``.
* Bench hold (FR-94): with ``require_bench_review_for_vendor_release`` the workflow condition
  ``bench.matches_reviewed`` must hold (``None`` = Epic 24 absent = skip).
* Handlers: demand leaves *Open* -> releases ``closed``; requirements changed -> snapshots refreshed.
"""

import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.events import emit
from app.core.sequences import next_human_id
from app.events.vendors import VendorReleaseClosed, VendorReleaseCreated
from app.models.clients import Client
from app.models.demands import Demand, DemandSkill, JdVersion
from app.models.vendors import OPEN_RELEASE_STATUSES, Vendor, VendorRelease, VendorSubmission
from app.schemas.vendors import ReleaseBatchOut, ReleaseCreate, ReleaseItemResult, ReleaseOut
from app.services.vendors.common import conflict, invalid, load_settings, not_found, now, tenant_of, user_id

CONFIDENTIAL = "Confidential client"
COUNTED_STATUSES = ("submitted", "under_review", "accepted", "rejected", "duplicate")
# (from, to) -> allowed actor kinds.
TRANSITIONS: dict[tuple[str, str], frozenset[str]] = {
    ("active", "paused"): frozenset({"user", "system"}),
    ("paused", "active"): frozenset({"user", "system"}),
    ("active", "withdrawn"): frozenset({"user"}),
    ("paused", "withdrawn"): frozenset({"user"}),
    ("active", "closed"): frozenset({"system"}),
    ("paused", "closed"): frozenset({"system"}),
}


def _experience(d: Demand) -> str | None:
    lo, hi = d.min_experience_years, d.max_experience_years
    if lo is None and hi is None:
        return None
    if lo is not None and hi is not None:
        return f"{lo:g}-{hi:g} years"
    return f"{lo:g}+ years" if lo is not None else f"up to {hi:g} years"


async def _snapshot(db: AsyncSession, d: Demand, reveal_client: bool) -> dict[str, Any]:
    skills = list(
        (
            await db.execute(
                select(DemandSkill.skill_label)
                .where(DemandSkill.demand_id == d.id)
                .order_by(DemandSkill.position, DemandSkill.id)
            )
        ).scalars()
    )
    jd = (
        await db.execute(
            select(JdVersion.content)
            .where(JdVersion.demand_id == d.id)
            .order_by(JdVersion.version_no.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    client_name = CONFIDENTIAL
    if reveal_client:
        client_name = (
            await db.execute(select(Client.name).where(Client.id == d.client_id))
        ).scalar_one_or_none() or CONFIDENTIAL
    return {
        "title": d.title,
        "positions": d.positions,
        "skills": skills,
        "location": d.location,
        "employment_type": d.employment_type,
        "experience_range": _experience(d),
        "jd_public_text": jd,
        "client_display_name": client_name,
    }


async def counts(db: AsyncSession, release_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, int]:
    if not release_ids:
        return {}
    rows = await db.execute(
        select(VendorSubmission.vendor_release_id, func.count())
        .where(
            VendorSubmission.vendor_release_id.in_(list(release_ids)),
            VendorSubmission.status.in_(COUNTED_STATUSES),
        )
        .group_by(VendorSubmission.vendor_release_id)
    )
    return {rid: int(n) for rid, n in rows.tuples()}


def release_out(r: VendorRelease, *, vendor_name: str | None = None, used: int = 0) -> ReleaseOut:
    return ReleaseOut(
        id=r.id,
        human_id=r.human_id,
        demand_id=r.demand_id,
        vendor_id=r.vendor_id,
        vendor_name=vendor_name,
        status=r.status,
        reveal_client=r.reveal_client,
        max_vendor_rate=r.max_vendor_rate,
        rate_unit=r.rate_unit,
        currency=r.currency,
        submission_limit=r.submission_limit,
        submissions_count=used,
        deadline_at=r.deadline_at,
        instructions=r.instructions,
        released_at=r.released_at,
        paused_by_system=r.paused_by_system,
        title=r.title,
    )


async def _demand(db: AsyncSession, demand_id: uuid.UUID) -> Demand:
    d = (await db.execute(select(Demand).where(Demand.id == demand_id))).scalar_one_or_none()
    if d is None:
        raise not_found("Demand")
    return d


async def create_releases(
    ctx: RequestContext, db: AsyncSession, demand_id: uuid.UUID, data: ReleaseCreate
) -> ReleaseBatchOut:
    from app.services.workflow.registry import evaluate_condition

    d = await _demand(db, demand_id)
    if d.status != "open":
        raise conflict("demand_not_open", "Only an open demand can be released to vendors", status=d.status)
    if not data.vendor_ids and data.tier is None:
        raise invalid("vendors_required", "Choose vendors or a tier")
    settings = await load_settings(db)
    if settings.require_bench_review_for_vendor_release:
        reviewed = await evaluate_condition(ctx, db, "bench.matches_reviewed", demand_id=d.id)
        if reviewed is False:
            raise conflict("bench_review_pending", "Review the bench matches before releasing to vendors")
    stmt = select(Vendor).where(Vendor.archived_at.is_(None))
    if data.vendor_ids:
        stmt = stmt.where(Vendor.id.in_(data.vendor_ids))
    else:
        stmt = stmt.where(Vendor.tier == data.tier, Vendor.status == "active")
    vendors = {v.id: v for v in (await db.execute(stmt)).scalars()}
    targets = (
        list(dict.fromkeys(data.vendor_ids))
        if data.vendor_ids
        else sorted(vendors, key=lambda i: vendors[i].legal_name)
    )
    snapshot = await _snapshot(db, d, data.reveal_client)
    items: list[ReleaseItemResult] = []
    released: list[VendorRelease] = []
    at = now()
    for vid in targets:
        v = vendors.get(vid)
        if v is None:
            items.append(ReleaseItemResult(vendor_id=vid, status="skipped", reason="not_found"))
            continue
        if v.status != "active":
            items.append(ReleaseItemResult(vendor_id=vid, status="skipped", reason="vendor_not_active"))
            continue
        row = VendorRelease(
            id=uuid.uuid4(),
            tenant_id=tenant_of(db),
            human_id=await next_human_id(db, "VR", width=6),
            demand_id=d.id,
            vendor_id=v.id,
            client_id=d.client_id,
            status="active",
            reveal_client=data.reveal_client,
            max_vendor_rate=data.max_vendor_rate,
            rate_unit=data.rate_unit or d.rate_unit,
            currency=(data.currency or d.currency or "INR").upper(),
            submission_limit=data.submission_limit,
            deadline_at=data.deadline_at,
            instructions=data.instructions,
            released_by=user_id(ctx),
            released_at=at,
            **snapshot,
        )
        try:
            async with db.begin_nested():
                db.add(row)
                await db.flush()
        except IntegrityError:
            items.append(ReleaseItemResult(vendor_id=vid, status="skipped", reason="already_released"))
            continue
        released.append(row)
        items.append(
            ReleaseItemResult(
                vendor_id=vid, status="released", release=release_out(row, vendor_name=v.legal_name)
            )
        )
        await _start_first_submission_clock(ctx, db, row)
    if released:
        for row in released:
            emit(
                db,
                VendorReleaseCreated(
                    tenant_id=row.tenant_id, release_id=row.id, demand_id=d.id, vendor_ids=(row.vendor_id,)
                ),
            )
    return ReleaseBatchOut(items=items)


async def _start_first_submission_clock(ctx: RequestContext, db: AsyncSession, row: VendorRelease) -> None:
    from app.services import sla_service

    try:
        await sla_service.start_clock(
            ctx, db, "vendor_release", row.id, "first_submission", scope={"vendor_id": str(row.vendor_id)}
        )
    except ValueError:  # milestone not registered (hooks not loaded)
        return


async def list_for_demand(ctx: RequestContext, db: AsyncSession, demand_id: uuid.UUID) -> list[ReleaseOut]:  # ruff: ignore[unused-function-argument]
    await _demand(db, demand_id)
    rows = (
        await db.execute(
            select(VendorRelease, Vendor.legal_name)
            .join(Vendor, Vendor.id == VendorRelease.vendor_id)
            .where(VendorRelease.demand_id == demand_id)
            .order_by(VendorRelease.released_at.desc(), VendorRelease.id)
        )
    ).all()
    used = await counts(db, [r.id for r, _ in rows])
    return [release_out(r, vendor_name=name, used=used.get(r.id, 0)) for r, name in rows]


async def list_for_vendor(ctx: RequestContext, db: AsyncSession, vendor_id: uuid.UUID) -> list[ReleaseOut]:  # ruff: ignore[unused-function-argument]
    rows = list(
        (
            await db.execute(
                select(VendorRelease)
                .where(VendorRelease.vendor_id == vendor_id)
                .order_by(VendorRelease.released_at.desc())
            )
        ).scalars()
    )
    used = await counts(db, [r.id for r in rows])
    return [release_out(r, used=used.get(r.id, 0)) for r in rows]


async def _load(db: AsyncSession, release_id: uuid.UUID, *, lock: bool = False) -> VendorRelease:
    stmt = select(VendorRelease).where(VendorRelease.id == release_id)
    if lock:
        stmt = stmt.with_for_update()
    row = (await db.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise not_found("Release")
    return row


async def _move(db: AsyncSession, row: VendorRelease, to: str, actor_kind: str) -> None:
    allowed = TRANSITIONS.get((row.status, to))
    if allowed is None or actor_kind not in allowed:
        raise conflict(
            "transition_not_allowed",
            f"A release cannot move from {row.status} to {to}",
            from_status=row.status,
        )
    row.status = to
    if to in {"withdrawn", "closed"}:
        row.closed_at = now()
        emit(db, VendorReleaseClosed(tenant_id=row.tenant_id, release_id=row.id, demand_id=row.demand_id))
    await db.flush()


async def change_status(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,
    release_id: uuid.UUID,
    action: str,
) -> ReleaseOut:
    to = {"pause": "paused", "resume": "active", "withdraw": "withdrawn"}.get(action)
    if to is None:
        raise not_found("Action")
    row = await _load(db, release_id, lock=True)
    if to == "active":
        vendor_status = (
            await db.execute(select(Vendor.status).where(Vendor.id == row.vendor_id))
        ).scalar_one()
        if vendor_status != "active":
            raise conflict("vendor_not_active", "The vendor is not active")
    await _move(db, row, to, "user")
    row.paused_by_system = False
    await db.flush()
    used = await counts(db, [row.id])
    return release_out(row, used=used.get(row.id, 0))


# ================================================================ after-commit handlers (system)
async def close_for_demand(ctx: RequestContext, db: AsyncSession, demand_id: uuid.UUID) -> int:  # ruff: ignore[unused-function-argument]
    rows = (
        (
            await db.execute(
                select(VendorRelease)
                .where(VendorRelease.demand_id == demand_id, VendorRelease.status.in_(OPEN_RELEASE_STATUSES))
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    for row in rows:
        await _move(db, row, "closed", "system")
    return len(rows)


async def refresh_snapshots(ctx: RequestContext, db: AsyncSession, demand_id: uuid.UUID) -> int:  # ruff: ignore[unused-function-argument]
    d = (await db.execute(select(Demand).where(Demand.id == demand_id))).scalar_one_or_none()
    if d is None:
        return 0
    rows = (
        (
            await db.execute(
                select(VendorRelease).where(
                    VendorRelease.demand_id == demand_id, VendorRelease.status.in_(OPEN_RELEASE_STATUSES)
                )
            )
        )
        .scalars()
        .all()
    )
    for row in rows:
        for key, value in (await _snapshot(db, d, row.reveal_client)).items():
            setattr(row, key, value)
    await db.flush()
    return len(rows)
