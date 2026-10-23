# ruff: file-ignore[line-too-long]
"""Slot -> draft Demand conversion (Epic 44, FR-224).

``POST /workforce/slots/convert`` (``workforce_plan:write`` and ``demand:create``): one draft Demand per open,
unconverted slot, created ONLY through the foundation's ``demands_service.create_draft`` with
``workforce_plan_line_id`` (the effective line) and the line's ``project_role_id``. The client is the plan's, else
the body's (422 ``client_required`` when both are missing; a client other than the line's -> the foundation's 422
``plan_line_client_mismatch``). Demand approval chains apply unchanged (P1 submit -> approval). Audited as
``workforce.slots_converted`` with the slot and Demand ids.
"""

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.context import RequestContext
from app.core.errors import NotFound
from app.core.skill_refs import SkillKey
from app.models.workforce import HeadcountSlot, WorkforcePlan, WorkforcePlanLine
from app.schemas.workforce import ConvertedSlot, ConvertIn, ConvertOut
from app.services.workforce.common import WRITE, conflict, effective_line_for, get_plan, invalid, now

EMPLOYMENT_BY_LINE_TYPE = {
    "new": "permanent",
    "replacement": "permanent",
    "project": "contract",
    "recurring": "contract",
}


def demand_payload(line: WorkforcePlanLine, client_id: uuid.UUID, overrides: dict[str, Any]) -> Any:
    from app.schemas.demands import DemandCreate, DemandSkillIn

    skills = []
    for s in line.skills or []:
        item: dict[str, Any] = {"label": str(s.get("name") or "")[:200] or "Skill", "kind": "mandatory"}
        if s.get("skill_key"):
            key = SkillKey.parse(str(s["skill_key"]))
            item["skill_id"] = key.id
            item["skill_ref_kind"] = key.kind
        if s.get("min_proficiency"):
            item["min_proficiency"] = int(s["min_proficiency"])
        skills.append(DemandSkillIn(**item))
    data: dict[str, Any] = {
        "title": overrides.get("title") or line.role_title,
        "client_id": client_id,
        "positions": 1,
        "employment_type": EMPLOYMENT_BY_LINE_TYPE.get(line.line_type, "permanent"),
        "start_date": overrides.get("start_date") or line.period_start,
    }
    if line.location_text:
        data["location"] = line.location_text[:300]
    if line.work_mode:
        data["work_mode"] = line.work_mode
    if skills:
        data["skills"] = skills
    for key in ("priority", "target_closure_date"):
        if overrides.get(key) is not None:
            data[key] = overrides[key]
    return DemandCreate(**data)


async def convert(ctx: RequestContext, db: AsyncSession, body: ConvertIn) -> ConvertOut:
    from app.services import demands_service

    ids = list(dict.fromkeys(body.slot_ids))
    rows = {
        s.id: s
        for s in (
            await db.execute(
                select(HeadcountSlot)
                .where(HeadcountSlot.id.in_(ids))
                .order_by(HeadcountSlot.seq)
                .with_for_update()
            )
        ).scalars()
    }
    if len(rows) != len(ids):
        raise NotFound("Headcount slot not found")
    plans: dict[uuid.UUID, WorkforcePlan] = {}
    for slot in rows.values():
        if slot.workforce_plan_id not in plans:
            plans[slot.workforce_plan_id] = await get_plan(ctx, db, slot.workforce_plan_id, permission=WRITE)
        if slot.status != "open" or slot.demand_id is not None:
            raise conflict(
                "slot_not_convertible",
                "Only open slots without a Demand can be converted",
                slot_id=str(slot.id),
                status=slot.status,
            )
    overrides = body.overrides.model_dump(exclude_none=True) if body.overrides else {}
    converted: list[ConvertedSlot] = []
    for slot_id in ids:
        slot = rows[slot_id]
        plan = plans[slot.workforce_plan_id]
        line = await effective_line_for(db, plan, slot.plan_line_id)
        if line is None:
            raise conflict(
                "slot_not_convertible", "The slot's line is not in the approved plan", slot_id=str(slot.id)
            )
        client_id = plan.client_id or body.client_id
        if client_id is None:
            raise invalid("client_required", "Choose the client for the Demands", field="client_id")
        demand = await demands_service.create_draft(
            ctx,
            db,
            demand_payload(line, client_id, overrides),
            workforce_plan_line_id=line.id,
            project_role_id=line.project_role_id,
        )
        slot.demand_id = uuid.UUID(str(demand["id"]))
        slot.converted_at = now()
        await db.flush()
        converted.append(
            ConvertedSlot(slot_id=slot.id, demand_id=slot.demand_id, demand_human_id=demand.get("req_id"))
        )
    audit.record(
        "workforce.slots_converted",
        "workforce_plan",
        next(iter(plans)),
        session=db,
        slot_ids=[str(c.slot_id) for c in converted],
        demand_ids=[str(c.demand_id) for c in converted],
    )
    return ConvertOut(converted=converted)
