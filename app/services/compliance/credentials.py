# ruff: file-ignore[non-imperative-mood, line-too-long]
"""Credentials (Epic 21 A5, P2-8.2, FR-115, §5.7): record, renew, verify, redaction review, unmask, lists.

* Numbers: ``encrypted`` types are validated by format (Indian passport ``^[A-Z][0-9]{7}$``, PAN, otherwise <= 30
  alphanumerics) and sealed (``number`` + ``number_last4`` + ``number_bidx``, AD-35); ``aadhaar_last4`` types keep
  **only** the 4 digits (NFR-24). Every free-text field runs ``assert_no_full_aadhaar``.
* Files are ``documents`` records (category ``credential``, ``access_level='hr'``) stored through the documents
  facade. An Aadhaar image waits in ``redaction_status='pending_review'``; a rejected redaction deletes the object
  from storage immediately and the Worker is asked to re-upload.
* Renewal creates ``version_no + 1`` (``previous_id``); the previous version stays effective until the new one is
  verified ``valid``, which supersedes the chain.
* Verify / redaction decisions are human-only (``credential:verify``). Unmask needs ``identity:view`` (audited).
"""

import uuid
from collections.abc import Iterable, Sequence
from datetime import date

from sqlalchemy import column, select, table
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.context import RequestContext
from app.core.encrypted import set_identity_field
from app.core.encrypted import unmask as unmask_field
from app.core.errors import ValidationFailed
from app.core.events import emit
from app.core.identity_docs import (
    GENERIC_NUMBER_RE,
    PAN_RE,
    PASSPORT_RE,
    assert_no_full_aadhaar,
    validate_aadhaar_last4,
)
from app.core.permissions import forbidden
from app.core.sequences import next_human_id
from app.events.compliance import CredentialRecorded, CredentialVerified
from app.models.compliance import Credential, CredentialType
from app.schemas.compliance import CredentialIn, CredentialOut, RenewIn
from app.services.compliance import expiry
from app.services.compliance.common import (
    CREDENTIAL_VERIFY,
    DEFAULT_OFFSETS,
    IDENTITY_VIEW,
    RedactionRejected,
    conflict,
    get_settings_row,
    has,
    not_found,
    now,
    require_human,
    tenant_of,
    today,
)

DOCUMENT_CATEGORY = "credential"
_candidates = table("candidates", column("id"), column("full_name"))


# ================================================================ helpers
def offsets_for(t: CredentialType | None, default: Sequence[int]) -> list[int]:
    if t is not None and t.alert_offsets:
        return list(t.alert_offsets)
    return list(default or DEFAULT_OFFSETS)


def effective_status(c: Credential) -> str:
    if c.superseded_at is not None:
        return "superseded"
    if c.verification_status != "valid":
        return c.verification_status
    if c.expiry_status in ("expiring_soon", "expired"):
        return c.expiry_status
    return "valid"


def gate_status(c: Credential | None) -> str:
    """The status vocabulary of ``CredentialStatusChanged`` (placements maps it to gate states)."""
    if c is None:
        return "missing"
    s = effective_status(c)
    return "valid" if s == "expiring_soon" else s


async def candidate_names(db: AsyncSession, ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, str]:
    ids = list({i for i in ids if i})
    if not ids:
        return {}
    rows = await db.execute(
        select(_candidates.c.id, _candidates.c.full_name).where(_candidates.c.id.in_(ids))
    )
    return {r.id: r.full_name for r in rows}


async def types_by_id(db: AsyncSession) -> dict[uuid.UUID, CredentialType]:
    return {t.id: t for t in (await db.execute(select(CredentialType))).scalars()}


def to_out(c: Credential, t: CredentialType | None, name: str | None = None) -> CredentialOut:
    return CredentialOut(
        id=c.id,
        human_id=c.human_id,
        candidate_id=c.candidate_id,
        candidate_name=name,
        credential_type_id=c.credential_type_id,
        type_key=c.type_key,
        type_label=t.label if t else c.type_key,
        number_kind=t.number_kind if t else None,
        number_last4=c.number_last4,
        issuer=c.issuer,
        issued_on=c.issued_on,
        expires_on=c.expires_on,
        document_id=c.document_id,
        redaction_status=c.redaction_status,
        verification_status=c.verification_status,
        expiry_status=c.expiry_status,
        status=effective_status(c),
        version_no=c.version_no,
        previous_id=c.previous_id,
        superseded_at=c.superseded_at,
        verified_by=c.verified_by,
        verified_at=c.verified_at,
        rejection_note=c.rejection_note,
        source=c.source,
        created_at=c.created_at,
    )


async def outs(db: AsyncSession, creds: Sequence[Credential]) -> list[CredentialOut]:
    types = await types_by_id(db)
    names = await candidate_names(db, [c.candidate_id for c in creds])
    return [to_out(c, types.get(c.credential_type_id), names.get(c.candidate_id)) for c in creds]


async def load(db: AsyncSession, credential_id: uuid.UUID) -> Credential:
    c = await db.get(Credential, credential_id)
    if c is None or c.erased_at is not None:
        raise not_found("Credential")
    return c


async def _candidate_exists(db: AsyncSession, candidate_id: uuid.UUID) -> None:
    if (await db.execute(select(_candidates.c.id).where(_candidates.c.id == candidate_id))).first() is None:
        raise not_found("Candidate")


def _normalise_number(t: CredentialType, number: str | None) -> str | None:
    if t.number_kind == "none":
        if number:
            assert_no_full_aadhaar(number)
        return None
    if t.number_kind == "aadhaar_last4":
        return validate_aadhaar_last4(number)
    raw = "".join((number or "").split()).upper()
    if not raw:
        raise ValidationFailed("Enter the document number", code="number_required")
    assert_no_full_aadhaar(number)
    pattern = PASSPORT_RE if t.key == "passport" else PAN_RE if t.key == "pan_card" else GENERIC_NUMBER_RE
    if not pattern.match(raw):
        raise ValidationFailed("The document number format is not valid", code="invalid_number_format")
    return raw


async def _store_document(
    db: AsyncSession,
    candidate_id: uuid.UUID,
    t: CredentialType,
    file: tuple[bytes, str | None],
    expires_on: date | None,
) -> uuid.UUID:
    """Store through the documents facade with a system context: access was checked by ``compliance``."""
    from app.services import documents_service

    doc = await documents_service.create_document(
        RequestContext(tenant_id=tenant_of(db), actor_kind="system"),
        db,
        owner_kind="candidate",
        owner_id=candidate_id,
        data=file[0],
        filename=file[1],
        category=DOCUMENT_CATEGORY,
        title=t.label,
        access_level="hr",
        expires_on=expires_on,
    )
    return doc.id


async def _apply_fields(
    db: AsyncSession,
    c: Credential,
    t: CredentialType,
    *,
    number: str | None,
    issuer: str | None,
    issued_on: date | None,
    expires_on: date | None,
    file: tuple[bytes, str | None] | None,
) -> None:
    assert_no_full_aadhaar(issuer)
    if t.has_expiry and expires_on is None:
        raise ValidationFailed("Enter the expiry date", code="expiry_required")
    if issued_on and expires_on and expires_on < issued_on:
        raise ValidationFailed("The expiry date is before the issue date", code="invalid_dates")
    if t.requires_document and file is None:
        raise ValidationFailed("Attach the document", code="document_required")
    value = _normalise_number(t, number)
    if t.number_kind == "aadhaar_last4":
        c.number = None
        c.number_bidx = None
        c.number_last4 = value
    elif t.number_kind == "encrypted":
        set_identity_field(c, "number", value, c.tenant_id)
    c.issuer = (issuer or "").strip() or None
    c.issued_on = issued_on
    c.expires_on = expires_on if t.has_expiry else None
    if file is not None:
        c.document_id = await _store_document(db, c.candidate_id, t, file, c.expires_on)
        c.redaction_status = "pending_review" if t.number_kind == "aadhaar_last4" else None


# ================================================================ record / renew
async def record(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,
    candidate_id: uuid.UUID,
    data: CredentialIn,
    file: tuple[bytes, str | None] | None,
    *,
    source: str = "user",
) -> Credential:
    await _candidate_exists(db, candidate_id)
    from app.services.compliance.requirements import ensure_types

    await ensure_types(db)
    t = (
        await db.execute(select(CredentialType).where(CredentialType.key == data.type_key))
    ).scalar_one_or_none()
    if t is None or t.archived_at is not None:
        raise ValidationFailed("Unknown credential type", code="unknown_credential_type")
    c = Credential(
        id=uuid.uuid4(),
        tenant_id=tenant_of(db),
        human_id=await next_human_id(db, "CRD"),
        candidate_id=candidate_id,
        credential_type_id=t.id,
        type_key=t.key,
        verification_status="pending_verification",
        expiry_status="none",
        version_no=1,
        source=source,
    )
    await _apply_fields(
        db,
        c,
        t,
        number=data.number,
        issuer=data.issuer,
        issued_on=data.issued_on,
        expires_on=data.expires_on,
        file=file,
    )
    db.add(c)
    await db.flush()
    emit(
        db,
        CredentialRecorded(
            tenant_id=c.tenant_id, credential_id=c.id, candidate_id=candidate_id, type_key=t.key
        ),
    )
    await _after_change(db, c)
    return c


async def renew(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,
    credential_id: uuid.UUID,
    data: RenewIn,
    file: tuple[bytes, str | None] | None,
    *,
    source: str = "user",
    candidate_id: uuid.UUID | None = None,
) -> Credential:
    prev = await load(db, credential_id)
    if candidate_id is not None and prev.candidate_id != candidate_id:
        raise not_found("Credential")
    if prev.superseded_at is not None:
        raise conflict("credential_superseded", "Renew the latest version of this credential")
    newer = (
        await db.execute(
            select(Credential.id).where(Credential.previous_id == prev.id, Credential.erased_at.is_(None))
        )
    ).first()
    if newer is not None:
        raise conflict("renewal_pending", "A renewal of this credential is already waiting")
    t = await db.get(CredentialType, prev.credential_type_id)
    assert t is not None
    c = Credential(
        id=uuid.uuid4(),
        tenant_id=prev.tenant_id,
        human_id=await next_human_id(db, "CRD"),
        candidate_id=prev.candidate_id,
        credential_type_id=t.id,
        type_key=t.key,
        verification_status="pending_verification",
        expiry_status="none",
        version_no=prev.version_no + 1,
        previous_id=prev.id,
        source=source,
    )
    number = data.number
    if number is None and t.number_kind == "aadhaar_last4":
        number = prev.number_last4
    if number is None and t.number_kind == "encrypted" and prev.number is not None:
        from app.core.encrypted import open_sealed

        number = open_sealed(
            prev.tenant_id, prev.number
        )  # system copy of the unchanged number, never returned
    await _apply_fields(
        db,
        c,
        t,
        number=number,
        issuer=data.issuer if data.issuer is not None else prev.issuer,
        issued_on=data.issued_on,
        expires_on=data.expires_on,
        file=file,
    )
    db.add(c)
    await db.flush()
    emit(
        db,
        CredentialRecorded(
            tenant_id=c.tenant_id, credential_id=c.id, candidate_id=c.candidate_id, type_key=t.key
        ),
    )
    await _after_change(db, c)
    return c


# ================================================================ verify / redaction / unmask
async def _chain(db: AsyncSession, c: Credential) -> list[Credential]:
    out: list[Credential] = []
    prev_id = c.previous_id
    while prev_id is not None:
        prev = await db.get(Credential, prev_id)
        if prev is None:
            break
        out.append(prev)
        prev_id = prev.previous_id
    return out


async def verify(
    ctx: RequestContext, db: AsyncSession, credential_id: uuid.UUID, decision: str, note: str | None
) -> Credential:
    uid = require_human(ctx, CREDENTIAL_VERIFY)
    c = await load(db, credential_id)
    if c.verification_status != "pending_verification":
        raise conflict("credential_not_pending", "This credential was already reviewed")
    t = await db.get(CredentialType, c.credential_type_id)
    if (
        t is not None
        and t.number_kind == "aadhaar_last4"
        and c.document_id is not None
        and c.redaction_status != "confirmed"
    ):
        raise conflict("redaction_review_required", "Confirm the Aadhaar image redaction before verifying")
    assert_no_full_aadhaar(note)
    c.verified_by = uid
    c.verified_at = now()
    if decision == "valid":
        c.verification_status = "valid"
        settings = await get_settings_row(db)
        c.expiry_status = expiry.status_for(
            c.expires_on, await today(db), offsets_for(t, settings.default_alert_offsets)
        )
        for prev in await _chain(db, c):
            if prev.superseded_at is None:
                prev.superseded_at = now()
    else:
        c.verification_status = "rejected"
        c.rejection_note = (note or "").strip() or None
    await db.flush()
    audit.record(
        "credential.verified", "credential", c.id, session=db, decision=decision, type_key=c.type_key
    )
    emit(
        db,
        CredentialVerified(
            tenant_id=c.tenant_id,
            credential_id=c.id,
            candidate_id=c.candidate_id,
            type_key=c.type_key,
            verification_status=c.verification_status,
        ),
    )
    await _after_change(db, c)
    return c


async def review_redaction(
    ctx: RequestContext, db: AsyncSession, credential_id: uuid.UUID, decision: str
) -> Credential:
    require_human(ctx, CREDENTIAL_VERIFY)
    c = await load(db, credential_id)
    if c.redaction_status != "pending_review":
        raise conflict("redaction_not_pending", "There is no redaction waiting for review")
    if decision == "confirmed":
        c.redaction_status = "confirmed"
    else:
        c.redaction_status = "rejected"
        await purge_document(db, c.document_id)
        c.document_id = None
        emit(
            db,
            RedactionRejected(
                tenant_id=c.tenant_id, credential_id=c.id, candidate_id=c.candidate_id, type_key=c.type_key
            ),
        )
    await db.flush()
    audit.record("credential.redaction_reviewed", "credential", c.id, session=db, decision=decision)
    return c


async def purge_document(db: AsyncSession, document_id: uuid.UUID | None) -> int:
    """Delete every stored object of a credential document now (idempotent)."""
    if document_id is None:
        return 0
    from app.models.documents import Document, DocumentVersion
    from app.services import files_service

    purged = 0
    for v in (
        await db.execute(select(DocumentVersion).where(DocumentVersion.document_id == document_id))
    ).scalars():
        purged += int(await files_service.purge_file(db, v.file_id))
    doc = await db.get(Document, document_id)
    if doc is not None and doc.archived_at is None:
        doc.archived_at = now()
    await db.flush()
    return purged


async def unmask(ctx: RequestContext, db: AsyncSession, credential_id: uuid.UUID) -> str:
    c = await load(db, credential_id)
    t = await db.get(CredentialType, c.credential_type_id)
    if not has(ctx, IDENTITY_VIEW):
        raise forbidden(IDENTITY_VIEW)
    if t is None or t.number_kind != "encrypted" or c.number is None:
        raise conflict("nothing_to_unmask", "This credential has no stored number to show")
    return await unmask_field(ctx, db, c, "number")


async def document_bytes(
    ctx: RequestContext, db: AsyncSession, c: Credential, *, worker: bool = False
) -> tuple[bytes, str, str]:
    """(data, filename, content type) of the credential's current file. A pending Aadhaar image is readable only
    with ``identity:view`` (internal) or by its own Worker; every download is audited."""
    from app.models.documents import Document
    from app.services import files_service
    from app.services.documents import repository

    if c.document_id is None:
        raise not_found("Document")
    if c.redaction_status == "pending_review" and not worker and not has(ctx, IDENTITY_VIEW):
        raise forbidden(IDENTITY_VIEW)
    doc = await db.get(Document, c.document_id)
    if doc is None or doc.current_version_id is None:
        raise not_found("Document")
    v, data = await repository.version_bytes(db, doc.current_version_id)
    f = await files_service.get_file(db, v.file_id)
    audit.record(
        "credential.document_downloaded",
        "credential",
        c.id,
        session=db,
        redaction_status=c.redaction_status,
        by_worker=worker,
    )
    return (
        data,
        (f.original_name if f else "credential"),
        (f.content_type if f else "application/octet-stream"),
    )


# ================================================================ lists
async def list_for_candidate(db: AsyncSession, candidate_id: uuid.UUID) -> list[CredentialOut]:
    await _candidate_exists(db, candidate_id)
    creds = list(
        (
            await db.execute(
                select(Credential)
                .where(Credential.candidate_id == candidate_id, Credential.erased_at.is_(None))
                .order_by(Credential.type_key, Credential.version_no.desc())
            )
        ).scalars()
    )
    return await outs(db, creds)


async def list_credentials(
    db: AsyncSession,
    *,
    expiring_within: int | None = None,
    status: str | None = None,
    type_key: str | None = None,
    candidate_id: uuid.UUID | None = None,
    limit: int = 200,
) -> list[CredentialOut]:
    stmt = select(Credential).where(Credential.erased_at.is_(None))
    if type_key:
        stmt = stmt.where(Credential.type_key == type_key)
    if candidate_id:
        stmt = stmt.where(Credential.candidate_id == candidate_id)
    if status in ("pending_verification", "valid", "rejected"):
        stmt = stmt.where(Credential.verification_status == status, Credential.superseded_at.is_(None))
    elif status in ("expiring_soon", "expired"):
        stmt = stmt.where(Credential.expiry_status == status, Credential.verification_status == "valid")
    elif status == "redaction_review":
        stmt = stmt.where(Credential.redaction_status == "pending_review")
    if expiring_within is not None:
        from datetime import timedelta

        d = await today(db)
        stmt = stmt.where(
            Credential.verification_status == "valid",
            Credential.superseded_at.is_(None),
            Credential.expires_on.is_not(None),
            Credential.expires_on <= d + timedelta(days=expiring_within),
        )
        stmt = stmt.order_by(Credential.expires_on)
    else:
        stmt = stmt.order_by(Credential.created_at.desc())
    creds = list((await db.execute(stmt.limit(limit))).scalars())
    return await outs(db, creds)


# ================================================================ best version per (candidate, type)
async def current_versions(
    db: AsyncSession, candidate_id: uuid.UUID, type_key: str | None = None
) -> list[Credential]:
    stmt = select(Credential).where(
        Credential.candidate_id == candidate_id,
        Credential.superseded_at.is_(None),
        Credential.erased_at.is_(None),
    )
    if type_key is not None:
        stmt = stmt.where(Credential.type_key == type_key)
    return list((await db.execute(stmt)).scalars())


def best_of(
    creds: Sequence[Credential], on_date: date, offsets_by_type: dict[str, list[int]]
) -> tuple[str, Credential | None]:
    """Checklist status of a credential item from its current versions: ``met | expiring | expired | pending |
    missing`` and the credential chosen."""
    valid = [c for c in creds if c.verification_status == "valid"]
    if valid:

        def rank(c: Credential) -> tuple[int, int]:
            st = expiry.status_for(
                c.expires_on, on_date, offsets_by_type.get(c.type_key, list(DEFAULT_OFFSETS))
            )
            order = {"none": 0, "expiring_soon": 1, "expired": 2}[st]
            return order, -(c.expires_on or date.max).toordinal()

        best = sorted(valid, key=rank)[0]
        st = expiry.status_for(
            best.expires_on, on_date, offsets_by_type.get(best.type_key, list(DEFAULT_OFFSETS))
        )
        return {"none": "met", "expiring_soon": "expiring", "expired": "expired"}[st], best
    pending = [c for c in creds if c.verification_status == "pending_verification"]
    if pending:
        return "pending", sorted(pending, key=lambda c: c.created_at or now())[-1]
    return "missing", None


async def _after_change(db: AsyncSession, c: Credential) -> None:
    from app.services.compliance import checklist

    await checklist.refresh_for_candidate(db, c.candidate_id, [c.type_key])
