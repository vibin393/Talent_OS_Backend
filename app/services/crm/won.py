# ruff: file-ignore[line-too-long]
"""Won conversion (FR-86, A6; story 2.4).

``won_preview`` proposes the records (no writes). ``win_opportunity`` runs in ONE transaction (the router adds
the ``Idempotency-Key`` guard): lock the Opportunity (stage must be open) -> stage ``won`` -> Client ``active``
(``clients_service``) -> Draft Contract (``commercials_service.create_contract_draft``) -> draft Rate Card
(``commercials_service.copy_rate_card_template``) -> draft Demands per selected line (``services/demands``,
owned by the Account Manager) -> ``crm_record_links`` -> ``OpportunityWon``. Any failure rolls everything back
(the error carries ``details.step`` and, for demands, ``details.line_id``).
"""

import uuid
from collections.abc import Awaitable, Callable
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, TypeVar

from pydantic import ValidationError as PydanticValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.errors import ConflictError, DomainError, ValidationFailed
from app.core.events import emit
from app.events.crm import OpportunityWon, OpportunityWonHandoff
from app.models.clients import Client
from app.models.crm import OPEN_STAGES, CrmRecordLink, Opportunity
from app.models.identity import BusinessUnit, User
from app.models.legal_entities import LegalEntity
from app.schemas.clients import ClientUpdate
from app.schemas.crm import (
    CreatedRecord,
    LegalEntityRef,
    RecordLinkOut,
    WinIn,
    WinOut,
    WonPreviewClient,
    WonPreviewContract,
    WonPreviewDemand,
    WonPreviewOut,
    WonPreviewTemplate,
)
from app.services import clients_service, entity_refs
from app.services.crm import opportunities
from app.services.crm.common import OPP_READ, OPP_WRITE, money_str, now, principal_of

T = TypeVar("T")


def _commercials() -> Any | None:
    """Epic 16 facade (guarded: the Won conversion still activates the Client without it)."""
    return entity_refs.optional_service("app.services.commercials_service")


def contract_type_for(service: str) -> str:
    return "sow" if service == "project" else "msa"


async def _default_legal_entity(db: AsyncSession, owner_id: uuid.UUID) -> uuid.UUID | None:
    bu_le = (
        await db.execute(
            select(BusinessUnit.default_legal_entity_id)
            .join(User, User.business_unit_id == BusinessUnit.id)
            .where(User.id == owner_id)
        )
    ).scalar_one_or_none()
    if bu_le is not None:
        return bu_le
    return (
        await db.execute(
            select(LegalEntity.id)
            .where(LegalEntity.status == "active")
            .order_by(LegalEntity.created_at)
            .limit(1)
        )
    ).scalar_one_or_none()


async def _templates(db: AsyncSession) -> list[WonPreviewTemplate]:
    try:
        from app.models.commercials import RateCard
    except ImportError:  # pragma: no cover - Epic 16 absent
        return []
    rows = await db.execute(
        select(RateCard.id, RateCard.name)
        .where(RateCard.is_template.is_(True), RateCard.status.in_(("draft", "published")))
        .order_by(RateCard.name)
    )
    return [WonPreviewTemplate(id=i, name=n) for i, n in rows.tuples()]


def _require_open(opp: Opportunity) -> None:
    if opp.stage not in OPEN_STAGES:
        raise ConflictError(
            "Only an open opportunity can be won",
            code="transition_not_allowed",
            details={"from": opp.stage, "to": "won"},
        )


async def won_preview(ctx: RequestContext, db: AsyncSession, opp_id: uuid.UUID) -> WonPreviewOut:
    principal_of(ctx, OPP_WRITE)
    opp = await opportunities.get_row(ctx, db, opp_id, OPP_WRITE)
    _require_open(opp)
    client = await db.get(Client, opp.client_id)
    assert client is not None
    ctype = contract_type_for(opp.service)
    les = await db.execute(
        select(LegalEntity.id, LegalEntity.name)
        .where(LegalEntity.status == "active")
        .order_by(LegalEntity.name)
    )
    lines = (await opportunities.lines_of(db, [opp.id]))[opp.id]
    return WonPreviewOut(
        opportunity_id=opp.id,
        client=WonPreviewClient(
            id=client.id,
            name=client.name,
            current_status=client.status,
            will_activate=client.status != "active",
        ),
        contract=WonPreviewContract(
            type=ctype,
            title=f"{client.name} {ctype.upper()}",
            legal_entity_id=await _default_legal_entity(db, opp.owner_id),
            start_date=now().date(),
        ),
        legal_entities=[LegalEntityRef(id=i, name=n) for i, n in les.tuples()],
        rate_card_templates=await _templates(db),
        demands=[
            WonPreviewDemand(
                line_id=line.id,
                title=line.title,
                positions=line.positions,
                bill_rate=money_str(line.expected_bill_rate),
                currency=line.currency,
                rate_unit=line.rate_unit,
                location=line.location,
                owner_id=opp.account_manager_id or opp.owner_id,
            )
            for line in lines
        ],
        commercials_available=_commercials() is not None,
    )


async def _step(step: str, fn: Callable[[], Awaitable[T]], line_id: uuid.UUID | None = None) -> T:
    """Run one conversion step; failures carry ``details.step`` (+ ``line_id``)."""
    extra: dict[str, Any] = {"step": step}
    if line_id is not None:
        extra["line_id"] = str(line_id)
    try:
        return await fn()
    except DomainError as exc:
        exc.details = {**exc.details, **extra}
        raise
    except PydanticValidationError as exc:
        raise ValidationFailed(
            f"The {step} could not be created",
            code="validation_error",
            details={
                **extra,
                "errors": [
                    {"field": ".".join(str(p) for p in e["loc"]), "message": e["msg"]} for e in exc.errors()
                ],
            },
        ) from exc


def _link(db: AsyncSession, opp: Opportunity, kind: str, record_id: uuid.UUID) -> None:
    db.add(
        CrmRecordLink(
            id=uuid.uuid4(),
            tenant_id=opp.tenant_id,
            opportunity_id=opp.id,
            record_kind=kind,
            record_id=record_id,
        )
    )


async def win_opportunity(ctx: RequestContext, db: AsyncSession, opp_id: uuid.UUID, data: WinIn) -> WinOut:
    principal = principal_of(ctx, OPP_WRITE)
    opp = await opportunities.get_row(ctx, db, opp_id, OPP_WRITE, lock=True)
    _require_open(opp)
    lines = {line.id: line for line in (await opportunities.lines_of(db, [opp.id]))[opp.id]}
    for d in data.demands:
        if d.line_id not in lines:
            raise ValidationFailed(
                "Unknown opportunity line",
                code="invalid_reference",
                details={"step": "demand", "line_id": str(d.line_id)},
            )
    records: list[CreatedRecord] = []

    # 1. stage -> won (the only path to Won)
    await opportunities.apply_stage(ctx, db, opp, "won", actor_kind="user", via_win=True)

    # 2. activate the client
    client = await db.get(Client, opp.client_id)
    assert client is not None
    activated = False
    if data.activate_client and client.status != "active":
        await _step(
            "client", lambda: clients_service.update_client(ctx, db, client.id, ClientUpdate(status="active"))
        )
        activated = True
    _link(db, opp, "client", client.id)
    records.append(CreatedRecord(kind="client", id=client.id, label=client.name))

    commercials = _commercials()
    # 3. draft contract
    contract_id: uuid.UUID | None = None
    if data.contract is not None and data.contract.create:
        if commercials is None:
            raise ConflictError(
                "Client commercials are not available",
                code="commercials_unavailable",
                details={"step": "contract"},
            )
        from app.schemas.commercials import ContractDraftIn

        c = data.contract
        le_id = c.legal_entity_id or await _default_legal_entity(db, opp.owner_id)
        if le_id is None:
            raise ValidationFailed(
                "A legal entity is required", code="legal_entity_required", details={"step": "contract"}
            )
        summary = await _step(
            "contract",
            lambda: commercials.create_contract_draft(
                ctx,
                db,
                ContractDraftIn(
                    client_id=client.id,
                    legal_entity_id=le_id,
                    contract_type=c.type,
                    start_date=c.start_date or now().date(),
                    end_date=c.end_date,
                    source_opportunity_id=opp.id,
                    document_file_ids=list(c.file_ids),
                    title=c.title or f"{client.name} {c.type.upper()}",
                    parent_contract_id=c.parent_contract_id,
                ),
            ),
        )
        contract_id = summary.id
        _link(db, opp, "contract", summary.id)
        records.append(CreatedRecord(kind="contract", id=summary.id, label=summary.human_id))

    # 4. draft rate card from a template
    rate_card_id: uuid.UUID | None = None
    if data.rate_card is not None:
        if commercials is None:
            raise ConflictError(
                "Client commercials are not available",
                code="commercials_unavailable",
                details={"step": "rate_card"},
            )
        rc = data.rate_card
        rate_card_id = await _step(
            "rate_card",
            lambda: commercials.copy_rate_card_template(
                ctx, db, rc.template_id, client.id, contract_id, rc.effective_from
            ),
        )
        _link(db, opp, "rate_card", rate_card_id)
        records.append(CreatedRecord(kind="rate_card", id=rate_card_id))

    # 5. draft demands per selected line (owned by the Account Manager)
    from app.schemas.demands import DemandCreate
    from app.services.demands import records as demand_records

    demand_ids: list[uuid.UUID] = []
    owner = opp.account_manager_id or opp.owner_id
    for d in data.demands:
        if not d.create:
            continue
        line = lines[d.line_id]
        ov = d.overrides
        rate = ov.bill_rate if ov and ov.bill_rate is not None else line.expected_bill_rate

        async def _create(line: Any = line, ov: Any = ov, rate: Decimal | None = rate) -> Any:
            payload: dict[str, Any] = {
                "title": (ov.title if ov and ov.title else line.title),
                "client_id": client.id,
                "positions": (ov.positions if ov and ov.positions else line.positions),
                "location": (ov.location if ov and ov.location is not None else line.location),
                "hiring_manager_contact_id": opp.primary_contact_id,
                "currency": line.currency,
                "rate_unit": line.rate_unit,
                "min_experience_years": line.experience_min_years,
                "max_experience_years": line.experience_max_years,
            }
            if rate is not None:
                payload["bill_rate"] = Decimal(rate).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            payload = {k: v for k, v in payload.items() if v is not None}
            return await demand_records.create_demand_row(ctx, db, DemandCreate(**payload), owner_id=owner)

        demand = await _step("demand", _create, line.id)
        demand_ids.append(demand.id)
        _link(db, opp, "demand", demand.id)
        records.append(CreatedRecord(kind="demand", id=demand.id, label=demand.req_id, line_id=line.id))

    await db.flush()
    emit(db, OpportunityWon(tenant_id=opp.tenant_id, opportunity_id=opp.id, client_id=client.id))
    if opp.account_manager_id is not None:
        emit(
            db,
            OpportunityWonHandoff(
                tenant_id=opp.tenant_id,
                opportunity_id=opp.id,
                account_manager_id=opp.account_manager_id,
                actor_id=principal.id,
            ),
        )
    return WinOut(
        opportunity=await opportunities.out(db, opp),
        client_id=client.id,
        client_activated=activated,
        contract_id=contract_id,
        rate_card_id=rate_card_id,
        demand_ids=demand_ids,
        records=records,
    )


async def record_link(
    ctx: RequestContext, db: AsyncSession, record_kind: str, record_id: uuid.UUID
) -> RecordLinkOut | None:
    """Back-link "From Opportunity OPP-…" for a Client / Contract / Rate Card / Demand page."""
    principal_of(ctx, OPP_READ)
    row = (
        await db.execute(
            select(Opportunity.id, Opportunity.human_id, Opportunity.name)
            .join(CrmRecordLink, CrmRecordLink.opportunity_id == Opportunity.id)
            .where(CrmRecordLink.record_kind == record_kind, CrmRecordLink.record_id == record_id)
            .order_by(CrmRecordLink.created_at.desc())
            .limit(1)
        )
    ).first()
    if row is None:
        return None
    return RecordLinkOut(opportunity_id=row[0], human_id=row[1], name=row[2])


async def links_for(db: AsyncSession, opp_id: uuid.UUID) -> list[CrmRecordLink]:
    rows = await db.execute(
        select(CrmRecordLink).where(CrmRecordLink.opportunity_id == opp_id).order_by(CrmRecordLink.created_at)
    )
    return list(rows.scalars())
