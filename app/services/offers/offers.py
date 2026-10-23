"""Offers: create, edit / revise, submit for approval, release, withdraw, reads and settings (stories 8.1 / 8.2).

* One non-terminal offer per application (409 ``offer_exists`` with ``details.offer_id``).
* ``PATCH`` edits the draft version in place; on an approved / released offer it creates version N+1 -- a
  draft needing re-approval when an approval-relevant field changed (FR-44), otherwise a version that inherits
  the approval (and, on a released offer, replaces the released version at once).
* Approvals go through the Epic 4 module (subject ``offer``); its callbacks run in the deciding transaction.
* ``release_offer`` is human-only (FR-65). Emails, links and PDFs are produced by Celery tasks after commit.
"""

import dataclasses
import uuid
from collections.abc import Sequence
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.errors import NotFound, ValidationFailed
from app.core.events import emit
from app.core.pagination import Page, PageParams, paginate
from app.core.sequences import next_human_id
from app.events.offers import OfferCreated, OfferEmailRequested, OfferLetterRequested, OfferVersionCreated
from app.models.offers import Offer, OfferResponse, OfferStatusHistory, OfferVersion
from app.schemas.approvals import SubjectSummary
from app.schemas.offers import (
    OfferCreate,
    OfferDetailOut,
    OfferOut,
    OfferResponseOut,
    OfferSettingsOut,
    OfferSettingsUpdate,
    OfferStatusHistoryOut,
    OfferUpdate,
    OfferVersionOut,
)
from app.services.offers import status as status_machine
from app.services.offers import templates, versions
from app.services.offers.common import (
    OFFER_SEQUENCE_KEY,
    OPEN_FOR_RESPONSE,
    READ,
    RELEASE,
    SUBJECT_TYPE,
    TERMINAL,
    WITHDRAW,
    WRITE,
    acting_as,
    actor_id,
    actor_kind_of,
    can_view_money,
    candidate_facts,
    client_name,
    conflict,
    demand_in_scope,
    end_of_day_utc,
    now,
    offer_for,
    principal_of,
    settings_row,
    tenant_facts,
    tenant_of,
    tenant_today,
    visible_offers,
)

APPROVAL_SUBJECT = "offer"
DEMAND_RELEASABLE = ("open", "on_hold")


# ================================================================ settings
def _settings_out(row: Any) -> OfferSettingsOut:
    return OfferSettingsOut(
        default_expiry_days=row.default_expiry_days,
        signatory_name=row.signatory_name,
        signatory_title=row.signatory_title,
        response_link_grace_hours=row.response_link_grace_hours,
        attach_letter_to_release_email=row.attach_letter_to_release_email,
    )


async def get_settings(ctx: RequestContext, db: AsyncSession) -> OfferSettingsOut:  # ruff: ignore[unused-function-argument]
    return _settings_out(await settings_row(db))


async def update_settings(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,
    data: OfferSettingsUpdate,
) -> OfferSettingsOut:
    row = await settings_row(db)
    for key, value in data.model_dump(exclude_unset=True).items():
        if value is None and key not in ("signatory_name", "signatory_title"):
            continue
        setattr(row, key, value.strip() or None if isinstance(value, str) else value)
    await db.flush()
    await db.refresh(row)
    return _settings_out(row)


# ================================================================ output
async def _history(db: AsyncSession, offer_id: uuid.UUID) -> list[OfferStatusHistoryOut]:
    rows = (
        await db.execute(
            select(OfferStatusHistory)
            .where(OfferStatusHistory.offer_id == offer_id)
            .order_by(OfferStatusHistory.at, OfferStatusHistory.id)
        )
    ).scalars()
    return [OfferStatusHistoryOut.model_validate(r) for r in rows]


async def _responses(db: AsyncSession, offer_id: uuid.UUID) -> list[OfferResponseOut]:
    rows = (
        await db.execute(
            select(OfferResponse).where(OfferResponse.offer_id == offer_id).order_by(OfferResponse.decided_at)
        )
    ).scalars()
    return [
        OfferResponseOut(
            decision=r.decision,
            version_no=r.version_no,
            typed_name=r.typed_name,
            decline_reason=r.decline_reason,
            ip=str(r.ip) if r.ip is not None else None,
            user_agent=r.user_agent,
            channel=r.channel,
            letter_sha256=r.letter_sha256,
            decided_at=r.decided_at,
        )
        for r in rows
    ]


async def offer_out(
    ctx: RequestContext, db: AsyncSession, offer: Offer, *, min_pct: Decimal | None = None
) -> OfferOut:
    from app.services.demands import pipeline_port as demands_port

    show = can_view_money(ctx)
    if show and min_pct is None:
        min_pct = await versions.min_margin_pct(ctx, db)
    current = await versions.current(db, offer)
    version_out = versions.version_out(current, show_money=show, min_pct=min_pct)
    released_no = None
    if offer.released_version_id is not None:
        released = await db.get(OfferVersion, offer.released_version_id)
        released_no = released.version_no if released else None
    demand = await demands_port.get_row(db, offer.demand_id)
    warnings = ["below_min_margin"] if version_out.below_min_margin else []
    return OfferOut(
        id=offer.id,
        human_id=offer.human_id,
        status=offer.status,
        application_id=offer.application_id,
        demand_id=offer.demand_id,
        candidate_id=offer.candidate_id,
        client_id=offer.client_id,
        candidate_name=(await candidate_facts(db, offer.candidate_id)).get("full_name"),
        demand_title=demand.title if demand else None,
        client_name=await client_name(db, offer.client_id),
        owner_id=offer.owner_id,
        lock_version=offer.lock_version,
        current_version=version_out,
        released_version_no=released_no,
        approval_request_id=offer.approval_request_id,
        status_comment=offer.status_comment,
        withdrawn_reason=offer.withdrawn_reason,
        released_at=offer.released_at,
        viewed_at=offer.viewed_at,
        responded_at=offer.responded_at,
        expires_at=offer.expires_at,
        accepted_letter_url=f"/api/v1/files/{offer.accepted_file_id}/download"
        if offer.accepted_file_id
        else None,
        compensation_masked=not show,
        warnings=warnings,
        created_at=offer.created_at,
        updated_at=offer.updated_at,
    )


async def offer_detail(ctx: RequestContext, db: AsyncSession, offer: Offer) -> OfferDetailOut:
    from app.services import approvals

    await db.refresh(offer)
    base = await offer_out(ctx, db, offer)
    rows = await versions.all_for(db, offer.id)
    return OfferDetailOut(
        **base.model_dump(),
        versions=[versions.brief(v, offer.released_version_id) for v in rows],
        history=await _history(db, offer.id),
        approvals=await approvals.history_for(ctx, db, APPROVAL_SUBJECT, offer.id),
        responses=await _responses(db, offer.id),
    )


# ================================================================ reads
async def get_offer(ctx: RequestContext, db: AsyncSession, offer_id: uuid.UUID) -> OfferDetailOut:
    offer = await offer_for(ctx, db, offer_id, READ)
    return await offer_detail(ctx, db, offer)


async def get_version(
    ctx: RequestContext, db: AsyncSession, offer_id: uuid.UUID, version_no: int
) -> OfferVersionOut:
    offer = await offer_for(ctx, db, offer_id, READ)
    version = await versions.by_no(db, offer.id, version_no)
    if version is None:
        raise NotFound("Offer version not found")
    show = can_view_money(ctx)
    return versions.version_out(
        version, show_money=show, min_pct=await versions.min_margin_pct(ctx, db) if show else None
    )


async def _outs(ctx: RequestContext, db: AsyncSession, rows: list[Offer]) -> list[OfferOut]:
    min_pct = await versions.min_margin_pct(ctx, db) if can_view_money(ctx) else None
    return [await offer_out(ctx, db, r, min_pct=min_pct) for r in rows]


async def list_offers(
    ctx: RequestContext,
    db: AsyncSession,
    params: PageParams,
    *,
    status: list[str] | None = None,
    owner: str | None = None,
) -> Page[OfferOut]:
    principal = principal_of(ctx)
    stmt = visible_offers(principal, READ)
    if status:
        stmt = stmt.where(Offer.status.in_(status))
    if owner == "mine":
        stmt = stmt.where(Offer.owner_id == principal.id)
    elif owner:
        try:
            stmt = stmt.where(Offer.owner_id == uuid.UUID(owner))
        except ValueError as exc:
            raise ValidationFailed("owner must be 'mine' or a user id", code="invalid_owner") from exc
    stmt = stmt.order_by(Offer.updated_at.desc(), Offer.id)
    page = await paginate(db, stmt, params)
    return Page[OfferOut](
        items=await _outs(ctx, db, list(page.items)),
        total=page.total,
        page=page.page,
        page_size=page.page_size,
    )


async def list_for_demand(ctx: RequestContext, db: AsyncSession, demand_id: uuid.UUID) -> list[OfferOut]:
    await demand_in_scope(ctx, db, demand_id, READ)
    principal = principal_of(ctx)
    stmt = (
        visible_offers(principal, READ).where(Offer.demand_id == demand_id).order_by(Offer.created_at.desc())
    )
    return await _outs(ctx, db, list((await db.execute(stmt)).scalars()))


async def _application(db: AsyncSession, application_id: uuid.UUID) -> Any:
    from app.models.pipeline import Application

    app = await db.get(Application, application_id)
    if app is None:
        raise NotFound("Application not found")
    return app


async def list_for_application(
    ctx: RequestContext, db: AsyncSession, application_id: uuid.UUID
) -> list[OfferOut]:
    app = await _application(db, application_id)
    try:
        await demand_in_scope(ctx, db, app.demand_id, READ)
    except NotFound as exc:
        raise NotFound("Application not found") from exc
    principal = principal_of(ctx)
    stmt = (
        visible_offers(principal, READ)
        .where(Offer.application_id == application_id)
        .order_by(Offer.created_at.desc())
    )
    return await _outs(ctx, db, list((await db.execute(stmt)).scalars()))


# ================================================================ create
async def _open_offer_id(db: AsyncSession, application_id: uuid.UUID) -> uuid.UUID | None:
    return (
        await db.execute(
            select(Offer.id).where(Offer.application_id == application_id, Offer.status.not_in(TERMINAL))
        )
    ).scalar()


async def _latest_submission(db: AsyncSession, application_id: uuid.UUID) -> Any:
    from app.models.pipeline import Submission

    return (
        await db.execute(
            select(Submission)
            .where(Submission.application_id == application_id, Submission.status == "sent")
            .order_by(Submission.sent_at.desc().nulls_last(), Submission.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


def _prefill(
    demand: Any, submission: Any, data: OfferCreate, today: date, expiry_days: int
) -> dict[str, Any]:
    """Version columns from the demand and the latest sent submission, overridden by the request."""
    employment_type = (
        data.employment_type
        or (submission.employment_type if submission is not None else None)
        or demand.employment_type
        or "permanent"
    )
    unit = (
        (submission.rate_unit if submission is not None else None)
        or demand.rate_unit
        or ("year" if employment_type == "permanent" else "hour")
    )
    currency = (submission.currency if submission is not None else None) or demand.currency or "INR"
    pay = submission.pay_rate if submission is not None else demand.pay_rate_max
    bill = submission.bill_rate if submission is not None else demand.bill_rate
    start = (
        demand.start_date if demand.start_date and demand.start_date >= today else today + timedelta(days=14)
    )
    out: dict[str, Any] = {
        "title": demand.title,
        "employment_type": employment_type,
        "pay_rate": Decimal(pay) if pay is not None else None,
        "pay_currency": currency,
        "pay_rate_unit": unit,
        "bill_rate": Decimal(bill) if bill is not None else None,
        "bill_currency": currency if bill is not None else None,
        "bill_rate_unit": unit if bill is not None else None,
        "bonus_amount": None,
        "bonus_currency": None,
        "benefits_summary": None,
        "start_date": start,
        "end_date": None,
        "location": demand.location,
        "work_mode": demand.work_mode,
        "expiry_date": today + timedelta(days=expiry_days),
        "template_id": None,
    }
    out.update(versions.changes_from(data))
    if out["employment_type"] in versions.CONTRACT_TYPES and out["end_date"] is None:
        # Prefill only (editable while Draft): six months after the start date.
        out["end_date"] = out["start_date"] + timedelta(days=182)
    if out["pay_rate"] is None:
        raise ValidationFailed(
            "A pay rate is required (none on the demand or a sent submission)",
            code="pay_rate_required",
            details={"field": "pay_rate"},
        )
    return out


async def create_offer(
    ctx: RequestContext, db: AsyncSession, application_id: uuid.UUID, data: OfferCreate
) -> OfferDetailOut:
    from app.models.pipeline import Application, PipelineStage

    app = (
        await db.execute(select(Application).where(Application.id == application_id).with_for_update())
    ).scalar_one_or_none()
    if app is None:
        raise NotFound("Application not found")
    try:
        demand = await demand_in_scope(ctx, db, app.demand_id, WRITE)
    except NotFound as exc:
        raise NotFound("Application not found") from exc
    stage = await db.get(PipelineStage, app.stage_id)
    if stage is None or stage.key != "selected":
        raise conflict(
            "application_not_selected",
            "Offers can only be created for Selected applications",
            stage=stage.key if stage else None,
        )
    existing = await _open_offer_id(db, app.id)
    if existing is not None:
        raise conflict("offer_exists", "This application already has an open offer", offer_id=str(existing))
    settings = await settings_row(db)
    tenant = await tenant_facts(db, app.tenant_id)
    terms = _prefill(
        demand,
        await _latest_submission(db, app.id),
        data,
        tenant_today(tenant["timezone"]),
        settings.default_expiry_days,
    )
    if terms.get("template_id") is None:
        template = await templates.default_template_for(db, terms["employment_type"])
        terms["template_id"] = template.id if template else None
    else:
        await templates.get_template(db, terms["template_id"])
    principal = principal_of(ctx)
    offer = Offer(
        id=uuid.uuid4(),
        tenant_id=app.tenant_id,
        human_id=await next_human_id(db, OFFER_SEQUENCE_KEY, width=6),
        application_id=app.id,
        demand_id=demand.id,
        candidate_id=app.candidate_id,
        client_id=demand.client_id,
        status="draft",
        owner_id=principal.id,
        lock_version=1,
    )
    version = OfferVersion(
        id=uuid.uuid4(),
        tenant_id=app.tenant_id,
        offer_id=offer.id,
        version_no=1,
        approval_state="pending",
        frozen=False,
        created_by=principal.id,
        **terms,
    )
    versions.apply(version, {})
    versions.validate(version, tenant["timezone"])
    await versions.compute_margin(ctx, db, version)
    offer.current_version_id = version.id
    try:
        async with db.begin_nested():
            db.add(offer)
            await db.flush()
            db.add(version)
            db.add(
                OfferStatusHistory(
                    id=uuid.uuid4(),
                    tenant_id=offer.tenant_id,
                    offer_id=offer.id,
                    from_status=None,
                    to_status="draft",
                    actor_kind="user",
                    actor_id=principal.id,
                    version_no=1,
                    reason=None,
                    at=now(),
                )
            )
            await db.flush()
    except IntegrityError as exc:
        existing = await _open_offer_id(db, app.id)
        raise conflict(
            "offer_exists",
            "This application already has an open offer",
            offer_id=str(existing) if existing else None,
        ) from exc
    emit(
        db,
        OfferCreated(
            tenant_id=offer.tenant_id,
            offer_id=offer.id,
            application_id=offer.application_id,
            demand_id=offer.demand_id,
            candidate_id=offer.candidate_id,
        ),
    )
    emit(
        db,
        OfferVersionCreated(
            tenant_id=offer.tenant_id, offer_id=offer.id, version_no=1, requires_approval=True
        ),
    )
    emit(db, OfferLetterRequested(tenant_id=offer.tenant_id, offer_id=offer.id, version_no=1))
    return await offer_detail(ctx, db, offer)


# ================================================================ Epic 43: bulk create (additive)
# Withdrawal reasons offered by the UI (free text stays allowed). ``Reneged`` is recorded by campus hiring
# when a student backs out; P1 offers are terminal at ``accepted``, so post-acceptance reneging lives on
# the campus
# registration (and the P2 dropout path when a Placement exists) -- no ``offers`` column is added.
WITHDRAWAL_REASON_PRESETS: tuple[str, ...] = (
    "Reneged",
    "Position closed",
    "Candidate not reachable",
    "Offer terms changed",
    "Duplicate offer",
    "Other",
)


@dataclasses.dataclass(frozen=True, slots=True)
class BulkOfferResult:
    """One ``create_bulk`` item: the created offer (``offer_id`` / ``status``) or the error code."""

    application_id: uuid.UUID
    offer_id: uuid.UUID | None = None
    status: str | None = None
    error_code: str | None = None
    message: str | None = None


async def create_bulk(
    ctx: RequestContext, db: AsyncSession, items: Sequence[tuple[uuid.UUID, OfferCreate]]
) -> list[BulkOfferResult]:
    """Epic 43: ``create_offer`` per ``(application_id, OfferCreate)`` item, each in its own SAVEPOINT
    (per-item errors, the others still commit). Every offer then follows its own approval and release
    (FR-43..45)."""
    from app.core.errors import DomainError

    out: list[BulkOfferResult] = []
    for application_id, data in items:
        try:
            async with db.begin_nested():
                detail = await create_offer(ctx, db, application_id, data)
        except DomainError as exc:
            out.append(BulkOfferResult(application_id, error_code=exc.code, message=exc.message))
            continue
        out.append(BulkOfferResult(application_id, offer_id=detail.id, status=detail.status))
    return out


# ================================================================ edit / revise
async def update_offer(
    ctx: RequestContext, db: AsyncSession, offer_id: uuid.UUID, data: OfferUpdate
) -> OfferDetailOut:
    offer = await offer_for(ctx, db, offer_id, WRITE, for_update=True)
    if data.lock_version != offer.lock_version:
        raise conflict(
            "version_conflict",
            "The offer changed in the meantime; reload and try again",
            lock_version=offer.lock_version,
        )
    if offer.status in TERMINAL:
        raise conflict("offer_terminal", "This offer is closed", status=offer.status)
    if offer.status == "pending_approval":
        raise conflict(
            "offer_locked_pending_approval",
            "The offer is awaiting approval; withdraw it or wait for the approver to return it",
        )
    changes = versions.changes_from(data)
    if "template_id" in changes and changes["template_id"] is not None:
        await templates.get_template(db, changes["template_id"])
    current = await versions.current(db, offer)
    changed = versions.changed_fields(current, changes)
    if not changed:
        return await offer_detail(ctx, db, offer)
    tenant = await tenant_facts(db, offer.tenant_id)
    user = actor_id(ctx)
    if offer.status == "draft":
        if current.frozen:
            raise conflict("version_frozen", "This offer version can no longer be edited")
        versions.apply(current, {k: changes[k] for k in changed})
        versions.validate(current, tenant["timezone"])
        await versions.compute_margin(ctx, db, current)
        offer.lock_version = offer.lock_version + 1
        await db.flush()
        emit(
            db,
            OfferLetterRequested(tenant_id=offer.tenant_id, offer_id=offer.id, version_no=current.version_no),
        )
        return await offer_detail(ctx, db, offer)

    # approved / released / viewed -> version N+1
    relevant = bool(changed & set(versions.APPROVAL_RELEVANT))
    new = versions.copy_version(current, await versions.next_no(db, offer.id), user)
    versions.apply(new, {k: changes[k] for k in changed})
    versions.validate(new, tenant["timezone"])
    await versions.compute_margin(ctx, db, new)
    if not relevant:
        new.approval_state = "not_required"
        new.approved_at = current.approved_at
        new.frozen = True
    db.add(new)
    await db.flush()
    offer.current_version_id = new.id
    if relevant:
        offer.approval_request_id = None
        offer.status_comment = None
        await status_machine.transition_offer(ctx, db, offer, "draft", actor_kind_of(ctx), "revised")
    else:
        offer.lock_version = offer.lock_version + 1
        if offer.status in OPEN_FOR_RESPONSE:
            offer.released_version_id = new.id
            offer.expires_at = end_of_day_utc(new.expiry_date, tenant["timezone"])
            offer.expiry_warned_at = None
            await status_machine.revoke_open_links(db, offer.id)
            emit(db, OfferEmailRequested(tenant_id=offer.tenant_id, offer_id=offer.id, kind="updated"))
        await db.flush()
    emit(
        db,
        OfferVersionCreated(
            tenant_id=offer.tenant_id,
            offer_id=offer.id,
            version_no=new.version_no,
            requires_approval=relevant,
        ),
    )
    emit(db, OfferLetterRequested(tenant_id=offer.tenant_id, offer_id=offer.id, version_no=new.version_no))
    return await offer_detail(ctx, db, offer)


# ================================================================ submit / approvals
async def _facts(
    ctx: RequestContext, db: AsyncSession, offer: Offer, version: OfferVersion
) -> dict[str, Any]:
    from app.services.demands import pipeline_port as demands_port

    del ctx
    demand = await demands_port.get_row(db, offer.demand_id)
    return {
        "pay_rate": Decimal(version.pay_rate),
        "bill_rate": Decimal(version.bill_rate) if version.bill_rate is not None else None,
        "margin_percent": Decimal(version.margin_pct) if version.margin_pct is not None else None,
        "employment_type": version.employment_type,
        "client_id": str(offer.client_id),
        "country": demand.country if demand else None,
    }


async def submit_offer(ctx: RequestContext, db: AsyncSession, offer_id: uuid.UUID) -> OfferDetailOut:
    from app.services import approvals

    offer = await offer_for(ctx, db, offer_id, WRITE, for_update=True)
    actor = actor_kind_of(ctx)
    status_machine.check_transition(offer.status, "pending_approval", actor)
    version = await versions.current(db, offer)
    tenant = await tenant_facts(db, offer.tenant_id)
    versions.check_expiry(version.expiry_date, tenant["timezone"])
    markdown, tenant_info, _ = await versions.letter_markdown(db, offer, version)
    digest = versions.input_hash(markdown, tenant_info["name"], offer.human_id, version.version_no)
    if version.letter_file_id is None or version.letter_input_hash != digest:
        raise conflict("letter_not_ready", "The offer letter is still being generated; try again in a moment")
    version.frozen = True
    version.approval_state = "pending"
    offer.status_comment = None
    await db.flush()
    result = await approvals.start(ctx, db, APPROVAL_SUBJECT, offer.id, await _facts(ctx, db, offer, version))
    offer.approval_request_id = result.request_id
    if result.status == "auto_approved":
        version.approval_state = "approved"
        version.approved_at = now()
        await db.flush()
        await status_machine.transition_offer(ctx, db, offer, "approved", actor, "auto_approved")
    else:
        await db.flush()
        await status_machine.transition_offer(ctx, db, offer, "pending_approval", actor)
    return await offer_detail(ctx, db, offer)


async def _on_approved(ctx: RequestContext, db: AsyncSession, offer_id: uuid.UUID) -> None:
    offer = (
        await db.execute(select(Offer).where(Offer.id == offer_id).with_for_update())
    ).scalar_one_or_none()
    if offer is None or offer.status != "pending_approval":
        return
    version = await versions.current(db, offer)
    with acting_as(db, "user", actor_id(ctx)):
        version.approval_state = "approved"
        version.approved_at = now()
        await db.flush()
    await status_machine.transition_offer(ctx, db, offer, "approved", "user")


async def _on_returned(
    ctx: RequestContext, db: AsyncSession, offer_id: uuid.UUID, outcome: str, comment: str
) -> None:
    offer = (
        await db.execute(select(Offer).where(Offer.id == offer_id).with_for_update())
    ).scalar_one_or_none()
    if offer is None or offer.status != "pending_approval":
        return
    version = await versions.current(db, offer)
    with acting_as(db, "user", actor_id(ctx)):
        version.frozen = False
        version.approval_state = "returned"
        offer.status_comment = comment or None
        await db.flush()
    await status_machine.transition_offer(ctx, db, offer, "draft", "user", f"{outcome}: {comment}"[:2000])


async def _describe(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,
    ids: list[uuid.UUID],
) -> dict[uuid.UUID, SubjectSummary]:
    from app.services.demands import pipeline_port as demands_port

    rows = list((await db.execute(select(Offer).where(Offer.id.in_(ids)))).scalars())
    titles = await demands_port.titles(db, list({r.demand_id for r in rows}))
    out: dict[uuid.UUID, SubjectSummary] = {}
    for r in rows:
        name = (await candidate_facts(db, r.candidate_id)).get("full_name")
        demand_title = (titles.get(r.demand_id) or {}).get("title")
        out[r.id] = SubjectSummary(
            label=r.human_id, title=" - ".join(x for x in (name, demand_title) if x) or None
        )
    return out


def register_approvals() -> None:
    from app.services import approvals

    approvals.register_subject(APPROVAL_SUBJECT, _on_approved, _on_returned, _describe)


# ================================================================ release / withdraw
async def release_offer(
    ctx: RequestContext, db: AsyncSession, offer_id: uuid.UUID, actor: str | None = None
) -> Offer:
    """Human-only (FR-65): ``ai`` / ``workflow`` / ``api_key`` actors get 409 ``human_only_action``."""
    from app.services.demands import pipeline_port as demands_port

    kind = actor or actor_kind_of(ctx)
    if kind in status_machine.NEVER_ALLOWED:
        status_machine.check_transition("approved", "released", kind)
    offer = await offer_for(ctx, db, offer_id, RELEASE, for_update=True)
    status_machine.check_transition(offer.status, "released", kind)
    demand = await demands_port.get_row(db, offer.demand_id)
    if demand.status not in DEMAND_RELEASABLE:
        raise conflict("demand_not_open", "The demand is not open", demand_status=demand.status)
    version = await versions.current(db, offer)
    tenant = await tenant_facts(db, offer.tenant_id)
    versions.check_expiry(version.expiry_date, tenant["timezone"])
    # Epic 16 (FR-91): the offer bill rate must clear the client's rate card (409 rate_exception_*).
    from app.services import rate_gate_port

    await rate_gate_port.check("offer_release", ctx, db, offer=offer, version=version, demand=demand)
    candidate = await candidate_facts(db, offer.candidate_id)
    if not candidate.get("email"):
        raise conflict("candidate_email_missing", "The candidate has no email address")
    offer.released_version_id = version.id
    offer.released_at = now()
    offer.expires_at = end_of_day_utc(version.expiry_date, tenant["timezone"])
    offer.expiry_warned_at = None
    await db.flush()
    await status_machine.transition_offer(ctx, db, offer, "released", kind)
    emit(db, OfferEmailRequested(tenant_id=offer.tenant_id, offer_id=offer.id, kind="released"))
    return offer


async def release(ctx: RequestContext, db: AsyncSession, offer_id: uuid.UUID) -> OfferDetailOut:
    offer = await release_offer(ctx, db, offer_id)
    return await offer_detail(ctx, db, offer)


async def withdraw_offer(
    ctx: RequestContext, db: AsyncSession, offer_id: uuid.UUID, reason: str
) -> OfferDetailOut:
    from app.services import approvals

    offer = await offer_for(ctx, db, offer_id, WITHDRAW, for_update=True)
    if offer.status in TERMINAL:
        raise conflict("offer_terminal", "This offer is closed", status=offer.status)
    text = reason.strip()
    if len(text) < 5:
        raise ValidationFailed("A reason of at least 5 characters is required", code="reason_required")
    status_machine.check_transition(offer.status, "withdrawn", actor_kind_of(ctx))
    if offer.status == "pending_approval":
        await approvals.cancel_open(ctx, db, APPROVAL_SUBJECT, offer.id)
    was_released = offer.status in OPEN_FOR_RESPONSE
    offer.withdrawn_reason = text
    await db.flush()
    await status_machine.transition_offer(ctx, db, offer, "withdrawn", actor_kind_of(ctx), text)
    if was_released:
        emit(db, OfferEmailRequested(tenant_id=offer.tenant_id, offer_id=offer.id, kind="withdrawn"))
    return await offer_detail(ctx, db, offer)


# ================================================================ letter
async def letter_preview(ctx: RequestContext, db: AsyncSession, offer_id: uuid.UUID) -> bytes:
    """PDF of the current version, rendered on the fly (nothing persisted)."""
    offer = await offer_for(ctx, db, offer_id, READ)
    version = await versions.current(db, offer)
    pdf, _, _, _ = await versions.render_pdf(db, offer, version)
    return pdf


async def letter_file_id(
    ctx: RequestContext, db: AsyncSession, offer_id: uuid.UUID, version_no: int | None
) -> uuid.UUID:
    offer = await offer_for(ctx, db, offer_id, READ)
    version = (
        await versions.by_no(db, offer.id, version_no)
        if version_no is not None
        else await versions.current(db, offer)
    )
    if version is None:
        raise NotFound("Offer version not found")
    if version.letter_file_id is None:
        raise NotFound("The letter for this version is not ready yet", code="letter_not_ready")
    return version.letter_file_id


async def accepted_letter_file_id(ctx: RequestContext, db: AsyncSession, offer_id: uuid.UUID) -> uuid.UUID:
    offer = await offer_for(ctx, db, offer_id, READ)
    if offer.accepted_file_id is None:
        raise NotFound("No signed offer letter", code="letter_not_ready")
    return offer.accepted_file_id


async def offer_access(ctx: RequestContext, db: AsyncSession, offer_id: uuid.UUID, write: bool) -> bool:
    """File download / activity hook: may the caller see (or edit) this offer."""
    from app.core.errors import DomainError

    try:
        await offer_for(ctx, db, offer_id, WRITE if write else READ)
    except DomainError:
        return False
    return True


__all__ = [
    "SUBJECT_TYPE",
    "accepted_letter_file_id",
    "create_offer",
    "get_offer",
    "get_settings",
    "get_version",
    "letter_file_id",
    "letter_preview",
    "list_for_application",
    "list_for_demand",
    "list_offers",
    "offer_access",
    "offer_detail",
    "register_approvals",
    "release",
    "release_offer",
    "submit_offer",
    "tenant_of",
    "update_offer",
    "update_settings",
    "withdraw_offer",
]
