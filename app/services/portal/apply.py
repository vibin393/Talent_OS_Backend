"""Consent-first career apply (Epic 9 story 9.2; FR-48, FR-21, FR-67).

Three calls, all rate limited:

1. ``create_session`` -- consents (``data_processing`` required, notice ids must be the active versions)
   and the bot challenge, *before* any upload or parse. Returns an opaque ``apply_token`` (only its SHA-256 is
   stored; ``apply_session_lookups`` maps it to the tenant).
2. ``upload_resume`` -- scanned store, text extraction, and an AI parse **only** when the session holds
   ``ai_processing`` consent and the tenant's ``resume_parsing`` feature is on. Never reveals whether the
   applicant is already known.
3. ``submit`` -- one transaction: link by exact email (resume added as a new version, parsed data stored but
   not applied) or create a ``career_portal`` candidate; duplicate review items (never merged); the session
   consents become ``consents`` rows with their original timestamps; the application (idempotent per
   candidate + demand) and its answers. The response is the same 202 in every case.

After commit ``send_apply_confirmation`` emails a Candidate Portal sign-in link; ``purge_apply_sessions``
deletes expired, unsubmitted sessions with their files.
"""

import secrets
import uuid
from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit, bot_challenge, rate_limit
from app.core.context import RequestContext
from app.core.database import tenant_session
from app.core.errors import ConflictError, Gone, NotFound, RateLimited, ValidationFailed
from app.core.events import emit
from app.core.settings import get_settings
from app.events.portal import CareerApplicationSubmitted
from app.models.portal import ApplicationAnswer, ApplySession, ApplySessionLookup
from app.schemas.careers import (
    ApplySessionCreate,
    ApplySubmit,
    PublicApplySession,
    PublicResumeResult,
    PublicStatus,
)
from app.services import ai, files_service
from app.services.candidates import parsing
from app.services.candidates import portal_port as candidates
from app.services.pipeline import portal_port as pipeline
from app.services.portal import careers, projection
from app.services.portal.common import email_hash, ip_hash, now, sha256_hex

APPLY_TOKEN_HEADER = "X-Apply-Token"
MAX_UPLOADS = 5
RESUME_FEATURE = "resume_parsing"
OWNER = "apply_session"


# Epic 39 (additive registry; ``portal`` never imports ``campaigns``): resolves a marketing ``attribution_token`` of
# this tenant to the campaign message id (``source_ref``), or None when invalid.
AttributionResolver = Any
_attribution_resolver: AttributionResolver | None = None


def register_attribution_resolver(fn: AttributionResolver | None) -> None:
    global _attribution_resolver  # process-wide registry
    _attribution_resolver = fn


async def _attribution(db: AsyncSession, tenant_id: uuid.UUID, token: str | None) -> str | None:
    if not token or _attribution_resolver is None:
        return None
    try:
        return await _attribution_resolver(db, tenant_id, token)  # type: ignore[no-any-return]
    except Exception:  # an attribution problem never blocks an application
        return None


# Epic 70 (D-18, additive keyed registry; ``portal`` never imports the Phase 5 packages): ``src`` tag of the apply
# link -> ``fn(db, tenant_id, ref) -> (source, source_ref) | None``. Epic 70 registers the job boards, Epic 75
# ``job_network``. Unknown tags, invalid refs and resolver errors leave the application as ``career_portal``.
ApplySourceResolver = Any
_source_resolvers: dict[str, ApplySourceResolver] = {}


def register_apply_source_resolver(key: str, fn: ApplySourceResolver | None) -> None:
    if fn is None:
        _source_resolvers.pop(key, None)
    else:
        _source_resolvers[key] = fn


async def _source_attribution(
    db: AsyncSession, tenant_id: uuid.UUID, src: str | None, ref: str | None
) -> tuple[str, str] | None:
    fn = _source_resolvers.get(src or "")
    if fn is None:
        return None
    try:
        out = await fn(db, tenant_id, ref)
    except Exception:  # an attribution problem never blocks an application
        return None
    return (str(out[0]), str(out[1])) if out else None


def _session_not_found() -> NotFound:
    return NotFound("Apply session not found", code="apply_session_not_found")


async def _load(
    db: AsyncSession, tenant_id: uuid.UUID, token: str | None, *, for_update: bool = True
) -> ApplySession:
    if not token or len(token) > 200:
        raise _session_not_found()
    digest = sha256_hex(token)
    lookup = await db.get(ApplySessionLookup, digest)
    if lookup is None or lookup.tenant_id != tenant_id:
        raise _session_not_found()
    row = await db.get(ApplySession, lookup.session_id, with_for_update=for_update)
    if row is None or row.token_hash != digest:
        raise _session_not_found()
    if row.submitted_at is not None:
        raise ConflictError("This application was already submitted", code="apply_session_submitted")
    if row.expires_at <= now():
        raise Gone("This application session has expired; please start again", code="apply_session_expired")
    return row


def _consent_purposes(row: ApplySession) -> dict[str, dict[str, Any]]:
    return {str(c["purpose"]): c for c in row.consents or []}


# ================================================================ 1. session
async def create_session(
    slug: str, posting_id: uuid.UUID, data: ApplySessionCreate, *, ip: str | None, user_agent: str | None
) -> PublicApplySession:
    from app.services import privacy

    site = await careers.resolve_site(slug)
    await rate_limit.enforce("apply_session", f"{site.tenant_id.hex}:{ip_hash(ip)}", 10, 3600)
    purposes = {c.purpose: c.notice_id for c in data.consents}
    if "data_processing" not in purposes:
        raise ValidationFailed("Consent to data processing is required to apply", code="consent_required")
    async with tenant_session(site.tenant_id) as db:
        await careers.live_posting(db, posting_id)
        for purpose, notice_id in purposes.items():
            active = await privacy.consent.active_notice(db, site.tenant_id, purpose)
            if active is None or active.id != notice_id:
                raise ConflictError(
                    "The privacy notice changed; please review it again",
                    code="notice_outdated",
                    details={"purpose": purpose, "notice_id": str(active.id) if active else None},
                )
    await bot_challenge.verify_or_fail(data.challenge_token, ip)
    token = secrets.token_urlsafe(32)
    digest = sha256_hex(token)
    granted_at = now()
    expires = granted_at + timedelta(minutes=get_settings().apply_session_minutes)
    async with tenant_session(site.tenant_id) as db:
        row = ApplySession(
            id=uuid.uuid4(),
            tenant_id=site.tenant_id,
            token_hash=digest,
            posting_id=posting_id,
            consents=[
                {"purpose": p, "notice_id": str(n), "granted_at": granted_at.isoformat()}
                for p, n in purposes.items()
            ],
            ip_hash=ip_hash(ip),
            user_agent=(user_agent or "")[:300] or None,
            expires_at=expires,
            src=data.src,
            src_ref=data.ref,
        )
        db.add(row)
        db.add(ApplySessionLookup(token_hash=digest, tenant_id=site.tenant_id, session_id=row.id))
        await db.flush()
    return PublicApplySession(apply_token=token, expires_at=expires)


# ================================================================ 2. resume
def _location_text(loc: dict[str, Any] | None) -> str | None:
    if not loc:
        return None
    parts = [str(loc.get(k)) for k in ("city", "state", "country") if loc.get(k)]
    return ", ".join(parts) or None


def _prefill(p: parsing.Parsed) -> dict[str, Any]:
    return {
        "full_name": p.full_name,
        "email": p.emails[0] if p.emails else None,
        "phone": p.phones[0] if p.phones else None,
        "location": _location_text(p.location),
        "current_title": p.current_title,
        "current_employer": p.current_employer,
        "total_experience_months": p.total_experience_months,
        "skills": [ref.name for ref, _, _ in p.skills] + [u.name for u in p.unresolved],
        "links": [u for u, _ in p.links],
    }


async def upload_resume(
    slug: str, token: str | None, data: bytes, filename: str | None
) -> PublicResumeResult:
    site = await careers.resolve_site(slug)
    async with tenant_session(site.tenant_id) as db:
        audit.set_actor(db, "system", None)
        ctx = RequestContext(tenant_id=site.tenant_id)
        row = await _load(db, site.tenant_id, token)
        if row.upload_count >= MAX_UPLOADS:
            raise RateLimited("Too many uploads for this application", code="rate_limited")
        file = await files_service.store_upload(
            ctx, db, data, filename, files_service.RESUME_KINDS, OWNER, row.id
        )
        if row.resume_file_id is not None:
            await files_service.purge_file(db, row.resume_file_id)
        row.resume_file_id = file.id
        row.upload_count += 1
        extraction = candidates.extract(data)
        row.resume_text = extraction.text
        row.text_status = extraction.status
        prefill: dict[str, Any] = {}
        parsed: dict[str, Any] | None = None
        action_id: uuid.UUID | None = None
        if "ai_processing" not in _consent_purposes(row):
            status = "skipped_consent"
        elif not await ai.is_feature_enabled(db, RESUME_FEATURE):
            status = "skipped_disabled"
        elif extraction.status != "ok" or not extraction.text:
            status = "failed_manual"
        else:
            status, output, action_id, _ = await parsing.run_parse(
                ctx, db, extraction.text, subject=None, input_ref=f"apply_session:{row.id}"
            )
            if output is not None:
                parsed = output.model_dump(mode="json")
                prefill = _prefill(await parsing.postprocess(ctx, db, output))
        row.parse_status = status
        row.parsed_data = parsed
        row.prefill = prefill or None
        row.ai_action_id = action_id
        await db.flush()
    return PublicResumeResult(parse_status=status, prefill=projection.prefill(prefill))


# ================================================================ 3. submit
def _clean_phone(value: str | None, required: bool) -> str | None:
    if not value:
        if required:
            raise ValidationFailed(
                "A phone number is required", code="phone_required", details={"fields": {"phone": "Required"}}
            )
        return None
    phone = candidates.clean_phone(value)
    if phone is None:
        raise ValidationFailed(
            "Invalid phone number",
            code="invalid_phone",
            details={"fields": {"phone": "Use an international or Indian number"}},
        )
    return phone


def _clean_links(values: list[str]) -> list[str]:
    out: list[str] = []
    for i, raw in enumerate(values):
        if not raw.strip():
            continue
        url = candidates.clean_url(raw)
        if url is None:
            raise ValidationFailed(
                "Invalid link", code="invalid_link", details={"fields": {f"links.{i}": "Invalid URL"}}
            )
        if url not in out:
            out.append(url)
    return out


async def submit(
    slug: str, token: str | None, data: ApplySubmit, *, ip: str | None, user_agent: str | None
) -> PublicStatus:
    from app.services import platform_service, privacy
    from app.services.pipeline import screening

    site = await careers.resolve_site(slug)
    await rate_limit.enforce("apply_submit", f"{site.tenant_id.hex}:{ip_hash(ip)}", 5, 3600)
    if not await platform_service.is_accepting_applications(site.tenant_id):
        raise ConflictError("Applications are not accepted at the moment", code="tenant_not_accepting")
    email = candidates.clean_email(str(data.email))
    if email is None:
        raise ValidationFailed(
            "Invalid email address", details={"fields": {"email": "Invalid email address"}}
        )
    async with tenant_session(site.tenant_id) as db:
        audit.set_actor(db, "system", None)
        base_ctx = RequestContext(tenant_id=site.tenant_id, ip=ip, user_agent=user_agent)
        row = await _load(db, site.tenant_id, token)
        posting = await careers.live_posting(db, row.posting_id)
        await rate_limit.enforce("apply_email", f"{posting['posting_id'].hex}:{email_hash(email)}", 3, 86400)
        phone = _clean_phone(data.phone, site.apply_requires_phone)
        links = _clean_links(data.links)
        if row.resume_file_id is None:
            raise ValidationFailed(
                "Please upload your resume",
                code="resume_required",
                details={"fields": {"resume": "Required"}},
            )
        questions = list(posting["apply_questions"])
        answers = screening.normalize_answers(questions, data.answers, require=True)
        marketing_notice = None
        if data.marketing_consent_notice_id is not None:
            marketing_notice = await privacy.consent.active_notice(db, site.tenant_id, "marketing")
            if marketing_notice is None or marketing_notice.id != data.marketing_consent_notice_id:
                raise ConflictError(
                    "The marketing notice changed; please review it again",
                    code="notice_outdated",
                    details={"purpose": "marketing"},
                )

        candidate_id = await candidates.link_by_exact_email(base_ctx, db, email)
        prefill = dict(row.prefill or {})
        if candidate_id is None:
            candidate_id = await candidates.create_from_application(
                base_ctx,
                db,
                owner_id=posting["owner_id"],
                full_name=data.full_name,
                email=email,
                phone=phone,
                location=data.location,
                links=links,
                prefill=prefill,
            )
            created = True
        else:
            created = False
        await candidates.add_resume(
            base_ctx,
            db,
            candidate_id,
            file_id=row.resume_file_id,
            text=row.resume_text,
            text_status=row.text_status or "failed",
            parse_status=row.parse_status or "failed_manual",
            parsed_data=row.parsed_data,
            ai_action_id=row.ai_action_id,
            note="Resume received via the career portal",
        )
        if created and row.ai_action_id is not None and row.parse_status == "parsed" and prefill:
            await candidates.record_parse_decision(
                base_ctx,
                db,
                row.ai_action_id,
                prefill,
                {"full_name": data.full_name, "email": email, "phone": phone},
            )

        audit.set_actor(db, "candidate", candidate_id)
        cctx = RequestContext(tenant_id=site.tenant_id, ip=ip, user_agent=user_agent)
        evidence = {"ip_hash": row.ip_hash, "user_agent": row.user_agent, "apply_session_id": str(row.id)}
        for purpose, consent in _consent_purposes(row).items():
            if await privacy.has_active_consent(db, candidate_id, purpose):
                continue
            granted = await privacy.grant_consent(
                cctx,
                db,
                candidate_id,
                purpose,
                source="application",
                notice_id=uuid.UUID(str(consent["notice_id"])),
                evidence=evidence,
            )
            granted.granted_at = _parse_dt(consent.get("granted_at")) or granted.granted_at
        if marketing_notice is not None and not await privacy.has_active_consent(
            db, candidate_id, "marketing"
        ):
            await privacy.grant_consent(
                cctx,
                db,
                candidate_id,
                "marketing",
                source="application",
                notice_id=marketing_notice.id,
                evidence=evidence,
            )

        existing = await pipeline.existing_application(db, posting["demand_id"], candidate_id)
        if existing is not None:
            application_id, already = existing, True
        else:
            campaign_ref = await _attribution(db, site.tenant_id, data.attribution_token)
            source, source_ref = "career_portal", str(posting["posting_id"])
            if campaign_ref:  # campaign attribution keeps precedence (Epic 70 spec A6)
                source, source_ref = "campaign", campaign_ref
            else:
                tagged = await _source_attribution(db, site.tenant_id, row.src, row.src_ref)
                if tagged is not None:
                    source, source_ref = tagged
            application_id = await pipeline.create_candidate_application(
                cctx,
                db,
                posting["demand_id"],
                candidate_id,
                source=source,
                source_ref=source_ref,
            )
            already = False
            if questions:
                db.add(
                    ApplicationAnswer(
                        id=uuid.uuid4(),
                        tenant_id=site.tenant_id,
                        application_id=application_id,
                        questions=questions,
                        answers=answers,
                        submitted_at=now(),
                    )
                )
        row.submitted_at = now()
        row.application_id = application_id
        row.confirmation_email = email
        # Data minimisation: the parse and prefill leave the session on submit.
        row.resume_text = None
        row.parsed_data = None
        row.prefill = None
        await db.flush()
        audit.record(
            "portal.application_submitted",
            "application",
            application_id,
            session=db,
            posting_id=str(posting["posting_id"]),
            already_applied=already,
            new_candidate=created,
        )
        emit(
            db,
            CareerApplicationSubmitted(
                tenant_id=site.tenant_id, application_id=application_id, already_applied=already
            ),
        )
    return PublicStatus(status="received")


def _parse_dt(value: Any) -> Any:
    from datetime import datetime

    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


# ================================================================ confirmation + cleanup (Celery)
async def send_confirmation(tenant_id: uuid.UUID, application_id: uuid.UUID, already_applied: bool) -> str:
    """Tenant-branded "We received your application for {title}" with a portal sign-in link (single use)."""
    from app.core.mailer import EmailMessage, send_email
    from app.services.portal.candidate_auth import issue_sign_in_link, portal_verify_url

    async with tenant_session(tenant_id) as db:
        audit.set_actor(db, "system", None)
        row = (
            await db.execute(
                select(ApplySession)
                .where(
                    ApplySession.application_id == application_id,
                    ApplySession.confirmation_email.is_not(None),
                )
                .order_by(ApplySession.submitted_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        candidate_id = await pipeline.application_candidate(db, application_id)
        if row is None or candidate_id is None:
            return "skipped"
        recipient = str(row.confirmation_email)
        row.confirmation_email = None
        demand_id = await pipeline.application_demand(db, application_id)
        title = "your application"
        if demand_id is not None:
            from app.services.demands import pipeline_port as demands_port

            title = (await demands_port.titles(db, [demand_id])).get(demand_id, {}).get("title") or title
        site = await careers.site_for_tenant(db, tenant_id)
        brand = site.display_name if site else "Careers"
        token = await issue_sign_in_link(
            db,
            tenant_id,
            candidate_id,
            timedelta(hours=get_settings().apply_confirmation_link_hours),
            revoke_open=False,
        )
    intro = (
        f"We already have your application for {title} and it is still being considered."
        if already_applied
        else f"Thank you for applying for {title}. We have received your application."
    )
    await send_email(
        EmailMessage(
            to=recipient,
            subject=f"We received your application for {title}",
            text=(
                f"Hello,\n\n{intro}\n\nFollow your application in the {brand} candidate portal:\n"
                f"{portal_verify_url(token)}\n\nThis sign-in link works once and expires in "
                f"{get_settings().apply_confirmation_link_hours} hours. You can always request a new one from the portal.\n\n"
                f"{brand}\n"
            ),
            tenant_id=tenant_id,
            kind="apply_confirmation",
            from_name=brand,
        )
    )
    return "sent"


async def purge_expired(tenant_id: uuid.UUID) -> dict[str, int]:
    """Delete expired, unsubmitted apply sessions with their stored files and parsed data (data minimisation);
    submitted sessions (data already cleared) are dropped once expired as well."""
    counts = {"sessions": 0, "files": 0}
    async with tenant_session(tenant_id) as db:
        audit.set_actor(db, "system", None)
        rows = list(
            (
                await db.execute(
                    select(ApplySession)
                    .where(ApplySession.expires_at <= now())
                    .with_for_update(skip_locked=True)
                )
            ).scalars()
        )
        for row in rows:
            if row.submitted_at is None and row.resume_file_id is not None:
                if await files_service.purge_file(db, row.resume_file_id):
                    counts["files"] += 1
            lookup = await db.get(ApplySessionLookup, row.token_hash)
            if lookup is not None:
                await db.delete(lookup)
            await db.delete(row)
            counts["sessions"] += 1
        await db.flush()
    return counts
