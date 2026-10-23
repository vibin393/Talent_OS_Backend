"""Contracts (Epic 16 A3, FR-89, PRD §5.3): MSA / SOW / Amendment / Vendor Agreement.

AD-4: the status changes only through ``transition_contract`` against the declared ``TRANSITIONS`` table;
anything else is 409 ``transition_not_allowed``. Automated actors (AI / workflow / API key) never activate,
terminate or approve a contract (``human_only_action``). Every transition writes Activity, is audited (the
row is ``AuditedMixin``) and emits ``ContractStatusChanged`` after commit.
"""

import uuid
from collections.abc import Sequence
from datetime import date
from typing import Any

from sqlalchemy import Select, and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.actors import ActorRef, actor_from_ctx, human_only_action
from app.core.context import RequestContext
from app.core.errors import NotFound
from app.core.events import emit
from app.core.sequences import next_human_id
from app.events.commercials import ContractCreated, ContractStatusChanged
from app.models.activity import Activity
from app.models.approvals import ApprovalChain
from app.models.commercials import Contract, ContractDocument, PaymentTerm
from app.models.files import StoredFile
from app.schemas.commercials import (
    ContractCreate,
    ContractDocumentOut,
    ContractDraftIn,
    ContractFacts,
    ContractHistoryItem,
    ContractOut,
    ContractRef,
    ContractSummary,
    ContractUpdate,
    ConversionFeeRule,
)
from app.services.commercials import payment_terms
from app.services.commercials.common import (
    conflict,
    invalid,
    le_today,
    legal_entity,
    now,
    require_client,
    tenant_of,
    timeline,
    user_id,
)

ENTITY = "contract"
APPROVAL_SUBJECT = "contract"
SEQUENCE_KEY = "contract"
HUMAN_ID_PREFIX = "CON"

# (from, to) -> actor kinds allowed. PRD §5.3 exactly; ``None`` is creation.
TRANSITIONS: dict[tuple[str | None, str], frozenset[str]] = {
    (None, "draft"): frozenset({"user", "system"}),
    ("draft", "pending_approval"): frozenset({"user"}),
    ("pending_approval", "draft"): frozenset({"user"}),
    ("draft", "active"): frozenset({"user", "system"}),
    ("pending_approval", "active"): frozenset({"user", "system"}),
    ("active", "expired"): frozenset({"system"}),
    ("active", "terminated"): frozenset({"user"}),
    ("expired", "active"): frozenset({"system"}),
}
AUTOMATED_KINDS = frozenset({"ai", "workflow", "api_key", "agent", "app"})  # Epic 53: +agent; Epic 66: +app
ACTIVE_EDITABLE = frozenset({"owner_id", "renewal_lead_days", "notice_period_days"})


def _actor_kind(actor: ActorRef) -> str:
    if actor.kind in ("user", "recorded_by_user"):
        return "user"
    return actor.kind


def check_transition(from_status: str | None, to_status: str, actor: ActorRef) -> None:
    allowed = TRANSITIONS.get((from_status, to_status))
    kind = _actor_kind(actor)
    if allowed is not None and kind in allowed:
        return
    if kind in AUTOMATED_KINDS and to_status in ("active", "terminated", "pending_approval", "draft"):
        raise human_only_action()
    raise conflict(
        "transition_not_allowed",
        f"A contract cannot move from {from_status} to {to_status}",
        from_status=from_status,
        to_status=to_status,
    )


async def transition_contract(
    ctx: RequestContext,
    db: AsyncSession,
    contract: Contract,
    to: str,
    actor: ActorRef,
    reason: str | None = None,
) -> Contract:
    """The only writer of ``contracts.status`` (AD-4)."""  # ruff: ignore[non-imperative-mood]
    from_status = contract.status
    check_transition(from_status, to, actor)
    contract.status = to
    at = now()
    if to == "active":
        contract.activated_at = at
    elif to == "terminated":
        contract.terminated_at = at
    await db.flush()
    data: dict[str, Any] = {"from": from_status, "to": to, "actor_kind": actor.kind}
    if reason:
        data["reason"] = reason[:500]
    await timeline(ctx, db, ENTITY, contract.id, f"{contract.human_id}: {from_status} -> {to}", data)
    emit(
        db,
        ContractStatusChanged(
            tenant_id=contract.tenant_id,
            contract_id=contract.id,
            client_id=contract.client_id,
            vendor_id=contract.vendor_id,
            from_status=from_status,
            to_status=to,
            end_date=contract.end_date,
        ),
    )
    return contract


# ================================================================ reads
async def get_row(db: AsyncSession, contract_id: uuid.UUID, *, lock: bool = False) -> Contract:
    stmt = select(Contract).where(Contract.id == contract_id)
    if lock:
        stmt = stmt.with_for_update()
    row = (await db.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise NotFound("Contract not found")
    return row


async def chain_configured(db: AsyncSession) -> bool:
    stmt = select(ApprovalChain.id).where(
        ApprovalChain.subject_type == APPROVAL_SUBJECT, ApprovalChain.is_active.is_(True)
    )
    return (await db.execute(stmt)).first() is not None


async def _out(db: AsyncSession, row: Contract, *, detail: bool = False) -> ContractOut:
    out = ContractOut.model_validate(row)
    update: dict[str, Any] = {"approval_chain_configured": await chain_configured(db)}
    if detail:
        children = (
            await db.execute(
                select(Contract)
                .where(Contract.parent_contract_id == row.id)
                .order_by(Contract.start_date, Contract.human_id)
            )
        ).scalars()
        update["children"] = [ContractRef.model_validate(c) for c in children]
    return out.model_copy(update=update)


def _summary(row: Contract) -> ContractSummary:
    return ContractSummary(
        id=row.id,
        human_id=row.human_id,
        contract_type=row.contract_type,
        client_id=row.client_id,
        vendor_id=row.vendor_id,
        legal_entity_id=row.legal_entity_id,
        status=row.status,
        start_date=row.start_date,
        end_date=row.end_date,
    )


async def list_contracts(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,
    *,
    client_id: uuid.UUID | None = None,
    vendor_id: uuid.UUID | None = None,
    contract_type: str | None = None,
    status: str | None = None,
    expiring_within_days: int | None = None,
) -> list[ContractOut]:
    stmt: Select[tuple[Contract]] = select(Contract).where(Contract.archived_at.is_(None))
    if client_id is not None:
        stmt = stmt.where(Contract.client_id == client_id)
    if vendor_id is not None:
        stmt = stmt.where(Contract.vendor_id == vendor_id)
    if contract_type:
        stmt = stmt.where(Contract.contract_type == contract_type)
    if status:
        stmt = stmt.where(Contract.status == status)
    if expiring_within_days is not None:
        from datetime import timedelta

        today = await le_today(db, None)
        stmt = stmt.where(
            Contract.status == "active",
            Contract.end_date.is_not(None),
            Contract.end_date <= today + timedelta(days=expiring_within_days),
        )
    rows = (await db.execute(stmt.order_by(Contract.created_at.desc()).limit(500))).scalars()
    configured = await chain_configured(db)
    return [
        ContractOut.model_validate(r).model_copy(update={"approval_chain_configured": configured})
        for r in rows
    ]


async def get_contract_out(ctx: RequestContext, db: AsyncSession, contract_id: uuid.UUID) -> ContractOut:  # ruff: ignore[unused-function-argument]
    return await _out(db, await get_row(db, contract_id), detail=True)


# ================================================================ validation
async def _check_parent(db: AsyncSession, row: Contract, *, strict: bool) -> Contract | None:
    """SOW / amendment need a parent of the same counterparty and LE that is not terminated. ``strict``
    (submit / activate) refuses a missing parent; create accepts it (CRM drafts)."""
    if row.contract_type not in ("sow", "amendment"):
        if row.parent_contract_id is not None:
            raise invalid("parent_contract_invalid", "Only SOWs and amendments have a parent contract")
        return None
    if row.parent_contract_id is None:
        if strict:
            raise invalid("parent_contract_invalid", "A SOW or amendment needs its parent contract")
        return None
    parent = (
        await db.execute(select(Contract).where(Contract.id == row.parent_contract_id))
    ).scalar_one_or_none()
    if (
        parent is None
        or parent.id == row.id
        or parent.client_id != row.client_id
        or parent.vendor_id != row.vendor_id
        or parent.legal_entity_id != row.legal_entity_id
        or parent.status == "terminated"
    ):
        raise invalid(
            "parent_contract_invalid", "The parent contract must belong to the same party and legal entity"
        )
    # Epic 45 (AD-51, FR-227): a SOW submitted / activated sits under an Active MSA.
    if strict and row.contract_type == "sow" and (parent.contract_type != "msa" or parent.status != "active"):
        raise invalid(
            "parent_contract_invalid", "A SOW needs an Active MSA of the same client and legal entity"
        )
    return parent


def check_sow_terms(row: Contract) -> None:
    """Epic 45 (FR-227): a SOW needs its pricing model and total value (``value_cap``) before it is submitted
    or activated; drafts may be incomplete (422 ``sow_terms_required`` with ``details.fields``)."""
    if row.contract_type != "sow":
        return
    missing = [
        {"field": f, "message": m}
        for f, m, v in (
            ("pricing_model", "Choose the pricing model", row.pricing_model),
            ("value_cap", "Give the SOW total value", row.value_cap),
        )
        if v is None
    ]
    if missing:
        raise invalid("sow_terms_required", "The SOW needs its pricing model and total value", fields=missing)


def _check_fields(values: dict[str, Any], contract_type: str) -> None:
    start, end = values.get("start_date"), values.get("end_date")
    if start is not None and end is not None and end < start:
        raise invalid("contract_dates_invalid", "The end date must be on or after the start date")
    if values.get("pricing_model") is not None and contract_type != "sow":
        raise invalid("pricing_model_sow_only", "Only SOWs have a pricing model")
    if values.get("amends_end_date") is not None and contract_type != "amendment":
        raise invalid("amends_end_date_invalid", "Only amendments change the parent's end date")
    if values.get("conversion_fee_kind", "none") != "none" and values.get("conversion_fee_value") is None:
        raise invalid("conversion_fee_value_required", "Give the conversion fee value")


async def _new_contract(
    ctx: RequestContext, db: AsyncSession, values: dict[str, Any], actor: ActorRef
) -> Contract:
    check_transition(None, "draft", actor)
    ctype = values["contract_type"]
    client_id, vendor_id = values.get("client_id"), values.get("vendor_id")
    if (client_id is None) == (vendor_id is None):
        raise invalid("counterparty_required", "A contract has exactly one counterparty (client or vendor)")
    if (ctype == "vendor_agreement") != (vendor_id is not None):
        raise invalid("counterparty_invalid", "Vendor agreements (only) are signed with a vendor")
    if client_id is not None:
        await require_client(db, client_id)
    await legal_entity(db, values["legal_entity_id"])
    await payment_terms.assert_usable(db, values.get("payment_terms_id"))
    _check_fields(values, ctype)
    row = Contract(
        id=uuid.uuid4(),
        tenant_id=tenant_of(db),
        human_id=await next_human_id(db, SEQUENCE_KEY, HUMAN_ID_PREFIX),
        counterparty_kind="vendor" if vendor_id is not None else "client",
        status="draft",
        **values,
    )
    if row.owner_id is None:
        row.owner_id = user_id(ctx)
    await _check_parent(db, row, strict=False)
    db.add(row)
    await db.flush()
    await timeline(
        ctx, db, ENTITY, row.id, f"{row.human_id}: contract created ({ctype})", {"actor_kind": actor.kind}
    )
    emit(
        db,
        ContractCreated(
            tenant_id=row.tenant_id,
            contract_id=row.id,
            client_id=row.client_id,
            vendor_id=row.vendor_id,
            contract_type=ctype,
        ),
    )
    return row


async def create_contract(ctx: RequestContext, db: AsyncSession, data: ContractCreate) -> ContractOut:
    row = await _new_contract(ctx, db, data.model_dump(), actor_from_ctx(ctx))
    await db.refresh(row)
    return await _out(db, row, detail=True)


async def create_contract_draft(
    ctx: RequestContext, db: AsyncSession, data: ContractDraftIn
) -> ContractSummary:
    """Contract-layer stub (CRM Won conversion): a ``draft`` linked to its opportunity, with draft documents."""  # ruff: ignore[line-too-long]
    actor = actor_from_ctx(ctx)
    if _actor_kind(actor) not in ("user", "system"):
        actor = ActorRef("system", None)
    le = await legal_entity(db, data.legal_entity_id)
    values: dict[str, Any] = {
        "contract_type": data.contract_type,
        "client_id": data.client_id,
        "legal_entity_id": data.legal_entity_id,
        "parent_contract_id": data.parent_contract_id,
        "title": data.title,
        "start_date": data.start_date,
        "end_date": data.end_date,
        "currency": le.default_currency or "INR",
        "opportunity_id": data.source_opportunity_id,
    }
    row = await _new_contract(ctx, db, values, actor)
    for file_id in dict.fromkeys(data.document_file_ids):
        if (await db.execute(select(StoredFile.id).where(StoredFile.id == file_id))).first() is None:
            raise invalid("file_invalid", "Unknown document file", file_id=str(file_id))
        db.add(
            ContractDocument(
                id=uuid.uuid4(),
                tenant_id=row.tenant_id,
                client_id=row.client_id,
                vendor_id=row.vendor_id,
                contract_id=row.id,
                file_id=file_id,
                kind="draft",
                uploaded_by=user_id(ctx),
            )
        )
    await db.flush()
    return _summary(row)


async def update_contract(
    ctx: RequestContext, db: AsyncSession, contract_id: uuid.UUID, data: ContractUpdate
) -> ContractOut:
    row = await get_row(db, contract_id, lock=True)
    changes = data.model_dump(exclude_unset=True)
    if row.status == "active":
        refused = sorted(set(changes) - ACTIVE_EDITABLE)
        if refused:
            raise conflict(
                "contract_not_editable",
                "Only the owner, renewal lead days and notice period change on an active contract",
                fields=refused,
            )
    elif row.status != "draft":
        raise conflict("contract_not_editable", "Only draft contracts can be edited", status=row.status)
    if "payment_terms_id" in changes:
        await payment_terms.assert_usable(db, changes["payment_terms_id"])
    merged = {
        "start_date": changes.get("start_date", row.start_date),
        "end_date": changes.get("end_date", row.end_date),
        "amends_end_date": changes.get("amends_end_date"),
        "conversion_fee_kind": changes.get("conversion_fee_kind", row.conversion_fee_kind),
        "conversion_fee_value": changes.get("conversion_fee_value", row.conversion_fee_value),
        "pricing_model": changes.get("pricing_model"),
    }
    _check_fields(merged, row.contract_type)
    for key, value in changes.items():
        setattr(row, key, value)
    await _check_parent(db, row, strict=False)
    await db.flush()
    await timeline(ctx, db, ENTITY, row.id, f"{row.human_id}: details updated", {"fields": sorted(changes)})
    await db.refresh(row)
    return await _out(db, row, detail=True)


# ================================================================ approval / activation
def _facts(row: Contract) -> dict[str, Any]:
    return {
        "contract": row.human_id,
        "contract_type": row.contract_type,
        "client_id": str(row.client_id) if row.client_id else None,
        "vendor_id": str(row.vendor_id) if row.vendor_id else None,
        "legal_entity_id": str(row.legal_entity_id),
        "value_cap": str(row.value_cap) if row.value_cap is not None else None,
        "currency": row.currency,
    }


async def submit_for_approval(ctx: RequestContext, db: AsyncSession, contract_id: uuid.UUID) -> ContractOut:
    from app.services import approvals

    actor = actor_from_ctx(ctx)
    row = await get_row(db, contract_id, lock=True)
    check_transition(row.status, "pending_approval", actor)
    if not await chain_configured(db):
        raise conflict(
            "approval_chain_missing",
            "No contract approval chain is configured; activate the contract directly",
        )
    await _check_parent(db, row, strict=True)
    check_sow_terms(row)
    result = await approvals.start(ctx, db, APPROVAL_SUBJECT, row.id, _facts(row))
    row.approval_request_id = result.request_id
    if result.status == "auto_approved":
        row.approved_at = now()
        await db.flush()
        await timeline(ctx, db, ENTITY, row.id, f"{row.human_id}: approval auto-approved", None)
        if row.signed_file_id is not None:
            await _activate(ctx, db, row, actor)
    else:
        row.approved_at = None
        await transition_contract(ctx, db, row, "pending_approval", actor)
    await db.refresh(row)
    return await _out(db, row, detail=True)


async def _apply_amendment(ctx: RequestContext, db: AsyncSession, row: Contract) -> None:
    if row.contract_type != "amendment" or row.amends_end_date is None or row.parent_contract_id is None:
        return
    parent = await get_row(db, row.parent_contract_id, lock=True)
    parent.end_date = row.amends_end_date
    await db.flush()
    await timeline(
        ctx,
        db,
        ENTITY,
        parent.id,
        f"{parent.human_id}: end date extended by {row.human_id}",
        {"end_date": row.amends_end_date.isoformat()},
    )
    today = await le_today(db, parent.legal_entity_id)
    if parent.status == "expired" and row.amends_end_date >= today:
        await transition_contract(
            ctx, db, parent, "active", ActorRef("system", None), reason=f"amended by {row.human_id}"
        )


async def _activate(ctx: RequestContext, db: AsyncSession, row: Contract, actor: ActorRef) -> None:
    from app.services import legal_entities_service

    check_transition(row.status, "active", actor)
    if row.signed_file_id is None:
        raise conflict("signed_document_required", "Upload the counter-signed document before activating")
    if await chain_configured(db) and row.approved_at is None:
        raise conflict("approval_pending", "The contract has not been approved yet")
    await _check_parent(db, row, strict=True)
    check_sow_terms(row)
    await legal_entities_service.assert_accepts_new_documents(ctx, db, row.legal_entity_id)
    await transition_contract(ctx, db, row, "active", actor)
    await _apply_amendment(ctx, db, row)


async def activate(ctx: RequestContext, db: AsyncSession, contract_id: uuid.UUID) -> ContractOut:
    row = await get_row(db, contract_id, lock=True)
    await _activate(ctx, db, row, actor_from_ctx(ctx))
    await db.refresh(row)
    return await _out(db, row, detail=True)


async def try_system_activation(ctx: RequestContext, db: AsyncSession, row: Contract) -> bool:
    """Signature completion: activate as the system when the guards pass; otherwise leave it as is."""
    if row.status not in ("draft", "pending_approval"):
        return False
    if await chain_configured(db) and row.approved_at is None:
        return False
    try:
        await _activate(ctx, db, row, ActorRef("system", None))
    except Exception:
        return False
    return True


async def on_final_decided(ctx: RequestContext, db: AsyncSession, decision: Any) -> None:
    """Approval engine callback (same transaction as the decision)."""
    row = await get_row(db, decision.subject_id, lock=True)
    actor: ActorRef = decision.actor or ActorRef("system", None)
    if decision.outcome in ("approved", "auto_approved"):
        row.approved_at = now()
        await db.flush()
        await timeline(ctx, db, ENTITY, row.id, f"{row.human_id}: approved", {"actor_kind": actor.kind})
        if row.signed_file_id is not None and row.status in ("draft", "pending_approval"):
            await _activate(
                ctx, db, row, actor if actor.kind != "recorded_by_user" else ActorRef("user", actor.id)
            )
    elif row.status == "pending_approval":
        await transition_contract(ctx, db, row, "draft", ActorRef("user", actor.id), reason=decision.comment)


async def describe(ctx: RequestContext, db: AsyncSession, ids: list[uuid.UUID]) -> dict[uuid.UUID, Any]:  # ruff: ignore[unused-function-argument]
    from app.schemas.approvals import SubjectSummary

    rows = (await db.execute(select(Contract).where(Contract.id.in_(ids)))).scalars()
    return {r.id: SubjectSummary(label=r.human_id, title=r.title) for r in rows}


async def terminate(
    ctx: RequestContext, db: AsyncSession, contract_id: uuid.UUID, reason: str, termination_date: date
) -> ContractOut:
    row = await get_row(db, contract_id, lock=True)
    check_transition(row.status, "terminated", actor_from_ctx(ctx))
    row.termination_reason = reason
    row.termination_date = termination_date
    await transition_contract(ctx, db, row, "terminated", actor_from_ctx(ctx), reason=reason)
    await db.refresh(row)
    return await _out(db, row, detail=True)


# ================================================================ documents
def _doc_out(doc: ContractDocument, name: str | None) -> ContractDocumentOut:
    return ContractDocumentOut.model_validate(doc).model_copy(update={"file_name": name})


async def list_documents(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,
    contract_id: uuid.UUID,
) -> list[ContractDocumentOut]:
    await get_row(db, contract_id)
    rows = (
        await db.execute(
            select(ContractDocument, StoredFile.original_name)
            .join(StoredFile, StoredFile.id == ContractDocument.file_id)
            .where(ContractDocument.contract_id == contract_id)
            .order_by(ContractDocument.created_at)
        )
    ).all()
    return [_doc_out(d, n) for d, n in rows]


async def _mark_signed(ctx: RequestContext, db: AsyncSession, row: Contract, file_id: uuid.UUID) -> None:
    row.signed_file_id = file_id
    await db.flush()
    await timeline(
        ctx, db, ENTITY, row.id, f"{row.human_id}: signed document attached", {"file_id": str(file_id)}
    )


async def add_document(
    ctx: RequestContext,
    db: AsyncSession,
    contract_id: uuid.UUID,
    data: bytes,
    filename: str | None,
    kind: str,
) -> ContractDocumentOut:
    from app.services import files_service

    row = await get_row(db, contract_id, lock=True)
    if row.status in ("terminated", "expired") and kind == "signed":
        raise conflict("contract_closed", "This contract is closed", status=row.status)
    stored = await files_service.store_upload(ctx, db, data, filename, {"pdf", "docx"}, ENTITY, row.id)
    doc = ContractDocument(
        id=uuid.uuid4(),
        tenant_id=row.tenant_id,
        client_id=row.client_id,
        vendor_id=row.vendor_id,
        contract_id=row.id,
        file_id=stored.id,
        kind=kind,
        uploaded_by=user_id(ctx),
    )
    db.add(doc)
    await db.flush()
    if kind == "signed":
        await _mark_signed(ctx, db, row, stored.id)
    await db.refresh(doc)
    return _doc_out(doc, stored.original_name)


async def mark_document_signed(
    ctx: RequestContext, db: AsyncSession, document_id: uuid.UUID
) -> ContractDocumentOut:
    doc = (
        await db.execute(select(ContractDocument).where(ContractDocument.id == document_id).with_for_update())
    ).scalar_one_or_none()
    if doc is None:
        raise NotFound("Contract document not found")
    row = await get_row(db, doc.contract_id, lock=True)
    doc.kind = "signed"
    await db.flush()
    await _mark_signed(ctx, db, row, doc.file_id)
    await db.refresh(doc)
    return _doc_out(doc, None)


async def delete_document(ctx: RequestContext, db: AsyncSession, document_id: uuid.UUID) -> None:
    doc = (
        await db.execute(select(ContractDocument).where(ContractDocument.id == document_id).with_for_update())
    ).scalar_one_or_none()
    if doc is None:
        raise NotFound("Contract document not found")
    row = await get_row(db, doc.contract_id, lock=True)
    if row.signed_file_id == doc.file_id and row.status not in ("draft", "pending_approval"):
        raise conflict(
            "signed_document_locked", "The signed document of an active or closed contract cannot be removed"
        )
    if row.signed_file_id == doc.file_id:
        row.signed_file_id = None
    await db.delete(doc)
    await db.flush()
    await timeline(
        ctx, db, ENTITY, row.id, f"{row.human_id}: document removed", {"file_id": str(doc.file_id)}
    )


async def request_signature(
    ctx: RequestContext,
    db: AsyncSession,
    contract_id: uuid.UUID,
    signers: Sequence[dict[str, Any]],
    document_file_id: uuid.UUID | None,
) -> ContractOut:
    """AD-28: delegate to ``documents_service`` when the documents epic has filled it; else 409 ``esign_unavailable``."""  # ruff: ignore[line-too-long]
    from app.schemas.documents import SignatureRequestCreateIn, SignerSpec
    from app.services import documents_service

    row = await get_row(db, contract_id, lock=True)
    if row.status not in ("draft", "pending_approval"):
        raise conflict(
            "contract_not_signable",
            "Only draft or pending contracts are sent for signature",
            status=row.status,
        )
    create = getattr(documents_service, "create_signature_request", None)
    document_id = await _document_for_file(db, document_file_id, row.id)
    if document_id is None and document_file_id is not None:
        document_id = await _link_contract_document(ctx, db, row, document_file_id)
    if create is None or document_id is None:
        raise conflict("esign_unavailable", "Electronic signature is not available for this document")
    try:
        summary = await create(
            ctx,
            db,
            SignatureRequestCreateIn(
                document_id=document_id,
                subject_kind=ENTITY,
                subject_id=row.id,
                owner_kind=ENTITY,
                owner_id=row.id,
                signers=[SignerSpec(**s) for s in signers],
            ),
        )
    except NotImplementedError as exc:
        raise conflict("esign_unavailable", "Electronic signature is not available yet") from exc
    row.signature_request_id = summary.id
    await db.flush()
    await timeline(
        ctx,
        db,
        ENTITY,
        row.id,
        f"{row.human_id}: sent for signature",
        {"signature_request_id": str(summary.id)},
    )
    await db.refresh(row)
    return await _out(db, row, detail=True)


async def _link_contract_document(
    ctx: RequestContext, db: AsyncSession, row: Contract, file_id: uuid.UUID
) -> uuid.UUID | None:
    """Create a documents-epic row for a file uploaded to this contract (``contract_documents``).

    The row is owned by the contract with that file as its first version, so it can be sent for eSign.
    Returns None if the file is not this contract's.
    """
    linked = (
        await db.execute(
            select(ContractDocument.id).where(
                ContractDocument.contract_id == row.id, ContractDocument.file_id == file_id
            )
        )
    ).first()
    stored = await db.get(StoredFile, file_id)
    if linked is None or stored is None:
        return None
    from app.services.documents import repository as documents_repository

    doc = await documents_repository.create_document(
        ctx,
        db,
        owner_kind=ENTITY,
        owner_id=row.id,
        category="contract",
        title=stored.original_name or f"{row.human_id} contract",
        retention_class="signed_agreements",
    )
    await documents_repository.append_version(ctx, db, doc, stored, source="upload")
    return doc.id


async def _document_for_file(
    db: AsyncSession, file_id: uuid.UUID | None, contract_id: uuid.UUID
) -> uuid.UUID | None:
    """This contract's documents-epic ``documents`` row carrying ``file_id`` (feature-detected: None until it
    exists)."""  # ruff: ignore[non-imperative-mood]
    if file_id is None:
        return None
    from sqlalchemy import text

    try:
        async with db.begin_nested():
            found = (
                await db.execute(
                    text(
                        "SELECT d.id FROM documents d JOIN document_versions v ON v.document_id = d.id "
                        "WHERE v.file_id = :f AND d.owner_kind = 'contract' AND d.owner_id = :c "
                        "ORDER BY v.version_no DESC LIMIT 1"
                    ),
                    {"f": file_id, "c": contract_id},
                )
            ).scalar_one_or_none()
    except Exception:
        return None
    return found


async def on_signature_completed(
    ctx: RequestContext,
    db: AsyncSession,
    contract_id: uuid.UUID,
    request_id: uuid.UUID,
    file_id: uuid.UUID | None,
) -> bool:
    """``SignatureRequestCompleted(subject_kind="contract")``: store the request, the final file as the signed
    document and attempt activation (system)."""
    row = (
        await db.execute(select(Contract).where(Contract.id == contract_id).with_for_update())
    ).scalar_one_or_none()
    if row is None:
        return False
    row.signature_request_id = request_id
    if file_id is not None:
        db.add(
            ContractDocument(
                id=uuid.uuid4(),
                tenant_id=row.tenant_id,
                client_id=row.client_id,
                vendor_id=row.vendor_id,
                contract_id=row.id,
                file_id=file_id,
                kind="signed",
                uploaded_by=None,
            )
        )
        await db.flush()
        await _mark_signed(ctx, db, row, file_id)
    return await try_system_activation(ctx, db, row)


async def history(ctx: RequestContext, db: AsyncSession, contract_id: uuid.UUID) -> list[ContractHistoryItem]:  # ruff: ignore[unused-function-argument]
    await get_row(db, contract_id)
    rows = (
        await db.execute(
            select(Activity)
            .where(Activity.entity_type == ENTITY, Activity.entity_id == contract_id)
            .order_by(Activity.occurred_at.desc())
            .limit(200)
        )
    ).scalars()
    return [
        ContractHistoryItem(
            id=r.id, occurred_at=r.occurred_at, body=r.body, data=r.data, author_id=r.author_id
        )
        for r in rows
    ]


# ================================================================ public helpers (placements / billing / extensions)  # ruff: ignore[line-too-long]
def _active_on(on_date: date) -> Any:
    return and_(
        Contract.status == "active",
        Contract.start_date <= on_date,
        or_(Contract.end_date.is_(None), Contract.end_date >= on_date),
    )


async def active_contract_for(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,
    client_id: uuid.UUID,
    legal_entity_id: uuid.UUID,
    on_date: date,
) -> ContractSummary | None:
    """The Active MSA / SOW of the client + LE on ``on_date`` (SOW preferred, then the latest start)."""  # ruff: ignore[non-imperative-mood]
    rows = (
        await db.execute(
            select(Contract)
            .where(
                Contract.client_id == client_id,
                Contract.legal_entity_id == legal_entity_id,
                Contract.contract_type.in_(("msa", "sow")),
                _active_on(on_date),
            )
            .order_by((Contract.contract_type == "sow").desc(), Contract.start_date.desc())
            .limit(1)
        )
    ).scalars()
    row = next(iter(rows), None)
    return None if row is None else _summary(row)


async def assert_contract_active(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,
    contract_id: uuid.UUID,
    client_id: uuid.UUID,
    legal_entity_id: uuid.UUID | None,
    on_date: date,
) -> ContractSummary:
    row = (
        await db.execute(select(Contract).where(Contract.id == contract_id, _active_on(on_date)))
    ).scalar_one_or_none()
    if (
        row is None
        or row.client_id != client_id
        or (legal_entity_id is not None and row.legal_entity_id != legal_entity_id)
    ):
        raise invalid(
            "contract_not_active",
            "The contract is not active for this client and legal entity",
            contract_id=str(contract_id),
        )
    return _summary(row)


async def get_contract(ctx: RequestContext, db: AsyncSession, contract_id: uuid.UUID) -> ContractFacts:  # ruff: ignore[unused-function-argument]
    row = await get_row(db, contract_id)
    days = None
    if row.payment_terms_id is not None:
        days = (
            await db.execute(select(PaymentTerm.days).where(PaymentTerm.id == row.payment_terms_id))
        ).scalar_one_or_none()
    return ContractFacts(
        id=row.id,
        human_id=row.human_id,
        contract_type=row.contract_type,
        client_id=row.client_id,
        vendor_id=row.vendor_id,
        legal_entity_id=row.legal_entity_id,
        status=row.status,
        start_date=row.start_date,
        end_date=row.end_date,
        currency=row.currency,
        notice_period_days=row.notice_period_days,
        payment_terms_days=days,
        placement_fee_pct=row.placement_fee_pct,
        reverse_charge=row.reverse_charge,
        share_bgv_outcome=row.share_bgv_outcome,
        conversion_fee_rule=ConversionFeeRule(
            kind=row.conversion_fee_kind,
            value=row.conversion_fee_value if row.conversion_fee_value is not None else None,
            waive_after_months=row.conversion_fee_waive_after_months,
        ),
    )


async def shares_bgv_outcome(ctx: RequestContext, db: AsyncSession, contract_id: uuid.UUID) -> bool:  # ruff: ignore[unused-function-argument]
    found = (
        await db.execute(select(Contract.share_bgv_outcome).where(Contract.id == contract_id))
    ).scalar_one_or_none()
    return bool(found)


# ================================================================ beat jobs
async def expire_due(ctx: RequestContext, db: AsyncSession, at: Any = None) -> int:
    """Active -> Expired once ``end_date`` < the LE's local today (system)."""
    from app.core.fiscal import local_date
    from app.models.legal_entities import LegalEntity

    rows = (
        await db.execute(
            select(Contract, LegalEntity.timezone)
            .join(LegalEntity, LegalEntity.id == Contract.legal_entity_id)
            .where(Contract.status == "active", Contract.end_date.is_not(None))
            .with_for_update(of=Contract)
        )
    ).all()
    count = 0
    for row, tz in rows:
        if row.end_date is not None and row.end_date < local_date(at or now(), tz or "Asia/Kolkata"):
            await transition_contract(ctx, db, row, "expired", ActorRef("system", None))
            count += 1
    return count


async def renewal_reminders(ctx: RequestContext, db: AsyncSession, at: Any = None) -> int:  # ruff: ignore[unused-function-argument]
    """One reminder per (contract, end_date, lead day) when the end is within the lead window."""
    from sqlalchemy.dialects.postgresql import insert

    from app.core.fiscal import local_date
    from app.events.commercials import ContractRenewalDue
    from app.models.commercials import ContractReminder
    from app.models.legal_entities import LegalEntity
    from app.services.commercials import settings as settings_mod

    settings = await settings_mod.get_row(db)
    rows = (
        await db.execute(
            select(Contract, LegalEntity.timezone)
            .join(LegalEntity, LegalEntity.id == Contract.legal_entity_id)
            .where(Contract.status == "active", Contract.end_date.is_not(None))
        )
    ).all()
    sent = 0
    for row, tz in rows:
        assert row.end_date is not None
        today = local_date(at or now(), tz or "Asia/Kolkata")
        days_left = (row.end_date - today).days
        if days_left < 0:
            continue
        leads = sorted(row.renewal_lead_days or settings.contract_renewal_lead_days)
        due = [d for d in leads if days_left <= d]
        if not due:
            continue
        lead = min(due)  # the closest crossed lead day; earlier ones were (or would have been) sent before
        result = await db.execute(
            insert(ContractReminder)
            .values(
                id=uuid.uuid4(),
                tenant_id=row.tenant_id,
                client_id=row.client_id,
                contract_id=row.id,
                end_date=row.end_date,
                lead_days=lead,
            )
            .on_conflict_do_nothing(index_elements=["contract_id", "end_date", "lead_days"])
            .returning(ContractReminder.id)
        )
        if result.scalar_one_or_none() is None:
            continue
        sent += 1
        emit(
            db,
            ContractRenewalDue(
                tenant_id=row.tenant_id,
                contract_id=row.id,
                client_id=row.client_id,
                owner_id=row.owner_id,
                end_date=row.end_date,
                lead_days=lead,
            ),
        )
    return sent
