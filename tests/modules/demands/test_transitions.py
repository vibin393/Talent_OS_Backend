"""Stories 4.1/4.2: the §5.1 transition table, reasons, auto-Filled, reopen guard, auto-unpublish."""

import itertools
import uuid

import pytest

from app.core.database import tenant_session
from app.core.errors import ConflictError, DomainError, Forbidden, ValidationFailed
from app.models.demands import Demand
from app.services import demands as svc
from app.services.demands.transitions import STATUSES, TRANSITIONS
from tests.modules.demands.helpers import DEMANDS, DOrg, admin_ctx, create_demand, h, open_demand

ALLOWED = sorted(TRANSITIONS)
DISALLOWED = sorted(set(itertools.product(STATUSES, STATUSES)) - set(TRANSITIONS))


async def _try(org: DOrg, demand_id: str, from_status: str, to: str, actor: str) -> DomainError | None:
    ctx = admin_ctx(org)
    async with tenant_session(org.tenant_id) as db:
        demand = await db.get(Demand, uuid.UUID(demand_id))
        assert demand is not None
        demand.status = from_status  # test setup only (bypasses the service on purpose)
        await db.flush()
        savepoint = await db.begin_nested()
        try:
            await svc.transition_demand(ctx, db, demand, to, actor, reason_code="other", comment="c")
            assert demand.status == to
            return None
        except DomainError as exc:
            return exc
        finally:
            await savepoint.rollback()


async def test_every_table_row_is_allowed(dorg: DOrg) -> None:
    d = await create_demand(dorg.admin, dorg.client_id)
    for from_status, to in ALLOWED:
        actor = "system" if (from_status, to) == ("open", "filled") else "user"
        error = await _try(dorg, d["id"], from_status, to, actor)
        assert error is None, (from_status, to, error)


async def test_every_other_pair_is_refused_with_409(dorg: DOrg) -> None:
    d = await create_demand(dorg.admin, dorg.client_id)
    for from_status, to in DISALLOWED:
        error = await _try(dorg, d["id"], from_status, to, "user")
        assert isinstance(error, ConflictError), (from_status, to, error)
        assert error.code == "invalid_transition"


@pytest.mark.parametrize("pair", [("open", "on_hold"), ("draft", "open"), ("filled", "open")])
async def test_system_actor_may_only_fill(dorg: DOrg, pair: tuple[str, str]) -> None:
    d = await create_demand(dorg.admin, dorg.client_id)
    error = await _try(dorg, d["id"], pair[0], pair[1], "system")
    assert isinstance(error, Forbidden)
    assert error.code == "human_only_action"


async def test_api_invalid_transition_and_reasons(dorg: DOrg) -> None:
    m = dorg.admin
    draft = await create_demand(m, dorg.client_id)
    url = f"{DEMANDS}/{draft['id']}/transition"
    resp = await m.client.post(url, json={"to": "on_hold", "reason_code": "other"}, headers=h(m))
    assert (resp.status_code, resp.json()["error"]["code"]) == (409, "invalid_transition")
    resp = await m.client.post(url, json={"to": "open"}, headers=h(m))
    assert (resp.status_code, resp.json()["error"]["code"]) == (409, "invalid_transition")
    resp = await m.client.post(url, json={"to": "cancelled"}, headers=h(m))
    assert (resp.status_code, resp.json()["error"]["code"]) == (422, "reason_required")
    resp = await m.client.post(url, json={"to": "cancelled", "reason_code": "nope"}, headers=h(m))
    assert (resp.status_code, resp.json()["error"]["code"]) == (422, "invalid_reason_code")
    resp = await m.client.post(url, json={"to": "cancelled", "reason_code": "duplicate"}, headers=h(m))
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "cancelled"
    assert resp.json()["status_reason_code"] == "duplicate"

    opened = await open_demand(m, dorg.client_id)
    url = f"{DEMANDS}/{opened['id']}/transition"
    for target in ("filled", "pending_approval", "draft"):
        resp = await m.client.post(url, json={"to": target}, headers=h(m))
        assert resp.status_code == 409, target
    resp = await m.client.post(url, json={"to": "closed"}, headers=h(m))
    assert resp.json()["error"]["code"] == "reason_required"
    resp = await m.client.post(url, json={"to": "on_hold", "reason_code": "client_paused"}, headers=h(m))
    assert resp.json()["status"] == "on_hold"
    resp = await m.client.post(url, json={"to": "open"}, headers=h(m))
    assert resp.json()["status"] == "open"
    resp = await m.client.post(url, json={"to": "closed", "reason_code": "filled_by_client"}, headers=h(m))
    assert resp.json()["status"] == "closed"
    edit = await m.client.patch(f"{DEMANDS}/{opened['id']}", json={"title": "x"}, headers=h(m))
    assert (edit.status_code, edit.json()["error"]["code"]) == (409, "demand_not_editable")


async def test_auto_filled_and_reopen_guard(dorg: DOrg) -> None:
    m = dorg.admin
    opened = await open_demand(m, dorg.client_id, positions=2)
    demand_id = uuid.UUID(opened["id"])
    ctx = admin_ctx(dorg)
    async with tenant_session(dorg.tenant_id) as db:
        await svc.apply_positions_filled(ctx, db, demand_id, 1)
    body = (await m.client.get(f"{DEMANDS}/{demand_id}")).json()
    assert (body["status"], body["progress"]) == ("open", "partially_filled")
    async with tenant_session(dorg.tenant_id) as db:
        await svc.apply_positions_filled(ctx, db, demand_id, 2)
    assert (await m.client.get(f"{DEMANDS}/{demand_id}")).json()["status"] == "filled"

    url = f"{DEMANDS}/{demand_id}/transition"
    resp = await m.client.post(url, json={"to": "open"}, headers=h(m))
    assert (resp.status_code, resp.json()["error"]["code"]) == (409, "reopen_guard")
    resp = await m.client.patch(f"{DEMANDS}/{demand_id}", json={"positions": 3}, headers=h(m))
    assert resp.status_code == 200, resp.text
    resp = await m.client.post(url, json={"to": "open"}, headers=h(m))
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "open"

    # Offer withdrawn after acceptance also reopens (hook flag from Epic 8).
    async with tenant_session(dorg.tenant_id) as db:
        await svc.apply_positions_filled(ctx, db, demand_id, 3)
        demand = await db.get(Demand, demand_id)
        assert demand is not None and demand.status == "filled"
        await svc.transition_demand(ctx, db, demand, "open", "user", offer_withdrawn=True)
        assert demand.status == "open"


async def test_progress_counters_hook(dorg: DOrg) -> None:
    opened = await open_demand(dorg.admin, dorg.client_id)
    async with tenant_session(dorg.tenant_id) as db:
        await svc.record_pipeline_counts(
            admin_ctx(dorg),
            db,
            uuid.UUID(opened["id"]),
            applications=1,
            submissions=1,
            interviews_scheduled=0,
        )
    body = (await dorg.admin.client.get(f"{DEMANDS}/{opened['id']}")).json()
    assert body["progress"] == "submitted"
    assert body["applications_count"] == 1
    resp = await dorg.admin.client.patch(
        f"{DEMANDS}/{opened['id']}", json={"progress": "offer"}, headers=h(dorg.admin)
    )
    assert resp.status_code == 422


async def test_leaving_open_unpublishes(dorg: DOrg) -> None:
    m = dorg.admin
    opened = await open_demand(m, dorg.client_id)
    base = f"{DEMANDS}/{opened['id']}"
    version = await m.client.post(
        f"{base}/jd-versions", json={"kind": "full", "content": "We need a data engineer."}, headers=h(m)
    )
    assert version.status_code == 201, version.text
    pub = await m.client.post(f"{base}/posting", json={"jd_version_id": version.json()["id"]}, headers=h(m))
    assert pub.status_code == 200, pub.text
    assert pub.json()["is_live"] is True
    resp = await m.client.post(
        f"{base}/transition", json={"to": "on_hold", "reason_code": "client_paused"}, headers=h(m)
    )
    assert resp.status_code == 200
    assert resp.json()["published_jd_version_id"] is None
    posting = (await m.client.get(f"{base}/posting")).json()
    assert posting["is_live"] is False
    assert posting["unpublished_at"] is not None


async def test_reason_validation_error_type(dorg: DOrg) -> None:
    d = await create_demand(dorg.admin, dorg.client_id)
    error = await _try(dorg, d["id"], "open", "on_hold", "user")
    assert error is None
    ctx = admin_ctx(dorg)
    async with tenant_session(dorg.tenant_id) as db:
        demand = await db.get(Demand, uuid.UUID(d["id"]))
        assert demand is not None
        demand.status = "open"
        await db.flush()
        with pytest.raises(ValidationFailed):
            await svc.transition_demand(ctx, db, demand, "on_hold", "user")
