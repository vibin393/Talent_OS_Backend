# ruff: noqa: E501
"""Epic 63 timesheet flags (DB): evaluation on submit with AI off, idempotency per version, supersede on reject,
review permissions / audit, the API (incl. cross-tenant 404), Client-portal isolation and the erasure handler."""

import uuid

from fastapi import FastAPI
from sqlalchemy import select

from app.core.database import tenant_session
from app.models.time import Timesheet
from app.models.timesheet_flags import TimesheetFlag
from app.services import timesheet_flags_service as svc
from tests.modules.demands.helpers import DOrg, h
from tests.modules.time.helpers import (
    CLIENT,
    active,
    admin_decide,
    client_user,
    first_timesheet,
    week,
    worker_save,
    worker_submit,
)

API = "/api/v1"


async def _flags(tenant_id: uuid.UUID, ts_id: str) -> list[TimesheetFlag]:
    async with tenant_session(tenant_id) as db:
        rows = list((await db.execute(select(TimesheetFlag).where(TimesheetFlag.timesheet_id == uuid.UUID(ts_id)))).scalars())
        for r in rows:
            db.expunge(r)
        return rows


async def _submitted_with_weekend(app: FastAPI, dorg: DOrg) -> tuple[object, dict[str, object]]:
    s = await active(app, dorg)
    ts = await first_timesheet(s)
    resp = await worker_save(s, str(ts["id"]), week(s.start, "8", "13", "8", "8", "8", "4"))
    assert resp.status_code == 200, resp.text
    resp = await worker_submit(s, str(ts["id"]))
    assert resp.status_code == 200, resp.text
    return s, ts


async def test_flags_on_submit_with_ai_off_and_review(app: FastAPI, dorg: DOrg, other_dorg: DOrg) -> None:
    _s, ts = await _submitted_with_weekend(app, dorg)
    rows = await _flags(dorg.tenant_id, str(ts["id"]))
    assert {r.rule for r in rows} >= {"weekend_work", "daily_hours_vs_schedule"}
    assert all(r.explanation_source == "rules" and r.status == "open" for r in rows)
    # idempotent per version
    async with tenant_session(dorg.tenant_id) as db:
        again = await svc.evaluate(db, uuid.UUID(str(ts["id"])))
    assert {r.id for r in again} == {r.id for r in rows}
    resp = await dorg.admin.client.get(f"{API}/timesheets/{ts['id']}/flags")
    assert resp.status_code == 200, resp.text
    weekend = next(f for f in resp.json() if f["rule"] == "weekend_work")
    assert weekend["label"] == "Weekend work" and weekend["rules_description"].startswith("4 hours")
    # dismiss needs a note; reviewed works; the approval itself is untouched
    resp = await dorg.admin.client.post(f"{API}/timesheet-flags/{weekend['id']}/review", json={"status": "dismissed"}, headers=h(dorg.admin))
    assert resp.status_code == 422 and resp.json()["error"]["code"] == "note_required"
    resp = await dorg.admin.client.post(
        f"{API}/timesheet-flags/{weekend['id']}/review", json={"status": "dismissed", "note": "Weekend support agreed"}, headers=h(dorg.admin)
    )
    assert resp.status_code == 200 and resp.json()["status"] == "dismissed"
    async with tenant_session(dorg.tenant_id) as db:
        ts_row = await db.get(Timesheet, uuid.UUID(str(ts["id"])))
        assert ts_row is not None and ts_row.status in ("submitted", "in_approval")
    listing = await dorg.admin.client.get(f"{API}/timesheet-flags", params={"rule": "weekend_work"})
    assert listing.status_code == 200 and listing.json()["total"] >= 1
    counts = await dorg.admin.client.get(f"{API}/timesheet-flags/counts", params={"timesheet_ids": [str(ts["id"])]})
    assert counts.status_code == 200 and counts.json()[0]["open_count"] == len(rows) - 1
    # cross-tenant
    resp = await other_dorg.admin.client.get(f"{API}/timesheets/{ts['id']}/flags")
    assert resp.status_code == 404
    resp = await other_dorg.admin.client.post(
        f"{API}/timesheet-flags/{weekend['id']}/review", json={"status": "reviewed"}, headers=h(other_dorg.admin)
    )
    assert resp.status_code == 404


async def test_reject_supersedes_and_client_portal_never_shows_flags(app: FastAPI, dorg: DOrg) -> None:
    _s, ts = await _submitted_with_weekend(app, dorg)
    cu = await client_user(app, dorg)
    detail = await cu.client.get(f"{CLIENT}/{ts['id']}")
    if detail.status_code == 200:
        assert not any(k in detail.text for k in ("weekend_work", "daily_hours_vs_schedule", "rules_description", "explanation_source"))
    resp = await admin_decide(dorg, str(ts["id"]), "reject", "Fix Tuesday")
    assert resp.status_code == 200, resp.text
    rows = await _flags(dorg.tenant_id, str(ts["id"]))
    assert rows and all(r.status == "superseded" for r in rows)
    shown = await dorg.admin.client.get(f"{API}/timesheets/{ts['id']}/flags")
    assert shown.json() == []


async def test_flag_explanations_number_check_and_erasure(app: FastAPI, dorg: DOrg) -> None:
    from app.core.context import RequestContext
    from app.core.errors import ValidationFailed
    from app.schemas.timesheet_flags import FlagExplanationIn
    from app.services.privacy.registry import ErasureContext
    from app.services.timesheet_flags_erasure import TimesheetFlagsHandler

    s, ts = await _submitted_with_weekend(app, dorg)
    rows = await _flags(dorg.tenant_id, str(ts["id"]))
    weekend = next(r for r in rows if r.rule == "weekend_work")
    ctx = RequestContext(tenant_id=dorg.tenant_id)
    async with tenant_session(dorg.tenant_id) as db:
        try:
            await svc.set_explanations(ctx, db, weekend.timesheet_id, [FlagExplanationIn(flag_id=weekend.id, explanation="77 hours were logged on a Saturday.")])
            raise AssertionError("an unknown number was accepted")
        except ValidationFailed as exc:
            assert exc.code == "explanation_unknown_number"
    async with tenant_session(dorg.tenant_id) as db:
        n = await svc.set_explanations(
            ctx, db, weekend.timesheet_id,
            [FlagExplanationIn(flag_id=weekend.id, explanation="4 hours were logged on a non-working day; check it was agreed.", group_key="schedule")],
        )  # fmt: skip
    assert n == 1
    flag = next(r for r in await _flags(dorg.tenant_id, str(ts["id"])) if r.id == weekend.id)
    assert flag.explanation_source == "ai" and flag.group_key == "schedule"
    async with tenant_session(dorg.tenant_id) as db:
        ectx = ErasureContext(tenant_id=dorg.tenant_id, candidate_id=s.candidate_id, run_id=uuid.uuid4(), reason="dsr")  # type: ignore[attr-defined]
        handler = TimesheetFlagsHandler()
        await handler.erase(db, ectx)
        assert await handler.verify(db, ectx) == []
    flag = next(r for r in await _flags(dorg.tenant_id, str(ts["id"])) if r.id == weekend.id)
    assert flag.explanation is None and flag.rule == "weekend_work"
