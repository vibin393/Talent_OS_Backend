# ruff: noqa: E501
"""Epic 41 story 41.3 (FR-211) *: transfer confirmation -- human only, the Worker transaction (new Placement +
offboarding, LWD + 1 = new start, rollback on failure), guards, the User record and ``InternalTransferConfirmed``."""

import uuid
from datetime import timedelta
from typing import Any

import pytest
from fastapi import FastAPI

from app.core.context import RequestContext
from app.core.database import tenant_session
from app.core.errors import DomainError
from app.events.mobility import InternalTransferConfirmed
from app.schemas.mobility import TransferConfirmIn
from app.services.mobility import transfers
from tests.modules.demands.helpers import DOrg, admin_ctx, h
from tests.modules.mobility.helpers import (
    TODAY,
    M,
    active_worker,
    apply_user,
    apply_worker,
    decide,
    demand,
    employee,
    overlay,
    rows,
    scalar,
    set_stage,
    stage_key,
    team_with_lead,
    worker,
)

EFFECTIVE = TODAY + timedelta(days=30)


async def _approved_worker(app: FastAPI, org: DOrg) -> tuple[str, Any, dict[str, Any], uuid.UUID]:
    am = await employee(app, org, "Avi AM", "account_manager")
    d = await demand(org, "internal", title="Internal Lead Engineer")
    p, cid = await active_worker(org, account_manager=am.user_id)
    w = await worker(app, org, cid)
    r = await apply_worker(w, d["id"])
    assert r.status_code == 201, r.text
    mid = r.json()["id"]
    _, req_id, _, _, app_id, _ = await overlay(org, mid)
    assert (await decide(am, req_id, "approve")).status_code == 200
    return mid, app_id, p, cid


@pytest.fixture
def captured(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    from app.core import events

    seen: list[Any] = []
    real = events.emit

    def spy(session: Any, event: Any) -> None:
        if isinstance(event, InternalTransferConfirmed):
            seen.append(event)
        real(session, event)

    monkeypatch.setattr(events, "emit", spy)
    monkeypatch.setattr(transfers, "emit", spy)
    return seen


@pytest.mark.parametrize("kind", ["ai", "workflow", "system", "api_key"])
async def test_confirm_refuses_non_human_actors(dorg: DOrg, kind: str) -> None:
    ctx = admin_ctx(dorg)
    ctx = RequestContext(tenant_id=ctx.tenant_id, principal=ctx.principal, actor_kind=kind)
    if kind == "api_key":
        ctx = RequestContext(tenant_id=dorg.tenant_id, principal=type("K", (), {"type": "api_key", "id": uuid.uuid4()})())
    async with tenant_session(dorg.tenant_id) as db:
        with pytest.raises(DomainError) as info:
            await transfers.confirm(ctx, db, uuid.uuid4(), TransferConfirmIn(effective_date=EFFECTIVE))
    assert (info.value.status_code, info.value.code) == (403, "human_only_action")


async def test_worker_transfer_one_transaction_no_gap(app: FastAPI, dorg: DOrg, captured: list[Any]) -> None:
    mid, app_id, p, _ = await _approved_worker(app, dorg)
    url = f"{M}/applications/{mid}/confirm-transfer"
    early = await dorg.admin.client.post(url, json={"effective_date": EFFECTIVE.isoformat()}, headers=h(dorg.admin))
    assert early.status_code == 409 and early.json()["error"]["code"] == "not_selected", early.text
    ctx_early = (await dorg.admin.client.get(f"{M}/applications/{mid}/transfer-context")).json()
    assert ctx_early["can_confirm"] is False and ctx_early["blocking_reason"]

    await set_stage(dorg, app_id, "selected")
    ctx_out = await dorg.admin.client.get(f"{M}/applications/{mid}/transfer-context")
    assert ctx_out.status_code == 200, ctx_out.text
    body = ctx_out.json()
    assert body["can_confirm"] is True and body["current_placement"]["id"] == p["id"]
    assert body["current_placement"]["pay_rate"] is not None  # admin holds compensation:view

    past = await dorg.admin.client.post(url, json={"effective_date": TODAY.isoformat()}, headers=h(dorg.admin))
    assert past.status_code == 422 and past.json()["error"]["code"] == "effective_date_invalid"
    viewer = await employee(app, dorg, "Vic Viewer")
    assert (await viewer.client.post(url, json={"effective_date": EFFECTIVE.isoformat()}, headers=viewer.h())).status_code in (403, 404)

    ok = await dorg.admin.client.post(url, json={"effective_date": EFFECTIVE.isoformat(), "pay_rate": "1200.00"}, headers=h(dorg.admin))
    assert ok.status_code == 200, ok.text
    t = ok.json()["transfer"]
    assert t["from_placement_id"] == p["id"] and t["to_placement_id"]
    assert t["previous_last_working_day"] == (EFFECTIVE - timedelta(days=1)).isoformat()
    new_start, new_app, new_role = (
        await rows(dorg, "SELECT planned_start_date, application_id, role_title FROM placements WHERE id = :i", i=uuid.UUID(t["to_placement_id"]))
    )[0]
    lwd, reason, note = (
        await rows(dorg, "SELECT last_working_day, reason, reason_note FROM offboarding_cases WHERE placement_id = :i", i=uuid.UUID(p["id"]))
    )[0]
    assert new_start == EFFECTIVE and lwd + timedelta(days=1) == new_start
    assert reason == "other" and note.startswith("Internal transfer to ")
    assert new_app == app_id and new_role == "Internal Lead Engineer"
    assert await scalar(dorg, "SELECT status FROM placements WHERE id = :i", i=uuid.UUID(p["id"])) == "offboarding"
    assert await stage_key(dorg, app_id) == "hired"
    assert (await overlay(dorg, mid))[0] == "transfer_confirmed"
    assert len(captured) == 1 and captured[0].new_placement_id == uuid.UUID(t["to_placement_id"])
    assert captured[0].employee_kind == "worker" and captured[0].from_placement_id == uuid.UUID(p["id"])
    assert await scalar(dorg, "SELECT count(*) FROM audit_logs WHERE action = 'mobility.transfer_confirmed'") >= 1

    again = await dorg.admin.client.post(url, json={"effective_date": EFFECTIVE.isoformat()}, headers=h(dorg.admin))
    assert again.status_code == 409 and len(captured) == 1


async def test_offboarding_failure_rolls_back_the_placement(app: FastAPI, dorg: DOrg, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services.placements import offboarding

    mid, app_id, _, cid = await _approved_worker(app, dorg)
    await set_stage(dorg, app_id, "selected")
    before = await scalar(dorg, "SELECT count(*) FROM placements WHERE candidate_id = :c", c=cid)

    async def boom(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("offboarding exploded")

    monkeypatch.setattr(offboarding, "initiate", boom)
    async with tenant_session(dorg.tenant_id) as db:
        with pytest.raises(RuntimeError):
            await transfers.confirm(admin_ctx(dorg), db, uuid.UUID(mid), TransferConfirmIn(effective_date=EFFECTIVE))
    assert await scalar(dorg, "SELECT count(*) FROM placements WHERE candidate_id = :c", c=cid) == before
    assert (await overlay(dorg, mid))[0] == "approved"
    assert await scalar(dorg, "SELECT count(*) FROM internal_transfers") == 0


async def test_open_offboarding_blocks_transfer(app: FastAPI, dorg: DOrg) -> None:
    from app.services.placements import offboarding

    mid, app_id, p, _ = await _approved_worker(app, dorg)
    await set_stage(dorg, app_id, "selected")
    async with tenant_session(dorg.tenant_id) as db:
        await offboarding.initiate(admin_ctx(dorg), db, uuid.UUID(p["id"]), "worker_resignation", TODAY + timedelta(days=10))
    r = await dorg.admin.client.post(
        f"{M}/applications/{mid}/confirm-transfer", json={"effective_date": EFFECTIVE.isoformat()}, headers=h(dorg.admin)
    )
    assert r.status_code == 409 and r.json()["error"]["code"] == "offboarding_in_progress", r.text


async def test_user_transfer_stores_record_only(app: FastAPI, dorg: DOrg, captured: list[Any]) -> None:
    d = await demand(dorg, "internal", title="Internal Product Owner")
    emp = await employee(app, dorg, "Uma User")
    lead = await employee(app, dorg, "Leo Lead")
    team = await team_with_lead(dorg, lead.user_id, emp.user_id)
    r = await apply_user(emp, d["id"])
    assert r.status_code == 201, r.text
    mid = r.json()["id"]
    _, req_id, _, _, app_id, cand = await overlay(dorg, mid)
    assert (await decide(lead, req_id, "approve")).status_code == 200
    await set_stage(dorg, app_id, "selected")
    placements_before = await scalar(dorg, "SELECT count(*) FROM placements")
    ok = await dorg.admin.client.post(
        f"{M}/applications/{mid}/confirm-transfer",
        json={"effective_date": EFFECTIVE.isoformat(), "to_role_title": "Product Owner"},
        headers=h(dorg.admin),
    )
    assert ok.status_code == 200, ok.text
    t = ok.json()["transfer"]
    assert t["to_placement_id"] is None and t["from_team_id"] == str(team) and t["to_role_title"] == "Product Owner"
    assert await scalar(dorg, "SELECT count(*) FROM placements") == placements_before
    assert await stage_key(dorg, app_id) == "hired"
    assert len(captured) == 1 and captured[0].employee_kind == "user" and captured[0].user_id == emp.user_id
    assert captured[0].candidate_id == cand and captured[0].new_placement_id is None
    (mine,) = (await emp.client.get(f"{M}/my-applications")).json()
    assert mine["status"] == "transfer_confirmed" and mine["stage_name"] == "Hired"
