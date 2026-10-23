"""Review, duplicates and ownership (Epic 17 A6, FR-96).

* ``duplicate_findings(facts)`` is pure (unit-tested) over facts loaded by ``load_facts`` (internal session):
  1. an active ownership of the same person for the same client held by **another** vendor
     -> ``owned_by_other_vendor``;
  2. an existing tenant Candidate (``candidates.duplicates.probe_matches``) with an Application / Activity in
     the last ``duplicate_activity_months`` -> ``existing_candidate``;
  3. another vendor's earlier non-withdrawn submission of the same person to the same demand
     -> ``submitted_by_other_vendor``.
* Accept is human-only (AD-4): creates or links the Candidate, the Application (source ``vendor``) and the
  ownership (``+ownership_window_days``, one active per client and candidate).
* The vendor only ever sees the status + category label (``submissions.outcome_category``).
"""

import dataclasses
import uuid
from collections.abc import Sequence
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.actors import actor_from_ctx, human_only_action
from app.core.context import RequestContext
from app.core.errors import ConflictError
from app.core.permissions import can_view_compensation
from app.models.activity import Activity
from app.models.demands import Demand
from app.models.pipeline import Application
from app.models.vendors import Vendor, VendorCandidateOwnership, VendorSubmission
from app.schemas.vendors import (
    AcceptIn,
    DuplicateFinding,
    MarginPreviewOut,
    MarkDuplicateIn,
    RejectIn,
    ReopenIn,
    VendorSubmissionOut,
)
from app.services.vendors import submissions
from app.services.vendors.common import conflict, invalid, labels_of, load_settings, now

REJECT_LABELS: dict[str, str] = {
    "consent_withdrawn": "Candidate withdrew consent",
    "skills_mismatch": "Not a fit for this role",
    "rate_too_high": "Rate not suitable",
    "position_filled": "Position filled",
}


# ================================================================ duplicate rules (pure)
@dataclasses.dataclass(frozen=True)
class OwnershipFact:
    candidate_id: uuid.UUID
    vendor_id: uuid.UUID
    client_id: uuid.UUID
    expires_at: datetime


@dataclasses.dataclass(frozen=True)
class OtherSubmissionFact:
    submission_id: uuid.UUID
    vendor_id: uuid.UUID
    demand_id: uuid.UUID
    status: str
    created_at: datetime


@dataclasses.dataclass(frozen=True)
class Facts:
    vendor_id: uuid.UUID
    client_id: uuid.UUID
    demand_id: uuid.UUID
    at: datetime
    matched_candidate_ids: frozenset[uuid.UUID] = frozenset()
    recently_active_candidate_ids: frozenset[uuid.UUID] = frozenset()
    ownerships: tuple[OwnershipFact, ...] = ()
    other_submissions: tuple[OtherSubmissionFact, ...] = ()


def duplicate_findings(facts: Facts) -> list[DuplicateFinding]:
    out: list[DuplicateFinding] = []
    for o in facts.ownerships:
        if (
            o.candidate_id in facts.matched_candidate_ids
            and o.client_id == facts.client_id
            and o.vendor_id != facts.vendor_id
            and o.expires_at > facts.at
        ):
            out.append(
                DuplicateFinding(
                    rule="ownership",
                    category="owned_by_other_vendor",
                    detail="Represented by another vendor for this client",
                    candidate_id=o.candidate_id,
                    vendor_id=o.vendor_id,
                )
            )
    for cid in sorted(facts.matched_candidate_ids & facts.recently_active_candidate_ids):
        out.append(
            DuplicateFinding(
                rule="existing",
                category="existing_candidate",
                detail="Existing candidate with recent activity",
                candidate_id=cid,
            )
        )
    for s in sorted(facts.other_submissions, key=lambda x: x.created_at):
        if (
            s.vendor_id != facts.vendor_id
            and s.demand_id == facts.demand_id
            and s.status != "withdrawn"
            and s.created_at < facts.at
        ):
            out.append(
                DuplicateFinding(
                    rule="other_vendor",
                    category="submitted_by_other_vendor",
                    detail="Submitted earlier by another vendor",
                    vendor_id=s.vendor_id,
                    submission_id=s.submission_id,
                )
            )
    return out


async def load_facts(
    db: AsyncSession,
    *,
    vendor_id: uuid.UUID,
    client_id: uuid.UUID,
    demand_id: uuid.UUID,
    full_name: str | None,
    email: str | None,
    phone: str | None,
    city: str | None,
    at: datetime,
    exclude_submission_id: uuid.UUID | None = None,
) -> Facts:
    """Load the facts with an **internal** session (system read; never exposed to the vendor)."""
    from app.services.candidates import duplicates

    probe = duplicates.Probe(
        full_name=full_name,
        city=city,
        emails=tuple(e for e in (email,) if e),
        phones=tuple(p for p in (phone,) if p),
    )
    matched = (
        frozenset((await duplicates.probe_matches(db, probe)).keys())
        if (email or phone or full_name)
        else frozenset()
    )
    settings = await load_settings(db)
    cutoff = at - timedelta(days=30 * settings.duplicate_activity_months)
    recent: set[uuid.UUID] = set()
    if matched:
        recent |= set(
            (
                await db.execute(
                    select(Application.candidate_id).where(
                        Application.candidate_id.in_(matched),
                        or_(Application.created_at >= cutoff, Application.updated_at >= cutoff),
                    )
                )
            ).scalars()
        )
        recent |= set(
            (
                await db.execute(
                    select(Activity.entity_id).where(
                        Activity.entity_type == "candidate",
                        Activity.entity_id.in_(matched),
                        Activity.occurred_at >= cutoff,
                    )
                )
            ).scalars()
        )
    owners = (
        (
            await db.execute(
                select(VendorCandidateOwnership).where(
                    VendorCandidateOwnership.candidate_id.in_(matched),
                    VendorCandidateOwnership.client_id == client_id,
                    VendorCandidateOwnership.ended_at.is_(None),
                )
            )
        ).scalars().all()
        if matched
        else []
    )
    conds = []
    if email:
        conds.append(VendorSubmission.email == email)
    if phone:
        conds.append(VendorSubmission.phone_e164 == phone)
    others: list[VendorSubmission] = []
    if conds:
        stmt = select(VendorSubmission).where(
            VendorSubmission.demand_id == demand_id, VendorSubmission.vendor_id != vendor_id, or_(*conds)
        )
        if exclude_submission_id is not None:
            stmt = stmt.where(VendorSubmission.id != exclude_submission_id)
        others = list((await db.execute(stmt)).scalars())
    return Facts(
        vendor_id=vendor_id,
        client_id=client_id,
        demand_id=demand_id,
        at=at,
        matched_candidate_ids=matched,
        recently_active_candidate_ids=frozenset(recent),
        ownerships=tuple(
            OwnershipFact(o.candidate_id, o.vendor_id, o.client_id, o.expires_at) for o in owners
        ),
        other_submissions=tuple(
            OtherSubmissionFact(s.id, s.vendor_id, s.demand_id, s.status, s.created_at) for s in others
        ),
    )


async def findings_for(db: AsyncSession, sub: VendorSubmission) -> list[DuplicateFinding]:
    facts = await load_facts(
        db,
        vendor_id=sub.vendor_id,
        client_id=sub.client_id,
        demand_id=sub.demand_id,
        full_name=sub.full_name,
        email=sub.email,
        phone=sub.phone_e164,
        city=sub.current_location,
        at=sub.created_at or now(),
        exclude_submission_id=sub.id,
    )
    return duplicate_findings(facts)


# ================================================================ margin preview (masked without
# compensation:view)
def margin_preview(ctx: RequestContext, sub: VendorSubmission, demand: Demand) -> MarginPreviewOut | None:
    if not can_view_compensation(ctx.principal):
        return None
    bill = demand.bill_rate
    pay = Decimal(sub.vendor_rate)
    if (
        bill is None
        or (demand.currency and demand.currency != sub.currency)
        or (demand.rate_unit and sub.rate_unit and demand.rate_unit != sub.rate_unit)
    ):
        return MarginPreviewOut(
            bill_rate=bill, pay_rate=pay, margin_amount=None, margin_pct=None, currency=sub.currency
        )
    amount = (Decimal(bill) - pay).quantize(Decimal("0.01"), ROUND_HALF_UP)
    pct = (amount * 100 / Decimal(bill)).quantize(Decimal("0.01"), ROUND_HALF_UP) if bill else None
    return MarginPreviewOut(
        bill_rate=bill, pay_rate=pay, margin_amount=amount, margin_pct=pct, currency=sub.currency
    )


# ================================================================ decisions
def _require_user(ctx: RequestContext) -> Any:
    actor = actor_from_ctx(ctx)
    if actor.kind != "user":
        raise human_only_action()
    return actor


async def accept(
    ctx: RequestContext, db: AsyncSession, submission_id: uuid.UUID, data: AcceptIn
) -> VendorSubmissionOut:
    """Human-only: Candidate (create / link) + Application (source ``vendor``) + ownership, one
    transaction."""
    actor = _require_user(ctx)
    sub = await submissions.load(db, submission_id, lock=True)
    if sub.status not in ("submitted", "under_review"):
        raise conflict(
            "transition_not_allowed", f"A {sub.status} submission cannot be accepted", from_status=sub.status
        )
    findings = await findings_for(db, sub)
    if findings and not data.override_duplicate_rule:
        raise conflict(
            "duplicate_findings",
            "Duplicate findings exist; override them with a reason",
            findings=len(findings),
        )
    if data.override_duplicate_rule and not (data.reason or "").strip():
        raise invalid("reason_required", "Overriding a duplicate rule needs a reason")
    if data.link_candidate_id is None and not data.create:
        raise invalid("candidate_choice_required", "Link an existing candidate or create a new one")
    vendor = (await db.execute(select(Vendor).where(Vendor.id == sub.vendor_id))).scalar_one()
    candidate_id = data.link_candidate_id or await _create_candidate(ctx, db, sub, vendor, actor.id)
    if data.link_candidate_id is not None:
        from app.services.candidates import records

        await records.ensure_readable(ctx, db, data.link_candidate_id)
    application_id = await _create_application(ctx, db, sub, candidate_id, actor.id)
    await _own(db, sub, candidate_id, override=data.override_duplicate_rule)
    sub.candidate_id = candidate_id
    sub.application_id = application_id
    if data.override_duplicate_rule:
        sub.reason_text_internal = (data.reason or "").strip()
    settings = await load_settings(db)
    sub.progress_key = "accepted"
    sub.progress_label = labels_of(settings).get("accepted", "Shortlisted")
    await submissions.set_status(ctx, db, sub, "accepted", actor)
    return (await submissions.internal_outs(ctx, db, [sub], with_margin=True))[0]


async def _create_candidate(
    ctx: RequestContext, db: AsyncSession, sub: VendorSubmission, vendor: Vendor, owner_id: uuid.UUID | None
) -> uuid.UUID:
    from app.models.candidates import CandidateResume
    from app.schemas.candidates import CandidateCreate, EmailIn, LocationIn, PhoneIn
    from app.services.candidates import parsing, records

    data = CandidateCreate(
        full_name=sub.full_name or "Vendor candidate",
        source="other",
        source_detail=f"Vendor: {vendor.legal_name}",
        emails=[EmailIn(email=sub.email, is_primary=True)] if sub.email else [],
        phones=[PhoneIn(phone=sub.phone_e164, is_primary=True)] if sub.phone_e164 else [],
        location=LocationIn(city=sub.current_location) if sub.current_location else None,
        total_experience_months=sub.total_experience_months,
        availability_date=sub.availability_date,
        notice_period_days=sub.notice_period_days,
    )
    cand, _ = await records.create_record(
        ctx, db, data, owner_id=owner_id or ctx.principal.id, source="other"
    )
    if sub.resume_draft_id is not None:
        draft = await db.get(CandidateResume, sub.resume_draft_id)
        if draft is not None and draft.candidate_id is None:
            await parsing.attach_draft(ctx, db, draft, cand, data)
    return cand.id


async def _create_application(
    ctx: RequestContext,
    db: AsyncSession,
    sub: VendorSubmission,
    candidate_id: uuid.UUID,
    owner_id: uuid.UUID | None,
) -> uuid.UUID:
    from app.services.pipeline import applications

    try:
        app_row = await applications.create_application(
            ctx,
            db,
            sub.demand_id,
            candidate_id,
            source="vendor",
            source_ref=str(sub.id),
            actor="user",
            owner_id=owner_id or ctx.principal.id,
        )
        return app_row.id
    except ConflictError as exc:
        if exc.code != "application_exists":
            raise
        raw = (exc.details or {}).get("application_id")
        if not raw:
            raise
        return uuid.UUID(str(raw))


async def _own(db: AsyncSession, sub: VendorSubmission, candidate_id: uuid.UUID, *, override: bool) -> None:
    at = now()
    settings = await load_settings(db)
    rows = (
        (
            await db.execute(
                select(VendorCandidateOwnership)
                .where(
                    VendorCandidateOwnership.client_id == sub.client_id,
                    VendorCandidateOwnership.candidate_id == candidate_id,
                    VendorCandidateOwnership.ended_at.is_(None),
                )
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    for row in rows:
        if row.expires_at <= at or row.vendor_id == sub.vendor_id or override:
            row.ended_at = at
        else:
            raise conflict(
                "owned_by_other_vendor", "Another vendor represents this candidate for this client"
            )
    await db.flush()
    db.add(
        VendorCandidateOwnership(
            id=uuid.uuid4(),
            tenant_id=sub.tenant_id,
            vendor_id=sub.vendor_id,
            client_id=sub.client_id,
            candidate_id=candidate_id,
            vendor_submission_id=sub.id,
            starts_at=at,
            expires_at=at + timedelta(days=settings.ownership_window_days),
        )
    )
    await db.flush()


async def reject(
    ctx: RequestContext, db: AsyncSession, submission_id: uuid.UUID, data: RejectIn
) -> VendorSubmissionOut:
    actor = _require_user(ctx)
    sub = await submissions.load(db, submission_id, lock=True)
    sub.reject_reason_code = data.reason_code
    sub.reason_text_internal = data.note
    sub.vendor_visible_reason = REJECT_LABELS.get(data.reason_code, "Not selected")
    await submissions.set_status(ctx, db, sub, "rejected", actor)
    return (await submissions.internal_outs(ctx, db, [sub]))[0]


async def mark_duplicate(
    ctx: RequestContext, db: AsyncSession, submission_id: uuid.UUID, data: MarkDuplicateIn
) -> VendorSubmissionOut:
    actor = _require_user(ctx)
    sub = await submissions.load(db, submission_id, lock=True)
    sub.reason_text_internal = data.note
    await submissions.set_status(ctx, db, sub, "duplicate", actor, category=data.category)
    return (await submissions.internal_outs(ctx, db, [sub]))[0]


async def reopen(
    ctx: RequestContext, db: AsyncSession, submission_id: uuid.UUID, data: ReopenIn
) -> VendorSubmissionOut:
    """``duplicate -> under_review`` (override of a duplicate rule; reason required)."""
    actor = _require_user(ctx)
    sub = await submissions.load(db, submission_id, lock=True)
    sub.reason_text_internal = data.reason
    sub.duplicate_reason_category = None
    await submissions.set_status(ctx, db, sub, "under_review", actor)
    return (await submissions.internal_outs(ctx, db, [sub]))[0]


async def system_reject_consent(ctx: RequestContext, db: AsyncSession, submission_id: uuid.UUID) -> bool:
    """Notice-link consent withdrawn before acceptance -> auto ``rejected`` (system,
    ``consent_withdrawn``)."""
    from app.core.actors import ActorRef

    sub = await submissions.load(db, submission_id, lock=True)
    sub.notice_consent = "withdrawn"
    if sub.status not in ("submitted", "under_review"):
        await db.flush()
        return False
    sub.reject_reason_code = "consent_withdrawn"
    sub.vendor_visible_reason = REJECT_LABELS["consent_withdrawn"]
    await submissions.set_status(ctx, db, sub, "rejected", ActorRef("system", None))
    return True


async def end_ownerships_for(db: AsyncSession, candidate_ids: Sequence[uuid.UUID]) -> int:
    result = await db.execute(
        update(VendorCandidateOwnership)
        .where(
            VendorCandidateOwnership.candidate_id.in_(list(candidate_ids)),
            VendorCandidateOwnership.ended_at.is_(None),
        )
        .values(ended_at=now())
        .execution_options(synchronize_session=False)
    )
    return int(result.rowcount or 0)
