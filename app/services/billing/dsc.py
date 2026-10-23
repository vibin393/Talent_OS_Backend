# ruff: file-ignore[line-too-long, non-imperative-mood, unused-function-argument]
"""DSC-signed invoice / credit-note PDFs (Epic 51, Story 51.1, FR-253; AD-21, AD-29, AD-49).

Flow (``InvoiceIssued`` / ``CreditNoteIssued`` after commit -> task ``finance_p3.dsc_sign``):

1. The ``tos_dsc_mark`` trigger (315_04) runs INSIDE the issue transaction (with number assignment): a required policy
   sets ``dsc_status='pending'`` so no delivery path can ever see an unsigned-but-required document as deliverable.
2. ``sign_document`` (task): resolve the policy; render ONCE (``billing.pdf.render``), store it as the P2 document
   version 1 (``generated``, ``access_level='finance'``, retention ``financial``) and set ``pdf_file_id``; call the
   ``dsc`` connector port. ``signed`` -> verify (``dsc_verify``) and append version 2 (``signed``);
   ``ManualActionRequired`` / ``pending_manual`` -> stays ``pending`` and Finance Admins get ``dsc_signing_pending``;
   an exception -> 3 attempts with backoff, then ``failed`` + notification. Idempotent by ``(kind, doc_id)``.
3. Manual upload (``upload_signed``): accepted only when the upload is an incremental update over the stored
   render and a PAdES signature verifies; the signed file is a NEW version, the stored render is never replaced.

Only allow-listed operational columns change on the issued document (``pdf_file_id``, ``signed_pdf_file_id``,
``dsc_status``, ``dsc_signed_at``); amounts, lines and ``content_hash`` never change. Public API used by P2
``billing/portal.py``: ``delivery_blocked`` and ``delivered_pdf``. No PDF bytes or certificate material are logged.
"""

import asyncio
import dataclasses
import uuid
from datetime import UTC, datetime
from typing import Any, cast

from loguru import logger
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.context import RequestContext
from app.core.errors import ConflictError, NotFound, ValidationFailed
from app.core.events import emit
from app.events.billing import DocumentDscSigned
from app.models.billing import CreditNote, DscPolicy, DscSignRequest, Invoice
from app.schemas.dsc import (
    DscPendingOut,
    DscPolicyIn,
    DscPolicyOut,
    DscPolicyPatch,
    DscStatusOut,
    DscUploadOut,
    DscVerificationOut,
)
from app.services.billing import dsc_verify
from app.services.billing.common import get_doc, tenant_of, user_id

KINDS = ("invoice", "credit_note")
MODEL = {"invoice": Invoice, "credit_note": CreditNote}
PATH_KIND = {"invoices": "invoice", "credit-notes": "credit_note"}
BLOCKING = ("pending", "failed")
MAX_ATTEMPTS = 3
RETRY_DELAYS: tuple[float, ...] = (0.0, 5.0, 30.0)  # seconds before attempt 1, 2, 3 (patched to 0 in tests)
NIL = uuid.UUID(int=0)
NOTIFY_KEY = "dsc_signing_pending"
_MARKED: dict[tuple[str, str, str], None] = {}


# ---------------------------------------------------------------- private after-commit events (not catalogue)
@dataclasses.dataclass(frozen=True, slots=True)
class DscSignQueued:
    """Re-run the signing task after the retry request commits."""

    tenant_id: uuid.UUID
    kind: str
    doc_id: uuid.UUID


@dataclasses.dataclass(frozen=True, slots=True)
class DscSigningPending:
    """Payload of the ``dsc_signing_pending`` notification (ids, number and age only)."""

    tenant_id: uuid.UUID
    kind: str
    doc_id: uuid.UUID
    number: str
    age_hours: int
    status: str


# ---------------------------------------------------------------- helpers
def _now() -> datetime:
    return datetime.now(UTC)


def _check_kind(kind: str) -> str:
    if kind not in KINDS:
        raise NotFound("Document not found")
    return kind


def kind_from_path(segment: str) -> str:
    kind = PATH_KIND.get(segment)
    if kind is None:
        raise NotFound("Document not found")
    return kind


async def resolve_policy(
    db: AsyncSession, gst_registration_id: uuid.UUID | None, client_id: uuid.UUID
) -> DscPolicy | None:
    """Exact Client row, else the NULL-Client row of the registration, else no policy (active rows only)."""
    if gst_registration_id is None:
        return None
    rows = list(
        (
            await db.execute(
                select(DscPolicy).where(
                    DscPolicy.gst_registration_id == gst_registration_id,
                    DscPolicy.active.is_(True),
                    (DscPolicy.client_id == client_id) | DscPolicy.client_id.is_(None),
                )
            )
        ).scalars()
    )
    exact = next((r for r in rows if r.client_id == client_id), None)
    return exact or next((r for r in rows if r.client_id is None), None)


async def _required(db: AsyncSession, doc: Any) -> DscPolicy | None:
    policy = await resolve_policy(db, doc.supplier_gst_registration_id, doc.client_id)
    return policy if policy is not None and policy.required else None


async def _request(
    db: AsyncSession, kind: str, doc_id: uuid.UUID, *, lock: bool = False
) -> DscSignRequest | None:
    stmt = select(DscSignRequest).where(DscSignRequest.kind == kind, DscSignRequest.doc_id == doc_id)
    if lock:
        stmt = stmt.with_for_update()
    return (await db.execute(stmt)).scalar_one_or_none()


# ---------------------------------------------------------------- issue-time marker + delivery gate (public)
def note_marked(kind: str, doc: Any) -> None:
    """Called by the P2 issue transactions after the content-hash refresh: the ``tos_dsc_mark`` trigger (315_04)
    already set ``dsc_status='pending'`` when a required policy applies, in the statement that assigned the
    number, so delivery waits for the signature from the very first moment. Remember it (process-local) so the
    after-commit hook queues signing only for these documents -- no extra query inside the issue transaction."""
    if kind not in KINDS or getattr(doc, "dsc_status", None) != "pending":
        return
    _MARKED[str(doc.tenant_id), kind, str(doc.id)] = None
    while len(_MARKED) > 10_000:  # bounded: entries of rolled-back issues are simply dropped
        _MARKED.pop(next(iter(_MARKED)))


def take_marked(tenant_id: uuid.UUID, kind: str, doc_id: uuid.UUID) -> bool:
    """After commit: was this document marked ``pending`` by its issue transaction (in this process)? Only those
    need the signing task, so documents without a DSC policy cost nothing after issue."""
    return _MARKED.pop((str(tenant_id), kind, str(doc_id)), "absent") is None


def delivery_blocked_sync(kind: str, doc: Any) -> bool:
    return kind in KINDS and getattr(doc, "dsc_status", "not_required") in BLOCKING


async def delivery_blocked(db: AsyncSession, kind: str, doc: Any) -> bool:
    """Policy required and not yet signed (``pending`` / ``failed``). Works in internal and Client Portal sessions
    (reads only the document's own operational column)."""
    return delivery_blocked_sync(kind, doc)


async def delivered_pdf(ctx: RequestContext, db: AsyncSession, kind: str, doc: Any) -> tuple[bytes, str]:
    """The bytes to deliver: the signed file when signed, else the stored render when present, else the existing
    live render. Never call while ``delivery_blocked`` (callers refuse first)."""
    from app.services import files_service
    from app.services.billing import pdf

    filename = f"{(doc.number or 'draft-' + str(doc.id)[:8]).replace('/', '-')}.pdf"
    for file_id, suffix in ((getattr(doc, "signed_pdf_file_id", None), "-signed"), (doc.pdf_file_id, "")):
        if file_id is None:
            continue
        f = await files_service.get_file(db, file_id)
        if f is not None and f.deleted_at is None:
            return await files_service.read_file(f), filename.replace(".pdf", f"{suffix}.pdf")
    return await pdf.render(ctx, db, kind, doc.id)


def dsc_pending_error() -> ConflictError:
    return ConflictError("This document is waiting for its digital signature (DSC)", code="dsc_pending")


# ---------------------------------------------------------------- adapter
async def _adapter(
    ctx: RequestContext, db: AsyncSession, policy: DscPolicy, legal_entity_id: uuid.UUID
) -> Any:
    from app.models.connectors import ProviderConnectorConfig
    from app.services.connectors import registry

    if policy.provider_config_id is not None:
        row = (
            await db.execute(
                select(ProviderConnectorConfig).where(
                    ProviderConnectorConfig.id == policy.provider_config_id,
                    ProviderConnectorConfig.category == "dsc",
                    ProviderConnectorConfig.status == "active",
                )
            )
        ).scalar_one_or_none()
        if row is not None:
            return registry.build("dsc", await registry.load_config(db, row))
    return await registry.get(ctx, db, "dsc", legal_entity_id)


# ---------------------------------------------------------------- render once (document version 1)
async def _store_render(
    ctx: RequestContext, db: AsyncSession, kind: str, doc: Any, req: DscSignRequest
) -> bytes:
    from app.services import files_service
    from app.services.billing import pdf
    from app.services.documents import repository

    if req.render_file_id is not None:
        f = await files_service.get_file(db, req.render_file_id)
        if f is not None:
            return await files_service.read_file(f)
    data, filename = await pdf.render(ctx, db, kind, doc.id)
    document = await repository.create_document(
        ctx,
        db,
        owner_kind=kind,
        owner_id=doc.id,
        category=kind,
        title=f"{pdf.TITLE[kind]} {doc.number or ''}".strip(),
        access_level="finance",
        retention_class="financial",
    )
    stored = await repository.store_generated_pdf(ctx, db, document.id, data, filename)
    await repository.append_version(ctx, db, document, stored, source="generated")
    req.document_id = document.id
    req.render_file_id = stored.id
    req.render_sha256 = dsc_verify.sha256(data)
    doc.pdf_file_id = stored.id
    await db.flush()
    return data


async def _accept_signed(
    ctx: RequestContext,
    db: AsyncSession,
    kind: str,
    doc: Any,
    req: DscSignRequest,
    signed: bytes,
    verification: dsc_verify.DscVerification,
    *,
    manual: bool,
    filename: str | None = None,
) -> None:
    from app.models.documents import Document
    from app.services import files_service
    from app.services.documents import repository

    document = await db.get(Document, req.document_id) if req.document_id else None
    if document is None:  # pragma: no cover - the render step always creates it
        raise ConflictError("The stored render is missing", code="dsc_render_missing")
    name = filename or f"{(doc.number or str(doc.id)[:8]).replace('/', '-')}-signed.pdf"
    if manual:
        stored = await files_service.store_upload(
            ctx, db, signed, name, {"pdf"}, repository.FILE_OWNER, document.id
        )
    else:
        stored = await repository.store_generated_pdf(ctx, db, document.id, signed, name)
    await repository.append_version(ctx, db, document, stored, source="signed")
    document.signed = True
    when = _now()
    req.status = "signed"
    req.signed_at = when
    req.signed_file_id = stored.id
    req.last_error = None
    req.signer_subject = verification.signer_subject
    req.signer_serial = verification.signer_serial
    req.signing_time = verification.signing_time
    req.verification = {**verification.to_json(), "manual": manual}
    doc.signed_pdf_file_id = stored.id
    doc.dsc_status = "signed"
    doc.dsc_signed_at = when
    await db.flush()
    audit.record(
        f"{kind}.dsc_signed",
        kind,
        doc.id,
        session=db,
        number=doc.number,
        manual=manual,
        adapter=req.adapter_key,
        signer_serial=verification.signer_serial,
        sub_filter=verification.sub_filter,
    )
    emit(db, DocumentDscSigned(tenant_id=doc.tenant_id, kind=kind, doc_id=doc.id))


def _adapter_verification(adapter_key: str, render: bytes, signed: bytes) -> dsc_verify.DscVerification:
    """A connector result gets the same checks as a manual upload. The ``fake`` test adapter (never registered in
    production) appends a marker instead of a CMS signature: only check (a) applies to it."""
    if adapter_key == "fake":
        if not dsc_verify.is_incremental_over(render, signed):
            raise dsc_verify.DscVerificationError(
                "dsc_content_mismatch", "The signed PDF is not the stored render"
            )
        return dsc_verify.DscVerification(
            intact=True,
            valid=True,
            trusted="unknown",
            sub_filter=None,
            signer_subject="Fake DSC (test adapter)",
            signer_serial=None,
            signing_time=None,
            render_sha256=dsc_verify.sha256(render),
            signed_sha256=dsc_verify.sha256(signed),
            warnings=("fake_adapter_signature_not_checked",),
        )
    return dsc_verify.verify(render, signed)


def _notify_pending(db: AsyncSession, kind: str, doc: Any, req: DscSignRequest) -> None:
    age = int(max((_now() - req.requested_at).total_seconds(), 0) // 3600) if req.requested_at else 0
    emit(
        db,
        DscSigningPending(
            tenant_id=doc.tenant_id,
            kind=kind,
            doc_id=doc.id,
            number=doc.number or "",
            age_hours=age,
            status=req.status,
        ),
    )


# ---------------------------------------------------------------- task body
async def sign_document(ctx: RequestContext, db: AsyncSession, kind: str, doc_id: uuid.UUID) -> str:
    """Idempotent by ``(kind, doc_id)``. Returns the resulting ``dsc_status``."""
    from app.services.connectors.base import ManualActionRequired
    from app.services.connectors.dsc.port import DscSignRequestDTO

    _check_kind(kind)
    doc = await get_doc(db, kind, doc_id, lock=True)
    if doc.status != "issued":
        return str(doc.dsc_status)
    req = await _request(db, kind, doc_id, lock=True)
    if req is not None and req.status == "signed":
        return "signed"
    policy = await _required(db, doc)
    if policy is None and req is None:
        if doc.dsc_status == "pending":  # the policy was withdrawn between issue and this task
            doc.dsc_status = "not_required"
            await db.flush()
        return str(doc.dsc_status)
    if policy is None:
        policy = await db.get(DscPolicy, req.policy_id) if req is not None and req.policy_id else None
        if policy is None:
            return str(doc.dsc_status)
    adapter = await _adapter(ctx, db, policy, doc.legal_entity_id)
    adapter_key = str(getattr(adapter, "provider_key", None) or getattr(adapter, "key", "manual"))
    first = req is None
    if req is None:
        req = DscSignRequest(
            id=uuid.uuid4(),
            tenant_id=tenant_of(db),
            kind=kind,
            doc_id=doc.id,
            policy_id=policy.id,
            status="pending",
            adapter_key=adapter_key,
            attempts=0,
            requested_at=_now(),
        )
        db.add(req)
        await db.flush()
    elif (
        req.status == "pending"
        and req.attempts > 0
        and req.adapter_key == "manual"
        and adapter_key == "manual"
    ):
        return "pending"  # duplicate event: already waiting for a manual upload
    req.adapter_key = adapter_key
    render = await _store_render(ctx, db, kind, doc, req)
    doc.dsc_status = "pending"
    await db.flush()
    dto = DscSignRequestDTO(
        tenant_id=doc.tenant_id,
        file_id=req.render_file_id or uuid.uuid4(),
        pdf=render,
        sha256=req.render_sha256 or dsc_verify.sha256(render),
        signer_ref=policy.signer_ref or "",
        reason=policy.reason,
        location=policy.location or "",
    )
    last_error: str | None = None
    for attempt in range(MAX_ATTEMPTS):
        delay = RETRY_DELAYS[min(attempt, len(RETRY_DELAYS) - 1)]
        if delay:
            await asyncio.sleep(delay)
        req.attempts += 1
        try:
            result = await adapter.sign_pdf(dto)
        except ManualActionRequired:
            req.status = "pending"
            req.last_error = None
            await db.flush()
            if first or req.attempts == 1:
                _notify_pending(db, kind, doc, req)
            return "pending"
        except Exception as exc:  # provider error -> retry with backoff
            last_error = f"{type(exc).__name__}: {str(exc)[:300]}"
            logger.warning("dsc signing attempt {} failed for {} {}", attempt + 1, kind, doc.id)
            continue
        req.external_ref = (result.external_ref or "")[:200] or None
        if result.status != "signed" or not result.signed_pdf:
            req.status = "pending"
            await db.flush()
            if first:
                _notify_pending(db, kind, doc, req)
            return "pending"
        try:
            verification = await asyncio.to_thread(
                _adapter_verification, adapter_key, render, result.signed_pdf
            )
        except dsc_verify.DscVerificationError as exc:
            last_error = f"{exc.code}: {exc.message}"
            break  # a bad signature does not get better by retrying
        await _accept_signed(ctx, db, kind, doc, req, result.signed_pdf, verification, manual=False)
        return "signed"
    req.status = "failed"
    req.last_error = last_error
    doc.dsc_status = "failed"
    await db.flush()
    audit.record(f"{kind}.dsc_failed", kind, doc.id, session=db, attempts=req.attempts, adapter=adapter_key)
    _notify_pending(db, kind, doc, req)
    return "failed"


# ---------------------------------------------------------------- API
async def status(ctx: RequestContext, db: AsyncSession, kind: str, doc_id: uuid.UUID) -> DscStatusOut:
    _check_kind(kind)
    doc = await get_doc(db, kind, doc_id)
    req = await _request(db, kind, doc_id)
    required = req is not None or doc.dsc_status != "not_required"
    return DscStatusOut(
        kind=kind,
        doc_id=doc.id,
        dsc_status=doc.dsc_status,
        required=required,
        request_status=req.status if req else None,
        adapter_key=req.adapter_key if req else None,
        attempts=req.attempts if req else 0,
        last_error=req.last_error if req else None,
        requested_at=req.requested_at if req else None,
        signed_at=req.signed_at if req else None,
        signer_subject=req.signer_subject if req else None,
        signer_serial=req.signer_serial if req else None,
        signing_time=req.signing_time if req else None,
        signed_pdf_file_id=doc.signed_pdf_file_id,
        render_sha256=req.render_sha256 if req else None,
    )


async def render_for_signing(
    ctx: RequestContext, db: AsyncSession, kind: str, doc_id: uuid.UUID
) -> tuple[bytes, str]:
    """The stored render (the exact bytes an external signer must sign incrementally)."""
    from app.services import files_service

    _check_kind(kind)
    doc = await get_doc(db, kind, doc_id)
    req = await _request(db, kind, doc_id)
    if req is None or req.render_file_id is None:
        raise NotFound("No stored render for this document")
    f = await files_service.get_file(db, req.render_file_id)
    if f is None or f.deleted_at is not None:
        raise NotFound("No stored render for this document")
    audit.record(f"{kind}.dsc_render_downloaded", kind, doc.id, session=db)
    return await files_service.read_file(
        f
    ), f"{(doc.number or str(doc.id)[:8]).replace('/', '-')}-to-sign.pdf"


async def upload_signed(
    ctx: RequestContext, db: AsyncSession, kind: str, doc_id: uuid.UUID, data: bytes, filename: str | None
) -> DscUploadOut:
    from app.services import files_service

    _check_kind(kind)
    doc = await get_doc(db, kind, doc_id, lock=True)
    req = await _request(db, kind, doc_id, lock=True)
    if req is None or req.status == "signed" or doc.dsc_status not in BLOCKING or req.render_file_id is None:
        raise ConflictError("This document is not waiting for a signature", code="dsc_not_pending")
    files_service.check_size(data)
    if files_service.sniff_kind(data) != "pdf":
        raise ValidationFailed(
            "Upload the signed PDF", code="unsupported_file_type", details={"allowed": ["pdf"]}
        )
    f = await files_service.get_file(db, req.render_file_id)
    if f is None:
        raise ConflictError("The stored render is missing", code="dsc_render_missing")
    render = await files_service.read_file(f)
    try:
        verification = await asyncio.to_thread(dsc_verify.verify, render, data)
    except dsc_verify.DscVerificationError as exc:
        audit.record(f"{kind}.dsc_upload_refused", kind, doc.id, session=db, code=exc.code)
        raise ValidationFailed(exc.message, code=exc.code) from exc
    req.attempts += 1
    req.adapter_key = "manual"
    await _accept_signed(ctx, db, kind, doc, req, data, verification, manual=True, filename=filename)
    return DscUploadOut(
        status=await status(ctx, db, kind, doc_id),
        verification=DscVerificationOut(
            intact=verification.intact,
            valid=verification.valid,
            sub_filter=verification.sub_filter,
            signer_subject=verification.signer_subject,
            signer_serial=verification.signer_serial,
            signing_time=verification.signing_time,
            warnings=list(verification.warnings),
        ),
    )


async def retry(ctx: RequestContext, db: AsyncSession, kind: str, doc_id: uuid.UUID) -> DscStatusOut:
    _check_kind(kind)
    doc = await get_doc(db, kind, doc_id, lock=True)
    req = await _request(db, kind, doc_id, lock=True)
    if req is None or req.status != "failed":
        raise ConflictError("Only a failed signing can be retried", code="dsc_not_failed")
    req.status = "pending"
    req.attempts = 0
    req.last_error = None
    doc.dsc_status = "pending"
    await db.flush()
    audit.record(f"{kind}.dsc_retry", kind, doc.id, session=db)
    emit(db, DscSignQueued(tenant_id=doc.tenant_id, kind=kind, doc_id=doc.id))
    return await status(ctx, db, kind, doc_id)


async def pending(ctx: RequestContext, db: AsyncSession) -> list[DscPendingOut]:
    rows = list(
        (
            await db.execute(
                select(DscSignRequest)
                .where(DscSignRequest.status.in_(("pending", "failed")))
                .order_by(DscSignRequest.requested_at)
                .limit(500)
            )
        ).scalars()
    )
    now = _now()
    out: list[DscPendingOut] = []
    for r in rows:
        doc = cast("Invoice | None", await db.get(MODEL[r.kind], r.doc_id))
        if doc is None:
            continue
        snap = doc.recipient_snapshot or {}
        out.append(
            DscPendingOut(
                request_id=r.id,
                kind=r.kind,
                doc_id=r.doc_id,
                number=doc.number,
                client_id=doc.client_id,
                client_name=snap.get("display_name") or snap.get("name"),
                status=r.status,
                age_hours=round(max((now - r.requested_at).total_seconds(), 0) / 3600, 1),
                adapter_key=r.adapter_key,
                attempts=r.attempts,
                last_error=r.last_error,
                requested_at=r.requested_at,
            )
        )
    return out


async def pending_digest(ctx: RequestContext, db: AsyncSession) -> int:
    """Daily digest (beat ``dsc-pending-digest``): one ``dsc_signing_pending`` notification per waiting document
    (deduplicated per day by the notification event id)."""
    # Sweep: documents marked pending at issue whose signing task never ran (e.g. a worker crash) get re-queued.
    for kind, model in MODEL.items():
        model = cast("type[Invoice]", model)
        orphans = select(model.id).where(
            model.dsc_status == "pending",
            model.status == "issued",
            model.id.not_in(select(DscSignRequest.doc_id).where(DscSignRequest.kind == kind)),
        )
        for doc_id in (await db.execute(orphans.limit(200))).scalars():
            emit(db, DscSignQueued(tenant_id=tenant_of(db), kind=kind, doc_id=doc_id))
    items = await pending(ctx, db)
    for item in items:
        emit(
            db,
            DscSigningPending(
                tenant_id=tenant_of(db),
                kind=item.kind,
                doc_id=item.doc_id,
                number=item.number or "",
                age_hours=int(item.age_hours),
                status=item.status,
            ),
        )
    return len(items)


# ---------------------------------------------------------------- policies
def _policy_out(row: DscPolicy) -> DscPolicyOut:
    return DscPolicyOut.model_validate(row)


async def list_policies(ctx: RequestContext, db: AsyncSession) -> list[DscPolicyOut]:
    rows = (await db.execute(select(DscPolicy).order_by(DscPolicy.created_at))).scalars()
    return [_policy_out(r) for r in rows]


async def _validate_refs(db: AsyncSession, data: DscPolicyIn | DscPolicyPatch, *, check_owner: bool) -> None:
    from app.models.clients import Client
    from app.models.connectors import ProviderConnectorConfig
    from app.models.legal_entities import GstRegistration

    if check_owner and isinstance(data, DscPolicyIn):
        if await db.get(GstRegistration, data.gst_registration_id) is None:
            raise ValidationFailed("Unknown GST registration", code="gst_registration_not_found")
        if data.client_id is not None and await db.get(Client, data.client_id) is None:
            raise ValidationFailed("Unknown client", code="client_not_found")
    if data.provider_config_id is not None:
        cfg = await db.get(ProviderConnectorConfig, data.provider_config_id)
        if cfg is None or cfg.category != "dsc":
            raise ValidationFailed("Pick a DSC connector configuration", code="connector_config_invalid")


async def create_policy(ctx: RequestContext, db: AsyncSession, data: DscPolicyIn) -> DscPolicyOut:
    await _validate_refs(db, data, check_owner=True)
    existing = (
        await db.execute(
            select(func.count(DscPolicy.id)).where(
                DscPolicy.gst_registration_id == data.gst_registration_id,
                func.coalesce(DscPolicy.client_id, NIL) == (data.client_id or NIL),
            )
        )
    ).scalar_one()
    if existing:
        raise ConflictError(
            "A DSC policy already exists for this registration and client", code="dsc_policy_exists"
        )
    row = DscPolicy(id=uuid.uuid4(), tenant_id=tenant_of(db), created_by=user_id(ctx), **data.model_dump())
    db.add(row)
    await db.flush()
    await db.refresh(row)
    audit.record("dsc_policy.created", "dsc_policy", row.id, session=db, required=row.required)
    return _policy_out(row)


async def _policy(db: AsyncSession, policy_id: uuid.UUID) -> DscPolicy:
    row = await db.get(DscPolicy, policy_id)
    if row is None:
        raise NotFound("DSC policy not found")
    return row


async def update_policy(
    ctx: RequestContext, db: AsyncSession, policy_id: uuid.UUID, data: DscPolicyPatch
) -> DscPolicyOut:
    row = await _policy(db, policy_id)
    await _validate_refs(db, data, check_owner=False)
    changes = data.model_dump(exclude_unset=True)
    for key, value in changes.items():
        if key in ("reason",) and value is None:
            continue
        setattr(row, key, value)
    await db.flush()
    await db.refresh(row)
    audit.record("dsc_policy.updated", "dsc_policy", row.id, session=db, fields=sorted(changes))
    return _policy_out(row)


async def delete_policy(ctx: RequestContext, db: AsyncSession, policy_id: uuid.UUID) -> None:
    row = await _policy(db, policy_id)
    used = (
        await db.execute(select(func.count(DscSignRequest.id)).where(DscSignRequest.policy_id == row.id))
    ).scalar_one()
    if used:
        row.active = False  # keep the history of requests that used it
    else:
        await db.delete(row)
    await db.flush()
    audit.record("dsc_policy.deleted", "dsc_policy", policy_id, session=db, deactivated_only=bool(used))


# ---------------------------------------------------------------- files access (owner kinds invoice / credit_note)
async def file_access(ctx: RequestContext, db: AsyncSession, entity_id: uuid.UUID, write: bool) -> bool:
    principal = ctx.principal
    if principal is None:
        return True
    has = getattr(principal, "has", None)
    return bool(has and has("billing:read"))
