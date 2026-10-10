"""Generic Secure Link approvals for Client Users (Epic 18 A4; UJ-10 edge case, NFR-17).

* When an approval step activates for a Client User (``approver_kind='client_user'``), ``issue_for_step`` issues a
  ``client_approval`` link (subject ``approval_request``, 14 days, single use, addressee in the metadata) and
  queues the email after commit. Viewing never decides.
* ``decide`` consumes the link (racing callers: one wins, the other 410 ``link_used``) and calls the engine with
  ``ActorRef(kind='secure_link', id=<link id>, attributed_to=('client_user', addressee))``. The decision evidence
  (addressee email, IP hash, time) is written to the audit log.
* Any decision on a request (portal, subject endpoint, link, offline) revokes its other open ``client_approval``
  links (``revoke_for_request``, after ``ApprovalCompleted`` / step change).
* Epic 50 (FR-252): when the addressee's Client turned "Allow approval links by email" off, view and decide
  answer 403 ``sso_required`` with the portal sign-in URL (``sso_service.assert_approval_link_allowed``).
"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit, secure_links
from app.core.actors import ActorRef
from app.core.context import RequestContext
from app.core.database import tenant_session
from app.core.errors import NotFound, ValidationFailed
from app.models.approvals import ApprovalRequest
from app.models.external_auth import ClientUser
from app.schemas.approvals import DecideIn

PURPOSE = "client_approval"
SUBJECT = "approval_request"
TTL = timedelta(days=14)
SUBJECT_ENDPOINT_KINDS = frozenset({"timesheet"})


async def issue_for_step(
    tenant_id: uuid.UUID, request_id: uuid.UUID, client_user_id: uuid.UUID
) -> str | None:
    """Issue the link + queue the email (idempotent per request/addressee/step). Returns the raw token."""
    from app.services import comms_service, identity_bridge
    from app.services.notifications_service import render_email

    async with tenant_session(tenant_id) as db:
        request = await db.get(ApprovalRequest, request_id)
        user = (
            await db.execute(select(ClientUser).where(ClientUser.id == client_user_id))
        ).scalar_one_or_none()
        if request is None or request.status != "pending" or user is None or user.status == "revoked":
            return None
        if request.current_approver_client_user_id != client_user_id:
            return None
        position = int(request.current_position or 0)
        for link in await secure_links.links_for_subject(db, SUBJECT, request_id):
            meta = link.meta or {}
            if (
                link.purpose == PURPOSE
                and link.revoked_at is None
                and meta.get("addressee_id") == str(client_user_id)
                and int(meta.get("position") or 0) == position
            ):
                return None
        _link, token = await secure_links.issue(
            db,
            tenant_id=tenant_id,
            purpose=PURPOSE,
            subject_type=SUBJECT,
            subject_id=request_id,
            ttl=TTL,
            max_uses=1,
            metadata={
                "addressee_kind": "client_user",
                "addressee_id": str(client_user_id),
                "position": position,
            },
        )
        tenant = await identity_bridge.get_tenant(db, tenant_id)
        html = render_email(
            "notification.html.j2",
            recipient_name=(user.display_name or "").split(" ")[0] or user.display_name,
            title="An item is awaiting your approval",
            body="Review and approve or reject it from the link below. The link works once and expires in 14 days.",
            link=secure_links.link_url(token),
            tenant_name=tenant.name if tenant else "Talent OS",
        )
        await comms_service.queue_email(
            RequestContext(tenant_id=tenant_id),
            db,
            origin="system",
            to=[comms_service.RecipientSpec("to", str(user.email).lower())],
            subject="An item is awaiting your approval",
            body_html=html,
            raw=True,
        )
        return token


async def revoke_for_request(tenant_id: uuid.UUID, request_id: uuid.UUID) -> int:
    async with tenant_session(tenant_id) as db:
        return await secure_links.revoke_for_subject(db, SUBJECT, request_id, PURPOSE)


async def _request_for(db: AsyncSession, link: secure_links.ResolvedLink) -> ApprovalRequest:
    if link.subject_type != SUBJECT:
        raise NotFound("Link not found", code="link_invalid")
    request = await db.get(ApprovalRequest, link.subject_id)
    if request is None:
        raise NotFound("Link not found", code="link_invalid")
    return request


async def _assert_links_allowed(db: AsyncSession, link: secure_links.ResolvedLink) -> None:
    from app.services import sso_service

    raw = (link.meta or {}).get("addressee_id")
    if raw:
        await sso_service.assert_approval_link_allowed(db, uuid.UUID(str(raw)))


async def view(token: str) -> dict[str, Any]:
    """GET: the kind's summary; never decides."""
    from app.services import identity_bridge
    from app.services.client_portal.approvals import summary_for_link

    link = await secure_links.resolve(token, (PURPOSE,))
    async with tenant_session(link.tenant_id) as db:
        await _assert_links_allowed(db, link)
        request = await _request_for(db, link)
        ctx = RequestContext(tenant_id=link.tenant_id)
        title, subtitle, fields = await summary_for_link(ctx, db, request)
        tenant = await identity_bridge.get_tenant(db, link.tenant_id)
        return {
            "kind": request.subject_type,
            "status": "open" if request.status == "pending" else request.status,
            "title": title,
            "subtitle": subtitle,
            "fields": [{"label": k, "value": v} for k, v in fields],
            "tenant_name": tenant.name if tenant else None,
            "expires_at": link.expires_at,
        }


async def decide(token: str, decision: str, comment: str | None, *, ip_hash: str | None) -> str:
    from app.services import approvals as approvals_service

    link = await secure_links.resolve(token, (PURPOSE,))
    text = (comment or "").strip() or None
    if decision == "reject" and text is None:
        raise ValidationFailed("A comment is required", code="comment_required")
    addressee = uuid.UUID(str(link.meta.get("addressee_id")))
    async with tenant_session(link.tenant_id) as db:
        await _assert_links_allowed(db, link)
        audit.set_actor(db, "secure_link", link.id)
        await secure_links.consume(db, link.id)
        request = await _request_for(db, link)
        email = (
            await db.execute(select(ClientUser.email).where(ClientUser.id == addressee))
        ).scalar_one_or_none()
        ctx = RequestContext(tenant_id=link.tenant_id)
        await approvals_service.decide(
            ctx,
            db,
            request.id,
            DecideIn(action=decision, comment=text),  # type: ignore[arg-type]
            actor=ActorRef("secure_link", link.id, attributed_to=("client_user", addressee)),
        )
        audit.record(
            "approval.secure_link_decided",
            SUBJECT,
            request.id,
            session=db,
            actor_type="secure_link",
            actor_id=link.id,
            decision=decision,
            addressee_kind="client_user",
            addressee_id=str(addressee),
            addressee_email=str(email).lower() if email else None,
            ip_hash=ip_hash,
            at=datetime.now(UTC).isoformat(),
        )
        await secure_links.revoke_for_subject(db, SUBJECT, request.id, PURPOSE)
    logger.info("Client approval link {} decided {}", link.id, decision)
    return "approved" if decision == "approve" else "rejected"
