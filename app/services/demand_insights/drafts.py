# ruff: file-ignore[line-too-long]
"""Draft demands from intake, draft updates and approved JD / skill updates (Epic 55, stories 55.2 / 55.3).

* ``create_draft`` (``demands.create_draft``, low): **intake values come from the stored extraction** (spec 0.2). The
  agent may override only ``title`` / ``positions``, and only with a value that occurs in the source text (else 422
  ``override_not_in_source``). The demand goes through the P1 ``create_demand_row`` (status ``draft`` always, owner =
  the Accountable Owner, ``actor_kind="agent"``), every extracted field gets its source quote, known skills are set
  with resolved refs only, unknown terms become ``new_skill`` suggestions (no ``TenantSkill`` is created), and a
  Contract / Rate Card suggestion is added when exactly one active candidate exists.
* ``update_draft`` (``demands.update_draft``, low): drafts only (409 ``demand_not_draft``); only fields still
  AI-extracted (422 ``field_confirmed_by_user``); a JD becomes a new ``ai`` version (never published).
* ``apply_update`` (``demands.update``, high -> always a Proposal): a non-draft demand's JD and / or skills, executed as
  the approving person (``save_jd_version``; ``set_skills`` may create a pending tenant skill -- P1 semantics).
"""

import re
import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.errors import ConflictError, NotFound, ValidationFailed
from app.models.demands import Demand, DemandSkill
from app.schemas.demand_insights import (
    DemandUpdateIn,
    DraftDemandIn,
    DraftDemandOut,
    SkillItemIn,
    SuggestSkillItem,
    UpdateDraftIn,
    UpdateDraftOut,
)
from app.services.demand_insights import jd as jd_mod
from app.services.demand_insights import provenance, suggestions

_NUMBER_WORDS = {
    1: "one", 2: "two", 3: "three", 4: "four", 5: "five", 6: "six", 7: "seven", 8: "eight", 9: "nine", 10: "ten",
    11: "eleven", 12: "twelve", 15: "fifteen", 20: "twenty",
}  # fmt: skip
_DIRECT_FIELDS = (
    "location",
    "work_mode",
    "employment_type",
    "min_experience_years",
    "max_experience_years",
    "start_date",
)
_UPDATABLE_FIELDS = (
    "title", "location", "work_mode", "employment_type", "positions", "min_experience_years", "max_experience_years", "start_date",
)  # fmt: skip


def _invalid(message: str, code: str) -> ValidationFailed:
    return ValidationFailed(message, code=code)


def positions_in_source(n: int, source_norm: str) -> bool:
    if re.search(rf"(?<![\d.]){n}(?![\d.])", source_norm):
        return True
    word = _NUMBER_WORDS.get(n)
    return bool(word and re.search(rf"\b{word}\b", source_norm))


async def _resolve_skills(
    ctx: RequestContext, db: AsyncSession, labels: list[tuple[str, str]]
) -> tuple[list[Any], list[tuple[str, str]]]:
    """``(DemandSkillIn list with resolved refs, unknown (label, kind) list)`` -- never creates a skill."""
    from app.schemas.demands import DemandSkillIn

    known: list[Any] = []
    unknown: list[tuple[str, str]] = []
    for label, kind in labels:
        ref = await suggestions.resolve_ref(ctx, db, label)
        if ref is None:
            unknown.append((label, kind))
        else:
            known.append(
                DemandSkillIn(label=ref.name[:200], skill_id=ref.id, skill_ref_kind=ref.kind, kind=kind)
            )
    return known, unknown


async def _link_suggestions(
    ctx: RequestContext, db: AsyncSession, demand: Demand, run_id: uuid.UUID | None
) -> None:
    from app.services.commercials.contracts import list_contracts
    from app.services.commercials.rate_cards import list_client_cards

    contracts = [c for c in await list_contracts(ctx, db, client_id=demand.client_id) if c.status == "active"]
    if len(contracts) == 1:
        c = contracts[0]
        await suggestions.add_link_suggestion(
            db, demand, "contract", c.id, f"{c.human_id} {c.title or ''}".strip(), run_id=run_id
        )
    cards = [r for r in await list_client_cards(ctx, db, demand.client_id) if r.status == "published"]
    if len(cards) == 1:
        r = cards[0]
        await suggestions.add_link_suggestion(
            db, demand, "rate_card", r.id, f"{r.human_id} {r.name}".strip(), run_id=run_id
        )


async def _owner_name_client(db: AsyncSession, client_id: uuid.UUID) -> str | None:
    from app.services import clients_service

    return await clients_service.client_name(db, client_id)


async def create_draft(ctx: RequestContext, db: AsyncSession, data: DraftDemandIn, *, owner_id: uuid.UUID,
                       run_id: uuid.UUID | None) -> DraftDemandOut:  # fmt: skip
    if data.mode == "intake":
        return await _create_from_intake(ctx, db, data, owner_id=owner_id, run_id=run_id)
    return await _create_direct(ctx, db, data, owner_id=owner_id, run_id=run_id)


async def _create_from_intake(ctx: RequestContext, db: AsyncSession, data: DraftDemandIn, *, owner_id: uuid.UUID,
                              run_id: uuid.UUID | None) -> DraftDemandOut:  # fmt: skip
    from app.schemas.demands import DemandCreate
    from app.services import demand_intake_service as intake
    from app.services.demand_intake.extraction import normalise, quoted, source_text
    from app.services.demands.common import acting_as
    from app.services.demands.records import create_demand_row, set_skills

    assert data.message_id is not None and data.role_index is not None
    message, client_id = await intake.matched_client(db, data.message_id)
    if message.status == "discarded":
        raise NotFound("Intake message not found")
    extraction = await intake.extract(ctx, db, message.id)
    already = dict((message.extraction or {}).get("drafted") or {})
    if str(data.role_index) in already:
        existing = await db.get(Demand, uuid.UUID(already[str(data.role_index)]))
        if existing is not None:
            return DraftDemandOut(demand_id=existing.id, human_id=existing.req_id)
    role = next((r for r in extraction.roles if r.role_index == data.role_index), None)
    if role is None:
        raise _invalid("No such role in the extraction", "role_not_found")
    source_norm = normalise(await source_text(db, message))
    fields = dict(role.fields)
    quotes: dict[str, str] = {k: v.source_quote for k, v in fields.items()}
    values: dict[str, Any] = {
        k: v.value for k, v in fields.items() if k not in ("mandatory_skills", "preferred_skills")
    }
    if data.title_override is not None:
        if not quoted(data.title_override, source_norm):
            raise _invalid("The title override does not occur in the source", "override_not_in_source")
        values["title"] = data.title_override
        quotes["title"] = data.title_override
    if data.positions_override is not None:
        if not positions_in_source(data.positions_override, source_norm):
            raise _invalid("The positions override does not occur in the source", "override_not_in_source")
        values["positions"] = data.positions_override
        quotes.setdefault("positions", str(data.positions_override))
    title = str(values.pop("title", None) or "Untitled demand")[:300]
    payload: dict[str, Any] = {"client_id": client_id, "title": title}
    for key, value in values.items():
        payload[key] = value
    create = DemandCreate(**payload)
    extracted = sorted(set(values) | ({"title"} if "title" in quotes else set()))
    labels = [(s, "mandatory") for s in role.mandatory_skills] + [
        (s, "preferred") for s in role.preferred_skills
    ]
    known, unknown = await _resolve_skills(ctx, db, labels)
    if any(k.kind == "mandatory" for k in known):
        extracted.append("mandatory_skills")
    if any(k.kind == "preferred" for k in known):
        extracted.append("preferred_skills")
    demand = await create_demand_row(
        ctx, db, create, actor_kind="agent", ai_extracted_fields=sorted(set(extracted)),
        intake_ai_action_id=message.extraction_ai_action_id, owner_id=owner_id,
    )  # fmt: skip
    if known:
        with acting_as(db, "agent", owner_id):
            await set_skills(ctx, db, demand, known)
    await provenance.record(
        db, demand.tenant_id, demand.id, {k: v for k, v in quotes.items() if k in set(extracted) | {"title"}},
        intake_message_id=message.id, run_id=run_id,
    )  # fmt: skip
    if unknown:
        await suggestions.add_skill_suggestions(
            ctx, db, demand,
            [SuggestSkillItem(label=label[:100], skill_kind=kind, evidence=quotes.get(f"{kind}_skills", "")[:300] or None) for label, kind in unknown],
            run_id=run_id,
        )  # fmt: skip
    await _link_suggestions(ctx, db, demand, run_id)
    extraction_json = dict(message.extraction or {})
    drafted = dict(extraction_json.get("drafted") or {})
    drafted[str(data.role_index)] = str(demand.id)
    extraction_json["drafted"] = drafted
    message.extraction = extraction_json
    await intake.record_drafts(db, message, demand.id, await _owner_name_client(db, client_id))
    return DraftDemandOut(demand_id=demand.id, human_id=demand.req_id)


async def _create_direct(ctx: RequestContext, db: AsyncSession, data: DraftDemandIn, *, owner_id: uuid.UUID,
                         run_id: uuid.UUID | None) -> DraftDemandOut:  # fmt: skip
    from app.schemas.demands import DemandCreate
    from app.services import clients_service
    from app.services.demands.common import acting_as
    from app.services.demands.records import create_demand_row, set_skills

    assert data.client_id is not None and data.title is not None and data.positions is not None
    await clients_service.get_client(ctx, db, data.client_id)  # 404 unless readable
    payload: dict[str, Any] = {"client_id": data.client_id, "title": data.title, "positions": data.positions}
    for key in _DIRECT_FIELDS:
        value = getattr(data, key)
        if value is not None:
            payload[key] = value
    demand = await create_demand_row(ctx, db, DemandCreate(**payload), actor_kind="agent", owner_id=owner_id)
    labels = [(s, "mandatory") for s in data.mandatory_skills] + [
        (s, "preferred") for s in data.preferred_skills
    ]
    known, unknown = await _resolve_skills(ctx, db, labels)
    if known:
        with acting_as(db, "agent", owner_id):
            await set_skills(ctx, db, demand, known)
    if unknown:
        await suggestions.add_skill_suggestions(
            ctx,
            db,
            demand,
            [SuggestSkillItem(label=label[:100], skill_kind=kind) for label, kind in unknown],
            run_id=run_id,
        )
    if data.notes:
        from app.services.demands.common import timeline

        await timeline(ctx, db, demand.id, "Draft prepared with the Copilot", actor_kind="agent")
    return DraftDemandOut(demand_id=demand.id, human_id=demand.req_id)


# ================================================================ updates
async def _skill_items(
    ctx: RequestContext, db: AsyncSession, items: list[SkillItemIn], *, allow_create: bool
) -> tuple[list[Any], list[SkillItemIn]]:
    from app.schemas.demands import DemandSkillIn

    out: list[Any] = []
    unknown: list[SkillItemIn] = []
    for item in items:
        ref = await suggestions.resolve_ref(ctx, db, item.label, item.skill_ref_kind, item.skill_id)
        if ref is None and not allow_create:
            unknown.append(item)
            continue
        out.append(
            DemandSkillIn(
                label=(ref.name if ref is not None else item.label)[:200], skill_id=ref.id if ref is not None else None,
                skill_ref_kind=ref.kind if ref is not None else None, kind=item.skill_kind, min_proficiency=item.min_proficiency,
            )
        )  # fmt: skip
    return out, unknown


async def update_draft(
    ctx: RequestContext, db: AsyncSession, data: UpdateDraftIn, *, run_id: uuid.UUID | None
) -> UpdateDraftOut:
    from app.services.demands.common import UPDATE, acting_as, get_for
    from app.services.demands.records import _validate_ranges, set_skills

    demand = await get_for(ctx, db, data.demand_id, UPDATE)
    if demand.status != "draft":
        raise ConflictError(
            "Only draft demands can be updated directly; propose the change instead", code="demand_not_draft"
        )
    ai_fields = set(demand.ai_extracted_fields or [])
    changed: list[str] = []
    if data.fields is not None:
        sent = data.fields.model_fields_set
        for field in sent:
            if field not in ai_fields:
                raise _invalid(f"{field} was confirmed or edited by a person", "field_confirmed_by_user")
        with acting_as(db, "agent", getattr(ctx.principal, "id", None)):
            for field in sent:
                value = getattr(data.fields, field)
                if value is None:
                    continue
                setattr(demand, field, value)
                changed.append(field)
            _validate_ranges(demand)
            await db.flush()
    jd_version_id: uuid.UUID | None = None
    if data.skills is not None:
        has_skills = (
            await db.execute(select(DemandSkill.id).where(DemandSkill.demand_id == demand.id).limit(1))
        ).first() is not None
        if has_skills and not ({"mandatory_skills", "preferred_skills"} & ai_fields):
            raise _invalid("The skills were confirmed or edited by a person", "field_confirmed_by_user")
        items, unknown = await _skill_items(ctx, db, data.skills, allow_create=False)
        with acting_as(db, "agent", getattr(ctx.principal, "id", None)):
            await set_skills(ctx, db, demand, items)
        if unknown:
            await suggestions.add_skill_suggestions(
                ctx, db, demand,
                [SuggestSkillItem(label=u.label, skill_kind=u.skill_kind, min_proficiency=u.min_proficiency) for u in unknown],
                run_id=run_id,
            )  # fmt: skip
        changed.append("skills")
    if data.jd_content is not None:
        from app.services.agent_runtime.sanitize import outbound

        jd_version_id = await jd_mod.save_ai_version(
            ctx, db, demand, outbound(data.jd_content, max_len=20_000).text
        )
        changed.append("jd")
    await db.flush()
    return UpdateDraftOut(demand_id=demand.id, jd_version_id=jd_version_id, changed_fields=changed)


async def apply_update(
    ctx: RequestContext, db: AsyncSession, data: DemandUpdateIn, *, execution: str
) -> UpdateDraftOut:
    """``demands.update`` -- only ever executed from an approved Proposal (as the approver)."""
    from app.schemas.demands import JdVersionIn
    from app.services.demands.common import UPDATE, get_for
    from app.services.demands.jd import save_jd_version
    from app.services.demands.records import set_skills

    if execution == "agent":  # defence in depth: risk ``high`` never reaches auto
        raise ValidationFailed(
            "JD and skill changes on a non-draft demand need a person's approval", code="proposal_required"
        )
    demand = await get_for(ctx, db, data.demand_id, UPDATE)
    if demand.status == "draft":
        raise ConflictError("Use update_draft for draft demands", code="use_update_draft")
    changed: list[str] = []
    jd_version_id: uuid.UUID | None = None
    if data.skills is not None:
        items, _ = await _skill_items(ctx, db, data.skills, allow_create=True)
        await set_skills(ctx, db, demand, items)
        changed.append("skills")
    if data.jd_content is not None:
        out = await save_jd_version(ctx, db, demand.id, JdVersionIn(kind="full", content=data.jd_content))
        jd_version_id = out.id
        changed.append("jd")
    await db.flush()
    return UpdateDraftOut(demand_id=demand.id, jd_version_id=jd_version_id, changed_fields=changed)


def decimal_or_none(value: Any) -> Decimal | None:
    return None if value is None else Decimal(str(value))


__all__ = ["apply_update", "create_draft", "positions_in_source", "update_draft"]
