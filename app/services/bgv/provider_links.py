# ruff: file-ignore[line-too-long]
"""Manual-connector provider links (Epic 20 A5, AD-29): ``/api/v1/public/links/{token}/bgv-check``.

* ``issue``: single-purpose Secure Link (``bgv_provider_check``, subject ``bgv_check``, TTL
  ``provider_link_days``, one submit) emailed to the provider contact. The raw token is never logged.
* ``view`` (GET, never mutates): check type, subject label, candidate first name + last initial, the check's
  own files (from its ``document_request_keys``) and declarations.
* ``download``: only a file of *that* check (link -> check -> file), streamed through ``files_service``, audited.
  Any other file id -> 404.
* ``submit``: stores ``provider_proposal`` (actor ``secure_link``) plus evidence; the check result is **not**
  changed until a Compliance User records it.
"""

import uuid
from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit, secure_links
from app.core.actors import ActorRef
from app.core.database import tenant_session
from app.core.errors import ConflictError, NotFound
from app.models.bgv import CASE_TERMINAL, BgvCase, BgvCheck, BgvDocumentRequest, BgvEvidence, BgvProvider
from app.schemas.bgv import EvidenceOut, ProviderCheckView, ProviderSubmitIn
from app.services.bgv.common import candidate_names, now, record_event, settings_row, short_name

PURPOSE = "bgv_provider_check"


async def issue(
    db: AsyncSession, check: BgvCheck, provider: BgvProvider, created_by: uuid.UUID | None
) -> bool:
    """Issue (or re-issue) the provider link and email it; returns whether an email was sent."""
    from app.core.mailer import EmailMessage, send_email

    await secure_links.revoke_for_subject(db, "bgv_check", check.id, PURPOSE)
    settings = await settings_row(db, check.tenant_id)
    link, token = await secure_links.issue(
        db,
        tenant_id=check.tenant_id,
        purpose=PURPOSE,
        subject_type="bgv_check",
        subject_id=check.id,
        ttl=timedelta(days=settings.provider_link_days),
        max_uses=1,
        metadata={"check_id": str(check.id)},
        created_by=created_by,
    )
    check.provider_link_id = link.id
    await db.flush()
    if not provider.contact_email:
        return False
    await send_email(
        EmailMessage(
            to=str(provider.contact_email),
            subject=f"Verification request: {check.subject_label}",
            text=(
                f"Hello {provider.contact_name or provider.name},\n\nPlease complete this verification "
                f"({check.type.replace('_', ' ')} - {check.subject_label}) and submit your result here:\n"
                f"{secure_links.link_url(token)}\n\nThe link expires in {settings.provider_link_days} days."
            ),
            tenant_id=check.tenant_id,
            kind="bgv_provider_check",
        )
    )
    return True


async def _resolve(token: str) -> secure_links.ResolvedLink:
    return await secure_links.resolve(token, (PURPOSE,))


async def _check_and_case(db: AsyncSession, link: secure_links.ResolvedLink) -> tuple[BgvCheck, BgvCase]:
    check = (await db.execute(select(BgvCheck).where(BgvCheck.id == link.subject_id))).scalar_one_or_none()
    if check is None:
        raise NotFound("Link not found", code="link_invalid")
    case = (await db.execute(select(BgvCase).where(BgvCase.id == check.case_id))).scalar_one()
    if case.status in CASE_TERMINAL:
        raise NotFound("Link not found", code="link_invalid")
    return check, case


async def check_file_ids(db: AsyncSession, check: BgvCheck) -> tuple[list[uuid.UUID], dict[str, Any]]:
    """Return the minimal data set of one check: files and declarations of its own document requests."""
    reqs = (
        await db.execute(
            select(BgvDocumentRequest).where(
                BgvDocumentRequest.case_id == check.case_id,
                BgvDocumentRequest.key.in_(list(check.document_request_keys or [])),
            )
        )
    ).scalars()
    files: list[uuid.UUID] = []
    declarations: dict[str, Any] = {}
    for r in reqs:
        files.extend(r.file_ids or [])
        if r.declared:
            declarations[r.key] = r.declared
    return files, declarations


async def view(token: str) -> ProviderCheckView:
    from app.models.files import StoredFile
    from app.services import identity_bridge

    link = await _resolve(token)
    async with tenant_session(link.tenant_id) as db:
        check, case = await _check_and_case(db, link)
        file_ids, declarations = await check_file_ids(db, check)
        rows = (
            (await db.execute(select(StoredFile).where(StoredFile.id.in_(file_ids or [uuid.uuid4()]))))
            .scalars()
            .all()
        )
        names = await candidate_names(db, {case.candidate_id})
        tenant = await identity_bridge.get_tenant(db, link.tenant_id)
        return ProviderCheckView(
            tenant_name=tenant.name if tenant else None,
            check_type=check.type,
            subject_label=check.subject_label,
            candidate_name=short_name(names.get(case.candidate_id)),
            files=[
                EvidenceOut(file_id=f.id, original_name=f.original_name, uploaded_by_kind="candidate")
                for f in rows
                if f.deleted_at is None
            ],
            declarations=declarations,
            submitted=check.provider_proposal is not None,
        )


async def download(token: str, file_id: uuid.UUID) -> tuple[bytes, str, str]:
    from app.services import files_service

    link = await _resolve(token)
    async with tenant_session(link.tenant_id) as db:
        check, _case = await _check_and_case(db, link)
        file_ids, _ = await check_file_ids(db, check)
        if file_id not in file_ids:
            raise NotFound("File not found")
        row = await files_service.get_file(db, file_id)
        if (
            row is None
            or row.deleted_at is not None
            or row.scan_status not in files_service.DOWNLOADABLE_SCAN_STATUSES
        ):
            raise NotFound("File not found")
        data = await files_service.read_file(row)
        audit.record(
            "bgv.provider_file_downloaded",
            "bgv_check",
            check.id,
            session=db,
            actor_type="system",
            actor_id=None,
            file_id=str(file_id),
        )
        return data, row.original_name, row.content_type


async def submit(token: str, data: ProviderSubmitIn, uploads: list[tuple[bytes, str | None]]) -> str:
    from app.core.context import RequestContext
    from app.core.identity_docs import assert_no_full_aadhaar
    from app.services import files_service
    from app.services.bgv.common import UPLOAD_KINDS

    assert_no_full_aadhaar(data.note, data.verifier_name, data.source_contacted)
    link = await _resolve(token)
    ctx = RequestContext(tenant_id=link.tenant_id)
    async with tenant_session(link.tenant_id) as db:
        await secure_links.consume(db, link.id)
        check, case = await _check_and_case(db, link)
        check = (
            await db.execute(select(BgvCheck).where(BgvCheck.id == check.id).with_for_update())
        ).scalar_one()
        if check.status != "in_progress":
            raise ConflictError("This check is no longer awaiting a result", code="check_not_in_progress")
        file_ids: list[str] = []
        for content, name in uploads[:10]:
            stored = await files_service.store_upload(
                ctx, db, content, name, UPLOAD_KINDS, "bgv_case", case.id
            )
            db.add(
                BgvEvidence(
                    id=uuid.uuid4(),
                    tenant_id=case.tenant_id,
                    check_id=check.id,
                    candidate_id=case.candidate_id,
                    file_id=stored.id,
                    uploaded_by_kind="provider_link",
                    uploaded_at=now(),
                )
            )
            file_ids.append(str(stored.id))
        check.provider_proposal = {
            **data.model_dump(mode="json"),
            "evidence_file_ids": file_ids,
            "submitted_at": now().isoformat(),
        }
        record_event(
            db,
            case,
            "provider_proposal",
            ActorRef("secure_link", link.id),
            check_id=check.id,
            result=data.result,
        )
        await db.flush()
    return "submitted"
