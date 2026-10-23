# ruff: file-ignore[line-too-long, missing-trailing-period, missing-terminal-punctuation]
"""Pre-send checks of a submission pack (Epic 61 A2, FR-306). Deterministic: never a model call, so consent re-checks
and the checks-only fallback work with AI off. Every check reuses the P1 / P2 rule exactly:

| Check | Source | Levels |
|---|---|---|
| ``consent`` | ``privacy.has_active_consent(.., "client_submission", client)``; tenant consent mode; Client rule | pass / warn ``attestation_possible`` / block ``consent_required`` |
| ``margin`` | ``pipeline.margin.compute`` with ``min_margin_pct`` and ``burden_for`` | needs_input / pass / warn ``below_min_margin`` / block ``invalid_bill_rate``, ``currency_mismatch`` |
| ``rate_card`` | commercials rate-check **preview** (no ``rate_checks`` row) | pass / warn ``outside``, ``no_match`` / block ``rate_exception_required`` / unavailable |
| ``duplicate`` | P1 prior-submission rule, window = Client override or tenant setting | pass / block ``prior_submission`` |
| ``vendor_ownership`` | vendor ownership + the candidate/client first submitter | pass / warn ``owned_by_other`` |
| ``required_fields`` | stage, submission limit, resume, notice period | block ``not_shortlisted``, ``submission_limit_reached``, ``resume_missing`` / warn ``notice_period_missing`` |
| ``resume_format`` | Client rule, latest resume ``parse_status`` | block ``resume_not_parsed`` / warn ``contact_details_in_original`` |
"""

import dataclasses
import uuid
from datetime import date
from decimal import Decimal
from typing import Any

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.errors import ValidationFailed
from app.models.candidates import Candidate, CandidateResume
from app.models.demands import Demand
from app.models.pipeline import Application
from app.schemas.submission_packs import CHECK_KEYS, PackCheck, PackCheckDetail, PackLink, PackRatesIn
from app.services.submission_packs.common import CONSENT_PURPOSE, stage_key
from app.services.submission_packs.rules import EffectiveRules, effective_rules

RATE_UNITS = ("hour", "day", "month", "year")
AMOUNT_DETAIL_KEYS = frozenset({"min_bill_rate", "max_bill_rate", "bill_rate", "pay_rate", "margin_amount"})


@dataclasses.dataclass(frozen=True, slots=True)
class Rates:
    pay_rate: Decimal | None = None
    bill_rate: Decimal | None = None
    pay_rate_unit: str | None = None
    bill_rate_unit: str | None = None
    currency: str | None = None

    @classmethod
    def of(cls, data: PackRatesIn | None) -> "Rates":
        if data is None:
            return cls()
        return cls(data.pay_rate, data.bill_rate, data.pay_rate_unit, data.bill_rate_unit, data.currency)


@dataclasses.dataclass(slots=True)
class Facts:
    app: Application
    demand: Demand
    candidate: Candidate
    rules: EffectiveRules
    stage: str
    resume: CandidateResume | None


async def latest_resume(db: AsyncSession, candidate_id: uuid.UUID) -> CandidateResume | None:
    return (
        await db.execute(
            select(CandidateResume)
            .where(CandidateResume.candidate_id == candidate_id)
            .order_by(CandidateResume.version.desc().nullslast(), CandidateResume.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def load_facts(db: AsyncSession, app: Application) -> Facts:
    demand = await db.get(Demand, app.demand_id)
    candidate = await db.get(Candidate, app.candidate_id)
    if demand is None or candidate is None:  # pragma: no cover - FK guarantees
        msg = "application without demand / candidate"
        raise RuntimeError(msg)
    return Facts(
        app=app,
        demand=demand,
        candidate=candidate,
        rules=await effective_rules(db, demand.client_id),
        stage=await stage_key(db, app),
        resume=await latest_resume(db, app.candidate_id),
    )


def _d(
    key: str, level: str, code: str, message: str, link: PackLink | None = None, **details: Any
) -> PackCheckDetail:
    clean = {
        k: (str(v) if isinstance(v, Decimal | uuid.UUID | date) else v)
        for k, v in details.items()
        if v is not None
    }
    return PackCheckDetail(key=key, level=level, code=code, message=message[:300], details=clean, link=link)


# ---------------------------------------------------------------- consent
async def check_consent(db: AsyncSession, f: Facts) -> PackCheckDetail:
    from app.services import privacy

    link = PackLink(
        kind="candidate", id=f.app.candidate_id, path=f"/candidates/{f.app.candidate_id}?tab=privacy"
    )
    if await privacy.has_active_consent(db, f.app.candidate_id, CONSENT_PURPOSE, f.demand.client_id):
        return _d(
            "consent",
            "pass",
            "consent_granted",
            "The candidate consented to be submitted to this client.",
            link,
        )
    if f.rules.consent_mode == "attestation_allowed" and not f.rules.require_candidate_consent:
        return _d(
            "consent", "warn", "attestation_possible",
            "No candidate consent yet. Recruiter can attest in the Submission dialog.", link,
        )  # fmt: skip
    return _d(
        "consent", "block", "consent_required",
        "The candidate must consent to be submitted to this client before anyone can send.", link,
        require_candidate_consent=f.rules.require_candidate_consent, consent_mode=f.rules.consent_mode,
    )  # fmt: skip


# ---------------------------------------------------------------- margin
async def check_margin(db: AsyncSession, f: Facts, rates: Rates) -> PackCheckDetail:
    from app.services import pipeline_service
    from app.services.pipeline import margin as margin_calc
    from app.services.pipeline.settings import burden_for

    if rates.pay_rate is None or rates.bill_rate is None or not rates.currency:
        return _d("margin", "needs_input", "needs_rates", "Enter the pay and bill rates to see the margin.")
    settings = await pipeline_service.get_pipeline_settings(db)
    unit = f.demand.rate_unit if f.demand.rate_unit in RATE_UNITS else (rates.bill_rate_unit or "hour")
    try:
        m = margin_calc.compute(
            pay=rates.pay_rate, bill=rates.bill_rate, pay_currency=rates.currency, bill_currency=rates.currency,
            rate_unit=unit, burden_pct=burden_for(settings, f.demand.employment_type),
            min_pct=Decimal(settings.min_margin_pct), pay_unit=rates.pay_rate_unit, bill_unit=rates.bill_rate_unit,
            demand_currency=f.demand.currency,
        )  # fmt: skip
    except ValidationFailed as exc:
        code = exc.code if exc.code in ("invalid_bill_rate", "currency_mismatch") else "invalid_rates"
        return _d("margin", "block", code, exc.message)
    details = {"margin_pct": m.pct, "min_margin_pct": m.min_pct, "rate_unit": m.rate_unit}
    if m.below_min:
        return _d(
            "margin", "warn", "below_min_margin",
            "Below minimum margin — approval is requested when you submit.",
            PackLink(kind="approval", path="/approvals"), **details,
        )  # fmt: skip
    return _d("margin", "pass", "margin_ok", "The margin meets the minimum.", **details)


# ---------------------------------------------------------------- rate card
async def check_rate_card(ctx: RequestContext, db: AsyncSession, f: Facts, rates: Rates) -> PackCheckDetail:
    unit = rates.bill_rate_unit or f.demand.rate_unit
    if rates.bill_rate is None or not rates.currency or not unit:
        return _d(
            "rate_card", "needs_input", "needs_rates", "Enter the bill rate to check the client's rate card."
        )
    try:
        from app.core.fiscal import today_for
        from app.schemas.commercials import RateCheckRequest
        from app.services import entity_refs
        from app.services.commercials.gates import candidate_experience_years
        from app.services.commercials.rate_checks import preview

        tenant = await entity_refs.tenant(db, entity_refs.session_tenant(db))
        req = RateCheckRequest(
            subject_kind="submission", subject_id=f.app.id, client_id=f.demand.client_id,
            on_date=today_for(getattr(tenant, "timezone", None) or "Asia/Kolkata"), role=f.demand.title,
            location=f.demand.location, experience_years=await candidate_experience_years(db, f.app.candidate_id),
            bill_rate=rates.bill_rate, currency=rates.currency, rate_unit=unit,
        )  # fmt: skip
        async with db.begin_nested():
            res = await preview(ctx, db, req)
    except Exception:
        logger.exception("submission pack: rate-card preview failed (application {})", f.app.id)
        return _d("rate_card", "unavailable", "unavailable", "The rate card could not be checked.")
    link = PackLink(
        kind="rate_card", id=res.rate_card_id, path=f"/clients/{f.demand.client_id}?tab=rate-cards"
    )
    details = {"min_bill_rate": res.min_bill_rate, "max_bill_rate": res.max_bill_rate, "status": res.status}
    if res.status == "outside" and res.requires_exception:
        return _d(
            "rate_card",
            "block",
            "rate_exception_required",
            "Rate exception approval needed at submit.",
            link,
            **details,
        )
    if res.status == "outside":
        return _d(
            "rate_card",
            "warn",
            "outside",
            "The bill rate is outside the client's rate card.",
            link,
            **details,
        )
    if res.status == "no_match":
        return _d("rate_card", "warn", "no_match", "No rate card line matches this role.", link, **details)
    if res.status == "not_applicable":
        return _d("rate_card", "pass", "not_applicable", "The client has no rate card for this role.")
    return _d(
        "rate_card", "pass", "within", "The bill rate is within the client's rate card.", link, **details
    )


# ---------------------------------------------------------------- duplicate
async def check_duplicate(db: AsyncSession, f: Facts) -> PackCheckDetail:
    from app.services import entity_refs, pipeline_service

    window = f.rules.duplicate_window_days
    prior = await pipeline_service.prior_submission(db, f.app.candidate_id, f.demand.client_id, window)
    if prior is None:
        return _d(
            "duplicate",
            "pass",
            "no_prior_submission",
            f"Not submitted to this client in the last {window} days.",
            window_days=window,
        )
    who = await entity_refs.user(db, prior.sent_by) if prior.sent_by else None
    when = prior.sent_at.date().isoformat() if prior.sent_at else "recently"
    return _d(
        "duplicate", "block", "prior_submission",
        f"Submitted to this client on {when} by {who.name if who else 'a colleague'}. You can still submit with a reason.",
        PackLink(kind="submission", id=prior.id, path=f"/submissions/{prior.id}"),
        last_submitted_at=prior.sent_at.isoformat() if prior.sent_at else None, by=prior.sent_by,
        demand_id=prior.demand_id, window_days=window,
    )  # fmt: skip


# ---------------------------------------------------------------- vendor ownership
async def check_vendor(ctx: RequestContext, db: AsyncSession, f: Facts) -> PackCheckDetail:
    from app.services import entity_refs, pipeline_service, vendors_service

    owner = await vendors_service.candidate_vendor_owner(ctx, db, f.app.candidate_id, f.demand.client_id)
    sourcing = await vendors_service.sourcing_for_application(ctx, db, f.app.id)
    if owner is not None and (sourcing is None or sourcing.vendor_id != owner["vendor_id"]):
        expires = owner["expires_at"]
        return _d(
            "vendor_ownership", "warn", "owned_by_other",
            f"{owner['vendor_name']} owns this candidate for this client until {expires.date().isoformat() if expires else 'further notice'}.",  # type: ignore[attr-defined]
            PackLink(kind="vendor", id=owner["vendor_id"], path=f"/vendors/{owner['vendor_id']}"),
            owner_kind="vendor", vendor_id=owner["vendor_id"],
        )  # fmt: skip
    first = await pipeline_service.client_first_submitter(db, f.demand.client_id, f.app.candidate_id)
    if first is not None and first.first_submitted_by not in (None, f.app.owner_id):
        who = await entity_refs.user(db, first.first_submitted_by)
        return _d(
            "vendor_ownership", "warn", "owned_by_other",
            f"First submitted to this client by {who.name if who else 'a colleague'} on {first.first_submitted_at.date().isoformat()}.",
            PackLink(kind="submission", id=first.submission_id, path=f"/submissions/{first.submission_id}"),
            owner_kind="first_submitter", user_id=first.first_submitted_by,
        )  # fmt: skip
    return _d("vendor_ownership", "pass", "no_conflict", "No vendor or first-submitter conflict.")


# ---------------------------------------------------------------- required fields
async def check_required(db: AsyncSession, f: Facts) -> PackCheckDetail:
    from app.services import pipeline_service

    issues: list[tuple[str, str, str]] = []
    if f.stage != "shortlisted":
        issues.append(("block", "not_shortlisted", "The application is no longer shortlisted."))
    limit = f.demand.submission_limit
    if limit is not None and await pipeline_service.counted_submissions(db, f.demand.id) >= limit:
        issues.append(
            ("block", "submission_limit_reached", f"The demand's submission limit ({limit}) is reached.")
        )
    if f.resume is None:
        issues.append(("block", "resume_missing", "The candidate has no resume on file."))
    if (
        f.rules.show_notice_period
        and f.candidate.notice_period_days is None
        and f.candidate.availability_date is None
    ):
        issues.append(("warn", "notice_period_missing", "Add the candidate's notice period or availability."))
    link = PackLink(kind="application", id=f.app.id, path=f"/pipeline/applications/{f.app.id}")
    if not issues:
        return _d("required_fields", "pass", "complete", "Everything the client needs is on file.")
    issues.sort(key=lambda i: 0 if i[0] == "block" else 1)
    level, code, message = issues[0]
    return _d("required_fields", level, code, message, link, issues=[i[1] for i in issues])


# ---------------------------------------------------------------- resume format
def resume_choice(rules: EffectiveRules, resume: CandidateResume | None) -> str | None:
    """Deterministic resume kind: client-formatted when the rule allows it and the latest resume is parsed."""
    if resume is None:
        return None
    parsed = resume.parse_status == "parsed"
    if rules.resume_format == "client_formatted":
        return "client_formatted" if parsed else None
    if rules.resume_format == "original":
        return "original"
    return "client_formatted" if parsed else "original"


def check_resume_format(f: Facts) -> PackCheckDetail:
    if f.resume is None:
        return _d("resume_format", "needs_input", "resume_missing", "Upload a resume first.")
    kind = resume_choice(f.rules, f.resume)
    if kind is None:
        return _d(
            "resume_format", "block", "resume_not_parsed",
            "This client needs the client-formatted profile, but the latest resume is not parsed yet.",
            PackLink(kind="candidate", id=f.app.candidate_id, path=f"/candidates/{f.app.candidate_id}?tab=resume"),
            parse_status=f.resume.parse_status,
        )  # fmt: skip
    if kind == "original" and f.rules.mask_contact_details:
        return _d(
            "resume_format", "warn", "contact_details_in_original",
            "The original resume shows contact details; this client asks to hide them.",
        )  # fmt: skip
    label = (
        "Client-formatted profile (no contact details)" if kind == "client_formatted" else "Original resume"
    )
    return _d("resume_format", "pass", kind, f"{label} matches the client's rule.")


# ---------------------------------------------------------------- run
async def evaluate(
    ctx: RequestContext, db: AsyncSession, f: Facts, rates: Rates
) -> tuple[PackCheckDetail, ...]:
    results = {
        "consent": await check_consent(db, f),
        "required_fields": await check_required(db, f),
        "resume_format": check_resume_format(f),
        "duplicate": await check_duplicate(db, f),
        "vendor_ownership": await check_vendor(ctx, db, f),
        "margin": await check_margin(db, f, rates),
        "rate_card": await check_rate_card(ctx, db, f, rates),
    }
    return tuple(results[k] for k in CHECK_KEYS)


def missing_of(
    checks: tuple[PackCheckDetail, ...] | list[PackCheckDetail], *, has_write_up: bool
) -> tuple[str, ...]:
    out: list[str] = []
    by = {c.key: c for c in checks}
    if by.get("resume_format") and by["resume_format"].code == "resume_not_parsed":
        out.append("parsed_resume")
    req = by.get("required_fields")
    if req is not None and "resume_missing" in (req.details.get("issues") or [req.code]):
        out.append("resume")
    if req is not None and "notice_period_missing" in (req.details.get("issues") or [req.code]):
        out.append("notice_period")
    if by.get("consent") and by["consent"].level == "block":
        out.append("consent")
    if by.get("margin") and by["margin"].level == "needs_input":
        out.append("rates")
    if not has_write_up:
        out.append("write_up")
    return tuple(out)


def to_stub(checks: list[PackCheckDetail] | tuple[PackCheckDetail, ...]) -> tuple[PackCheck, ...]:
    """Stub DTO mapping: ``ok = level != "block"``, ``message = f"[{level}] {message}"``."""
    return tuple(
        PackCheck(key=c.key, ok=c.level != "block", message=f"[{c.level}] {c.message}") for c in checks
    )


def mask_details(checks: list[PackCheckDetail], *, may_see_amounts: bool) -> list[PackCheckDetail]:
    if may_see_amounts:
        return checks
    return [
        c.model_copy(
            update={
                "details": {
                    k: v for k, v in c.details.items() if k not in AMOUNT_DETAIL_KEYS and "margin" not in k
                }
            }
        )
        for c in checks
    ]


__all__ = [
    "Facts",
    "Rates",
    "evaluate",
    "latest_resume",
    "load_facts",
    "mask_details",
    "missing_of",
    "resume_choice",
    "to_stub",
]
