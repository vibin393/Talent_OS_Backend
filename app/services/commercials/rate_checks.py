"""Rate checks and the rate-exception gate (Epic 16 A6, FR-91).

* ``validate_rate`` -- the single check function (contract layer). Persists a ``rate_checks`` row when the
  subject is given, otherwise a pure preview. No gate.
* ``require_rate_clearance`` -- the gate used by the proceed points (Demand submit, Submission create, Offer
  release; Vendor Release, Placement activation, Extension and Rate Segment from their epics). Passes for
  ``ok`` / ``not_validated`` or an **approved** exception with the same ``input_hash`` on the same subject;
  otherwise starts (once) a ``rate_exception`` approval and raises 409 ``rate_exception_required``
  (``rate_exception_pending`` / ``rate_exception_rejected`` on retries).

The refusal is an error response, so the request transaction rolls back; the rate check and its approval
request are therefore written in their own (committed) tenant session so the approver can act on them.
"""

import hashlib
import json
import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.actors import actor_from_ctx
from app.core.context import RequestContext
from app.core.errors import ConflictError
from app.core.events import emit
from app.events.commercials import RateExceptionDecided
from app.models.commercials import RateCheck
from app.schemas.commercials import RateCheckOut, RateCheckPreviewOut, RateCheckRequest, RateCheckResult
from app.services.commercials import rate_cards
from app.services.commercials.common import tenant_of
from app.services.commercials.rate_validation import (
    STATUS_OF,
    RateQuery,
    evaluate,
    match_line,
    needs_exception,
)

APPROVAL_SUBJECT = "rate_exception"


def _s(value: Decimal | None) -> str | None:
    return None if value is None else format(value.normalize(), "f")


def canonical_input(req: RateCheckRequest) -> dict[str, Any]:
    return {
        "client_id": str(req.client_id),
        "contract_id": str(req.contract_id) if req.contract_id else None,
        "role": (req.role or "").strip() or None,
        "location": (req.location or "").strip() or None,
        "experience_years": _s(req.experience_years),
        "bill_rate": _s(req.bill_rate),
        "vendor_rate": _s(req.vendor_rate),
        "currency": req.currency.upper(),
        "rate_unit": req.rate_unit,
    }


def input_hash(req: RateCheckRequest) -> str:
    return hashlib.sha256(json.dumps(canonical_input(req), sort_keys=True).encode()).hexdigest()


async def evaluate_request(db: AsyncSession, req: RateCheckRequest) -> dict[str, Any]:
    """Resolve the effective card and grade the request (no writes)."""
    card = await rate_cards.effective_card(db, req.client_id, req.on_date, req.contract_id)
    if card is None:
        return {"result": "not_validated", "rate_card_id": None, "line": None}
    lines = await rate_cards.effective_lines(db, card)
    query = RateQuery(
        role=req.role,
        location=req.location,
        experience_years=req.experience_years,
        currency=req.currency,
        rate_unit=req.rate_unit,
        bill_rate=req.bill_rate,
        vendor_rate=req.vendor_rate,
    )
    line = match_line(lines, query)
    return {"result": evaluate(line, query), "rate_card_id": card.id, "line": line}


def _result(graded: dict[str, Any], rate_check_id: uuid.UUID | None) -> RateCheckResult:
    line = graded["line"]
    return RateCheckResult(
        status=STATUS_OF[graded["result"]],
        matched_line_id=line.id if line else None,
        min_bill_rate=line.min_bill_rate if line else None,
        max_bill_rate=line.max_bill_rate if line else None,
        max_vendor_rate=line.max_vendor_rate if line else None,
        requires_exception=needs_exception(graded["result"]),
        rate_check_id=rate_check_id,
    )


def _row(ctx: RequestContext, db: AsyncSession, req: RateCheckRequest, graded: dict[str, Any]) -> RateCheck:
    assert req.subject_id is not None
    line = graded["line"]
    actor = actor_from_ctx(ctx)
    return RateCheck(
        id=uuid.uuid4(),
        tenant_id=tenant_of(db),
        client_id=req.client_id,
        subject_kind=req.subject_kind,
        subject_id=req.subject_id,
        input_hash=input_hash(req),
        input={**canonical_input(req), "on_date": req.on_date.isoformat()},
        result=graded["result"],
        rate_card_id=graded["rate_card_id"],
        rate_card_line_id=line.id if line else None,
        min_bill_rate=line.min_bill_rate if line else None,
        max_bill_rate=line.max_bill_rate if line else None,
        max_vendor_rate=line.max_vendor_rate if line else None,
        exception_status="none",
        actor_kind=actor.kind,
        actor_id=actor.id,
    )


async def validate_rate(ctx: RequestContext, db: AsyncSession, req: RateCheckRequest) -> RateCheckResult:
    graded = await evaluate_request(db, req)
    if req.subject_id is None:
        return _result(graded, None)
    row = _row(ctx, db, req, graded)
    db.add(row)
    await db.flush()
    return _result(graded, row.id)


async def preview(ctx: RequestContext, db: AsyncSession, req: RateCheckRequest) -> RateCheckPreviewOut:  # ruff: ignore[unused-function-argument]
    graded = await evaluate_request(db, req)
    res = _result(graded, None)
    return RateCheckPreviewOut(
        result=graded["result"],
        status=res.status,
        requires_exception=res.requires_exception,
        rate_card_id=graded["rate_card_id"],
        matched_line_id=res.matched_line_id,
        min_bill_rate=res.min_bill_rate,
        max_bill_rate=res.max_bill_rate,
        max_vendor_rate=res.max_vendor_rate,
    )


def _refusal(code: str, message: str, row: RateCheck) -> ConflictError:
    return ConflictError(
        message,
        code=code,
        details={
            "rate_check_id": str(row.id),
            "approval_request_id": str(row.approval_request_id) if row.approval_request_id else None,
            "result": row.result,
            "min": _s(row.min_bill_rate),
            "max": _s(row.max_bill_rate),
            "subject_kind": row.subject_kind,
            "subject_id": str(row.subject_id),
        },
    )


async def _latest_exception(db: AsyncSession, req: RateCheckRequest, digest: str) -> RateCheck | None:
    return (
        await db.execute(
            select(RateCheck)
            .where(
                RateCheck.subject_kind == req.subject_kind,
                RateCheck.subject_id == req.subject_id,
                RateCheck.input_hash == digest,
                RateCheck.exception_status != "none",
            )
            .order_by(RateCheck.checked_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def _start_exception(ctx: RequestContext, req: RateCheckRequest, graded: dict[str, Any]) -> RateCheck:
    """Persist the failing check + its approval request in a committed session of its own."""
    from app.core.database import tenant_session
    from app.services import approvals
    from app.services.approvals import StepSpec
    from app.services.commercials import settings as settings_mod

    assert ctx.tenant_id is not None
    async with tenant_session(ctx.tenant_id) as own:
        row = _row(ctx, own, req, graded)
        own.add(row)
        await own.flush()
        settings = await settings_mod.get_row(own)
        steps = [
            StepSpec(
                name=s.get("name") or "Approval",
                approver_kind=s["approver_kind"],
                approver_ref=s.get("approver_ref") or {},
            )
            for s in settings.rate_exception_steps
        ]
        facts = {
            "subject_kind": req.subject_kind,
            "subject_id": str(req.subject_id),
            "client_id": str(req.client_id),
            "result": graded["result"],
            "bill_rate": _s(req.bill_rate),
            "currency": req.currency,
            "rate_unit": req.rate_unit,
            "min": _s(row.min_bill_rate),
            "max": _s(row.max_bill_rate),
        }
        result = await approvals.start(ctx, own, APPROVAL_SUBJECT, row.id, facts, steps=steps)
        row.approval_request_id = result.request_id
        row.exception_status = "approved" if result.status == "auto_approved" else "pending"
        await own.flush()
        await own.refresh(row)
        own.expunge(row)
    return row


async def require_rate_clearance(
    ctx: RequestContext, db: AsyncSession, req: RateCheckRequest
) -> RateCheckResult:
    if req.subject_id is None:
        msg = "require_rate_clearance needs a subject"
        raise ValueError(msg)
    graded = await evaluate_request(db, req)
    if not needs_exception(graded["result"]):
        return await validate_rate(ctx, db, req)
    digest = input_hash(req)
    previous = await _latest_exception(db, req, digest)
    if previous is not None:
        if previous.exception_status == "approved":
            row = _row(ctx, db, req, graded)
            row.exception_status = "approved"
            row.approval_request_id = previous.approval_request_id
            db.add(row)
            await db.flush()
            return _result(graded, row.id)
        if previous.exception_status == "pending":
            raise _refusal(
                "rate_exception_pending", "A rate exception for this rate is awaiting approval", previous
            )
        if previous.exception_status == "rejected":
            raise _refusal(
                "rate_exception_rejected", "The rate exception was rejected; change the rate", previous
            )
    started = await _start_exception(ctx, req, graded)
    if started.exception_status == "approved":
        return _result(graded, started.id)
    raise _refusal("rate_exception_required", "Outside rate card -- needs approval", started)


async def history(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,
    subject_kind: str,
    subject_id: uuid.UUID,
) -> list[RateCheckOut]:
    rows = (
        await db.execute(
            select(RateCheck)
            .where(RateCheck.subject_kind == subject_kind, RateCheck.subject_id == subject_id)
            .order_by(RateCheck.checked_at.desc())
            .limit(200)
        )
    ).scalars()
    return [RateCheckOut.model_validate(r) for r in rows]


async def on_final_decided(ctx: RequestContext, db: AsyncSession, decision: Any) -> None:
    """``rate_exception`` approval callback (decision transaction): record the outcome."""
    row = (
        await db.execute(select(RateCheck).where(RateCheck.id == decision.subject_id).with_for_update())
    ).scalar_one_or_none()
    if row is None:
        return
    outcome = "approved" if decision.outcome in ("approved", "auto_approved") else "rejected"
    row.exception_status = outcome
    await db.flush()
    emit(
        db,
        RateExceptionDecided(
            tenant_id=row.tenant_id,
            rate_check_id=row.id,
            subject_kind=row.subject_kind,
            subject_id=row.subject_id,
            outcome=outcome,
        ),
    )
    from app.services.commercials.common import timeline

    entity = {"submission": "application"}.get(row.subject_kind, row.subject_kind)
    if entity in ("demand", "application", "offer"):
        try:
            async with db.begin_nested():
                await timeline(
                    ctx,
                    db,
                    entity,
                    row.subject_id,
                    f"Rate exception {outcome}",
                    {"rate_check_id": str(row.id)},
                )
        except Exception:
            pass


async def describe(ctx: RequestContext, db: AsyncSession, ids: list[uuid.UUID]) -> dict[uuid.UUID, Any]:  # ruff: ignore[unused-function-argument]
    from app.schemas.approvals import SubjectSummary

    rows = (await db.execute(select(RateCheck).where(RateCheck.id.in_(ids)))).scalars()
    out: dict[uuid.UUID, Any] = {}
    for r in rows:
        rate = r.input.get("bill_rate")
        bounds = f" (card {_s(r.min_bill_rate)}-{_s(r.max_bill_rate)})" if r.min_bill_rate is not None else ""
        out[r.id] = SubjectSummary(
            label=f"Rate exception: {r.subject_kind}",
            title=f"{r.input.get('role') or ''} at {rate} {r.input.get('currency')}/{r.input.get('rate_unit')}{bounds}".strip(),  # ruff: ignore[line-too-long]
        )
    return out
