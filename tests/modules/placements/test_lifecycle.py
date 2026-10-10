"""Creation from an accepted offer, direct creation / PATCH, Joined with start gates and overrides, Dropped."""

import asyncio
import uuid
from datetime import date, timedelta
from typing import Any

from sqlalchemy import select, text

from app.core.database import tenant_session
from app.models.placements import Placement, RateSegment, StartGate, WorkerProfile
from app.services.placements import creation, gates
from app.services.placements.registry import GateState, StartGateKindDef, register_start_gate_kind
from tests.modules.demands.helpers import DEMANDS, DOrg, admin_ctx, h
from tests.modules.placements.helpers import (
    PLACEMENTS,
    accepted_placement,
    direct_placement,
    placement_for_offer,
    waive_all,
)


async def test_offer_accepted_creates_one_placement_with_defaults(dorg: DOrg) -> None:
    s, offer, p = await accepted_placement(dorg)
    assert p.human_id.startswith("PLC-")
    assert p.status == "pre_joining"
    assert (
        p.candidate_id == s.candidate_id
        and p.demand_id == s.demand_id
        and p.application_id == s.application_id
    )
    assert p.legal_entity_id is not None
    assert p.worker_type == "employee" and p.payee_kind == "worker" and p.engagement_kind == "contract"
    assert "contract_missing" in p.setup_items and "po_missing" in p.setup_items
    async with tenant_session(dorg.tenant_id) as db:
        segs = list((await db.execute(select(RateSegment).where(RateSegment.placement_id == p.id))).scalars())
        assert (
            len(segs) == 1 and segs[0].status == "active" and segs[0].effective_from == p.planned_start_date
        )
        assert segs[0].margin_amount is not None
        prof = (
            await db.execute(select(WorkerProfile).where(WorkerProfile.candidate_id == p.candidate_id))
        ).scalar_one()
        assert prof.worker_human_id.startswith("WRK-")
        gates_ = list((await db.execute(select(StartGate).where(StartGate.placement_id == p.id))).scalars())
        assert {g.kind for g in gates_} >= {"contract"}
    # idempotent: a second (and a concurrent) run returns the same placement
    ctx = admin_ctx(dorg)

    async def again() -> uuid.UUID:
        async with tenant_session(dorg.tenant_id) as db:
            return (await creation.create_from_offer(ctx, db, uuid.UUID(offer["id"]))).id

    ids = await asyncio.gather(again(), again())
    assert set(ids) == {p.id}
    async with tenant_session(dorg.tenant_id) as db:
        count = (
            await db.execute(
                text("SELECT count(*) FROM placements WHERE offer_id = :o"), {"o": uuid.UUID(offer["id"])}
            )
        ).scalar_one()
    assert count == 1


async def test_setup_items_when_vendors_unimplemented(dorg: DOrg, monkeypatch: Any) -> None:
    from app.services import vendors_service

    async def boom(*_: Any, **__: Any) -> Any:
        raise NotImplementedError

    monkeypatch.setattr(vendors_service, "sourcing_for_application", boom)
    _, _, p = await accepted_placement(dorg)
    assert "vendor_unchecked" in p.setup_items
    assert p.vendor_id is None


async def test_direct_create_patch_and_stale(dorg: DOrg) -> None:
    body = await direct_placement(dorg)
    assert body["status"] == "pre_joining" and body["human_id"].startswith("PLC-")
    pid = body["id"]
    resp = await dorg.admin.client.patch(
        f"{PLACEMENTS}/{pid}",
        json={"lock_version": body["lock_version"], "work_location_text": "Pune"},
        headers=h(dorg.admin),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["work_location_text"] == "Pune"
    stale = await dorg.admin.client.patch(
        f"{PLACEMENTS}/{pid}",
        json={"lock_version": body["lock_version"], "role_title": "x"},
        headers=h(dorg.admin),
    )
    assert stale.status_code == 409 and stale.json()["error"]["code"] == "stale"
    listed = (await dorg.admin.client.get(PLACEMENTS, params={"q": body["human_id"]})).json()
    assert [i["id"] for i in listed["items"]] == [pid]


async def test_join_refused_until_gates_clear_then_override(dorg: DOrg) -> None:
    body = await direct_placement(dorg)
    pid = body["id"]
    today = date.today().isoformat()
    resp = await dorg.admin.client.post(
        f"{PLACEMENTS}/{pid}/join", json={"actual_start_date": today}, headers=h(dorg.admin)
    )
    assert resp.status_code == 409, resp.text
    err = resp.json()["error"]
    assert err["code"] == "start_gates_unmet"
    assert any(g["kind"] == "contract" for g in err["details"]["gates"])
    future = (date.today() + timedelta(days=3)).isoformat()
    await waive_all(dorg, pid)
    overrides = (await dorg.admin.client.get(f"{PLACEMENTS}/{pid}/gate-overrides")).json()
    assert overrides[0]["status"] == "approved" and overrides[0]["reason"] == "Client confirmed in writing"
    resp = await dorg.admin.client.post(
        f"{PLACEMENTS}/{pid}/join", json={"actual_start_date": future}, headers=h(dorg.admin)
    )
    assert resp.status_code == 422 and resp.json()["error"]["code"] == "start_in_future"
    resp = await dorg.admin.client.post(
        f"{PLACEMENTS}/{pid}/join", json={"actual_start_date": today}, headers=h(dorg.admin)
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "active" and resp.json()["actual_start_date"] == today
    hist = (await dorg.admin.client.get(f"{PLACEMENTS}/{pid}/history")).json()
    assert [r["to_status"] for r in hist] == ["pre_joining", "active"]


async def test_resolver_refresh_catches_unhandled_gate(dorg: DOrg) -> None:
    body = await direct_placement(dorg)
    pid = uuid.UUID(body["id"])
    kind = f"t{uuid.uuid4().hex[:8]}"
    state = {"status": "pending"}

    async def resolver(ctx: Any, db: Any, placement_id: uuid.UUID) -> list[GateState]:
        if placement_id != pid:
            return []
        return [GateState("x", state["status"], True, {})]  # type: ignore[arg-type]

    register_start_gate_kind(kind, StartGateKindDef("Test gate", overridable=True, resolver=resolver))
    await waive_all(dorg, str(pid))  # waives contract (+ test gate if already present)
    async with tenant_session(dorg.tenant_id) as db:
        await db.execute(
            text("DELETE FROM start_gates WHERE placement_id = :p AND kind = :k"), {"p": pid, "k": kind}
        )
    # the gate has never been written: Joined re-runs the resolver and refuses
    resp = await dorg.admin.client.post(
        f"{PLACEMENTS}/{pid}/join",
        json={"actual_start_date": date.today().isoformat()},
        headers=h(dorg.admin),
    )
    assert resp.status_code == 409 and resp.json()["error"]["details"]["gates"][0]["kind"] == kind
    state["status"] = "met"
    resp = await dorg.admin.client.post(
        f"{PLACEMENTS}/{pid}/join",
        json={"actual_start_date": date.today().isoformat()},
        headers=h(dorg.admin),
    )
    assert resp.status_code == 200, resp.text


async def test_failed_bgv_is_not_overridable(dorg: DOrg) -> None:
    body = await direct_placement(dorg)
    pid = uuid.UUID(body["id"])
    ctx = admin_ctx(dorg)
    case = uuid.uuid4()
    async with tenant_session(dorg.tenant_id) as db:
        await gates.handle_upstream(
            ctx, db, "BgvCaseOpened", {"placement_id": str(pid), "case_id": str(case), "required": True}
        )
    async with tenant_session(dorg.tenant_id) as db:
        await gates.handle_upstream(
            ctx,
            db,
            "BgvCaseAdjudicated",
            {"placement_id": str(pid), "case_id": str(case), "outcome": "failed", "with_exceptions": False},
        )
    gl = (await dorg.admin.client.get(f"{PLACEMENTS}/{pid}/start-gates")).json()
    bgv = next(g for g in gl if g["kind"] == "bgv")
    assert bgv["status"] == "failed" and bgv["overridable"] is False
    resp = await dorg.admin.client.post(
        f"{PLACEMENTS}/{pid}/gate-overrides",
        json={"gate_ids": [bgv["id"]], "reason": "Please let them start"},
        headers=h(dorg.admin),
    )
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "gate_not_overridable"
    # BGV pending is refused at Joined; a later clear adjudication meets it (idempotent per case)
    detail = (await dorg.admin.client.get(f"{PLACEMENTS}/{pid}")).json()
    assert detail["bgv_summary"]["status"] == "adjudicated_failed"


async def test_drop_on_filled_demand_decrements_and_notifies(dorg: DOrg) -> None:
    s, _offer, p = await accepted_placement(dorg)
    demand = (await dorg.admin.client.get(f"{DEMANDS}/{s.demand_id}")).json()
    assert demand["status"] == "filled" and demand["positions_filled"] == 1
    resp = await dorg.admin.client.post(
        f"{PLACEMENTS}/{p.id}/drop",
        json={"reason": "no_show", "note": "Did not turn up"},
        headers=h(dorg.admin),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "dropped" and resp.json()["drop_reason"] == "no_show"
    demand = (await dorg.admin.client.get(f"{DEMANDS}/{s.demand_id}")).json()
    assert demand["status"] == "filled" and demand["positions_filled"] == 0  # never reopened automatically
    async with tenant_session(dorg.tenant_id) as db:
        rows = (
            await db.execute(
                text(
                    "SELECT type FROM notifications WHERE recipient_id = :u AND type = 'placements.demand_reopen_suggested'"
                ),
                {"u": dorg.admin.user_id},
            )
        ).all()
    assert rows, "the demand owner is told they can reopen"
    again = await dorg.admin.client.post(
        f"{PLACEMENTS}/{p.id}/drop", json={"reason": "no_show"}, headers=h(dorg.admin)
    )
    assert again.status_code == 409 and again.json()["error"]["code"] == "transition_not_allowed"


async def test_cross_tenant_404(dorg: DOrg, other_dorg: DOrg) -> None:
    body = await direct_placement(dorg)
    pid = body["id"]
    m = other_dorg.admin
    for method, path, payload in (
        ("get", f"{PLACEMENTS}/{pid}", None),
        ("get", f"{PLACEMENTS}/{pid}/rate-segments", None),
        ("get", f"{PLACEMENTS}/{pid}/start-gates", None),
        ("get", f"{PLACEMENTS}/{pid}/history", None),
        ("post", f"{PLACEMENTS}/{pid}/join", {"actual_start_date": date.today().isoformat()}),
        ("post", f"{PLACEMENTS}/{pid}/drop", {"reason": "declined"}),
        (
            "post",
            f"{PLACEMENTS}/{pid}/rate-segments",
            {"effective_from": "2030-01-01", "pay_rate": "1", "reason": "x"},
        ),
        ("get", f"/api/v1/workers/{body['candidate_id']}", None),
    ):
        fn = getattr(m.client, method)
        resp = await (fn(path, json=payload, headers=h(m)) if method == "post" else fn(path))
        assert resp.status_code == 404, (path, resp.status_code, resp.text)


async def test_placement_lookup_helper(dorg: DOrg) -> None:
    _, offer, p = await accepted_placement(dorg)
    again = await placement_for_offer(dorg, offer["id"])
    assert again is not None and again.id == p.id
    async with tenant_session(dorg.tenant_id) as db:
        assert (
            await db.execute(select(Placement.status).where(Placement.id == p.id))
        ).scalar_one() == "pre_joining"
