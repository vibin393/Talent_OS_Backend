"""Candidate magic-link sign-in (Epic 9 story 9.3; FR-49, AD-10, NFR-17).

* ``request_link`` always answers the same 202 after a constant minimum delay; the lookup, the per-email
  limit (5 / email / tenant / hour, silent) and the send run after the response (``BackgroundTasks`` ->
  Celery ``send_candidate_sign_in_link``), so neither timing nor body reveals whether the email exists. Only
  the per-IP limit (20 / hour) answers differently (429), independent of the email.
* Links: secure-link purpose ``candidate_sign_in``, TTL ``CANDIDATE_LINK_MINUTES``, single use, URL
  ``{FRONTEND_URL}/portal/verify#t=<token>`` (the fragment never reaches servers or referrers). A new link
  revokes the candidate's earlier open sign-in links.
* ``verify`` is a POST (mail-scanner GET prefetches never consume): resolve in the link's tenant, consume
  atomically (exactly one of racing verifies wins), then open a ``sessions(principal_type='candidate')`` row
  (idle 24 h, absolute 7 d) and set the ``tos_candidate`` + ``tos_csrf`` cookies.
* ``authenticate_candidate`` backs ``app.dependencies.candidate.require_candidate``: reads only
  ``tos_candidate``.
"""

import asyncio
import time
import uuid
from datetime import timedelta

from fastapi import BackgroundTasks, Request, Response
from loguru import logger
from sqlalchemy import update

from app.core import audit, bot_challenge, rate_limit, secure_links
from app.core.context import RequestContext, update_context
from app.core.database import platform_session, tenant_session
from app.core.errors import NotFound, Unauthorized
from app.core.events import emit
from app.core.security import (
    CANDIDATE_COOKIE,
    clear_auth_cookies,
    csrf_token_for,
    set_csrf_cookie,
    set_session_cookie,
    verify_csrf,
)
from app.core.settings import get_settings
from app.events.portal import CandidateSignedIn
from app.models.identity import Session as AuthSession
from app.schemas.careers import CandidateLinkRequest, PublicStatus
from app.schemas.portal import PortalBranding, PortalCandidateRef, PortalMe, PortalTenantRef
from app.services import auth_service as identity_service
from app.services.candidates import portal_port as candidates
from app.services.pipeline import portal_port as pipeline
from app.services.portal import careers
from app.services.portal.common import CANDIDATE_SIGN_IN, CandidatePrincipal, email_hash, ip_hash, now
from app.services.portal.projection import asset_url

MIN_RESPONSE_SECONDS = 0.4
EMAIL_LIMIT = 5
IP_LIMIT = 20
PRINCIPAL_STATE_ATTR = "tos_candidate_principal"


def portal_verify_url(token: str) -> str:
    return f"{get_settings().frontend_url.rstrip('/')}/portal/verify#t={token}"


def _idle() -> timedelta:
    return timedelta(minutes=get_settings().candidate_session_idle_minutes)


def _absolute() -> timedelta:
    return timedelta(hours=get_settings().candidate_session_absolute_hours)


# ================================================================ links
async def issue_sign_in_link(
    db: object, tenant_id: uuid.UUID, candidate_id: uuid.UUID, ttl: timedelta, *, revoke_open: bool = True
) -> str:
    """Issue a single-use ``candidate_sign_in`` link in ``db`` (tenant session); returns the raw token."""
    if revoke_open:
        await secure_links.revoke_for_subject(db, "candidate", candidate_id, CANDIDATE_SIGN_IN)  # type: ignore[arg-type]
    _, token = await secure_links.issue(
        db,  # type: ignore[arg-type]
        tenant_id=tenant_id,
        purpose=CANDIDATE_SIGN_IN,
        subject_type="candidate",
        subject_id=candidate_id,
        ttl=ttl,
        max_uses=1,
    )
    return token


async def request_link(
    data: CandidateLinkRequest, background: BackgroundTasks, *, ip: str | None
) -> PublicStatus:
    started = time.monotonic()
    await rate_limit.enforce("candidate_link_ip", ip_hash(ip), IP_LIMIT, 3600)
    if get_settings().bot_challenge_provider != "none":
        await bot_challenge.verify_or_fail(data.challenge_token, ip)
    background.add_task(
        _lookup_and_enqueue, data.tenant_slug.strip().lower(), str(data.email).strip().lower()
    )
    elapsed = time.monotonic() - started
    if elapsed < MIN_RESPONSE_SECONDS:
        await asyncio.sleep(MIN_RESPONSE_SECONDS - elapsed)
    return PublicStatus(status="check_email")


async def _email_allowed(slug: str, email: str) -> bool:
    if not get_settings().rate_limit_enabled:
        return True
    result = await rate_limit.check("candidate_link_email", email_hash(email, slug), EMAIL_LIMIT, 3600)
    return result.allowed


async def _lookup_and_enqueue(slug: str, email: str) -> None:
    """After the response: find the candidate, audit (email hash only) and enqueue the email task."""
    try:
        await _lookup(slug, email)
    except Exception:
        logger.exception("Candidate sign-in link request failed")


async def _lookup(slug: str, email: str) -> None:
    ehash = email_hash(email)
    tenant = await careers.tenant_by_slug(slug)
    if tenant is None:
        async with platform_session() as pdb:
            audit.record(
                "candidate_auth.link_requested", "candidate", None, session=pdb, actor_type="system", actor_id=None,
                email_hash=ehash, outcome="unknown_tenant",
            )  # fmt: skip
        return
    allowed = await _email_allowed(slug, email)
    chosen: uuid.UUID | None = None
    async with tenant_session(tenant.id) as db:
        audit.set_actor(db, "system", None)
        outcome = "throttled"
        if allowed:
            found = await candidates.candidates_for_email(db, email)
            if not found:
                outcome = "unknown_email"
            else:
                chosen = await _choose(db, found)
                outcome = "issued"
                if len(found) > 1:
                    audit.record(
                        "candidate_auth.ambiguous_email", "candidate", chosen, session=db,
                        email_hash=ehash, candidates=len(found),
                    )  # fmt: skip
        audit.record(
            "candidate_auth.link_requested",
            "candidate",
            chosen,
            session=db,
            email_hash=ehash,
            outcome=outcome,
        )
    if chosen is not None:
        from app.tasks import portal as tasks
        from app.tasks.ops_runtime import enqueue

        enqueue(tasks.send_candidate_sign_in_link, str(tenant.id), str(chosen), email)


async def _choose(db: object, found: list[dict[str, object]]) -> uuid.UUID:
    """Several live candidates share the email: the one with the latest application, else the latest
    ``updated_at``."""
    best: tuple[object, object, uuid.UUID] | None = None
    for c in found:
        cid = c["id"]
        assert isinstance(cid, uuid.UUID)
        apps = await pipeline.candidate_applications(db, cid)  # type: ignore[arg-type]
        latest = max((a["applied_at"] for a in apps), default=None)
        key = (latest is not None, latest or c["updated_at"], cid)
        if best is None or key[:2] > best[:2]:
            best = key
    assert best is not None
    return best[2]


async def send_sign_in_link(tenant_id: uuid.UUID, candidate_id: uuid.UUID, email: str) -> str:
    """Celery body: issue the link (revoking earlier open ones) and email it."""
    from app.core.mailer import EmailMessage, send_email

    minutes = get_settings().candidate_link_minutes
    async with tenant_session(tenant_id) as db:
        audit.set_actor(db, "system", None)
        facts = await candidates.status(db, candidate_id)
        if facts is None or facts["merged"] or facts["erased"]:
            return "skipped"
        token = await issue_sign_in_link(db, tenant_id, candidate_id, timedelta(minutes=minutes))
        site = await careers.site_for_tenant(db, tenant_id)
    brand = site.display_name if site else "Careers"
    await send_email(
        EmailMessage(
            to=email,
            subject=f"Your sign-in link for {brand}",
            text=(
                f"Hello,\n\nUse this link to sign in to the {brand} candidate portal:\n{portal_verify_url(token)}\n\n"
                f"The link works once and expires in {minutes} minutes. If you did not ask for it, you can ignore "
                f"this email.\n\n{brand}\n"
            ),
            tenant_id=tenant_id,
            kind="candidate_sign_in",
            from_name=brand,
        )
    )
    return "sent"


# ================================================================ verify / sessions
async def verify(token: str, response: Response, *, ip: str | None, user_agent: str | None) -> PortalMe:
    link = await secure_links.resolve(token, (CANDIDATE_SIGN_IN,))
    async with tenant_session(link.tenant_id) as db:
        audit.set_actor(db, "candidate", link.subject_id)
        await secure_links.consume(db, link.id)
        facts = await candidates.status(db, link.subject_id)
        if facts is None or facts["merged"] or facts["erased"]:
            raise NotFound("Link not found", code="link_invalid")
        issued = await identity_service.create_session(
            db,
            principal_type="candidate",
            principal_id=link.subject_id,
            tenant_id=link.tenant_id,
            ip=ip,
            user_agent=user_agent,
            absolute_timeout=_absolute(),
        )
        audit.record("candidate_auth.signed_in", "session", issued.session.id, session=db)
        emit(
            db,
            CandidateSignedIn(
                tenant_id=link.tenant_id, candidate_id=link.subject_id, session_id=issued.session.id
            ),
        )
        principal = CandidatePrincipal(
            candidate_id=link.subject_id, tenant_id=link.tenant_id, session_id=issued.session.id
        )
        me = await _me(db, principal, facts["full_name"])
    set_session_cookie(response, CANDIDATE_COOKIE, issued.token, issued.max_age)
    set_csrf_cookie(response, csrf_token_for(issued.token), issued.max_age)
    return me


def _rejected() -> Unauthorized:
    exc = Unauthorized("Session expired or revoked", code="session_expired")
    exc.response_hooks.append(lambda response: clear_auth_cookies(response, CANDIDATE_COOKIE))
    return exc


async def authenticate_candidate(request: Request) -> CandidatePrincipal:
    """``tos_candidate`` only. 401 without it (a user's ``tos_session`` never counts); 401 + cookies cleared
    when the session is unknown / idle / past its absolute limit, the tenant is not active, or the candidate
    was merged or erased; 403 ``csrf_failed`` on a mutation without the CSRF header."""
    cached = getattr(request.state, PRINCIPAL_STATE_ATTR, None)
    if isinstance(cached, CandidatePrincipal):
        return cached
    token = request.cookies.get(CANDIDATE_COOKIE)
    if not token:
        raise Unauthorized()
    at = now()
    async with platform_session() as pdb:
        row = await identity_service.find_session(pdb, "candidate", token)
        live = (
            row is not None
            and row.tenant_id is not None
            and identity_service.session_is_live(row, at, _idle())
            and at - row.created_at <= _absolute()
        )
        session_id = row.id if row is not None else None
        tenant_id = row.tenant_id if row is not None else None
        principal_id = row.principal_id if row is not None else None
        last_seen = row.last_seen_at if row is not None else None
    if not live or tenant_id is None or principal_id is None or session_id is None:
        raise _rejected()
    if await identity_service.active_tenant(tenant_id) is None:
        raise _rejected()
    async with tenant_session(tenant_id) as db:
        facts = await candidates.status(db, principal_id)
        if facts is None or facts["merged"] or facts["erased"]:
            await identity_service.revoke_session(db, session_id)
            facts = None
        else:
            verify_csrf(request)
            if last_seen is not None and at - last_seen >= identity_service.LAST_SEEN_RESOLUTION:
                await db.execute(
                    update(AuthSession).where(AuthSession.id == session_id).values(last_seen_at=at)
                )
    if facts is None:
        raise _rejected()
    principal = CandidatePrincipal(candidate_id=principal_id, tenant_id=tenant_id, session_id=session_id)
    update_context(principal=principal.core_principal(), tenant_id=tenant_id)
    setattr(request.state, PRINCIPAL_STATE_ATTR, principal)
    return principal


async def logout(principal: CandidatePrincipal, response: Response) -> None:
    async with tenant_session(principal.tenant_id) as db:
        await identity_service.revoke_session(db, principal.session_id)
        audit.record(
            "candidate_auth.logout", "session", principal.session_id, session=db,
            actor_type="candidate", actor_id=principal.candidate_id,
        )  # fmt: skip
    clear_auth_cookies(response, CANDIDATE_COOKIE)


async def _me(db: object, principal: CandidatePrincipal, full_name: str) -> PortalMe:
    site = await careers.site_for_tenant(db, principal.tenant_id)  # type: ignore[arg-type]
    first = (full_name or "").strip().split(" ")[0] or "there"
    slug = site.slug if site else ""
    return PortalMe(
        candidate=PortalCandidateRef(id=principal.candidate_id, first_name=first),
        tenant=PortalTenantRef(
            slug=slug,
            display_name=site.display_name if site else "",
            branding=PortalBranding(
                logo_url=asset_url(slug, "logo") if site and site.logo_file_id else None,
                brand_color=site.brand_color if site else None,
            ),
        ),
    )


async def me(principal: CandidatePrincipal) -> PortalMe:
    async with tenant_session(principal.tenant_id) as db:
        facts = await candidates.status(db, principal.candidate_id)
        if facts is None:
            raise Unauthorized()
        return await _me(db, principal, facts["full_name"])


def request_context(principal: CandidatePrincipal, base: RequestContext) -> RequestContext:
    return principal.ctx(base)
