# ruff: noqa: E501
"""Public upload page for counsel and Candidates without the Worker portal (Epic 46 A3; Secure Link purpose
``immigration_upload``). No session: the token is the credential and is never logged.

* ``view`` shows the case reference (human id only), the requested items covered by the link and the
  ``immigration`` notice; it never mutates (viewing does not consume a use).
* ``upload`` needs the notice to be acknowledged (``consent=true``; for a Candidate link the consent is recorded
  with source ``recipient_link``), consumes one use (410 once ``max_uses`` is reached or after expiry) and stores
  the file as a P2 document. An item not covered by the link is a 404.
"""

import uuid

from sqlalchemy import select

from app.core import secure_links
from app.core.database import tenant_session
from app.core.errors import NotFound
from app.models.immigration import CaseDocumentRequest, ImmigrationCase
from app.schemas.immigration_portal import PublicUploadItemOut, PublicUploadOut, PublicUploadViewOut
from app.services.immigration.common import CONSENT_PURPOSE, LINK_PURPOSE, conflict, invalid

PURPOSES = (LINK_PURPOSE,)


def _request_ids(link: secure_links.ResolvedLink) -> list[uuid.UUID]:
    out: list[uuid.UUID] = []
    for raw in link.meta.get("request_ids") or []:
        try:
            out.append(uuid.UUID(str(raw)))
        except ValueError:
            continue
    return out


async def link_summary(link: secure_links.ResolvedLink) -> tuple[str, str | None]:
    """(status, tenant name) for the generic ``GET /public/links/{token}`` frame."""
    from app.services import identity_bridge

    async with tenant_session(link.tenant_id) as db:
        tenant = await identity_bridge.get_tenant(db, link.tenant_id)
    return "open", tenant.name if tenant is not None else None


async def view(token: str) -> PublicUploadViewOut:
    from app.services import identity_bridge
    from app.services.immigration.portal import notice

    link = await secure_links.resolve(token, PURPOSES)
    ids = _request_ids(link)
    async with tenant_session(link.tenant_id) as db:
        case = await db.get(ImmigrationCase, link.subject_id)
        if case is None:
            raise NotFound("Link not found", code="link_invalid")
        rows = list(
            (
                await db.execute(
                    select(CaseDocumentRequest)
                    .where(CaseDocumentRequest.id.in_(ids), CaseDocumentRequest.case_id == case.id)
                    .order_by(CaseDocumentRequest.label)
                )
            ).scalars()
        )
        n = await notice(db)
        tenant = await identity_bridge.get_tenant(db, link.tenant_id)
        return PublicUploadViewOut(
            reference=case.human_id,
            tenant_name=tenant.name if tenant is not None else None,
            expires_at=link.expires_at,
            uses_left=max(0, link.max_uses - link.use_count),
            items=[
                PublicUploadItemOut(
                    id=r.id,
                    label=r.label,
                    status=r.status,
                    can_upload=r.status in ("requested", "rejected", "uploaded"),
                )
                for r in rows
            ],
            notice_title=n.title,
            notice_body_md=n.body_md,
            consent_required=True,
        )


async def upload(
    token: str, item_id: uuid.UUID, consent: bool, data: bytes, filename: str | None
) -> PublicUploadOut:
    from app.services.immigration import checklist

    link = await secure_links.resolve(token, PURPOSES)
    if item_id not in _request_ids(link):
        raise NotFound("Item not found")
    if not consent:
        raise invalid("consent_required", "Read and accept the notice before uploading")
    async with tenant_session(link.tenant_id) as db:
        req = await db.get(CaseDocumentRequest, item_id)
        if req is None or req.case_id != link.subject_id:
            raise NotFound("Item not found")
        if req.status not in ("requested", "rejected", "uploaded"):
            raise conflict("request_closed", "This item is already complete")
        await secure_links.consume(db, link.id)
        if link.meta.get("audience") == "candidate":
            from app.services.privacy import consent as consent_svc

            await consent_svc.record(db, req.candidate_id, CONSENT_PURPOSE, "recipient_link", None, None)
        await checklist.store_upload(
            db,
            req,
            data,
            filename,
            actor={"kind": "secure_link", "id": str(link.id), "audience": link.meta.get("audience")},
            via="secure_link",
        )
        return PublicUploadOut(status="uploaded", item_id=req.id)
