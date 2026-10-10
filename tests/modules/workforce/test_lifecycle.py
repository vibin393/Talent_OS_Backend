# ruff: noqa: E501
"""Epic 44 stories 44.2 / 44.3 (FR-223 / FR-224): approval ★, revisions, slots, conversion and slot handlers."""

import uuid
from typing import Any

import pytest

from app.core import events
from app.core.actors import ActorRef
from app.core.database import tenant_session
from app.core.errors import Forbidden
from app.events.workforce import PlanLineApproved
from app.schemas.approvals import DecideIn
from app.services import approvals
from tests.modules.commercials.helpers import COrg
from tests.modules.workforce.helpers import (
    WF,
    act,
    activate_placement,
    add_line,
    approved_plan,
    candidate,
    decide,
    err,
    ok,
    open_demand,
    plan,
    slots,
)


@pytest.fixture
def approved_events() -> Any:
    seen: list[PlanLineApproved] = []

    def handler(e: PlanLineApproved) -> None:
        seen.append(e)

    events.subscribe(PlanLineApproved, handler)
    yield seen
    events._handlers[PlanLineApproved].remove(handler)


async def _lines(org: COrg, plan_id: str, revision: int | None = None) -> list[dict[str, Any]]:
    url = f"{WF}/plans/{plan_id}/lines" + (f"?revision={revision}" if revision is not None else "")
    return list(await ok(await org.c.get(url, headers=org.h())))


async def test_approval_creates_slots_and_emits(corg: COrg, approved_events: list[PlanLineApproved]) -> None:
    p = await approved_plan(
        corg, client_id=str(corg.client_id), lines=[{"headcount": 3}, {"role_title": "QA", "headcount": 2}]
    )
    assert p["approved_revision"] == 0 and p["slots"]["open"] == 5
    assert {(e.headcount, e.client_id) for e in approved_events} == {(3, corg.client_id), (2, corg.client_id)}
    assert all(ln["status"] == "approved" for ln in await _lines(corg, p["id"]))
    revs = await ok(await corg.c.get(f"{WF}/plans/{p['id']}/revisions", headers=corg.h()))
    assert [(r["revision_no"], r["status"]) for r in revs] == [(0, "approved")]
    err(await corg.c.delete(f"{WF}/plans/{p['id']}", headers=corg.h()), 409, "plan_not_deletable")
    err(
        await corg.c.post(
            f"{WF}/plans/{p['id']}/lines",
            json={"role_title": "X", "headcount": 1, "start_month": "2027-05"},
            headers=corg.h(),
        ),
        409,
        "plan_not_editable",
    )


async def test_rejection_and_reopen(corg: COrg) -> None:
    p = await plan(corg)
    await add_line(corg, p["id"])
    await act(corg, p["id"], "budget")
    sub = await act(corg, p["id"], "submit")
    await decide(corg, sub["approval_request_id"], "reject", "Too expensive")
    out = await ok(await corg.c.get(f"{WF}/plans/{p['id']}", headers=corg.h()))
    assert out["status"] == "rejected" and out["slots"]["open"] == 0
    assert (await act(corg, p["id"], "reopen"))["status"] == "draft"


async def test_human_only_approval(corg: COrg) -> None:
    p = await plan(corg)
    await add_line(corg, p["id"])
    await act(corg, p["id"], "budget")
    sub = await act(corg, p["id"], "submit")
    request_id = uuid.UUID(sub["approval_request_id"])
    for kind in ("ai", "workflow", "system", "api_key"):
        async with tenant_session(corg.tenant_id) as db:
            with pytest.raises(Forbidden) as exc:
                await approvals.decide(
                    corg.ctx(), db, request_id, DecideIn(action="approve"), actor=ActorRef(kind, None)
                )  # type: ignore[arg-type]
            assert exc.value.code == "human_only_action"
    still = await ok(await corg.c.get(f"{WF}/plans/{p['id']}", headers=corg.h()))
    assert still["status"] == "pending_approval"
    # the subject callback itself refuses a non-human actor
    from app.services.approvals.registry import FinalDecision
    from app.services.workforce import approval

    async with tenant_session(corg.tenant_id) as db:
        with pytest.raises(Forbidden):
            await approval.on_final_decided(
                corg.ctx(), db,
                FinalDecision(request_id, "workforce_plan", uuid.UUID(p["id"]), "approved", ActorRef("workflow", None), None),
            )  # fmt: skip


async def test_revision_only_changed_lines(corg: COrg, approved_events: list[PlanLineApproved]) -> None:
    p = await approved_plan(corg, lines=[{"headcount": 5}, {"role_title": "QA", "headcount": 1}])
    approved_events.clear()
    rev = await act(corg, p["id"], "start-revision", reason="More Java")
    assert (
        rev["status"] == "revision_draft" and rev["current_revision"] == 1 and rev["approved_revision"] == 0
    )
    lines = await _lines(corg, p["id"])
    assert {ln["change_kind"] for ln in lines} == {"unchanged"}
    java = next(ln for ln in lines if ln["role_title"] == "Java Developer")
    # while the revision is drafted the approved revision stays effective
    assert len(await slots(corg, p["id"], "open")) == 6
    await ok(
        await corg.c.patch(
            f"{WF}/plans/{p['id']}/lines/{java['id']}", json={"headcount": 7}, headers=corg.h()
        )
    )
    diff = await ok(await corg.c.get(f"{WF}/plans/{p['id']}/revisions/1/diff", headers=corg.h()))
    assert len(diff["lines"]) == 1 and diff["lines"][0]["changed_fields"] == ["headcount"]
    assert diff["lines"][0]["before"]["headcount"] == 5 and diff["lines"][0]["after"]["headcount"] == 7
    sub = await act(corg, p["id"], "submit")
    assert sub["status"] == "pending_approval" and sub["approved_revision"] == 0
    await decide(corg, sub["approval_request_id"])
    assert len(await slots(corg, p["id"], "open")) == 8
    assert [(e.headcount) for e in approved_events] == [7]
    out = await ok(await corg.c.get(f"{WF}/plans/{p['id']}", headers=corg.h()))
    assert out["approved_revision"] == 1 and out["status"] == "approved"
    # discard: back to the approved revision
    await act(corg, p["id"], "start-revision")
    disc = await act(corg, p["id"], "discard-revision")
    assert disc["status"] == "approved" and disc["current_revision"] == 1


async def test_revision_conflicts_with_committed_slots(corg: COrg) -> None:
    p = await approved_plan(
        corg, client_id=str(corg.client_id), lines=[{"headcount": 5}, {"role_title": "QA", "headcount": 1}]
    )
    java_open = [s for s in await slots(corg, p["id"]) if s["role_title"] == "Java Developer"]
    qa_open = [s for s in await slots(corg, p["id"]) if s["role_title"] == "QA"]
    conv = await ok(
        await corg.c.post(
            f"{WF}/slots/convert",
            json={"slot_ids": [s["id"] for s in java_open[:4]] + [qa_open[0]["id"]]},
            headers=corg.h(),
        )
    )
    for c in conv["converted"][:4]:
        await open_demand(corg, c["demand_id"])
    await act(corg, p["id"], "start-revision")
    lines = await _lines(corg, p["id"])
    java = next(ln for ln in lines if ln["role_title"] == "Java Developer")
    qa = next(ln for ln in lines if ln["role_title"] == "QA")
    await ok(
        await corg.c.patch(
            f"{WF}/plans/{p['id']}/lines/{java['id']}", json={"headcount": 2}, headers=corg.h()
        )
    )
    err(await corg.c.post(f"{WF}/plans/{p['id']}/submit", headers=corg.h()), 409, "headcount_below_committed")
    await ok(
        await corg.c.patch(
            f"{WF}/plans/{p['id']}/lines/{java['id']}", json={"headcount": 5}, headers=corg.h()
        )
    )
    assert (
        await corg.c.delete(f"{WF}/plans/{p['id']}/lines/{qa['id']}", headers=corg.h())
    ).status_code == 204
    err(await corg.c.post(f"{WF}/plans/{p['id']}/submit", headers=corg.h()), 409, "line_has_committed_slots")


async def test_convert_and_slot_handlers(corg: COrg) -> None:
    p = await approved_plan(corg, client_id=str(corg.client_id), lines=[{"headcount": 5}])
    all_slots = await slots(corg, p["id"])
    assert len(all_slots) == 5
    conv = await ok(
        await corg.c.post(
            f"{WF}/slots/convert", json={"slot_ids": [s["id"] for s in all_slots[:3]]}, headers=corg.h()
        )
    )
    assert len(conv["converted"]) == 3 and len({c["demand_id"] for c in conv["converted"]}) == 3
    (line,) = await _lines(corg, p["id"])
    for c in conv["converted"]:
        d = await ok(await corg.c.get(f"/api/v1/demands/{c['demand_id']}", headers=corg.h()))
        assert d["status"] == "draft" and d["workforce_plan_line_id"] == line["id"] and d["positions"] == 1
    # converting again -> 409
    err(
        await corg.c.post(f"{WF}/slots/convert", json={"slot_ids": [all_slots[0]["id"]]}, headers=corg.h()),
        409,
        "slot_not_convertible",
    )
    # DemandOpened -> requisitioned
    first = conv["converted"][0]
    await open_demand(corg, first["demand_id"])
    by_id = {s["id"]: s for s in await slots(corg, p["id"])}
    assert by_id[first["slot_id"]]["status"] == "requisitioned"
    assert (await _lines(corg, p["id"]))[0]["status"] == "requisitioned"
    # PlacementActivated -> filled
    await activate_placement(corg, uuid.UUID(first["demand_id"]), await candidate(corg))
    by_id = {s["id"]: s for s in await slots(corg, p["id"])}
    assert by_id[first["slot_id"]]["status"] == "filled" and by_id[first["slot_id"]]["placement_id"]
    # cancelled Demand releases its slot
    second = conv["converted"][1]
    await open_demand(corg, second["demand_id"])
    await ok(
        await corg.c.post(
            f"/api/v1/demands/{second['demand_id']}/transition",
            json={"to": "cancelled", "reason_code": "duplicate"},
            headers=corg.h(),
        )
    )
    released = {s["id"]: s for s in await slots(corg, p["id"])}[second["slot_id"]]
    assert released["status"] == "open" and released["demand_id"] is None
    # a manual Demand with the line id claims the lowest open slot
    from app.schemas.demands import DemandCreate
    from app.services import demands_service

    async with tenant_session(corg.tenant_id) as db:
        manual = await demands_service.create_draft(
            corg.ctx(),
            db,
            DemandCreate(title="Manual", client_id=corg.client_id),
            workforce_plan_line_id=uuid.UUID(line["id"]),
        )
    await open_demand(corg, str(manual["id"]))
    claimed = [s for s in await slots(corg, p["id"]) if s["demand_id"] == str(manual["id"])]
    assert len(claimed) == 1 and claimed[0]["status"] == "requisitioned" and claimed[0]["seq"] == 2
    # cancel an open slot; one holding a Demand cannot be cancelled
    free = next(s for s in await slots(corg, p["id"], "open") if s["demand_id"] is None)
    out = await ok(
        await corg.c.post(f"{WF}/slots/{free['id']}/cancel", json={"reason": "Budget cut"}, headers=corg.h())
    )
    assert out["status"] == "cancelled" and out["cancel_reason"] == "Budget cut"
    third = conv["converted"][2]
    err(
        await corg.c.post(f"{WF}/slots/{third['slot_id']}/cancel", json={"reason": "x"}, headers=corg.h()),
        409,
        "slot_has_demand",
    )


async def test_convert_requires_client(corg: COrg, other_corg: COrg) -> None:
    p = await approved_plan(corg, lines=[{"headcount": 1}])
    (slot,) = await slots(corg, p["id"])
    err(
        await corg.c.post(f"{WF}/slots/convert", json={"slot_ids": [slot["id"]]}, headers=corg.h()),
        422,
        "client_required",
    )
    conv = await ok(
        await corg.c.post(
            f"{WF}/slots/convert",
            json={"slot_ids": [slot["id"]], "client_id": str(corg.client_id)},
            headers=corg.h(),
        )
    )
    assert len(conv["converted"]) == 1
    # a client plan: another client -> the foundation's plan_line_client_mismatch
    from tests.modules.demands.helpers import create_client

    other_client = await create_client(corg.admin, "Other Co")
    p2 = await approved_plan(corg, client_id=str(corg.client_id), lines=[{"headcount": 1}])
    (s2,) = await slots(corg, p2["id"])
    async with tenant_session(corg.tenant_id) as db:
        from app.models.workforce import WorkforcePlan

        row = await db.get(WorkforcePlan, uuid.UUID(p2["id"]))
        assert row is not None
        row.client_id = None  # simulate a plan-level client cleared after approval; the line keeps its client
    err(
        await corg.c.post(
            f"{WF}/slots/convert",
            json={"slot_ids": [s2["id"]], "client_id": str(other_client)},
            headers=corg.h(),
        ),
        422,
        "plan_line_client_mismatch",
    )
    # cross-tenant slot -> 404
    err(
        await other_corg.c.post(f"{WF}/slots/convert", json={"slot_ids": [s2["id"]]}, headers=other_corg.h()),
        404,
        "not_found",
    )
