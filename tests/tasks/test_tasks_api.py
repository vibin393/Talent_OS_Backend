"""Tasks (story 10.2, FR-56): CRUD, scope, validation, overdue (owner timezone), reminders, events."""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from fastapi import FastAPI
from freezegun import freeze_time

from app.core.database import tenant_session
from app.models.tasks import Task
from app.services import tasks_service
from tests.epic10 import API, TASKS, DOrg, candidate, count, create_demand, h, member


def _iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


async def _create(m: Any, **body: Any) -> dict[str, Any]:
    resp = await m.client.post(TASKS, json={"title": "Call the hiring manager", **body}, headers=h(m))
    assert resp.status_code == 201, resp.text
    return dict(resp.json())


async def test_crud_complete_reopen_delete(dorg: DOrg) -> None:
    demand = await create_demand(dorg.admin, dorg.client_id)
    due = datetime.now(UTC) + timedelta(days=2)
    task = await _create(
        dorg.admin, entity_type="demand", entity_id=demand["id"], due_at=_iso(due), priority="high"
    )
    assert (
        task["owner_id"] == str(dorg.admin.user_id)
        and task["status"] == "open"
        and task["is_overdue"] is False
    )
    resp = await dorg.admin.client.patch(
        f"{TASKS}/{task['id']}", json={"title": "Call HM"}, headers=h(dorg.admin)
    )
    assert resp.status_code == 200 and resp.json()["title"] == "Call HM"
    resp = await dorg.admin.client.post(f"{TASKS}/{task['id']}/complete", headers=h(dorg.admin))
    assert resp.json()["status"] == "done" and resp.json()["completed_by"] == str(dorg.admin.user_id)
    resp = await dorg.admin.client.post(f"{TASKS}/{task['id']}/reopen", headers=h(dorg.admin))
    assert resp.json()["status"] == "open" and resp.json()["completed_at"] is None
    listed = await dorg.admin.client.get(f"{API}/demands/{demand['id']}/tasks")
    assert [t["id"] for t in listed.json()["items"]] == [task["id"]]
    resp = await dorg.admin.client.delete(f"{TASKS}/{task['id']}", headers=h(dorg.admin))
    assert resp.status_code == 204
    assert (await dorg.admin.client.get(f"{TASKS}/{task['id']}")).status_code == 404


async def test_unreadable_linked_entity_is_404(app: FastAPI, dorg: DOrg) -> None:
    demand = await create_demand(dorg.admin, dorg.client_id)  # owned by the admin, not the recruiter's team
    rec = await member(app, dorg, "recruiter")
    resp = await rec.client.post(
        TASKS, json={"title": "x", "entity_type": "demand", "entity_id": demand["id"]}, headers=h(rec)
    )
    assert resp.status_code == 404
    resp = await rec.client.post(
        TASKS, json={"title": "x", "entity_type": "candidate", "entity_id": str(uuid.uuid4())}, headers=h(rec)
    )
    assert resp.status_code == 404
    await rec.client.aclose()


async def test_owner_and_reminder_validation(dorg: DOrg) -> None:
    now = datetime.now(UTC)
    resp = await dorg.admin.client.post(
        TASKS, json={"title": "x", "owner_id": str(uuid.uuid4())}, headers=h(dorg.admin)
    )
    assert resp.status_code == 422 and resp.json()["error"]["code"] == "invalid_owner"
    resp = await dorg.admin.client.post(
        TASKS, json={"title": "x", "reminder_at": _iso(now - timedelta(minutes=5))}, headers=h(dorg.admin)
    )
    assert resp.status_code == 422 and resp.json()["error"]["code"] == "invalid_reminder"
    resp = await dorg.admin.client.post(
        TASKS,
        json={
            "title": "x",
            "due_at": _iso(now + timedelta(hours=1)),
            "reminder_at": _iso(now + timedelta(hours=2)),
        },
        headers=h(dorg.admin),
    )
    assert resp.status_code == 422 and resp.json()["error"]["code"] == "invalid_reminder"
    ok = await _create(
        dorg.admin, due_at=_iso(now + timedelta(hours=2)), reminder_at=_iso(now + timedelta(hours=1))
    )
    assert ok["reminder_at"] is not None
    too_long = await dorg.admin.client.post(TASKS, json={"title": "x" * 201}, headers=h(dorg.admin))
    assert too_long.status_code == 422


def test_all_day_overdue_uses_owner_timezone() -> None:
    tz = ZoneInfo("Asia/Kolkata")
    task = Task(status="open", due_all_day=True, due_at=datetime(2026, 10, 1, 0, 0, tzinfo=tz))
    with freeze_time("2026-10-01T18:20:00Z"):  # 23:50 in Kolkata, still Oct 1
        assert tasks_service.is_overdue(task, tz) is False
    with freeze_time("2026-10-01T18:40:00Z"):  # 00:10 on Oct 2 in Kolkata
        assert tasks_service.is_overdue(task, tz) is True
    utc = ZoneInfo("UTC")
    noon_utc = Task(status="open", due_all_day=True, due_at=datetime(2026, 10, 1, 12, 0, tzinfo=UTC))
    with freeze_time("2026-10-01T23:00:00Z"):  # still Oct 1 for a UTC owner, already Oct 2 in Kolkata
        assert tasks_service.is_overdue(noon_utc, utc) is False
        assert tasks_service.is_overdue(noon_utc, tz) is True
    timed = Task(status="open", due_all_day=False, due_at=datetime(2026, 10, 1, 10, 0, tzinfo=UTC))
    with freeze_time("2026-10-01T10:01:00Z"):
        assert tasks_service.is_overdue(timed, utc) is True
    timed.status = "done"
    with freeze_time("2026-10-01T10:01:00Z"):
        assert tasks_service.is_overdue(timed, utc) is False


async def test_views_and_summary(dorg: DOrg) -> None:
    now = datetime.now(UTC)
    overdue = await _create(dorg.admin, title="late", due_at=_iso(now - timedelta(days=1)))
    yesterday_all_day = await _create(
        dorg.admin, title="late all-day", due_at=_iso(now - timedelta(days=2)), due_all_day=True
    )
    upcoming = await _create(dorg.admin, title="later", due_at=_iso(now + timedelta(days=3)))
    no_date = await _create(dorg.admin, title="someday")
    assert overdue["is_overdue"] is True and yesterday_all_day["is_overdue"] is True

    async def view(name: str) -> set[str]:
        resp = await dorg.admin.client.get(TASKS, params={"owner": "me", "view": name})
        assert resp.status_code == 200, resp.text
        return {t["id"] for t in resp.json()["items"]}

    assert await view("overdue") == {overdue["id"], yesterday_all_day["id"]}
    assert await view("upcoming") == {upcoming["id"]}
    assert await view("no_date") == {no_date["id"]}
    summary = await dorg.admin.client.get(f"{TASKS}/summary")
    assert summary.json()["overdue"] == 2 and summary.json()["open"] == 4
    assert summary.headers["cache-control"] == "no-store"
    found = await dorg.admin.client.get(TASKS, params={"q": "all-day"})
    assert [t["id"] for t in found.json()["items"]] == [yesterday_all_day["id"]]


async def test_own_scope(app: FastAPI, dorg: DOrg) -> None:
    rec = await member(app, dorg, "recruiter", name="Rita Recruiter")
    admins_task = await _create(dorg.admin, title="admin only")
    for_admin = await _create(rec, title="for the admin", owner_id=str(dorg.admin.user_id))
    mine = await _create(rec, title="mine")
    listed = {t["id"] for t in (await rec.client.get(TASKS)).json()["items"]}
    assert listed == {for_admin["id"], mine["id"]}
    assert (await rec.client.get(f"{TASKS}/{admins_task['id']}")).status_code == 404
    assert (await rec.client.post(f"{TASKS}/{admins_task['id']}/complete", headers=h(rec))).status_code == 404
    await rec.client.aclose()


async def test_task_assigned_notifies_other_owner_only(app: FastAPI, dorg: DOrg) -> None:
    rec = await member(app, dorg, "recruiter", name="Rita Recruiter")
    await _create(dorg.admin, title="self")
    assert await count(dorg.tenant_id, "notifications", "type = 'task_assigned'") == 0
    await _create(dorg.admin, title="for rita", owner_id=str(rec.user_id))
    assert (
        await count(dorg.tenant_id, "notifications", "type = 'task_assigned' AND user_id = :u", u=rec.user_id)
        == 1
    )
    await rec.client.aclose()


async def test_reminder_fires_once(dorg: DOrg) -> None:
    now = datetime.now(UTC)
    task = await _create(
        dorg.admin, due_at=_iso(now + timedelta(hours=2)), reminder_at=_iso(now + timedelta(minutes=5))
    )
    async with tenant_session(dorg.tenant_id) as db:
        assert await tasks_service.send_reminders(db, now=now) == 0
    async with tenant_session(dorg.tenant_id) as db:
        assert await tasks_service.send_reminders(db, now=now + timedelta(minutes=6)) == 1
    async with tenant_session(dorg.tenant_id) as db:
        assert await tasks_service.send_reminders(db, now=now + timedelta(minutes=7)) == 0
    assert (
        await count(
            dorg.tenant_id,
            "notifications",
            "type = 'task_reminder' AND user_id = :u AND entity_id = :t",
            u=dorg.admin.user_id,
            t=uuid.UUID(task["id"]),
        )
        == 1
    )
    detail = await dorg.admin.client.get(f"{TASKS}/{task['id']}")
    assert detail.json()["reminded_at"] is not None


async def test_candidate_linked_task(dorg: DOrg) -> None:
    cand = await candidate(dorg.admin)
    task = await _create(dorg.admin, entity_type="candidate", entity_id=cand["id"])
    listed = await dorg.admin.client.get(f"{API}/candidates/{cand['id']}/tasks")
    assert [t["id"] for t in listed.json()["items"]] == [task["id"]]


@pytest.mark.parametrize(
    ("method", "suffix", "body"),
    [
        ("get", "", None),
        ("patch", "", {"title": "x"}),
        ("delete", "", None),
        ("post", "/complete", None),
        ("post", "/reopen", None),
    ],
)
async def test_cross_tenant_404(dorg: DOrg, other_dorg: DOrg, method: str, suffix: str, body: Any) -> None:
    task = await _create(dorg.admin)
    kwargs: dict[str, Any] = {"headers": h(other_dorg.admin)}
    if body is not None:
        kwargs["json"] = body
    resp = await getattr(other_dorg.admin.client, method)(f"{TASKS}/{task['id']}{suffix}", **kwargs)
    assert resp.status_code == 404


async def test_cross_tenant_entity_tasks_and_links(dorg: DOrg, other_dorg: DOrg) -> None:
    demand = await create_demand(dorg.admin, dorg.client_id)
    assert (await other_dorg.admin.client.get(f"{API}/demands/{demand['id']}/tasks")).status_code == 404
    resp = await other_dorg.admin.client.post(
        TASKS,
        json={"title": "x", "entity_type": "demand", "entity_id": demand["id"]},
        headers=h(other_dorg.admin),
    )
    assert resp.status_code == 404
    assert (await other_dorg.admin.client.get(TASKS)).json()["total"] == 0
