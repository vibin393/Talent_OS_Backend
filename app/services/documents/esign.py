"""Signature Requests and the signing ceremony (Epic 22 A5/A6/A9, FR-118/119).

* ``create_request`` -> draft (or sent): template merge + render, or a stored PDF version; ordered signers;
  ``org_grants`` per signer organisation (Worker, client, vendor) on the request and its document.
* ``send_request`` freezes ``doc_sha256`` (recomputed from storage == ``document_versions.sha256``), resolves the
  provider (template config, else tenant default, else built-in) and notifies signer 1 after commit.
* ``SignerSession`` is the one ceremony of every channel (internal session, Worker portal, secure link, P1 offer
  acceptance): view, email OTP (hashed, 10 min, 5 attempts, 5 codes / hour), consent, typed-name sign, decline.
  Evidence is written in the sign transaction; completion (certificate + final PDF) runs after commit.
* ``TRANSITIONS`` (AD-4) is the status machine; every change emits ``SignatureRequestStatusChanged``.
"""

import dataclasses
import hashlib
import hmac
import re
import secrets
import unicodedata
import uuid
from collections.abc import Iterable, Sequence
from datetime import datetime, timedelta
from typing import Any

from loguru import logger
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit, org_grants, secure_links
from app.core.actors import ActorRef
from app.core.context import RequestContext
from app.core.database import tenant_session
from app.core.errors import ConflictError, Gone, NotFound, RateLimited, ValidationFailed
from app.core.events import emit
from app.core.mailer import EmailAttachment, EmailMessage, send_email
from app.core.settings import get_settings
from app.events.documents import SignatureRequestCompleted, SignatureRequestStatusChanged
from app.models.documents import (
    OPEN_REQUEST_STATUSES,
    Document,
    DocumentVersion,
    EsignCertificateLookup,
    Signature,
    SignatureEvent,
    SignatureRequest,
    Signer,
    SignerOtp,
)
from app.models.files import StoredFile
from app.schemas.documents import CeremonyOut, SignatureEventOut, SignatureRequestOut, SignerOut
from app.services.documents import certificate, repository, templates
from app.services.documents.common import (
    ESIGN_LINK_PURPOSE,
    SIGNER_SUBJECT,
    candidate_contact,
    conflict,
    contact_info,
    email_hint,
    external_user,
    not_found,
    now,
    tenant_name,
    tenant_of,
    user_id,
)

SIGNER_ACTORS = frozenset({"candidate", "user", "client_user", "contact", "vendor_user", "secure_link"})
_USER_OR_SYSTEM = frozenset({"user", "system"})
_SYSTEM = frozenset({"system"})
TRANSITIONS: dict[tuple[str, str], frozenset[str]] = {
    ("draft", "sent"): _USER_OR_SYSTEM,
    ("sent", "partially_signed"): _SYSTEM,
    ("sent", "completed"): _SYSTEM,
    ("partially_signed", "completed"): _SYSTEM,
    ("sent", "declined"): SIGNER_ACTORS,
    ("partially_signed", "declined"): SIGNER_ACTORS,
    ("sent", "expired"): _SYSTEM,
    ("partially_signed", "expired"): _SYSTEM,
    ("draft", "cancelled"): _USER_OR_SYSTEM,
    ("sent", "cancelled"): _USER_OR_SYSTEM,
    ("partially_signed", "cancelled"): _USER_OR_SYSTEM,
}

OTP_TTL = timedelta(minutes=10)
OTP_MAX_ATTEMPTS = 5
OTP_MAX_PER_HOUR = 5
_SPACES = re.compile(r"\s+")


# ================================================================ helpers
def normalize_name(value: str | None) -> str:
    """NFKC, casefold, collapsed whitespace (moved from ``offers/response.py``; re-exported there)."""
    text = unicodedata.normalize("NFKC", value or "").casefold()
    return _SPACES.sub(" ", text).strip()


def clean_typed(value: str) -> str:
    return _SPACES.sub(" ", unicodedata.normalize("NFKC", value)).strip()


def clean_ip(value: str | None) -> str | None:
    import ipaddress

    if not value:
        return None
    try:
        return str(ipaddress.ip_address(value.strip()))
    except ValueError:
        return None


def _otp_hash(signer_id: uuid.UUID, code: str) -> str:
    settings = get_settings()
    key = (settings.audit_master_key or settings.mfa_encryption_key or "talent-os-esign-otp").encode()
    return hmac.new(key, f"{signer_id}:{code}".encode(), hashlib.sha256).hexdigest()


def check_transition(from_status: str, to_status: str, actor: str) -> None:
    allowed = TRANSITIONS.get((from_status, to_status))
    if allowed is None or actor not in allowed:
        raise conflict(
            "invalid_transition",
            f"A {from_status} signature request cannot become {to_status}",
            from_status=from_status,
            to_status=to_status,
        )


def _event(
    db: AsyncSession,
    req: SignatureRequest,
    kind: str,
    *,
    signer_id: uuid.UUID | None = None,
    ip: str | None = None,
    user_agent: str | None = None,
    **data: Any,
) -> None:
    db.add(
        SignatureEvent(
            id=uuid.uuid4(),
            tenant_id=req.tenant_id,
            request_id=req.id,
            signer_id=signer_id,
            kind=kind,
            ip=ip,
            user_agent=user_agent,
            at=now(),
            data={k: (str(v) if isinstance(v, uuid.UUID) else v) for k, v in data.items()},
        )
    )


def transition(db: AsyncSession, req: SignatureRequest, to_status: str, actor: str) -> None:
    from_status = req.status
    check_transition(from_status, to_status, actor)
    req.status = to_status
    emit(
        db,
        SignatureRequestStatusChanged(
            tenant_id=req.tenant_id,
            request_id=req.id,
            subject_kind=req.subject_kind,
            subject_id=req.subject_id,
            from_status=from_status,
            to_status=to_status,
        ),
    )


# ---------------------------------------------------------------- after-commit work (private event)
@dataclasses.dataclass(frozen=True, slots=True)
class EsignWork:
    """After-commit work of this domain (never exported: not part of the events catalogue)."""

    tenant_id: uuid.UUID
    kind: str  # notify | remind | complete | declined
    request_id: uuid.UUID
    signer_id: uuid.UUID | None = None


def _work(db: AsyncSession, req: SignatureRequest, kind: str, signer_id: uuid.UUID | None = None) -> None:
    emit(db, EsignWork(req.tenant_id, kind, req.id, signer_id))


# ================================================================ read models
async def _signers(db: AsyncSession, request_id: uuid.UUID) -> list[Signer]:
    stmt = select(Signer).where(Signer.request_id == request_id).order_by(Signer.order)
    return list((await db.execute(stmt)).scalars())


async def _signatures(db: AsyncSession, request_id: uuid.UUID) -> dict[uuid.UUID, Signature]:
    rows = (await db.execute(select(Signature).where(Signature.request_id == request_id))).scalars()
    return {s.signer_id: s for s in rows}


async def request_out(
    db: AsyncSession, req: SignatureRequest, *, with_events: bool = True
) -> SignatureRequestOut:
    await db.refresh(req)
    signers = await _signers(db, req.id)
    sigs = await _signatures(db, req.id)
    events: list[SignatureEventOut] = []
    if with_events:
        rows = (
            await db.execute(
                select(SignatureEvent)
                .where(SignatureEvent.request_id == req.id)
                .order_by(SignatureEvent.at, SignatureEvent.id)
            )
        ).scalars()
        events = [
            SignatureEventOut(kind=e.kind, signer_id=e.signer_id, at=e.at, data=dict(e.data or {}))
            for e in rows
        ]
    return SignatureRequestOut(
        id=req.id,
        human_id=req.human_id,
        title=req.title,
        status=req.status,
        document_id=req.document_id,
        document_version_id=req.document_version_id,
        subject_kind=req.subject_kind,
        subject_id=req.subject_id,
        provider=req.provider,
        doc_sha256=req.doc_sha256,
        final_sha256=req.final_sha256,
        final_version_id=req.final_version_id,
        certificate_id=req.certificate_id,
        require_otp=req.require_otp,
        expires_at=req.expires_at,
        sent_at=req.sent_at,
        completed_at=req.completed_at,
        cancel_reason=req.cancel_reason,
        created_at=req.created_at,
        signers=[
            SignerOut(
                id=s.id,
                order=s.order,
                kind=s.kind,
                principal_id=s.principal_id,
                name=s.name,
                email=str(s.email),
                status=s.status,
                notified_at=s.notified_at,
                signed_at=sigs[s.id].signed_at if s.id in sigs else None,
            )
            for s in signers
        ],
        events=events,
    )


# ================================================================ create / send / cancel
@dataclasses.dataclass(frozen=True, slots=True)
class SignerInput:
    kind: str
    principal_id: uuid.UUID | None = None
    contact_id: uuid.UUID | None = None
    email: str | None = None
    name: str | None = None


async def _resolve_signer(db: AsyncSession, s: SignerInput) -> tuple[str, str, tuple[str, uuid.UUID] | None]:
    """(name, email, (org kind, org id) | None) of one signer; 422 when unresolvable."""
    from app.services import entity_refs

    name, email, org = s.name, s.email, None
    if s.kind == "candidate" and s.principal_id is not None:
        full, primary = await candidate_contact(db, s.principal_id)
        if full is None:
            raise ValidationFailed("Unknown candidate signer", code="signer_not_found")
        name, email, org = name or full, email or primary, ("worker", s.principal_id)
    elif s.kind == "user" and s.principal_id is not None:
        info = await entity_refs.user(db, s.principal_id)
        if info is None or not info.is_active:
            raise ValidationFailed("Unknown user signer", code="signer_not_found")
        name, email = name or info.name, email or info.email
    elif s.kind in ("client_user", "vendor_user") and s.principal_id is not None:
        ext = await external_user(db, s.kind, s.principal_id)
        if ext is None:
            raise ValidationFailed("Unknown signer", code="signer_not_found")
        name, email = name or ext[0], email or ext[1]
        org = ("client" if s.kind == "client_user" else "vendor", ext[2])
    elif s.kind == "contact" and s.contact_id is not None:
        cname, cemail, _client = await contact_info(db, s.contact_id)
        if cname is None:
            raise ValidationFailed("Unknown contact signer", code="signer_not_found")
        name, email = name or cname, email or cemail
    if not name or not email or "@" not in email:
        raise ValidationFailed(
            "Every signer needs a name and an email address",
            code="signer_incomplete",
            details={"kind": s.kind},
        )
    return name.strip()[:300], email.strip().lower(), org


async def _provider(db: AsyncSession, config_id: uuid.UUID | None) -> tuple[str, Any, uuid.UUID | None]:
    """(provider key, adapter, config id): the template's config, else the tenant default, else built-in."""
    from app.models.connectors import ProviderConnectorConfig
    from app.services.connectors import registry
    from app.services.connectors.esign.builtin import BuiltinEsignProvider

    row = await db.get(ProviderConnectorConfig, config_id) if config_id else None
    if row is None or row.status != "active" or row.category != "esign":
        row = await registry.active_config(db, "esign", None)
    if row is None or row.provider_key in ("builtin", "manual"):
        return "builtin", BuiltinEsignProvider(None), None
    return row.provider_key, registry.build("esign", await registry.load_config(db, row)), row.id


async def create_request(
    ctx: RequestContext,
    db: AsyncSession,
    *,
    owner_kind: str,
    owner_id: uuid.UUID,
    signers: Sequence[SignerInput],
    template_id: uuid.UUID | None = None,
    document_version_id: uuid.UUID | None = None,
    merge_refs: dict[str, uuid.UUID] | None = None,
    merge_data: dict[str, Any] | None = None,
    subject_kind: str | None = None,
    subject_id: uuid.UUID | None = None,
    title: str | None = None,
    expires_at: datetime | None = None,
    send: bool = True,
    legal_entity_id: uuid.UUID | None = None,
    require_otp: bool = True,
    check_access: bool = True,
    category: str | None = None,
) -> SignatureRequest:
    if (template_id is None) == (document_version_id is None):
        raise ValidationFailed("Give either a template or a document version", code="source_required")
    if not signers:
        raise ValidationFailed("At least one signer is required", code="signers_required")
    if check_access and not await repository.entity_access(ctx, db, owner_kind, owner_id, write=True):
        raise not_found("Resource")
    resolved = [await _resolve_signer(db, s) for s in signers]
    emails = [e for _, e, _ in resolved]
    if len(set(emails)) != len(emails):
        raise ValidationFailed("Each signer needs a different email address", code="duplicate_signer_email")
    config_id: uuid.UUID | None = None
    worker = owner_id if owner_kind == "candidate" else None
    if template_id is not None:
        tid = await templates.resolve_variant(db, template_id, legal_entity_id)
        pdf, template, tv = await templates.render_template(
            db, tid, merge_refs=merge_refs, merge_data=merge_data
        )
        doc_id = uuid.uuid4()
        file = await repository.store_generated_pdf(ctx, db, doc_id, pdf, f"{template.name}.pdf")
        doc = await repository.create_document(
            ctx,
            db,
            owner_kind=owner_kind,
            owner_id=owner_id,
            category=category or template.kind,
            title=title or template.name,
            worker_candidate_id=worker,
            document_id=doc_id,
        )
        version = await repository.append_version(
            ctx, db, doc, file, source="generated", template_version_id=tv.id
        )
        config_id = template.esign_config_id
    else:
        version = await db.get(DocumentVersion, document_version_id)
        doc = await db.get(Document, version.document_id) if version else None
        if version is None or doc is None or (check_access and not await repository.can_access(ctx, db, doc)):
            raise not_found("Document version")
        f = await db.get(StoredFile, version.file_id)
        if f is None or f.content_type != "application/pdf" or f.scan_status not in ("clean", "skipped_dev"):
            raise ValidationFailed("Only a scanned PDF can be sent for signature", code="pdf_required")
    tenant_id = tenant_of(db)
    from app.core.sequences import next_human_id

    req = SignatureRequest(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        human_id=await next_human_id(db, "SIG"),
        title=(title or doc.title)[:300],
        document_id=doc.id,
        document_version_id=version.id,
        subject_kind=subject_kind or owner_kind,
        subject_id=subject_id or owner_id,
        provider="builtin",
        provider_config_id=config_id,
        status="draft",
        require_otp=require_otp,
        expires_at=expires_at or (now() + timedelta(days=14)),
        created_by=user_id(ctx),
        certificate_id=uuid.uuid4(),
    )
    db.add(req)
    db.add(EsignCertificateLookup(certificate_id=req.certificate_id, tenant_id=tenant_id, request_id=req.id))
    await db.flush()
    actor = ActorRef("user", user_id(ctx)) if user_id(ctx) else ActorRef("system", None)
    for i, (s, (name, email, org)) in enumerate(zip(signers, resolved, strict=True), start=1):
        db.add(
            Signer(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                request_id=req.id,
                order=i,
                kind=s.kind,
                principal_id=s.principal_id,
                contact_id=s.contact_id,
                name=name,
                email=email,
                status="pending",
            )
        )
        if org is not None:
            await org_grants.grant(db, org[0], org[1], "signature_request", req.id, actor)
            await org_grants.grant(db, org[0], org[1], "document", doc.id, actor)
    await db.flush()
    _event(db, req, "created", signers=len(signers))
    audit.record("signature_request.created", "signature_request", req.id, session=db, human_id=req.human_id)
    if send:
        await send_request(
            ctx,
            db,
            req,
            actor="agent" if ctx.actor_kind == "agent" else ("user" if user_id(ctx) else "system"),
        )
    return req


async def send_request(
    ctx: RequestContext, db: AsyncSession, req: SignatureRequest, *, actor: str = "user"
) -> None:
    check_transition(req.status, "sent", actor)
    version, data = await repository.version_bytes(db, req.document_version_id)
    digest = repository.sha256(data)
    if digest != version.sha256:
        logger.error("ALERT signature request {}: stored document hash mismatch", req.id)
        raise conflict("document_hash_mismatch", "The stored document changed; it cannot be sent")
    req.doc_sha256 = digest
    key, adapter, config_id = await _provider(db, req.provider_config_id)
    req.provider = key
    req.provider_config_id = config_id
    if key != "builtin":
        from app.services.connectors.esign.port import EsignRequestDTO, EsignSignerDTO

        signers = await _signers(db, req.id)
        req.external_ref = await adapter.create_request(
            EsignRequestDTO(
                tenant_id=req.tenant_id,
                request_id=req.id,
                title=req.title,
                doc_sha256=digest,
                pdf=data,
                signers=tuple(EsignSignerDTO(s.order, s.name, str(s.email)) for s in signers),
                expires_at=req.expires_at,
            )
        )
    else:
        req.external_ref = str(req.id)
    transition(db, req, "sent", actor)
    req.sent_at = now()
    _event(db, req, "sent", provider=key)
    await db.flush()
    if key == "builtin" and req.subject_kind != "offer":
        first = (await _signers(db, req.id))[0]
        _work(db, req, "notify", first.id)
    del ctx


async def load_request(
    db: AsyncSession, request_id: uuid.UUID, *, for_update: bool = False
) -> SignatureRequest:
    stmt = select(SignatureRequest).where(SignatureRequest.id == request_id)
    if for_update:
        stmt = stmt.with_for_update(
            of=SignatureRequest, key_share=True
        )  # FOR NO KEY UPDATE: FK inserts elsewhere pass
    req = (await db.execute(stmt)).scalar_one_or_none()
    if req is None:
        raise not_found("Signature request")
    return req


async def visible_request(
    ctx: RequestContext, db: AsyncSession, request_id: uuid.UUID, *, for_update: bool = False
) -> SignatureRequest:
    """Internal API access: the document's access rule (404 otherwise)."""
    req = await load_request(db, request_id, for_update=for_update)
    doc = await db.get(Document, req.document_id)
    if doc is None or not await repository.can_access(ctx, db, doc):
        raise not_found("Signature request")
    return req


async def _close_signers(db: AsyncSession, req: SignatureRequest) -> None:
    for s in await _signers(db, req.id):
        if s.status in ("pending", "notified"):
            s.status = "void"
    await secure_links_revoke(db, req)


async def secure_links_revoke(db: AsyncSession, req: SignatureRequest) -> None:
    for s in await _signers(db, req.id):
        await secure_links.revoke_for_subject(db, SIGNER_SUBJECT, s.id, ESIGN_LINK_PURPOSE)


async def cancel_request(
    ctx: RequestContext, db: AsyncSession, req: SignatureRequest, reason: str, *, actor: str = "user"
) -> None:
    transition(db, req, "cancelled", actor)
    req.cancel_reason = reason
    await _close_signers(db, req)
    _event(db, req, "cancelled", reason_given=bool(reason))
    audit.record("signature_request.cancelled", "signature_request", req.id, session=db)
    await db.flush()
    del ctx


# ================================================================ OTP (own short transaction)
@dataclasses.dataclass(frozen=True, slots=True)
class _OtpResult:
    ok: bool
    code: str | None = None  # error code


async def _verify_otp(tenant_id: uuid.UUID, signer_id: uuid.UUID, code: str | None) -> _OtpResult:
    """Checks the latest unconsumed code in its own committed transaction (failed attempts persist even
    though the sign request then fails)."""
    async with tenant_session(tenant_id) as odb:
        otp = (
            await odb.execute(
                select(SignerOtp)
                .where(SignerOtp.signer_id == signer_id, SignerOtp.consumed_at.is_(None))
                .order_by(SignerOtp.created_at.desc())
                .limit(1)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if otp is None:
            return _OtpResult(False, "otp_invalid")
        if otp.attempts >= OTP_MAX_ATTEMPTS:
            return _OtpResult(False, "otp_locked")
        if otp.expires_at <= now():
            return _OtpResult(False, "otp_expired")
        if not code or not hmac.compare_digest(otp.code_hash, _otp_hash(signer_id, code.strip())):
            otp.attempts += 1
            req = await odb.get(SignatureRequest, otp.request_id)
            if req is not None:
                _event(odb, req, "otp_failed", signer_id=signer_id, attempt=otp.attempts)
            return _OtpResult(False, "otp_locked" if otp.attempts >= OTP_MAX_ATTEMPTS else "otp_invalid")
        otp.consumed_at = now()
        return _OtpResult(True)


def _otp_error(code: str) -> Exception:
    if code == "otp_expired":
        return Gone("The email code has expired; request a new one", code="otp_expired")
    if code == "otp_locked":
        return RateLimited("Too many wrong codes; request a new one", code="otp_locked")
    return ValidationFailed("The email code is not correct", code="otp_invalid")


# ================================================================ the ceremony
class SignerSession:
    """One signer acting through one channel (``session`` | ``portal`` | ``secure_link``)."""

    def __init__(
        self,
        db: AsyncSession,
        signer_id: uuid.UUID,
        channel: str,
        ip: str | None = None,
        user_agent: str | None = None,
        *,
        link_id: uuid.UUID | None = None,
        actor_kind: str | None = None,
    ) -> None:
        self.db = db
        self.signer_id = signer_id
        self.channel = channel
        self.ip = clean_ip(ip)
        self.user_agent = (user_agent or "")[:500] or None
        self.link_id = link_id
        self._actor_kind = actor_kind

    async def load(
        self, *, for_update: bool = False, require_turn: bool = True
    ) -> tuple[Signer, SignatureRequest]:
        signer = await self.db.get(Signer, self.signer_id)
        if signer is None:
            raise not_found("Signature request")
        req = await load_request(self.db, signer.request_id, for_update=for_update)
        if for_update:
            await self.db.refresh(signer)
        if req.status not in OPEN_REQUEST_STATUSES:
            raise Gone("This signature request is closed", code="request_closed")
        if req.expires_at <= now():
            raise Gone("This signature request has expired", code="request_closed")
        if signer.status in ("signed", "declined", "void"):
            raise conflict("already_signed", "You already answered this request", signer_status=signer.status)
        if require_turn:
            earlier = (
                await self.db.execute(
                    select(func.count())
                    .select_from(Signer)
                    .where(
                        Signer.request_id == req.id, Signer.order < signer.order, Signer.status != "signed"
                    )
                )
            ).scalar_one()
            if earlier:
                raise conflict("not_your_turn", "An earlier signer has not signed yet")
        return signer, req

    @property
    def actor_kind(self) -> str:
        return self._actor_kind or ("secure_link" if self.channel == "secure_link" else "user")

    async def ceremony(self, *, document_url: str) -> CeremonyOut:
        signer = await self.db.get(Signer, self.signer_id)
        if signer is None:
            raise not_found("Signature request")
        req = await load_request(self.db, signer.request_id)
        signers = await _signers(self.db, req.id)
        sigs = await _signatures(self.db, req.id)
        your_turn = (
            req.status in OPEN_REQUEST_STATUSES
            and signer.status in ("pending", "notified")
            and all(s.status == "signed" for s in signers if s.order < signer.order)
        )
        return CeremonyOut(
            request_id=req.id,
            signer_id=signer.id,
            human_id=req.human_id,
            title=req.title,
            tenant_name=await tenant_name(self.db),
            status=req.status,
            signer_name=signer.name,
            signer_status=signer.status,
            your_turn=your_turn,
            require_otp=req.require_otp,
            consented=signer.consented_at is not None,
            otp_verified=signer.otp_verified_at is not None,
            expires_at=req.expires_at,
            doc_sha256=req.doc_sha256,
            final_sha256=req.final_sha256,
            signers=[
                {
                    "order": s.order,
                    "name": s.name,
                    "status": s.status,
                    "signed_at": sigs[s.id].signed_at if s.id in sigs else None,
                }
                for s in signers
            ],
            document_url=document_url,
        )

    async def view(self) -> tuple[bytes, str]:
        """The frozen version's bytes (``final`` once completed). Records a ``viewed`` event, never a status."""
        signer = await self.db.get(Signer, self.signer_id)
        if signer is None:
            raise not_found("Signature request")
        req = await load_request(self.db, signer.request_id)
        version_id = (
            req.final_version_id
            if req.status == "completed" and req.final_version_id
            else req.document_version_id
        )
        if req.status not in (*OPEN_REQUEST_STATUSES, "completed"):
            raise Gone("This signature request is closed", code="request_closed")
        _, data = await repository.version_bytes(self.db, version_id)
        if req.status in OPEN_REQUEST_STATUSES:
            _event(
                self.db,
                req,
                "viewed",
                signer_id=signer.id,
                ip=self.ip,
                user_agent=self.user_agent,
                channel=self.channel,
            )
            await self.db.flush()
        return data, f"{req.human_id}.pdf"

    async def request_otp(self) -> tuple[datetime, str]:
        signer, req = await self.load()
        recent = (
            await self.db.execute(
                select(func.count())
                .select_from(SignerOtp)
                .where(SignerOtp.signer_id == signer.id, SignerOtp.created_at > now() - timedelta(hours=1))
            )
        ).scalar_one()
        if recent >= OTP_MAX_PER_HOUR:
            raise RateLimited("Too many codes requested; try again later", code="otp_rate_limited")
        code = f"{secrets.randbelow(1_000_000):06d}"
        expires = now() + OTP_TTL
        self.db.add(
            SignerOtp(
                id=uuid.uuid4(),
                tenant_id=req.tenant_id,
                signer_id=signer.id,
                request_id=req.id,
                code_hash=_otp_hash(signer.id, code),
                expires_at=expires,
                attempts=0,
            )
        )
        _event(self.db, req, "otp_sent", signer_id=signer.id, ip=self.ip, user_agent=self.user_agent)
        await self.db.flush()
        name = await tenant_name(self.db)
        lines = [
            f"Your signing code for {req.title} is {code}.",
            "It is valid for 10 minutes. Never share this code.",
            name,
        ]
        await send_email(
            EmailMessage(
                to=str(signer.email),
                subject=f"Your signing code: {code}",
                text="\n\n".join(lines),
                tenant_id=req.tenant_id,
                kind="esign_otp",
            )
        )
        return expires, email_hint(str(signer.email))

    async def consent(self, agreed: bool) -> None:
        signer, req = await self.load()
        if not agreed:
            raise ValidationFailed("Agree to sign electronically to continue", code="consent_required")
        if signer.consented_at is None:
            signer.consented_at = now()
            _event(self.db, req, "consented", signer_id=signer.id, ip=self.ip, user_agent=self.user_agent)
            await self.db.flush()

    async def sign(self, typed_name: str, otp: str | None, *, agreed: bool = False) -> SignatureRequest:
        signer, req = await self.load(for_update=True)
        if agreed and signer.consented_at is None:
            signer.consented_at = now()
            _event(self.db, req, "consented", signer_id=signer.id, ip=self.ip, user_agent=self.user_agent)
        if signer.consented_at is None:
            raise ValidationFailed("Agree to sign electronically first", code="consent_required")
        typed = clean_typed(typed_name or "")
        if not typed or normalize_name(typed) != normalize_name(signer.name):
            raise ValidationFailed("The typed name does not match the signer's name", code="name_mismatch")
        otp_at = None
        if req.require_otp:
            result = await _verify_otp(req.tenant_id, signer.id, otp)
            if not result.ok:
                raise _otp_error(result.code or "otp_invalid")
            otp_at = now()
            signer.otp_verified_at = otp_at
        _, data = await repository.version_bytes(self.db, req.document_version_id)
        if repository.sha256(data) != req.doc_sha256:
            logger.error("ALERT signature request {}: frozen document hash mismatch at signing", req.id)
            raise conflict("document_hash_mismatch", "The document changed after it was sent")
        if self.link_id is not None:
            await secure_links.consume(self.db, self.link_id)
        at = now()
        self.db.add(
            Signature(
                id=uuid.uuid4(),
                tenant_id=req.tenant_id,
                signer_id=signer.id,
                request_id=req.id,
                typed_name=typed,
                email=str(signer.email),
                ip=self.ip,
                user_agent=self.user_agent,
                consented_at=signer.consented_at,
                otp_verified_at=otp_at,
                signed_at=at,
                doc_sha256=req.doc_sha256 or "",
                channel=self.channel,
                provider=req.provider,
            )
        )
        signer.status = "signed"
        _event(
            self.db,
            req,
            "signed",
            signer_id=signer.id,
            ip=self.ip,
            user_agent=self.user_agent,
            channel=self.channel,
        )
        await self.db.flush()
        await advance(self.db, req)
        return req

    async def decline(self, reason: str | None) -> SignatureRequest:
        signer, req = await self.load(for_update=True, require_turn=False)
        if self.link_id is not None:
            await secure_links.consume(self.db, self.link_id)
        signer.status = "declined"
        transition(
            self.db, req, "declined", self.actor_kind if self.actor_kind in SIGNER_ACTORS else signer.kind
        )
        await _close_signers(self.db, req)
        _event(
            self.db,
            req,
            "declined",
            signer_id=signer.id,
            ip=self.ip,
            user_agent=self.user_agent,
            reason_given=bool(reason),
        )
        await self.db.flush()
        if req.subject_kind != "offer":
            _work(self.db, req, "declined")
        return req


async def advance(db: AsyncSession, req: SignatureRequest) -> None:
    """After a signature: complete, or move to ``partially_signed`` and notify the next signer."""
    signers = await _signers(db, req.id)
    pending = [s for s in signers if s.status != "signed"]
    if not pending:
        transition(db, req, "completed", "system")
        req.completed_at = now()
        await secure_links_revoke(db, req)
        await db.flush()
        _work(db, req, "complete")
        return
    if req.status == "sent":
        transition(db, req, "partially_signed", "system")
    await db.flush()
    if req.subject_kind != "offer" and req.provider == "builtin":
        _work(db, req, "notify", pending[0].id)


# ================================================================ completion (after commit, idempotent)
async def ensure_internal_scope(db: AsyncSession) -> None:
    """System sessions opened without an org scope (P1 ``tasks.pipeline.in_tenant``, e.g. the offer
    finalisation task) see no org-RLS rows; mark them internal for this transaction."""
    from sqlalchemy import text

    from app.core.database import ORG_KIND_KEY

    if db.sync_session.info.get(ORG_KIND_KEY) is None:
        await db.execute(text("SELECT set_config('app.org_kind', 'internal', true)"))


async def complete_request(ctx: RequestContext, db: AsyncSession, request_id: uuid.UUID) -> uuid.UUID | None:
    """Certificate + final PDF (``document_versions(source=signed)``), ``final_sha256``,
    ``SignatureRequestCompleted`` and the completion email. Returns the final file id."""
    await ensure_internal_scope(db)
    req = await load_request(db, request_id, for_update=True)
    if req.final_version_id is not None:
        final = await db.get(DocumentVersion, req.final_version_id)
        return final.file_id if final else None
    if req.status != "completed":
        return None
    doc = await db.get(Document, req.document_id)
    assert doc is not None
    version, frozen = await repository.version_bytes(db, req.document_version_id)
    signers = await _signers(db, req.id)
    sigs = await _signatures(db, req.id)
    if req.provider != "builtin":
        _, adapter, _ = await _provider(db, req.provider_config_id)
        signed_pdf = await adapter.fetch_signed(req.external_ref or "")
        frozen = signed_pdf or frozen
    evidence = [
        certificate.SignerEvidence(
            name=s.name,
            email=str(s.email),
            typed_name=sigs[s.id].typed_name or "" if s.id in sigs else "",
            signed_at=sigs[s.id].signed_at if s.id in sigs else (req.completed_at or now()),
            ip=str(sigs[s.id].ip) if s.id in sigs and sigs[s.id].ip is not None else None,
            channel=sigs[s.id].channel if s.id in sigs else req.provider,
            otp_verified=bool(s.id in sigs and sigs[s.id].otp_verified_at),
        )
        for s in signers
    ]
    cert = certificate.render_certificate(
        tenant_name=await tenant_name(db),
        request_ref=req.human_id,
        certificate_id=req.certificate_id,
        title=req.title,
        version_no=version.version_no,
        doc_sha256=req.doc_sha256 or "",
        signers=evidence,
        completed_at=req.completed_at or now(),
        heading="Electronic acceptance certificate"
        if req.subject_kind == "offer"
        else "Signature certificate",
    )
    from app.services.documents import render

    final_pdf = render.append_pages(frozen, cert)
    file = await repository.store_generated_pdf(ctx, db, doc.id, final_pdf, f"{req.human_id}-signed.pdf")
    final = await repository.append_version(ctx, db, doc, file, source="signed", signature_request_id=req.id)
    doc.signed = True
    doc.retention_class = "signed_agreements"
    req.final_version_id = final.id
    req.final_sha256 = file.sha256
    _event(db, req, "completed", final_sha256=file.sha256)
    await db.flush()
    emit(
        db,
        SignatureRequestCompleted(
            tenant_id=req.tenant_id,
            request_id=req.id,
            subject_kind=req.subject_kind,
            subject_id=req.subject_id,
            document_id=doc.id,
            final_sha256=file.sha256,
        ),
    )
    if req.subject_kind != "offer":
        await _completion_emails(db, req, signers, final_pdf)
    return file.id


async def _completion_emails(
    db: AsyncSession, req: SignatureRequest, signers: Iterable[Signer], pdf: bytes
) -> None:
    name = await tenant_name(db)
    att = EmailAttachment(filename=f"{req.human_id}-signed.pdf", content_type="application/pdf", data=pdf)
    lines = [
        f"{req.title} ({req.human_id}) is signed by everyone. The signed copy with its certificate is attached.",
        f"Signed-file SHA-256: {req.final_sha256}",
        f"Verify: {certificate.verify_url(req.certificate_id)}",
        name,
    ]
    for s in signers:
        await send_email(
            EmailMessage(
                to=str(s.email),
                subject=f"Signed: {req.title}",
                text="\n\n".join(lines),
                tenant_id=req.tenant_id,
                kind="esign_completed",
                attachments=(att,),
            )
        )


# ================================================================ notifications (after commit)
def _signing_url(signer: Signer, token: str | None) -> str:
    base = get_settings().frontend_url.rstrip("/")
    if signer.kind == "user":
        return f"{base}/documents/sign/{signer.id}"
    if token:
        return secure_links.link_url(token)
    return f"{base}/portal/work/signatures"


async def notify_signer(
    ctx: RequestContext, db: AsyncSession, signer_id: uuid.UUID, *, reminder: bool = False
) -> bool:
    """Issue the signer's single-use link and email it (+ in-app notification for principals). Skips when the
    request moved on (idempotent per state)."""
    from app.services import notifications_service as ns

    signer = await db.get(Signer, signer_id)
    if signer is None or signer.status not in ("pending", "notified"):
        return False
    req = await load_request(db, signer.request_id, for_update=True)
    if req.status not in OPEN_REQUEST_STATUSES or req.provider != "builtin":
        return False
    earlier = [s for s in await _signers(db, req.id) if s.order < signer.order and s.status != "signed"]
    if earlier:
        return False
    if not reminder and signer.notified_at is not None:
        return False
    token: str | None = None
    if signer.kind != "user":
        await secure_links.revoke_for_subject(db, SIGNER_SUBJECT, signer.id, ESIGN_LINK_PURPOSE)
        link, token = await secure_links.issue(
            db,
            tenant_id=req.tenant_id,
            purpose=ESIGN_LINK_PURPOSE,
            subject_type=SIGNER_SUBJECT,
            subject_id=signer.id,
            ttl=max(req.expires_at - now(), timedelta(minutes=5)),
            max_uses=1,
            metadata={"request_id": str(req.id)},
            created_by=req.created_by,
        )
        signer.secure_link_id = link.id
    at = now()
    if reminder:
        signer.last_reminded_at = at
    else:
        signer.notified_at = at
    signer.status = "notified"
    _event(db, req, "reminded" if reminder else "sent", signer_id=signer.id)
    await db.flush()
    name = await tenant_name(db)
    url = _signing_url(signer, token)
    subject = f"{'Reminder: please sign' if reminder else 'Please sign'}: {req.title}"
    lines = [
        f"Dear {signer.name},",
        f"{name} asks you to review and sign {req.title} ({req.human_id}).",
        f"Please sign before {req.expires_at:%d %b %Y}.",
        name,
    ]
    await send_email(
        EmailMessage(
            to=str(signer.email),
            subject=subject,
            text="\n\n".join([*lines, f"Review and sign: {url}"]),
            tenant_id=req.tenant_id,
            kind="esign_reminder" if reminder else "esign_requested",
        )
    )
    if signer.principal_id is not None and signer.kind in ("user", "candidate", "client_user", "vendor_user"):
        content = ns.Content(
            title=subject[:300],
            link_path="/documents/sign/" + str(signer.id) if signer.kind == "user" else None,
        )
        recipient: Any = (
            signer.principal_id if signer.kind == "user" else ns.Recipient(signer.kind, signer.principal_id)
        )
        try:
            await ns.deliver(
                db,
                "esign_reminder" if reminder else "esign_requested",
                [recipient],
                content,
                event_id=f"esign:{signer.id}:{'r' + at.isoformat() if reminder else 'n'}",
            )
        except Exception:  # never block the email on the in-app copy
            logger.exception("esign in-app notification failed for signer {}", signer.id)
    return True


async def notify_declined(ctx: RequestContext, db: AsyncSession, request_id: uuid.UUID) -> bool:
    from app.services import notifications_service as ns

    req = await load_request(db, request_id)
    if req.status != "declined" or req.created_by is None:
        return False
    await ns.deliver(
        db,
        "esign_declined",
        [req.created_by],
        ns.Content(title=f"Signature declined: {req.title}"[:300]),
        event_id=f"esign:declined:{req.id}",
    )
    del ctx
    return True


async def notify_completed_owner(db: AsyncSession, req: SignatureRequest) -> None:
    from app.services import notifications_service as ns

    if req.created_by is None:
        return
    await ns.deliver(
        db,
        "esign_completed",
        [req.created_by],
        ns.Content(title=f"Signed by everyone: {req.title}"[:300]),
        event_id=f"esign:completed:{req.id}",
    )


# ================================================================ beat jobs
async def send_reminders(ctx: RequestContext, db: AsyncSession) -> int:
    at = now()
    rows = list(
        (
            await db.execute(
                select(SignatureRequest).where(
                    SignatureRequest.status.in_(OPEN_REQUEST_STATUSES),
                    SignatureRequest.provider == "builtin",
                    SignatureRequest.subject_kind != "offer",
                    SignatureRequest.expires_at > at,
                )
            )
        ).scalars()
    )
    sent = 0
    for req in rows:
        current = next((s for s in await _signers(db, req.id) if s.status != "signed"), None)
        if current is None or current.status != "notified":
            continue
        last = current.last_reminded_at or current.notified_at
        if last is not None and last <= at - timedelta(days=req.reminder_every_days):
            sent += int(await notify_signer(ctx, db, current.id, reminder=True))
    return sent


async def expire_due(ctx: RequestContext, db: AsyncSession) -> int:
    rows = list(
        (
            await db.execute(
                select(SignatureRequest)
                .where(
                    SignatureRequest.status.in_(OPEN_REQUEST_STATUSES), SignatureRequest.expires_at <= now()
                )
                .with_for_update(skip_locked=True)
            )
        ).scalars()
    )
    for req in rows:
        transition(db, req, "expired", "system")
        await _close_signers(db, req)
        _event(db, req, "expired")
    await db.flush()
    del ctx
    return len(rows)


async def purge_otps(db: AsyncSession) -> int:
    from sqlalchemy import delete, or_

    cutoff = now() - timedelta(hours=24)
    result = await db.execute(
        delete(SignerOtp).where(or_(SignerOtp.consumed_at < cutoff, SignerOtp.expires_at < cutoff))
    )
    return int(result.rowcount or 0)  # type: ignore[attr-defined]


# ================================================================ external provider status (A8)
async def apply_provider_status(ctx: RequestContext, db: AsyncSession, request_id: uuid.UUID) -> str:
    """Poll a non-built-in provider and mirror its status (consumers see the same events as built-in)."""
    req = await load_request(db, request_id, for_update=True)
    if req.provider == "builtin" or req.status not in OPEN_REQUEST_STATUSES:
        return req.status
    _, adapter, _ = await _provider(db, req.provider_config_id)
    status = await adapter.get_status(req.external_ref or "")
    target = getattr(status, "status", "sent")
    if target == "completed":
        at = now()
        for s in await _signers(db, req.id):
            if s.status != "signed":
                s.status = "signed"
                db.add(
                    Signature(
                        id=uuid.uuid4(),
                        tenant_id=req.tenant_id,
                        signer_id=s.id,
                        request_id=req.id,
                        typed_name=None,
                        email=str(s.email),
                        signed_at=at,
                        doc_sha256=req.doc_sha256 or "",
                        channel="secure_link",
                        provider=req.provider,
                    )
                )
        await db.flush()
        await advance(db, req)
    elif target in ("declined", "expired", "cancelled"):
        transition(db, req, target, "system" if target != "declined" else "secure_link")
        await _close_signers(db, req)
    await db.flush()
    del ctx
    return req.status


# ================================================================ P1 offers (A9)
async def ensure_offer_request(
    db: AsyncSession,
    *,
    offer_id: uuid.UUID,
    candidate_id: uuid.UUID,
    offer_ref: str,
    version_no: int,
    letter_file_id: uuid.UUID | None,
    expires_at: datetime | None,
    require_otp: bool,
    current_request_id: uuid.UUID | None,
) -> SignatureRequest:
    """The open e-acceptance request of the released letter; a different letter cancels the prior one."""
    if letter_file_id is None:
        raise ConflictError("The offer letter is not ready", code="letter_not_ready")
    letter = await db.get(StoredFile, letter_file_id)
    if letter is None:
        raise ConflictError("The offer letter is not ready", code="letter_not_ready")
    if current_request_id is not None:
        current = await load_request(db, current_request_id, for_update=True)
        if current.status in OPEN_REQUEST_STATUSES and current.doc_sha256 == letter.sha256:
            current.require_otp = require_otp
            return current
        if current.status in (*OPEN_REQUEST_STATUSES, "draft"):
            await cancel_request(
                RequestContext(tenant_id=current.tenant_id),
                db,
                current,
                "A new offer version was released",
                actor="system",
            )
    ctx = RequestContext(tenant_id=tenant_of(db))
    doc = await repository.create_document(
        ctx,
        db,
        owner_kind="offer",
        owner_id=offer_id,
        category="offer_letter",
        title=f"Offer {offer_ref} v{version_no}",
        worker_candidate_id=candidate_id,
        retention_class="signed_agreements",
    )
    await repository.append_version(ctx, db, doc, letter, source="generated")
    req = await create_request(
        ctx,
        db,
        owner_kind="offer",
        owner_id=offer_id,
        signers=[SignerInput("candidate", candidate_id)],
        document_version_id=doc.current_version_id,
        subject_kind="offer",
        subject_id=offer_id,
        title=f"Offer {offer_ref} v{version_no}",
        expires_at=expires_at or now() + timedelta(days=30),
        send=True,
        require_otp=require_otp,
        check_access=False,
    )
    return req


async def offer_signer(db: AsyncSession, request_id: uuid.UUID) -> Signer:
    signers = await _signers(db, request_id)
    if not signers:
        raise NotFound("Offer not found")
    return signers[0]


async def decline_offer_request(db: AsyncSession, request_id: uuid.UUID | None) -> None:
    if request_id is None:
        return
    req = await load_request(db, request_id, for_update=True)
    if req.status in OPEN_REQUEST_STATUSES:
        signer = await offer_signer(db, req.id)
        signer.status = "declined"
        transition(db, req, "declined", "candidate")
        await _close_signers(db, req)
        _event(db, req, "declined", signer_id=signer.id)
        await db.flush()
