"""Vendor Submissions: status machine, vendor-visible timeline and internal read models (Epic 17 A5/A6).

| From | To | Actors |
|---|---|---|
| submitted | under_review | user, system |
| submitted / under_review | accepted (human only) | user |
| submitted / under_review | rejected | user, system (consent withdrawn) |
| submitted / under_review | duplicate | user, system |
| duplicate | under_review | user (reason required) |
| submitted | withdrawn | vendor_user |

``set_status`` is the only writer of ``vendor_submissions.status``; every change adds a vendor-visible
timeline
row (label only, no reason text) and stops the review SLA clock on a decision.
"""

import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.actors import ActorRef
from app.core.context import RequestContext
from app.core.events import emit
from app.events.vendors import VendorSubmissionDecided
from app.models.demands import Demand
from app.models.vendors import Vendor, VendorRelease, VendorSubmission, VendorSubmissionEvent
from app.schemas.vendors import DuplicateFinding, VendorSubmissionOut
from app.services.vendors.common import CATEGORY_LABELS, STATUS_LABELS, conflict, not_found, now

TRANSITIONS: dict[tuple[str, str], frozenset[str]] = {
    ("submitted", "under_review"): frozenset({"user", "system"}),
    ("submitted", "accepted"): frozenset({"user"}),
    ("under_review", "accepted"): frozenset({"user"}),
    ("submitted", "rejected"): frozenset({"user", "system"}),
    ("under_review", "rejected"): frozenset({"user", "system"}),
    ("submitted", "duplicate"): frozenset({"user", "system"}),
    ("under_review", "duplicate"): frozenset({"user", "system"}),
    ("duplicate", "under_review"): frozenset({"user"}),
    ("submitted", "withdrawn"): frozenset({"vendor_user"}),
}
DECIDED = frozenset({"accepted", "rejected", "duplicate"})
REVIEW_SUBJECT = "vendor_submission"
REVIEW_MILESTONE = "review"


def vendor_label(sub: VendorSubmission) -> str:
    if sub.status == "accepted" and sub.progress_label:
        return sub.progress_label
    return STATUS_LABELS.get(sub.status, sub.status)


def outcome_category(sub: VendorSubmission) -> str | None:
    """Return the vendor-visible outcome: a category label only."""
    if sub.status == "duplicate":
        return CATEGORY_LABELS.get(sub.duplicate_reason_category or "other", "Duplicate")
    if sub.status == "rejected":
        return sub.vendor_visible_reason or "Not selected"
    return None


def add_timeline(db: AsyncSession, sub: VendorSubmission, label: str) -> None:
    db.add(
        VendorSubmissionEvent(
            id=uuid.uuid4(),
            tenant_id=sub.tenant_id,
            submission_id=sub.id,
            vendor_id=sub.vendor_id,
            label=label,
            at=now(),
        )
    )


async def set_status(
    ctx: RequestContext,
    db: AsyncSession,
    sub: VendorSubmission,
    to: str,
    actor: ActorRef,
    *,
    category: str | None = None,
) -> None:
    allowed = TRANSITIONS.get((sub.status, to))
    if allowed is None or actor.kind not in allowed:
        if to == "withdrawn":
            raise conflict("already_in_review", "The submission is already in review and cannot be withdrawn")
        if to == "accepted" and allowed is not None:
            from app.core.actors import human_only_action

            raise human_only_action()
        raise conflict(
            "transition_not_allowed",
            f"A submission cannot move from {sub.status} to {to}",
            from_status=sub.status,
        )
    sub.status = to
    if to in DECIDED:
        sub.reviewed_at = now()
        sub.reviewed_by = actor.id if actor.kind == "user" else None
        await _stop_review_clock(ctx, db, sub)
    if to == "duplicate":
        sub.duplicate_reason_category = category or "other"
    sub.progress_updated_at = now()
    await db.flush()
    label = outcome_category(sub) if to in {"duplicate", "rejected"} else STATUS_LABELS.get(to, to)
    add_timeline(db, sub, label or to)
    if to in DECIDED:
        emit(
            db,
            VendorSubmissionDecided(
                tenant_id=sub.tenant_id,
                submission_id=sub.id,
                vendor_id=sub.vendor_id,
                outcome=to,
                application_id=sub.application_id,
            ),
        )


async def _stop_review_clock(ctx: RequestContext, db: AsyncSession, sub: VendorSubmission) -> None:
    from app.services import sla_service

    await sla_service.stop_clock(ctx, db, REVIEW_SUBJECT, sub.id, REVIEW_MILESTONE)


async def load(db: AsyncSession, submission_id: uuid.UUID, *, lock: bool = False) -> VendorSubmission:
    stmt = select(VendorSubmission).where(VendorSubmission.id == submission_id)
    if lock:
        stmt = stmt.with_for_update()
    sub = (await db.execute(stmt)).scalar_one_or_none()
    if sub is None:
        raise not_found("Submission")
    return sub


async def internal_outs(
    ctx: RequestContext, db: AsyncSession, subs: Sequence[VendorSubmission], *, with_margin: bool = False
) -> list[VendorSubmissionOut]:
    from app.services.vendors import review

    vendor_ids = {s.vendor_id for s in subs}
    names = (
        dict(
            (await db.execute(select(Vendor.id, Vendor.legal_name).where(Vendor.id.in_(vendor_ids))))
            .tuples()
            .all()
        )
        if subs
        else {}
    )
    demand_ids = {s.demand_id for s in subs}
    demands = (
        {d.id: d for d in (await db.execute(select(Demand).where(Demand.id.in_(demand_ids)))).scalars()}
        if subs
        else {}
    )
    release_ids = {s.vendor_release_id for s in subs}
    maxes = (
        dict(
            (
                await db.execute(
                    select(VendorRelease.id, VendorRelease.max_vendor_rate).where(
                        VendorRelease.id.in_(release_ids)
                    )
                )
            )
            .tuples()
            .all()
        )
        if subs
        else {}
    )
    out: list[VendorSubmissionOut] = []
    for s in subs:
        d = demands.get(s.demand_id)
        margin = review.margin_preview(ctx, s, d) if with_margin and d is not None else None
        out.append(
            VendorSubmissionOut(
                id=s.id,
                human_id=s.human_id,
                vendor_release_id=s.vendor_release_id,
                vendor_id=s.vendor_id,
                vendor_name=names.get(s.vendor_id),
                demand_id=s.demand_id,
                demand_title=d.title if d else None,
                client_id=s.client_id,
                full_name=s.full_name,
                email=s.email,
                phone_e164=s.phone_e164,
                current_location=s.current_location,
                total_experience_months=s.total_experience_months,
                availability_date=s.availability_date,
                notice_period_days=s.notice_period_days,
                vendor_rate=s.vendor_rate,
                rate_unit=s.rate_unit,
                currency=s.currency,
                max_vendor_rate=maxes.get(s.vendor_release_id),
                status=s.status,
                duplicate_reason_category=s.duplicate_reason_category,
                reject_reason_code=s.reject_reason_code,
                reason_text_internal=s.reason_text_internal,
                duplicate_findings=[DuplicateFinding(**f) for f in (s.duplicate_findings or [])],
                candidate_id=s.candidate_id,
                application_id=s.application_id,
                progress_label=s.progress_label,
                resume_file_id=s.resume_file_id,
                notice_consent=s.notice_consent,
                margin=margin,
                review_due_at=s.review_due_at,
                reviewed_at=s.reviewed_at,
                created_at=s.created_at,
            )
        )
    return out


async def list_internal(
    ctx: RequestContext,
    db: AsyncSession,
    *,
    status: str | None = None,
    demand_id: uuid.UUID | None = None,
    vendor_id: uuid.UUID | None = None,
) -> list[VendorSubmissionOut]:
    stmt = select(VendorSubmission).where(VendorSubmission.erased_at.is_(None))
    if status:
        stmt = stmt.where(VendorSubmission.status == status)
    if demand_id:
        stmt = stmt.where(VendorSubmission.demand_id == demand_id)
    if vendor_id:
        stmt = stmt.where(VendorSubmission.vendor_id == vendor_id)
    rows = list(
        (
            await db.execute(
                stmt.order_by(VendorSubmission.created_at.desc(), VendorSubmission.id).limit(500)
            )
        ).scalars()
    )
    return await internal_outs(ctx, db, rows)


async def get_internal(
    ctx: RequestContext, db: AsyncSession, submission_id: uuid.UUID
) -> VendorSubmissionOut:
    """Review detail; opening a ``submitted`` submission moves it to ``under_review`` (user)."""
    from app.core.actors import actor_from_ctx

    sub = await load(db, submission_id, lock=True)
    actor = actor_from_ctx(ctx)
    if sub.status == "submitted" and actor.kind == "user":
        await set_status(ctx, db, sub, "under_review", actor)
    return (await internal_outs(ctx, db, [sub], with_margin=True))[0]


def findings_payload(findings: Sequence[Any]) -> list[dict[str, Any]]:
    return [
        {k: (str(v) if isinstance(v, uuid.UUID) else v) for k, v in f.model_dump().items()} for f in findings
    ]
