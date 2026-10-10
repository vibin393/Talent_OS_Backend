"""Start Gates (Epic 19 A5, FR-106, AD-25).

Gate rows are written **only** here: by the upstream-event handlers (``handle_upstream``) and by the registered
resolvers (``refresh``). ``assert_clear_for_update`` is the public helper Joined (and the extension epic, with
``["credential", "po"]``) calls: it re-runs the resolvers (closing the race with late events), re-selects the gates
``FOR UPDATE`` and raises 409 ``start_gates_unmet`` unless every required gate is met / waived / not required.

Overrides (``start_gate_override`` approval subject): the final-approved callback runs in the decision transaction
and waives the gates. Failed non-overridable gates (BGV) are refused at request time (409 ``gate_not_overridable``).
"""

import uuid
from collections.abc import Mapping, Sequence
from datetime import date
from typing import Any

from loguru import logger
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.actors import actor_from_ctx
from app.core.context import RequestContext
from app.core.errors import ConflictError, DomainError, ValidationFailed
from app.core.events import emit
from app.events.placements import StartGateStatusChanged
from app.models.placements import Placement, StartGate, StartGateOverride
from app.schemas.placements_api import GateOverrideOut, StartGateOut
from app.services.placements.common import PRE_START, get_placement, now
from app.services.placements.registry import GateState, StartGateKindDef, start_gate_kinds

OVERRIDE_SUBJECT = "start_gate_override"
CLEAR = frozenset({"met", "waived", "not_required"})
# Kinds announced by events before their owning epic registers them.
_FALLBACK: dict[str, StartGateKindDef] = {
    "bgv": StartGateKindDef("Background verification", overridable=False),
    "onboarding": StartGateKindDef("Onboarding", overridable=True),
    "credential": StartGateKindDef("Credential", overridable=True),
    "contract": StartGateKindDef("Client contract", overridable=True),
    "po": StartGateKindDef("Purchase order", overridable=True),
}


def kind_def(kind: str) -> StartGateKindDef:
    return (
        start_gate_kinds().get(kind)
        or _FALLBACK.get(kind)
        or StartGateKindDef(kind.title(), overridable=True)
    )


def gate_out(g: StartGate) -> StartGateOut:
    d = kind_def(g.kind)
    out = StartGateOut.model_validate(g)
    return out.model_copy(update={"label": d.label, "overridable": d.overridable})


async def upsert_gate(db: AsyncSession, placement: Placement, kind: str, state: GateState) -> StartGate:
    """Idempotent per ``(placement, kind, source_ref)``. A waived gate stays waived unless it becomes met / not
    required. Emits ``StartGateStatusChanged`` when the status changes."""
    inserted = await db.execute(
        pg_insert(StartGate)
        .values(
            id=uuid.uuid4(),
            tenant_id=placement.tenant_id,
            placement_id=placement.id,
            kind=kind,
            source_ref=state.source_ref,
            required=state.required,
            status=state.status,
            detail=dict(state.detail),
            refreshed_at=now(),
        )
        .on_conflict_do_nothing(constraint="uq_start_gates_key")
    )
    created = bool(getattr(inserted, "rowcount", 0))
    gate = (
        await db.execute(
            select(StartGate)
            .where(
                StartGate.placement_id == placement.id,
                StartGate.kind == kind,
                StartGate.source_ref == state.source_ref,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    previous = gate.status
    new_status = state.status
    if previous == "waived" and new_status not in ("met", "not_required"):
        new_status = "waived"
    gate.status = new_status
    gate.required = state.required
    gate.detail = {**(gate.detail or {}), **dict(state.detail)}
    gate.refreshed_at = now()
    await db.flush()
    if created or previous != new_status:
        emit(
            db,
            StartGateStatusChanged(
                tenant_id=placement.tenant_id,
                placement_id=placement.id,
                gate_kind=kind,
                source_ref=state.source_ref,
                status=new_status,
            ),
        )
    return gate


async def refresh(
    ctx: RequestContext, db: AsyncSession, placement: Placement, kinds: Sequence[str] | None = None
) -> None:
    """Re-run the registered resolvers (all kinds, or ``kinds``) and upsert their states."""
    for kind, definition in start_gate_kinds().items():
        if kinds is not None and kind not in kinds:
            continue
        if definition.resolver is None:
            continue
        try:
            async with db.begin_nested():
                states = await definition.resolver(ctx, db, placement.id)
        except NotImplementedError:
            continue
        except Exception:
            logger.exception("start-gate resolver {} failed for placement {}", kind, placement.id)
            continue
        for state in states:
            await upsert_gate(db, placement, kind, state)


async def unmet_gates(
    db: AsyncSession, placement_id: uuid.UUID, kinds: Sequence[str] | None = None
) -> list[StartGate]:
    stmt = select(StartGate).where(StartGate.placement_id == placement_id).with_for_update()
    if kinds is not None:
        stmt = stmt.where(StartGate.kind.in_(list(kinds)))
    rows = list((await db.execute(stmt.execution_options(populate_existing=True))).scalars())
    return [g for g in rows if g.required and g.status not in CLEAR]


async def assert_clear_for_update(
    ctx: RequestContext, db: AsyncSession, placement_id: uuid.UUID, kinds: Sequence[str] | None = None
) -> None:
    placement = await get_placement(db, placement_id)
    await refresh(ctx, db, placement, kinds)
    unmet = await unmet_gates(db, placement_id, kinds)
    if unmet:
        raise ConflictError(
            "Start gates are not met",
            code="start_gates_unmet",
            details={
                "gates": [
                    {
                        "id": str(g.id),
                        "kind": g.kind,
                        "label": kind_def(g.kind).label,
                        "source_ref": g.source_ref,
                        "status": g.status,
                    }
                    for g in unmet
                ]
            },
        )


async def list_gates(ctx: RequestContext, db: AsyncSession, placement_id: uuid.UUID) -> list[StartGateOut]:  # noqa: ARG001
    await get_placement(db, placement_id)
    rows = (
        await db.execute(
            select(StartGate)
            .where(StartGate.placement_id == placement_id)
            .order_by(StartGate.kind, StartGate.created_at)
        )
    ).scalars()
    return [gate_out(g) for g in rows]


# ================================================================ overrides
async def request_override(
    ctx: RequestContext, db: AsyncSession, placement_id: uuid.UUID, gate_ids: Sequence[uuid.UUID], reason: str
) -> GateOverrideOut:
    from app.services import approvals

    placement = await get_placement(db, placement_id, lock=True)
    if placement.status not in PRE_START:
        raise ConflictError(
            "Only pre-joining placements have start gates to override", code="transition_not_allowed"
        )
    gates = list(
        (
            await db.execute(
                select(StartGate).where(
                    StartGate.placement_id == placement_id, StartGate.id.in_(list(gate_ids))
                )
            )
        ).scalars()
    )
    if len(gates) != len(set(gate_ids)):
        raise ValidationFailed("Unknown start gate", code="unknown_gate")
    for g in gates:
        if g.status == "failed" and not kind_def(g.kind).overridable:
            raise ConflictError(
                f"A failed {kind_def(g.kind).label} gate cannot be overridden",
                code="gate_not_overridable",
                details={"gate_id": str(g.id), "kind": g.kind},
            )
    actor = actor_from_ctx(ctx)
    row = StartGateOverride(
        tenant_id=placement.tenant_id,
        placement_id=placement.id,
        gate_ids=[g.id for g in gates],
        reason=reason,
        status="pending",
        requested_by=actor.id,
    )
    db.add(row)
    await db.flush()
    facts = {
        "placement_id": str(placement.id),
        "client_id": str(placement.client_id),
        "gate_kinds": sorted({g.kind for g in gates}),
    }
    result = await approvals.start(ctx, db, OVERRIDE_SUBJECT, row.id, facts)
    if result.request_id is not None:
        row.approval_request_id = result.request_id
    await db.flush()
    await db.refresh(row)
    return GateOverrideOut.model_validate(row)


async def list_overrides(
    ctx: RequestContext,
    db: AsyncSession,
    placement_id: uuid.UUID,
) -> list[GateOverrideOut]:
    await get_placement(db, placement_id)
    rows = (
        await db.execute(
            select(StartGateOverride)
            .where(StartGateOverride.placement_id == placement_id)
            .order_by(StartGateOverride.created_at.desc())
        )
    ).scalars()
    return [GateOverrideOut.model_validate(r) for r in rows]


async def on_override_decided(ctx: RequestContext, db: AsyncSession, decision: Any) -> None:  # noqa: ARG001
    """Apply the final decision of ``start_gate_override`` (runs in the decision transaction)."""
    row = (
        await db.execute(
            select(StartGateOverride).where(StartGateOverride.id == decision.subject_id).with_for_update()
        )
    ).scalar_one_or_none()
    if row is None or row.status != "pending":
        return
    approved = decision.outcome in ("approved", "auto_approved")
    row.status = "approved" if approved else "rejected"
    row.decided_at = now()
    row.decided_by = getattr(decision.actor, "id", None) if decision.actor else None
    placement = (await db.execute(select(Placement).where(Placement.id == row.placement_id))).scalar_one()
    if approved:
        gates = (
            await db.execute(select(StartGate).where(StartGate.id.in_(row.gate_ids)).with_for_update())
        ).scalars()
        for g in gates:
            if g.status in ("met", "not_required"):
                continue
            g.status = "waived"
            g.override_id = row.id
            emit(
                db,
                StartGateStatusChanged(
                    tenant_id=placement.tenant_id,
                    placement_id=placement.id,
                    gate_kind=g.kind,
                    source_ref=g.source_ref,
                    status="waived",
                ),
            )
    await db.flush()
    audit.record(
        "placement.start_gate_override_decided",
        "placement",
        placement.id,
        session=db,
        override_id=str(row.id),
        outcome=row.status,
        reason=row.reason,
        approver_id=str(row.decided_by) if row.decided_by else None,
    )


async def describe_overrides(
    ctx: RequestContext,
    db: AsyncSession,
    ids: list[uuid.UUID],
) -> dict[uuid.UUID, Any]:
    from app.schemas.approvals import SubjectSummary

    rows = (
        await db.execute(
            select(StartGateOverride, Placement.human_id)
            .join(Placement, Placement.id == StartGateOverride.placement_id)
            .where(StartGateOverride.id.in_(ids))
        )
    ).all()
    return {
        r.id: SubjectSummary(label="Start gate override", title=f"{human}: {r.reason[:120]}")
        for r, human in rows
    }


# ================================================================ resolvers registered by this epic
async def contract_resolver(
    ctx: RequestContext, db: AsyncSession, placement_id: uuid.UUID
) -> list[GateState]:
    from app.services import commercials_service

    p = await get_placement(db, placement_id)
    on = p.planned_start_date
    contract = await commercials_service.active_contract_for(ctx, db, p.client_id, p.legal_entity_id, on)
    if contract is None:
        return [GateState("contract", "pending", True, {"reason": "contract_missing"})]
    return [GateState("contract", "met", True, {"contract_id": str(contract.id)})]


async def po_resolver(ctx: RequestContext, db: AsyncSession, placement_id: uuid.UUID) -> list[GateState]:
    from app.services import commercials_service

    p = await get_placement(db, placement_id)
    try:
        profile = await commercials_service.get_billing_profile(ctx, db, p.client_id)
        required = bool(profile.po_required)
    except DomainError:  # no (complete) billing profile -> no PO requirement known
        required = False
    if not required:
        return [GateState("po", "not_required", False, {})]
    validity = await commercials_service.validate_po(ctx, db, p.po_id, p.planned_start_date)
    status = "met" if validity.valid else "pending"
    return [
        GateState("po", status, True, {"reason": validity.reason, "po_id": str(p.po_id) if p.po_id else None})
    ]


# ================================================================ upstream events (AD-25)
_MET = frozenset({"verified", "valid", "active", "approved", "clear"})
_FAILED = frozenset({"expired", "rejected", "failed", "revoked"})


async def _placement_or_none(db: AsyncSession, raw: Any) -> Placement | None:
    from app.services.placements.common import as_uuid

    pid = as_uuid(raw)
    if pid is None:
        return None
    return (
        await db.execute(select(Placement).where(Placement.id == pid).with_for_update())
    ).scalar_one_or_none()


async def handle_upstream(
    ctx: RequestContext, db: AsyncSession, name: str, payload: Mapping[str, Any]
) -> int:
    """Apply one upstream event (by class name). Idempotent per ``(kind, source_ref)``. Returns gates touched."""
    from app.services.placements import joining

    touched = 0
    if name in ("BgvCaseOpened", "BgvCaseStatusChanged", "BgvCaseAdjudicated"):
        p = await _placement_or_none(db, payload.get("placement_id"))
        if p is None:
            return 0
        case_ref = str(payload.get("case_id"))
        if name == "BgvCaseOpened":
            p.bgv_summary = {"case_id": case_ref, "status": "opened"}
            await upsert_gate(
                db, p, "bgv", GateState(case_ref, "pending", bool(payload.get("required", True)), {})
            )
            touched += 1
        elif name == "BgvCaseStatusChanged":
            p.bgv_summary = {**(p.bgv_summary or {}), "case_id": case_ref, "status": payload.get("to_status")}
        else:
            outcome = payload.get("outcome")
            p.bgv_summary = {**(p.bgv_summary or {}), "case_id": case_ref, "status": f"adjudicated_{outcome}"}
            status = "met" if outcome == "clear" else "failed"
            await upsert_gate(
                db,
                p,
                "bgv",
                GateState(case_ref, status, True, {"with_exceptions": bool(payload.get("with_exceptions"))}),
            )
            touched += 1
        await db.flush()
    elif name in (
        "OnboardingCaseCreated",
        "OnboardingCaseCompleted",
        "OnboardingCaseCancelled",
        "OnboardingProgressChanged",
    ):
        p = await _placement_or_none(db, payload.get("placement_id"))
        if p is None:
            return 0
        case_ref = str(payload.get("case_id"))
        if name == "OnboardingProgressChanged":
            p.onboarding_pct = max(0, min(100, int(payload.get("pct") or 0)))
            await db.flush()
            await joining.evaluate_dropout(ctx, db, p)
            return 0
        status = {
            "OnboardingCaseCreated": "pending",
            "OnboardingCaseCompleted": "met",
            "OnboardingCaseCancelled": "not_required",
        }[name]
        if name == "OnboardingCaseCompleted":
            p.onboarding_pct = 100
        await upsert_gate(db, p, "onboarding", GateState(case_ref, status, status != "not_required", {}))
        touched += 1
    elif name == "CredentialStatusChanged":
        to_status = str(payload.get("to_status") or "")
        status = "met" if to_status in _MET else "failed" if to_status in _FAILED else "pending"
        for raw in payload.get("mandatory_for_placement_ids") or ():
            p = await _placement_or_none(db, raw)
            if p is None or p.status not in (*PRE_START, "active"):
                continue
            await upsert_gate(
                db,
                p,
                "credential",
                GateState(
                    str(payload.get("credential_type")),
                    status,
                    bool(payload.get("mandatory", True)),
                    {"credential_id": str(payload.get("credential_id"))},
                ),
            )
            touched += 1
    elif name == "PlacementComplianceChanged":
        p = await _placement_or_none(db, payload.get("placement_id"))
        if p is None:
            return 0
        p.non_compliant = not bool(payload.get("compliant"))
        p.compliance_blocked = bool(payload.get("blocking"))
        await db.flush()
    elif name in ("PoValidityChanged", "ContractStatusChanged"):
        kind = "po" if name == "PoValidityChanged" else "contract"
        stmt = select(Placement).where(Placement.status.in_(PRE_START))
        if kind == "po":
            stmt = stmt.where(Placement.po_id == payload.get("po_id"))
        elif payload.get("client_id"):
            stmt = stmt.where(Placement.client_id == payload.get("client_id"))
        else:
            return 0
        for p in (await db.execute(stmt)).scalars():
            await refresh(ctx, db, p, [kind])
            touched += 1
    return touched


async def placements_starting(db: AsyncSession, on: date) -> list[Placement]:
    return list(
        (
            await db.execute(
                select(Placement).where(Placement.status.in_(PRE_START), Placement.planned_start_date == on)
            )
        ).scalars()
    )
