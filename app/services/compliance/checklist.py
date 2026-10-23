# ruff: file-ignore[unused-function-argument, line-too-long]
"""Per-Placement compliance checklist (Epic 21 A4/A6, P2-8.1/8.3, FR-110/115/116).

* ``evaluate_subject`` (handlers of ``PlacementCreated`` and, when configured, ``ApplicationStageChanged`` to
  ``selected``): load facts, run the pure matcher, upsert the checklist (never deleting waived items), refresh
  statuses and emit ``ComplianceRequirementsEvaluated`` (BGV opens cases, onboarding adds tasks).
* ``refresh_placement``: item statuses (credential = best current version; document = an unexpired document of
  the category; ``bgv_package`` stays ``pending``; ``policy_ack`` set by ``PolicyAcknowledged``), then the flags
  ``non_compliant`` / ``blocking``. Changes are published **only** as events (AD-25):
  ``CredentialStatusChanged`` per mandatory credential item, ``PlacementComplianceChanged`` per placement.
* Retroactive items (added to an *active* placement by a requirement change) count as non-compliant but never
  block (FR-116).
"""

import uuid
from collections.abc import Iterable, Sequence
from datetime import date
from typing import Any

from loguru import logger
from sqlalchemy import column, select, table
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.context import RequestContext
from app.core.events import emit
from app.core.identity_docs import assert_no_full_aadhaar
from app.events.compliance import (
    ComplianceRequirementsEvaluated,
    CredentialStatusChanged,
    PlacementComplianceChanged,
)
from app.models.compliance import (
    ComplianceEvaluationRun,
    ComplianceRequirement,
    CredentialType,
    PlacementComplianceItem,
)
from app.models.placements import Placement
from app.schemas.compliance import ChecklistItemOut, PlacementComplianceOut
from app.services.compliance import evaluation
from app.services.compliance.common import (
    COMPLIANCE_WAIVE,
    OPEN_PLACEMENT_STATUSES,
    REEVALUATE_STATUSES,
    conflict,
    get_settings_row,
    not_found,
    now,
    require_human,
    system_ctx,
    today,
)

_clients = table("clients", column("id"), column("industry"), column("locations"), column("name"))
_demands = table("demands", column("id"), column("title"), column("client_id"))
_applications = table("applications", column("id"), column("candidate_id"), column("demand_id"))


def _flag_status(status: str) -> str:
    return {"met": "valid", "expiring": "valid", "expired": "expired", "pending": "pending_verification"}.get(
        status, "missing"
    )


# ================================================================ facts
async def _client_facts(db: AsyncSession, client_id: uuid.UUID | None) -> tuple[str | None, str | None]:
    if client_id is None:
        return None, None
    row = (
        await db.execute(select(_clients.c.industry, _clients.c.locations).where(_clients.c.id == client_id))
    ).first()
    if row is None:
        return None, None
    country = None
    for loc in row.locations or []:
        if isinstance(loc, dict) and loc.get("country"):
            country = str(loc["country"])
            break
    return row.industry, country


async def placement_facts(db: AsyncSession, p: Placement, on_date: date | None = None) -> evaluation.Facts:
    industry, country = await _client_facts(db, p.client_id)
    title = p.role_title
    if not title and p.demand_id is not None:
        title = (
            await db.execute(select(_demands.c.title).where(_demands.c.id == p.demand_id))
        ).scalar_one_or_none()
    return evaluation.Facts(
        client_id=p.client_id,
        industry=industry,
        country=country or ("India" if p.work_state_code else None),
        worker_type=p.worker_type,
        engagement_kind=p.engagement_kind,
        role_title=title,
        on_date=on_date,
    )


async def _requirements(db: AsyncSession) -> list[ComplianceRequirement]:
    return list(
        (
            await db.execute(
                select(ComplianceRequirement).where(
                    ComplianceRequirement.active.is_(True), ComplianceRequirement.archived_at.is_(None)
                )
            )
        ).scalars()
    )


def _event(
    p_tenant: uuid.UUID,
    subject_kind: str,
    subject_id: uuid.UUID,
    candidate_id: uuid.UUID,
    placement_id: uuid.UUID | None,
    items: Sequence[evaluation.Item],
) -> ComplianceRequirementsEvaluated:
    s = evaluation.summary(items)

    def _uuids(refs: Iterable[str]) -> tuple[uuid.UUID, ...]:
        out = []
        for r in refs:
            try:
                out.append(uuid.UUID(r))
            except ValueError:
                continue
        return tuple(out)

    return ComplianceRequirementsEvaluated(
        tenant_id=p_tenant,
        subject_kind=subject_kind,
        subject_id=subject_id,
        candidate_id=candidate_id,
        placement_id=placement_id,
        bgv_package_ids=_uuids(s["bgv_package"]),
        credential_type_keys=s["credential"],
        document_categories=s["document"],
        policy_template_ids=_uuids(s["policy_ack"]),
    )


# ================================================================ evaluation
async def evaluate_placement(
    db: AsyncSession, p: Placement, *, retroactive: bool = False, emit_event: bool = True
) -> list[evaluation.Item]:
    d = await today(db)
    items = evaluation.applicable(await _requirements(db), await placement_facts(db, p, d))
    existing = {
        (i.kind, i.ref): i
        for i in (
            await db.execute(
                select(PlacementComplianceItem).where(PlacementComplianceItem.placement_id == p.id)
            )
        ).scalars()
    }
    wanted = {(i.kind, i.ref) for i in items}
    new_ids: set[uuid.UUID] = set()
    for it in items:
        row = existing.get((it.kind, it.ref))
        if row is None:
            new_id = uuid.uuid4()
            new_ids.add(new_id)
            db.add(
                PlacementComplianceItem(
                    id=new_id,
                    tenant_id=p.tenant_id,
                    placement_id=p.id,
                    candidate_id=p.candidate_id,
                    requirement_ids=list(it.requirement_ids),
                    kind=it.kind,
                    ref=it.ref,
                    label=it.label,
                    mandatory=it.mandatory,
                    status="missing" if it.kind != "bgv_package" else "pending",
                    retroactive=retroactive and p.status == "active",
                )
            )
        else:
            row.label = it.label
            row.mandatory = it.mandatory
            row.requirement_ids = list(it.requirement_ids)
            row.closed_at = None
    for key, row in existing.items():
        if key not in wanted and row.status != "waived":
            await db.delete(row)
    await db.flush()
    await refresh_placement(db, p, new_ids=new_ids)
    if emit_event:
        emit(db, _event(p.tenant_id, "placement", p.id, p.candidate_id, p.id, items))
    return items


async def evaluate_subject(
    ctx: RequestContext, db: AsyncSession, subject_kind: str, subject_id: uuid.UUID
) -> int:
    """Idempotent evaluation of a Placement or a Selected Application. Returns the number of items."""
    if subject_kind == "placement":
        p = (
            await db.execute(select(Placement).where(Placement.id == subject_id).with_for_update())
        ).scalar_one_or_none()
        if p is None or p.status not in OPEN_PLACEMENT_STATUSES:
            return 0
        return len(await evaluate_placement(db, p))
    settings = await get_settings_row(db)
    if settings.evaluate_at != "application_selected":
        return 0
    app = (
        await db.execute(
            select(_applications.c.candidate_id, _demands.c.title, _demands.c.client_id)
            .join(_demands, _demands.c.id == _applications.c.demand_id)
            .where(_applications.c.id == subject_id)
        )
    ).first()
    if app is None:
        return 0
    industry, country = await _client_facts(db, app.client_id)
    facts = evaluation.Facts(
        client_id=app.client_id,
        industry=industry,
        country=country,
        role_title=app.title,
        on_date=await today(db),
    )
    items = evaluation.applicable(await _requirements(db), facts)
    from app.services.compliance.common import tenant_of

    emit(db, _event(tenant_of(db), "application", subject_id, app.candidate_id, None, items))
    return len(items)


async def reevaluate_active(
    ctx: RequestContext, db: AsyncSession, run_id: uuid.UUID
) -> ComplianceEvaluationRun:
    run = await db.get(ComplianceEvaluationRun, run_id)
    if run is None:
        raise not_found("Evaluation run")
    placements = list(
        (
            await db.execute(
                select(Placement).where(Placement.status.in_(REEVALUATE_STATUSES)).order_by(Placement.id)
            )
        ).scalars()
    )
    newly: list[uuid.UUID] = []
    for p in placements:
        before, _ = await flags(db, p)
        await evaluate_placement(db, p, retroactive=True)
        after, _ = await flags(db, p)
        if after and not before:
            newly.append(p.id)
    run.placements_evaluated = len(placements)
    run.newly_non_compliant = newly
    run.finished_at = now()
    await db.flush()
    return run


# ================================================================ statuses and flags
async def _offsets_by_type(db: AsyncSession) -> dict[str, list[int]]:
    settings = await get_settings_row(db)
    return {
        t.key: list(t.alert_offsets or settings.default_alert_offsets)
        for t in (await db.execute(select(CredentialType))).scalars()
    }


async def _document_met(db: AsyncSession, candidate_id: uuid.UUID, category: str, on_date: date) -> bool:
    from app.services import documents_service

    try:
        docs = await documents_service.list_documents(system_ctx(db), db, "candidate", candidate_id)
    except Exception:  # documents unavailable -> stays missing
        logger.exception("could not list documents of candidate {}", candidate_id)
        return False
    return any(d.category == category and (d.expires_on is None or d.expires_on >= on_date) for d in docs)


async def refresh_placement(
    db: AsyncSession,
    p: Placement,
    *,
    on_date: date | None = None,
    offsets: dict[str, list[int]] | None = None,
    new_ids: set[uuid.UUID] | None = None,
) -> tuple[bool, bool]:
    """Recompute item statuses and flags; publish changes as events. Returns ``(non_compliant, blocking)``."""
    from app.services.compliance import credentials

    d = on_date or await today(db)
    offsets = offsets if offsets is not None else await _offsets_by_type(db)
    items = list(
        (
            await db.execute(
                select(PlacementComplianceItem).where(
                    PlacementComplianceItem.placement_id == p.id, PlacementComplianceItem.closed_at.is_(None)
                )
            )
        ).scalars()
    )
    for it in items:
        old = it.status
        if it.status == "waived":
            continue
        if it.kind == "credential":
            status, best = credentials.best_of(
                await credentials.current_versions(db, p.candidate_id, it.ref), d, offsets
            )
            it.status = status
            it.credential_id = best.id if best else None
        elif it.kind == "document":
            it.status = "met" if await _document_met(db, p.candidate_id, it.ref, d) else "missing"
        elif it.kind == "bgv_package":
            it.status = "pending"
        # policy_ack: set by the PolicyAcknowledged handler
        is_new = it.id in (new_ids or set())
        if it.kind == "credential" and it.mandatory and (old != it.status or is_new):
            emit(
                db,
                CredentialStatusChanged(
                    tenant_id=p.tenant_id,
                    credential_id=it.credential_id or it.id,
                    candidate_id=p.candidate_id,
                    credential_type=it.ref,
                    from_status=_flag_status(old) if old else "missing",
                    to_status=_flag_status(it.status),
                    mandatory=True,
                    mandatory_for_placement_ids=(p.id,),
                ),
            )
    await db.flush()
    nc, blocking = _flags(p, items)
    if nc != bool(p.non_compliant) or blocking != bool(p.compliance_blocked):
        emit(
            db,
            PlacementComplianceChanged(
                tenant_id=p.tenant_id, placement_id=p.id, compliant=not nc, blocking=blocking
            ),
        )
    return nc, blocking


def _bad(p: Placement, it: PlacementComplianceItem) -> bool:
    if not it.mandatory or it.closed_at is not None or it.status == "waived":
        return False
    return it.status == "expired" or (it.status == "missing" and p.status == "active")


def _flags(p: Placement, items: Sequence[PlacementComplianceItem]) -> tuple[bool, bool]:
    bad = [it for it in items if _bad(p, it)]
    return bool(bad), any(not it.retroactive for it in bad)


async def flags(db: AsyncSession, p: Placement) -> tuple[bool, bool]:
    items = list(
        (
            await db.execute(
                select(PlacementComplianceItem).where(
                    PlacementComplianceItem.placement_id == p.id, PlacementComplianceItem.closed_at.is_(None)
                )
            )
        ).scalars()
    )
    return _flags(p, items)


async def refresh_for_candidate(
    db: AsyncSession, candidate_id: uuid.UUID, type_keys: Sequence[str] | None = None
) -> int:
    stmt = select(Placement).where(
        Placement.candidate_id == candidate_id, Placement.status.in_(OPEN_PLACEMENT_STATUSES)
    )
    if type_keys:
        stmt = stmt.where(
            Placement.id.in_(
                select(PlacementComplianceItem.placement_id).where(
                    PlacementComplianceItem.kind == "credential",
                    PlacementComplianceItem.ref.in_(list(type_keys)),
                )
            )
        )
    count = 0
    for p in (await db.execute(stmt)).scalars():
        await refresh_placement(db, p)
        count += 1
    return count


async def refresh_all(db: AsyncSession, on_date: date | None = None) -> int:
    offsets = await _offsets_by_type(db)
    ps = list(
        (
            await db.execute(
                select(Placement).where(
                    Placement.status.in_(OPEN_PLACEMENT_STATUSES),
                    Placement.id.in_(select(PlacementComplianceItem.placement_id)),
                )
            )
        ).scalars()
    )
    for p in ps:
        await refresh_placement(db, p, on_date=on_date, offsets=offsets)
    return len(ps)


# ================================================================ handlers
async def close_placement(db: AsyncSession, placement_id: uuid.UUID) -> int:
    rows = list(
        (
            await db.execute(
                select(PlacementComplianceItem).where(
                    PlacementComplianceItem.placement_id == placement_id,
                    PlacementComplianceItem.closed_at.is_(None),
                )
            )
        ).scalars()
    )
    for r in rows:
        r.closed_at = now()
    await db.flush()
    return len(rows)


async def policy_acknowledged(db: AsyncSession, candidate_id: uuid.UUID, template_id: uuid.UUID) -> int:
    rows = list(
        (
            await db.execute(
                select(PlacementComplianceItem)
                .join(Placement, Placement.id == PlacementComplianceItem.placement_id)
                .where(
                    Placement.candidate_id == candidate_id,
                    PlacementComplianceItem.kind == "policy_ack",
                    PlacementComplianceItem.ref == str(template_id),
                    PlacementComplianceItem.closed_at.is_(None),
                )
            )
        ).scalars()
    )
    for r in rows:
        if r.status != "waived":
            r.status = "met"
    await db.flush()
    for pid in {r.placement_id for r in rows}:
        p = await db.get(Placement, pid)
        if p is not None:
            await refresh_placement(db, p)
    return len(rows)


# ================================================================ API
async def _placement(db: AsyncSession, placement_id: uuid.UUID) -> Placement:
    p = await db.get(Placement, placement_id)
    if p is None:
        raise not_found("Placement")
    return p


async def get_checklist(
    ctx: RequestContext, db: AsyncSession, placement_id: uuid.UUID
) -> PlacementComplianceOut:
    p = await _placement(db, placement_id)
    items = list(
        (
            await db.execute(
                select(PlacementComplianceItem)
                .where(PlacementComplianceItem.placement_id == p.id)
                .order_by(PlacementComplianceItem.kind, PlacementComplianceItem.label)
            )
        ).scalars()
    )
    req_ids = {rid for it in items for rid in it.requirement_ids}
    names = {
        r.id: r.name
        for r in (
            await db.execute(
                select(ComplianceRequirement).where(
                    ComplianceRequirement.id.in_(list(req_ids) or [uuid.uuid4()])
                )
            )
        ).scalars()
    }
    types = {t.key for t in (await db.execute(select(CredentialType))).scalars()}
    nc, blocking = _flags(p, [i for i in items if i.closed_at is None])
    return PlacementComplianceOut(
        placement_id=p.id,
        non_compliant=nc,
        blocking=blocking,
        items=[
            ChecklistItemOut(
                id=i.id,
                kind=i.kind,
                ref=i.ref,
                label=i.label,
                mandatory=i.mandatory,
                status=i.status,
                retroactive=i.retroactive,
                credential_id=i.credential_id,
                waived_reason=i.waived_reason,
                waived_by=i.waived_by,
                waived_at=i.waived_at,
                requirement_names=sorted(names[r] for r in i.requirement_ids if r in names),
                unresolved=i.kind == "credential" and i.ref not in types,
            )
            for i in items
        ],
    )


async def waive(
    ctx: RequestContext, db: AsyncSession, placement_id: uuid.UUID, item_id: uuid.UUID, reason: str
) -> PlacementComplianceOut:
    uid = require_human(ctx, COMPLIANCE_WAIVE)
    assert_no_full_aadhaar(reason)
    p = await _placement(db, placement_id)
    it = await db.get(PlacementComplianceItem, item_id)
    if it is None or it.placement_id != p.id:
        raise not_found("Checklist item")
    if it.status == "waived":
        raise conflict("already_waived", "This item is already waived")
    it.status = "waived"
    it.waived_reason = reason
    it.waived_by = uid
    it.waived_at = now()
    await db.flush()
    audit.record("compliance.item_waived", "placement", p.id, session=db, kind=it.kind, ref=it.ref)
    await refresh_placement(db, p)
    return await get_checklist(ctx, db, placement_id)


# ================================================================ start gate (registered in hooks)
async def gate_resolver(ctx: RequestContext, db: AsyncSession, placement_id: uuid.UUID) -> list[Any]:
    from app.services.placements.registry import (
        GateStatus,  # noqa: TC001 - local import avoids a circular import
    )
    from app.services.placements_service import GateState

    rows = list(
        (
            await db.execute(
                select(PlacementComplianceItem).where(
                    PlacementComplianceItem.placement_id == placement_id,
                    PlacementComplianceItem.kind == "credential",
                    PlacementComplianceItem.mandatory.is_(True),
                    PlacementComplianceItem.closed_at.is_(None),
                )
            )
        ).scalars()
    )
    out = []
    for r in rows:
        status: GateStatus = (
            "met"
            if r.status in ("met", "expiring")
            else "waived"
            if r.status == "waived"
            else "failed"
            if r.status == "expired"
            else "pending"
        )
        out.append(GateState(r.ref, status, True, {"label": r.label}))
    return out
