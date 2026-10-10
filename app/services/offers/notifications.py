"""Offer emails and the accepted-letter finalisation (task bodies, AD-9). Never bill rate, burden or margin.

* ``released`` -- to the candidate: portal link ``{FRONTEND_URL}/portal/offers/{id}`` plus a fresh secure link
  (purpose ``offer_response``, ``max_uses=1``, expiring ``response_link_grace_hours`` after the offer); the
  letter is attached only when ``attach_letter_to_release_email`` is on.
* ``updated`` -- to the candidate after a non-approval change of a released offer (new link).
* ``withdrawn`` -- to the candidate (only for offers that had been released).
* ``declined_owner`` / ``expired_owner`` -- to the offer owner.
* ``finalize_accepted`` -- appends the acceptance certificate page to a copy of the accepted letter, stores it
  as ``accepted.pdf`` and emails it to the candidate and the owner (idempotent: ``offers.accepted_file_id``).
  Epic 22: offers accepted on the signature tables use the documents completion output (signature certificate,
  ``document_versions(source=signed)``); ``accepted_file_id`` points to that signed version's file.
* ``send_expiry_warnings`` -- one email to owner and candidate 24 h before expiry (``expiry_warned_at``).
"""

import uuid
from datetime import timedelta
from html import escape
from typing import Any

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import secure_links
from app.core.context import RequestContext
from app.core.mailer import EmailAttachment, EmailMessage, send_email
from app.core.settings import get_settings
from app.models.offers import Offer, OfferResponse, OfferVersion
from app.services.offers import letter_pdf, versions
from app.services.offers.common import (
    LINK_PURPOSE,
    OPEN_FOR_RESPONSE,
    SUBJECT_TYPE,
    candidate_facts,
    fmt_date,
    now,
    settings_row,
    tenant_facts,
    user_contact,
)
from app.services.offers.response import released_version


def _html(paragraphs: list[str], links: list[tuple[str, str]] | None = None) -> str:
    body = "".join(f"<p>{escape(p)}</p>" for p in paragraphs)
    for label, url in links or []:
        body += f'<p><a href="{escape(url, quote=True)}">{escape(label)}</a></p>'
    return f"<html><body>{body}</body></html>"


async def _letter_attachment(
    db: AsyncSession, file_id: uuid.UUID | None, filename: str
) -> EmailAttachment | None:
    from app.services import files_service

    stored = await files_service.get_file(db, file_id) if file_id else None
    if stored is None:
        return None
    return EmailAttachment(
        filename=filename, content_type="application/pdf", data=await files_service.read_file(stored)
    )


async def _issue_link(db: AsyncSession, offer: Offer) -> str:
    settings = await settings_row(db)
    await secure_links.revoke_for_subject(db, SUBJECT_TYPE, offer.id, LINK_PURPOSE)
    expires = (offer.expires_at or now()) + timedelta(hours=settings.response_link_grace_hours)
    _, token = await secure_links.issue(
        db,
        tenant_id=offer.tenant_id,
        purpose=LINK_PURPOSE,
        subject_type=SUBJECT_TYPE,
        subject_id=offer.id,
        ttl=max(expires - now(), timedelta(minutes=5)),
        max_uses=1,
        metadata={"offer_id": str(offer.id)},
        created_by=offer.owner_id,
    )
    return secure_links.link_url(token)


def portal_url(offer_id: uuid.UUID) -> str:
    return f"{get_settings().frontend_url.rstrip('/')}/portal/offers/{offer_id}"


async def _candidate_email(
    ctx: RequestContext, db: AsyncSession, offer: Offer, kind: str, *, with_link: bool
) -> bool:
    del ctx
    candidate = await candidate_facts(db, offer.candidate_id)
    if not candidate.get("email"):
        logger.warning("Offer {}: candidate has no email ({} not sent)", offer.id, kind)
        return False
    tenant = await tenant_facts(db, offer.tenant_id)
    version = await released_version(db, offer)
    settings = await settings_row(db)
    greeting = f"Dear {candidate.get('first_name') or candidate.get('full_name') or 'candidate'},"
    links: list[tuple[str, str]] = []
    if with_link:
        await versions.ensure_letter(RequestContext(tenant_id=offer.tenant_id), db, offer, version)
        links = [
            ("Review and respond to your offer", await _issue_link(db, offer)),
            ("Open in the candidate portal", portal_url(offer.id)),
        ]
    if kind == "released":
        subject = f"Your offer from {tenant['name']}: {version.title}"
        lines = [
            greeting,
            f"{tenant['name']} is pleased to offer you the position of {version.title}.",
            f"Please review the offer and respond by {fmt_date(version.expiry_date)}.",
        ]
    elif kind == "updated":
        subject = f"Your offer was updated: {version.title}"
        lines = [
            greeting,
            f"{tenant['name']} updated your offer for {version.title} (version {version.version_no}).",
            f"Please review the updated offer and respond by {fmt_date(version.expiry_date)}.",
        ]
    elif kind == "withdrawn":
        subject = f"Update on your offer from {tenant['name']}"
        lines = [greeting, f"{tenant['name']} has withdrawn the offer for {version.title}."]
    elif kind == "expiry_warning":
        subject = f"Reminder: your offer for {version.title} expires soon"
        lines = [greeting, f"Your offer for {version.title} expires on {fmt_date(version.expiry_date)}."]
        links = [("Open in the candidate portal", portal_url(offer.id))]
    else:  # pragma: no cover - guarded by callers
        return False
    lines.append(tenant["name"])
    attachments: tuple[EmailAttachment, ...] = ()
    if kind in ("released", "updated") and settings.attach_letter_to_release_email:
        await db.refresh(version)
        att = await _letter_attachment(
            db, version.letter_file_id, f"{offer.human_id}-v{version.version_no}.pdf"
        )
        attachments = (att,) if att else ()
    text = "\n\n".join(lines + [f"{label}: {url}" for label, url in links])
    await db.flush()
    await send_email(
        EmailMessage(
            to=str(candidate["email"]),
            subject=subject,
            text=text,
            html=_html(lines, links),
            tenant_id=offer.tenant_id,
            kind=f"offer_{kind}",
            attachments=attachments,
        )
    )
    return True


async def _owner_email(db: AsyncSession, offer: Offer, kind: str) -> bool:
    name, email = await user_contact(db, offer.owner_id)
    if not email:
        return False
    tenant = await tenant_facts(db, offer.tenant_id)
    candidate = await candidate_facts(db, offer.candidate_id)
    who = candidate.get("full_name") or "The candidate"
    url = f"{get_settings().frontend_url.rstrip('/')}/offers/{offer.id}"
    if kind == "declined_owner":
        subject = f"Offer {offer.human_id} declined"
        lines = [f"Hello {name or ''},".strip(), f"{who} declined offer {offer.human_id}."]
    elif kind == "expired_owner":
        subject = f"Offer {offer.human_id} expired"
        lines = [
            f"Hello {name or ''},".strip(),
            f"Offer {offer.human_id} for {who} expired without a response.",
        ]
    elif kind == "expiry_warning":
        subject = f"Offer {offer.human_id} expires in 24 hours"
        lines = [f"Hello {name or ''},".strip(), f"Offer {offer.human_id} for {who} expires within 24 hours."]
    else:  # pragma: no cover
        return False
    lines.append(tenant["name"])
    await send_email(
        EmailMessage(
            to=email,
            subject=subject,
            text="\n\n".join([*lines, f"Open offer: {url}"]),
            html=_html(lines, [("Open offer", url)]),
            tenant_id=offer.tenant_id,
            kind=f"offer_{kind}",
        )
    )
    return True


async def send_offer_email(ctx: RequestContext, db: AsyncSession, offer_id: uuid.UUID, kind: str) -> bool:
    """Task body for ``OfferEmailRequested``. Skips when the offer moved on in the meantime."""
    offer = (
        await db.execute(select(Offer).where(Offer.id == offer_id).with_for_update())
    ).scalar_one_or_none()
    if offer is None:
        return False
    if kind in ("released", "updated"):
        if offer.status not in OPEN_FOR_RESPONSE:
            return False
        return await _candidate_email(ctx, db, offer, kind, with_link=True)
    if kind == "withdrawn":
        if offer.status != "withdrawn":
            return False
        return await _candidate_email(ctx, db, offer, kind, with_link=False)
    if kind in ("declined_owner", "expired_owner"):
        return await _owner_email(db, offer, kind)
    logger.warning("Unknown offer email kind {}", kind)
    return False


async def render_letter(ctx: RequestContext, db: AsyncSession, offer_id: uuid.UUID, version_no: int) -> bool:
    """Task body of ``render_offer_letter`` (idempotent; frozen versions with a letter are skipped)."""
    offer = await db.get(Offer, offer_id)
    if offer is None:
        return False
    version = await versions.by_no(db, offer_id, version_no)
    if version is None:
        return False
    return await versions.ensure_letter(ctx, db, offer, version)


async def finalize_accepted(ctx: RequestContext, db: AsyncSession, offer_id: uuid.UUID) -> bool:
    from app.services import files_service

    offer = (
        await db.execute(select(Offer).where(Offer.id == offer_id).with_for_update())
    ).scalar_one_or_none()
    if offer is None or offer.status != "accepted" or offer.accepted_file_id is not None:
        return False
    response = (
        await db.execute(
            select(OfferResponse).where(
                OfferResponse.offer_id == offer.id, OfferResponse.decision == "accepted"
            )
        )
    ).scalar_one_or_none()
    if response is None:
        return False
    version = await db.get(OfferVersion, response.version_id)
    if version is None:
        return False
    await versions.ensure_letter(ctx, db, offer, version)
    letter = await files_service.get_file(db, version.letter_file_id) if version.letter_file_id else None
    if letter is None:
        logger.error("Offer {}: accepted version has no letter", offer.id)
        return False
    tenant = await tenant_facts(db, offer.tenant_id)
    if offer.signature_request_id is not None:
        from app.services.documents import esign

        final_file_id = await esign.complete_request(ctx, db, offer.signature_request_id)
        final = await files_service.get_file(db, final_file_id) if final_file_id else None
        if final is None:
            logger.error("Offer {}: signature request not completed", offer.id)
            return False
        offer.accepted_file_id = final.id
        await db.flush()
        await _send_accepted(db, offer, version, response, tenant, await files_service.read_file(final))
        return True
    certificate = letter_pdf.acceptance_certificate(
        tenant_name=tenant["name"],
        offer_ref=offer.human_id,
        version_no=response.version_no,
        typed_name=response.typed_name or "",
        decided_at=response.decided_at,
        ip=str(response.ip) if response.ip is not None else None,
        letter_sha256=response.letter_sha256,
        channel=response.channel,
    )
    signed = letter_pdf.append_pages(await files_service.read_file(letter), certificate)
    task_ctx = ctx if ctx.tenant_id is not None else RequestContext(tenant_id=offer.tenant_id)
    stored = await files_service.store_upload(
        task_ctx, db, signed, "accepted.pdf", {"pdf"}, "offer", offer.id
    )
    offer.accepted_file_id = stored.id
    await db.flush()
    await _send_accepted(db, offer, version, response, tenant, signed)
    return True


async def _send_accepted(
    db: AsyncSession,
    offer: Offer,
    version: OfferVersion,
    response: OfferResponse,
    tenant: dict[str, Any],
    signed: bytes,
) -> None:
    attachment = EmailAttachment(
        filename=f"{offer.human_id}-accepted.pdf", content_type="application/pdf", data=signed
    )
    candidate = await candidate_facts(db, offer.candidate_id)
    owner_name, owner_email = await user_contact(db, offer.owner_id)
    lines = [
        f"The offer {offer.human_id} ({version.title}) was accepted on "
        f"{response.decided_at:%Y-%m-%d %H:%M} UTC. The signed copy is attached.",
        tenant["name"],
    ]
    for to in (candidate.get("email"), owner_email):
        if not to:
            continue
        await send_email(
            EmailMessage(
                to=str(to),
                subject=f"Signed offer {offer.human_id}",
                text="\n\n".join(lines),
                html=_html(lines),
                tenant_id=offer.tenant_id,
                kind="offer_accepted",
                attachments=(attachment,),
            )
        )
    del owner_name


async def send_expiry_warnings(ctx: RequestContext, db: AsyncSession) -> int:
    """Claim released / viewed offers expiring within 24 h (``FOR UPDATE SKIP LOCKED``) and warn once."""
    at = now()
    rows = list(
        (
            await db.execute(
                select(Offer)
                .where(
                    Offer.status.in_(OPEN_FOR_RESPONSE),
                    Offer.expiry_warned_at.is_(None),
                    Offer.expires_at > at,
                    Offer.expires_at <= at + timedelta(hours=24),
                )
                .with_for_update(skip_locked=True)
            )
        ).scalars()
    )
    for offer in rows:
        offer.expiry_warned_at = at
        await db.flush()
        await _owner_email(db, offer, "expiry_warning")
        await _candidate_email(ctx, db, offer, "expiry_warning", with_link=False)
    return len(rows)
