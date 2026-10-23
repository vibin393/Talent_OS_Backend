# ruff: file-ignore[line-too-long]
"""Opportunities, role lines and the pipeline (FR-85, PRD §5.2; story 2.3).

``apply_stage`` is the only writer of ``stage`` (AD-4). Transitions (user only): create -> qualification;
qualification -> proposal -> negotiation (forward only); open -> lost (reason code, optional competitor / note);
lost -> qualification (reopen reason); open -> won only through ``won.win_opportunity`` (the generic endpoint
answers 409 ``use_won_conversion``). Stale ``expected_stage`` -> 409 ``stage_conflict``.

Probability: on create and on every stage change, unless overridden, it is the tenant's stage default;
``PATCH probability_pct`` sets the override, ``reset_probability`` clears it. Weighted revenue is computed
(``estimated_revenue x probability / 100``, ROUND_HALF_UP to the currency's minor units), never stored.
"""

import uuid
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import Numeric, case, cast, func, literal, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import money
from app.core.context import RequestContext
from app.core.errors import ConflictError, NotFound, ValidationFailed
from app.core.events import emit
from app.core.sequences import next_human_id
from app.events.crm import OpportunityAssigned, OpportunityCreated, OpportunityLost, OpportunityStageChanged
from app.models.clients import Client
from app.models.crm import OPEN_STAGES, OPPORTUNITY_STAGES, Opportunity, OpportunityLine
from app.schemas.crm import (
    BoardColumn,
    BoardOut,
    ClientOpportunitiesOut,
    OpportunityCreate,
    OpportunityLineIn,
    OpportunityLineOut,
    OpportunityOut,
    OpportunityPage,
    OpportunityTransitionIn,
    OpportunityUpdate,
    StatusHistoryOut,
    SummaryGroup,
    SummaryOut,
)
from app.services import activity_service, clients_service, entity_refs
from app.services.crm import settings as crm_settings
from app.services.crm.common import (
    OPP_READ,
    OPP_WRITE,
    add_history,
    check_user,
    history,
    money_str,
    now,
    principal_of,
    scoped,
    tenant_of,
    totals,
    transition_refused,
    user_principal,
    user_refs,
    weighted,
)

SEQUENCE_KEY = "OPP"
TRANSITIONS: dict[tuple[str | None, str], frozenset[str]] = {
    (None, "qualification"): frozenset({"user"}),
    ("qualification", "proposal"): frozenset({"user"}),
    ("proposal", "negotiation"): frozenset({"user"}),
    **{(s, "won"): frozenset({"user"}) for s in OPEN_STAGES},
    **{(s, "lost"): frozenset({"user"}) for s in OPEN_STAGES},
    ("lost", "qualification"): frozenset({"user"}),
}
STAGE_LABELS = {s: s.capitalize() for s in OPPORTUNITY_STAGES}
CLOSED_EDITABLE = frozenset({"owner_id", "account_manager_id"})
BOARD_LIMIT = 200
CLOSED_WINDOW_DAYS = 90


def can_transition(frm: str | None, to: str, actor_kind: str) -> bool:
    return actor_kind in TRANSITIONS.get((frm, to), frozenset())


def _zero_minor() -> tuple[str, ...]:
    return tuple(code for code, row in money._currencies().items() if int(row.get("minor_units", 2)) == 0)


def weighted_sql() -> Any:
    """Per-row weighted revenue in SQL, rounded half-up (numeric ``round``) to the currency's minor units."""
    raw = Opportunity.estimated_revenue * Opportunity.probability_pct / literal(100)
    zero = _zero_minor()
    places = case((Opportunity.currency.in_(zero), 0), else_=2) if zero else literal(2)
    return func.round(cast(raw, Numeric), places)


# ---------------------------------------------------------------- read
async def get_row(
    ctx: RequestContext,
    db: AsyncSession,
    opp_id: uuid.UUID,
    permission: str = OPP_READ,
    *,
    lock: bool = False,
) -> Opportunity:
    principal = user_principal(ctx)
    if principal is not None:
        principal_of(ctx, permission)
    stmt = scoped(
        select(Opportunity).where(Opportunity.id == opp_id), principal, permission, Opportunity.owner_id
    )
    if lock:
        stmt = stmt.with_for_update()
    opp = (await db.execute(stmt)).scalar_one_or_none()
    if opp is None:
        raise NotFound("Opportunity not found")
    return opp


async def opportunity_access(ctx: RequestContext, db: AsyncSession, opp_id: uuid.UUID, write: bool) -> bool:
    principal = user_principal(ctx)
    if principal is None:
        return False
    permission = OPP_WRITE if write else OPP_READ
    if not principal.has(permission):
        return False
    stmt = scoped(
        select(Opportunity.id).where(Opportunity.id == opp_id), principal, permission, Opportunity.owner_id
    )
    return (await db.execute(stmt)).first() is not None


def _line_out(line: OpportunityLine) -> OpportunityLineOut:
    return OpportunityLineOut(
        id=line.id,
        title=line.title,
        positions=line.positions,
        expected_bill_rate=money_str(line.expected_bill_rate),
        currency=line.currency,
        rate_unit=line.rate_unit,
        location=line.location,
        experience_min_years=money_str(line.experience_min_years),
        experience_max_years=money_str(line.experience_max_years),
        position=line.position,
    )


async def lines_of(db: AsyncSession, opp_ids: list[uuid.UUID]) -> dict[uuid.UUID, list[OpportunityLine]]:
    out: dict[uuid.UUID, list[OpportunityLine]] = {i: [] for i in opp_ids}
    if not opp_ids:
        return out
    rows = await db.execute(
        select(OpportunityLine)
        .where(OpportunityLine.opportunity_id.in_(opp_ids))
        .order_by(OpportunityLine.position)
    )
    for line in rows.scalars():
        out[line.opportunity_id].append(line)
    return out


async def outs(
    db: AsyncSession, opps: list[Opportunity], *, with_lines: bool = False
) -> list[OpportunityOut]:
    users = await user_refs(db, [o.owner_id for o in opps] + [o.account_manager_id for o in opps])
    client_ids = {o.client_id for o in opps}
    names = (
        dict(
            (await db.execute(select(Client.id, Client.name).where(Client.id.in_(client_ids)))).tuples().all()
        )
        if client_ids
        else {}
    )
    lines = await lines_of(db, [o.id for o in opps]) if with_lines else {}
    return [
        OpportunityOut(
            id=o.id,
            human_id=o.human_id,
            name=o.name,
            client_id=o.client_id,
            client_name=names.get(o.client_id),
            primary_contact_id=o.primary_contact_id,
            lead_id=o.lead_id,
            service=o.service,  # type: ignore[arg-type]
            estimated_revenue=format(o.estimated_revenue, "f"),
            weighted_revenue=format(weighted(o.estimated_revenue, o.probability_pct, o.currency), "f"),
            currency=o.currency,
            probability_pct=format(o.probability_pct, "f"),
            probability_overridden=o.probability_overridden,
            expected_close_date=o.expected_close_date,
            owner_id=o.owner_id,
            owner=users.get(o.owner_id),
            account_manager_id=o.account_manager_id,
            account_manager=users.get(o.account_manager_id) if o.account_manager_id else None,
            stage=o.stage,  # type: ignore[arg-type]
            stage_entered_at=o.stage_entered_at,
            lost_reason_code=o.lost_reason_code,
            lost_competitor=o.lost_competitor,
            lost_note=o.lost_note,
            won_at=o.won_at,
            lost_at=o.lost_at,
            version=o.version,
            archived_at=o.archived_at,
            created_at=o.created_at,
            updated_at=o.updated_at,
            lines=[_line_out(line) for line in lines.get(o.id, [])],
        )
        for o in opps
    ]


async def out(db: AsyncSession, opp: Opportunity) -> OpportunityOut:
    await db.refresh(opp)
    return (await outs(db, [opp], with_lines=True))[0]


async def get_opportunity(ctx: RequestContext, db: AsyncSession, opp_id: uuid.UUID) -> OpportunityOut:
    return await out(db, await get_row(ctx, db, opp_id))


async def opportunity_history(
    ctx: RequestContext, db: AsyncSession, opp_id: uuid.UUID
) -> list[StatusHistoryOut]:
    await get_row(ctx, db, opp_id)
    return [StatusHistoryOut.model_validate(h) for h in await history(db, "opportunity", opp_id)]


def _filtered(stmt: Any, f: dict[str, Any]) -> Any:
    if f.get("stage"):
        stmt = stmt.where(Opportunity.stage.in_(f["stage"]))
    for key, col in (
        ("owner_id", Opportunity.owner_id),
        ("account_manager_id", Opportunity.account_manager_id),
        ("client_id", Opportunity.client_id),
        ("service", Opportunity.service),
    ):
        if f.get(key):
            stmt = stmt.where(col == f[key])
    if f.get("close_from"):
        stmt = stmt.where(Opportunity.expected_close_date >= f["close_from"])
    if f.get("close_to"):
        stmt = stmt.where(Opportunity.expected_close_date <= f["close_to"])
    if f.get("q"):
        term = f"%{str(f['q']).strip().replace('%', '').replace('_', '')}%"
        stmt = stmt.where(or_(Opportunity.name.ilike(term), Opportunity.human_id.ilike(term)))
    if not f.get("include_archived"):
        stmt = stmt.where(Opportunity.archived_at.is_(None))
    return stmt


async def list_opportunities(
    ctx: RequestContext, db: AsyncSession, filters: dict[str, Any], page: int = 1, page_size: int = 25
) -> OpportunityPage:
    principal = principal_of(ctx, OPP_READ)
    stmt = _filtered(scoped(select(Opportunity), principal, OPP_READ, Opportunity.owner_id), filters)
    page, page_size = max(1, page), max(1, min(page_size, 200))
    total = int((await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one())
    rows = await db.execute(
        stmt.order_by(Opportunity.expected_close_date.asc().nulls_last(), Opportunity.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    return OpportunityPage(
        items=await outs(db, list(rows.scalars())), total=total, page=page, page_size=page_size
    )


async def board(ctx: RequestContext, db: AsyncSession, filters: dict[str, Any]) -> BoardOut:
    principal = principal_of(ctx, OPP_READ)
    base = _filtered(scoped(select(Opportunity), principal, OPP_READ, Opportunity.owner_id), filters)
    if not (filters.get("close_from") or filters.get("close_to")):
        cutoff = now() - timedelta(days=CLOSED_WINDOW_DAYS)
        base = base.where(
            or_(
                Opportunity.stage.in_(OPEN_STAGES),
                (Opportunity.stage == "won") & (Opportunity.won_at >= cutoff),
                (Opportunity.stage == "lost") & (Opportunity.lost_at >= cutoff),
            )
        )
    sub = base.subquery()
    agg = await db.execute(
        select(sub.c.stage, sub.c.currency, func.count(), func.sum(sub.c.estimated_revenue), func.sum(
            func.round(cast(sub.c.estimated_revenue * sub.c.probability_pct / 100, Numeric),
                       case((sub.c.currency.in_(_zero_minor() or ("---",)), 0), else_=2))
        )).group_by(sub.c.stage, sub.c.currency)
    )  # fmt: skip
    counts: dict[str, int] = {}
    per_stage: dict[str, list[tuple[str, Decimal, Decimal]]] = {}
    for stage, currency, count, est, wtd in agg.tuples():
        counts[stage] = counts.get(stage, 0) + int(count)
        per_stage.setdefault(stage, []).append((currency, est or Decimal(0), wtd or Decimal(0)))
    columns: list[BoardColumn] = []
    for stage in OPPORTUNITY_STAGES:
        rows = await db.execute(
            base.where(Opportunity.stage == stage)
            .order_by(Opportunity.expected_close_date.asc().nulls_last(), Opportunity.created_at.desc())
            .limit(BOARD_LIMIT)
        )
        columns.append(
            BoardColumn(
                stage=stage,  # type: ignore[arg-type]
                count=counts.get(stage, 0),
                totals=totals(per_stage.get(stage, [])),
                items=await outs(db, list(rows.scalars())),
            )
        )
    return BoardOut(columns=columns)


async def summary(
    ctx: RequestContext, db: AsyncSession, group_by: str, filters: dict[str, Any]
) -> SummaryOut:
    principal = principal_of(ctx, OPP_READ)
    base = _filtered(scoped(select(Opportunity), principal, OPP_READ, Opportunity.owner_id), filters)
    sub = base.subquery()
    key_col: Any
    if group_by == "owner":
        key_col = sub.c.owner_id
    elif group_by == "stage":
        key_col = sub.c.stage
    else:
        key_col = func.to_char(sub.c.expected_close_date, "YYYY-MM")
    wtd = func.round(
        cast(sub.c.estimated_revenue * sub.c.probability_pct / 100, Numeric),
        case((sub.c.currency.in_(_zero_minor() or ("---",)), 0), else_=2),
    )
    rows = await db.execute(
        select(
            key_col, sub.c.currency, func.count(), func.sum(sub.c.estimated_revenue), func.sum(wtd)
        ).group_by(key_col, sub.c.currency)
    )
    groups: dict[str, dict[str, Any]] = {}
    for key, currency, count, est, w in rows.tuples():
        k = str(key) if key is not None else "none"
        g = groups.setdefault(k, {"count": 0, "rows": []})
        g["count"] += int(count)
        g["rows"].append((currency, est or Decimal(0), w or Decimal(0)))
    labels: dict[str, str] = {}
    if group_by == "owner":
        refs = await user_refs(db, [uuid.UUID(k) for k in groups if k != "none"])
        labels = {str(i): r.name for i, r in refs.items()}
    elif group_by == "stage":
        labels = {k: STAGE_LABELS.get(k, k) for k in groups}
    out_groups = [
        SummaryGroup(
            key=k,
            label=labels.get(k, "No close date" if k == "none" else k),
            count=g["count"],
            totals=totals(g["rows"]),
        )
        for k, g in sorted(groups.items())
    ]
    return SummaryOut(group_by=group_by, groups=out_groups)  # type: ignore[arg-type]


async def client_opportunities(
    ctx: RequestContext, db: AsyncSession, client_id: uuid.UUID
) -> ClientOpportunitiesOut:
    principal = principal_of(ctx, OPP_READ)
    if not await clients_service.client_access(ctx, db, client_id, False):
        raise NotFound("Client not found")
    stmt = scoped(select(Opportunity), principal, OPP_READ, Opportunity.owner_id).where(
        Opportunity.client_id == client_id, Opportunity.archived_at.is_(None)
    )
    opps = list((await db.execute(stmt.order_by(Opportunity.created_at.desc()).limit(100))).scalars())
    open_opps = [o for o in opps if o.stage in OPEN_STAGES]
    return ClientOpportunitiesOut(
        open_count=len(open_opps),
        weighted_pipeline=totals(
            (o.currency, o.estimated_revenue, weighted(o.estimated_revenue, o.probability_pct, o.currency))
            for o in open_opps
        ),
        items=await outs(db, opps),
    )


# ---------------------------------------------------------------- write
def _closed_conflict(opp: Opportunity) -> ConflictError:
    return ConflictError(
        "A won or lost opportunity only accepts owner / account manager changes",
        code="opportunity_closed",
        details={"stage": opp.stage},
    )


async def _check_client(db: AsyncSession, client_id: uuid.UUID) -> Client:
    client = await db.get(Client, client_id)
    if client is None or client.archived_at is not None:
        raise ValidationFailed(
            "Unknown client",
            code="invalid_reference",
            details={
                "fields": [{"field": "client_id", "message": "Unknown client", "type": "invalid_reference"}]
            },
        )
    if client.status not in ("prospect", "active"):
        raise ConflictError(
            "The client must be a prospect or active",
            code="client_not_open",
            details={"status": client.status},
        )
    return client


async def _check_contact(db: AsyncSession, contact_id: uuid.UUID | None, client_id: uuid.UUID) -> None:
    if contact_id is not None and not await clients_service.contact_belongs_to_client(
        db, contact_id, client_id
    ):
        raise ValidationFailed(
            "The contact does not belong to the client",
            code="invalid_reference",
            details={
                "fields": [
                    {"field": "primary_contact_id", "message": "Unknown contact", "type": "invalid_reference"}
                ]
            },
        )


def _new_lines(db: AsyncSession, opp: Opportunity, lines: list[OpportunityLineIn]) -> None:
    for pos, line in enumerate(lines):
        if (
            line.experience_min_years is not None
            and line.experience_max_years is not None
            and line.experience_min_years > line.experience_max_years
        ):
            raise ValidationFailed(
                "experience_min_years must be <= experience_max_years", code="invalid_range"
            )
        db.add(
            OpportunityLine(
                id=uuid.uuid4(),
                tenant_id=opp.tenant_id,
                opportunity_id=opp.id,
                title=line.title,
                positions=line.positions,
                expected_bill_rate=line.expected_bill_rate,
                currency=line.currency or opp.currency,
                rate_unit=line.rate_unit,
                location=line.location,
                experience_min_years=line.experience_min_years,
                experience_max_years=line.experience_max_years,
                position=pos,
            )
        )


async def _stage_entry(
    ctx: RequestContext, db: AsyncSession, opp: Opportunity, body: str, data: dict[str, Any]
) -> None:
    await activity_service.add_system_entry(ctx, db, "opportunity", opp.id, body, data)
    await activity_service.add_system_entry(ctx, db, "client", opp.client_id, f"{opp.human_id}: {body}", data)


async def create_opportunity(
    ctx: RequestContext, db: AsyncSession, data: OpportunityCreate, *, lead_id: uuid.UUID | None = None
) -> Opportunity:
    principal = principal_of(ctx, OPP_WRITE)
    if data.client_id is None:
        raise ValidationFailed(
            "client_id is required",
            code="validation_error",
            details={"fields": [{"field": "client_id", "message": "Required", "type": "missing"}]},
        )
    await _check_client(db, data.client_id)
    await _check_contact(db, data.primary_contact_id, data.client_id)
    owner_id = data.owner_id or principal.id
    if owner_id != principal.id:
        await check_user(db, owner_id, "owner_id")
    am = data.account_manager_id or owner_id
    if am not in (owner_id, principal.id):
        await check_user(db, am, "account_manager_id")
    probabilities = await crm_settings.stage_probabilities(db)
    overridden = data.probability_pct is not None
    opp = Opportunity(
        id=uuid.uuid4(),
        tenant_id=tenant_of(db),
        human_id=await next_human_id(db, SEQUENCE_KEY, prefix="OPP", width=6),
        name=data.name,
        client_id=data.client_id,
        primary_contact_id=data.primary_contact_id,
        lead_id=lead_id,
        service=data.service,
        estimated_revenue=data.estimated_revenue,
        currency=data.currency or await crm_settings.default_currency(db),
        probability_pct=data.probability_pct if overridden else Decimal(probabilities["qualification"]),
        probability_overridden=overridden,
        expected_close_date=data.expected_close_date,
        owner_id=owner_id,
        account_manager_id=am,
        stage="qualification",
        stage_entered_at=now(),
        version=1,
    )
    db.add(opp)
    await db.flush()
    _new_lines(db, opp, data.lines)
    await db.flush()
    add_history(
        db,
        entity_kind="opportunity",
        entity_id=opp.id,
        frm=None,
        to="qualification",
        actor_kind="user",
        actor_id=principal.id,
    )
    await _stage_entry(ctx, db, opp, f"Opportunity {opp.human_id} created", {"stage": "qualification"})
    emit(
        db,
        OpportunityCreated(
            tenant_id=opp.tenant_id, opportunity_id=opp.id, client_id=opp.client_id, owner_id=owner_id
        ),
    )
    if owner_id != principal.id:
        emit(
            db,
            OpportunityAssigned(
                tenant_id=opp.tenant_id, opportunity_id=opp.id, owner_id=owner_id, actor_id=principal.id
            ),
        )
    return opp


async def create(ctx: RequestContext, db: AsyncSession, data: OpportunityCreate) -> OpportunityOut:
    return await out(db, await create_opportunity(ctx, db, data))


async def update_opportunity(
    ctx: RequestContext, db: AsyncSession, opp_id: uuid.UUID, data: OpportunityUpdate
) -> OpportunityOut:
    principal = principal_of(ctx, OPP_WRITE)
    opp = await get_row(ctx, db, opp_id, OPP_WRITE, lock=True)
    changes = data.model_dump(exclude_unset=True)
    if opp.stage in ("won", "lost") and set(changes) - CLOSED_EDITABLE:
        raise _closed_conflict(opp)
    if "owner_id" in changes:
        if changes["owner_id"] is None:
            changes.pop("owner_id")
        elif changes["owner_id"] != opp.owner_id:
            await check_user(db, changes["owner_id"], "owner_id")
            emit(
                db,
                OpportunityAssigned(
                    tenant_id=opp.tenant_id,
                    opportunity_id=opp.id,
                    owner_id=changes["owner_id"],
                    actor_id=principal.id,
                ),
            )
    if changes.get("account_manager_id"):
        await check_user(db, changes["account_manager_id"], "account_manager_id")
    if "primary_contact_id" in changes:
        await _check_contact(db, changes["primary_contact_id"], opp.client_id)
    for key in ("name", "service", "estimated_revenue", "currency"):
        if key in changes and changes[key] is None:
            changes.pop(key)
    if "probability_pct" in changes:
        value = changes.pop("probability_pct")
        if value is not None:
            opp.probability_pct = value
            opp.probability_overridden = True
    for key, value in changes.items():
        setattr(opp, key, value)
    opp.version += 1
    await db.flush()
    return await out(db, opp)


async def replace_lines(
    ctx: RequestContext, db: AsyncSession, opp_id: uuid.UUID, lines: list[OpportunityLineIn]
) -> OpportunityOut:
    principal_of(ctx, OPP_WRITE)
    opp = await get_row(ctx, db, opp_id, OPP_WRITE, lock=True)
    if opp.stage == "won":
        raise ConflictError(
            "Lines of a won opportunity cannot change",
            code="opportunity_closed",
            details={"stage": opp.stage},
        )
    for line in (await lines_of(db, [opp.id]))[opp.id]:
        await db.delete(line)
    await db.flush()
    _new_lines(db, opp, lines)
    opp.version += 1
    await db.flush()
    return await out(db, opp)


async def reset_probability(ctx: RequestContext, db: AsyncSession, opp_id: uuid.UUID) -> OpportunityOut:
    principal_of(ctx, OPP_WRITE)
    opp = await get_row(ctx, db, opp_id, OPP_WRITE, lock=True)
    probabilities = await crm_settings.stage_probabilities(db)
    opp.probability_pct = Decimal(probabilities[opp.stage])
    opp.probability_overridden = False
    opp.version += 1
    await db.flush()
    return await out(db, opp)


async def apply_stage(
    ctx: RequestContext,
    db: AsyncSession,
    opp: Opportunity,
    to: str,
    *,
    actor_kind: str,
    reason_code: str | None = None,
    competitor: str | None = None,
    note: str | None = None,
    via_win: bool = False,
) -> None:
    frm = opp.stage
    if not can_transition(frm, to, actor_kind) or (to == "won" and not via_win):
        raise transition_refused("opportunity", frm, to)
    if to == "lost":
        reason_code = await crm_settings.require_reason(db, "opportunity_lost", reason_code, note)
        opp.lost_reason_code, opp.lost_competitor, opp.lost_note, opp.lost_at = (
            reason_code,
            competitor,
            note,
            now(),
        )
    elif frm == "lost":
        reason_code = await crm_settings.require_reason(db, "reopen", reason_code, note)
        opp.lost_reason_code = opp.lost_competitor = opp.lost_note = None
        opp.lost_at = None
    if to == "won":
        opp.won_at = now()
    if not opp.probability_overridden:
        opp.probability_pct = Decimal((await crm_settings.stage_probabilities(db))[to])
    principal = user_principal(ctx)
    actor_id = principal.id if principal else None
    with entity_refs.acting_as(db, actor_kind, actor_id):
        opp.stage = to
        opp.stage_entered_at = now()
        opp.version += 1
        await db.flush()
    add_history(
        db, entity_kind="opportunity", entity_id=opp.id, frm=frm, to=to, actor_kind=actor_kind, actor_id=actor_id,
        reason_code=reason_code, note=note, competitor=competitor,
    )  # fmt: skip
    await _stage_entry(
        ctx, db, opp, f"Stage changed: {STAGE_LABELS[frm]} -> {STAGE_LABELS[to]}",
        {"from": frm, "to": to, "reason_code": reason_code},
    )  # fmt: skip
    emit(
        db,
        OpportunityStageChanged(tenant_id=opp.tenant_id, opportunity_id=opp.id, from_stage=frm, to_stage=to),
    )
    if to == "lost":
        emit(
            db, OpportunityLost(tenant_id=opp.tenant_id, opportunity_id=opp.id, reason_code=reason_code or "")
        )


async def transition_opportunity(
    ctx: RequestContext,
    db: AsyncSession,
    opp_id: uuid.UUID,
    data: OpportunityTransitionIn,
    *,
    actor_kind: str = "user",
) -> OpportunityOut:
    if actor_kind == "user":
        principal_of(ctx, OPP_WRITE)
    opp = await get_row(ctx, db, opp_id, OPP_WRITE, lock=True)
    if data.to == "won":
        raise ConflictError("Use the Won conversion to win an opportunity", code="use_won_conversion")
    if opp.stage != data.expected_stage:
        raise ConflictError(
            "The opportunity changed stage meanwhile", code="stage_conflict", details={"stage": opp.stage}
        )
    await apply_stage(
        ctx,
        db,
        opp,
        data.to,
        actor_kind=actor_kind,
        reason_code=data.reason_code,
        competitor=data.competitor,
        note=data.note,
    )
    return await out(db, opp)


def today() -> date:
    return now().date()
