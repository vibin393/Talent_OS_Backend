# ruff: noqa: E501
"""47.5: limits (row cap, timeout), audit of every run, async exports, needs-repair, run log, settings."""

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select, update

from app.core.audit import AuditLog
from app.core.database import tenant_session
from app.core.semantic import CMP, FieldDef, ScopeSpec, register_semantic_source
from app.core.semantic import executor as semantic_executor
from app.core.semantic.registry import unregister_semantic_source
from app.models.notifications import Notification
from app.models.placements import Placement
from app.models.report_builder import ReportDefinition, ReportRun, ReportSchedule
from app.services.reporting import builder
from app.services.reporting.builder import runs as runs_module
from tests.reporting.builder.helpers import BASE, SRC, create_report, example_definition, seed_placements
from tests.reporting.helpers import API, ROrg, h

TEST_SRC = "testreports.placement"


def _test_source(*, with_extra: bool) -> None:
    fields = {
        "id": FieldDef("uuid", "Id", "none", lambda p: p.id, frozenset({"eq", "in"})),
        "role_title": FieldDef("string", "Role", "none", lambda p: p.role_title, frozenset({"eq", "contains"}), groupable=True),
        "slow": FieldDef("bool", "Slow", "none", lambda p: func.pg_sleep(2).is_(None), frozenset({"eq"})),
    }
    if with_extra:
        fields["extra"] = FieldDef("int", "Extra", "none", lambda p: p.onboarding_pct, CMP, groupable=True)
    unregister_semantic_source(TEST_SRC)
    register_semantic_source(
        TEST_SRC, Placement, fields, {},
        ScopeSpec(permission="placement:read", owner_cols=(lambda p: p.recruiter_user_id,)),
        label="Test placements", subject=lambda p: p.id,
    )  # fmt: skip


@pytest.fixture
def test_source() -> Iterator[None]:
    _test_source(with_extra=True)
    yield
    unregister_semantic_source(TEST_SRC)


async def test_row_cap_export_and_audit(rorg: ROrg) -> None:
    await seed_placements(rorg)
    admin = rorg.admin
    # Tenants may lower the caps, never raise them.
    resp = await admin.client.put(f"{BASE}/settings", json={"export_row_cap": 200_000}, headers=h(admin))
    assert resp.status_code == 422
    resp = await admin.client.put(f"{BASE}/settings", json={"export_row_cap": 2, "preview_rows": 3}, headers=h(admin))
    assert resp.status_code == 200 and resp.json()["export_row_cap"] == 2
    assert (await rorg.manager.client.put(f"{BASE}/settings", json={"preview_rows": 3}, headers=h(rorg.manager))).status_code == 403
    report = await create_report(admin, {"source": SRC, "fields": ["human_id", "client.name"], "sort": [{"key": "human_id"}]}, {"type": "table"}, name="All placements")
    resp = await admin.client.post(f"{BASE}/{report['id']}/run", headers=h(admin))
    body = resp.json()
    assert len(body["rows"]) == 3 and body["total"] == 4 and body["truncated"] and body["limit_hit"] is None
    resp = await admin.client.post(f"{BASE}/{report['id']}/export", json={"format": "csv"}, headers=h(admin))
    assert resp.status_code == 200, resp.text
    out = resp.json()
    assert out["mode"] == "sync" and out["status"] == "limit_hit" and out["row_count"] == 2
    assert out["limit_hit"]["kind"] == "row_cap" and "Stopped at 2 rows" in out["limit_hit"]["message"]
    resp = await admin.client.get(f"{API}{out['download_url'].removeprefix('/api/v1')}", headers=h(admin))
    assert resp.status_code == 200 and resp.content.decode("utf-8-sig").count("\r\n") == 3  # header + 2 rows
    # Export needs report:export (the Account Manager has none).
    assert (await rorg.account.client.post(f"{BASE}/{report['id']}/export", json={"format": "csv"}, headers=h(rorg.account))).status_code == 403
    async with tenant_session(rorg.tenant_id) as db:
        actions = (await db.execute(select(AuditLog.action).where(AuditLog.action.like("report.%")))).scalars().all()
        run = (await db.execute(select(ReportRun).where(ReportRun.run_kind == "export"))).scalar_one()
    assert {"report.run", "report.export", "report.downloaded"} <= set(actions)
    assert (run.status, run.limit_kind, run.row_count) == ("limit_hit", "row_cap", 2)
    # Run log (audit:read): filterable by kind.
    resp = await admin.client.get(f"{BASE}/runs", params={"run_kind": "export"}, headers=h(admin))
    assert resp.status_code == 200 and [r["status"] for r in resp.json()["items"]] == ["limit_hit"]
    assert (await rorg.recruiter.client.get(f"{BASE}/runs", headers=h(rorg.recruiter))).status_code == 403


async def test_large_export_is_queued_and_delivered(rorg: ROrg, monkeypatch: pytest.MonkeyPatch) -> None:
    await seed_placements(rorg)
    admin = rorg.admin
    monkeypatch.setattr(runs_module, "SYNC_MAX_ROWS", 1)
    report = await create_report(admin, {"source": SRC, "fields": ["human_id"]}, {"type": "table"}, name="Big")
    resp = await admin.client.post(f"{BASE}/{report['id']}/export", json={"format": "xlsx"}, headers=h(admin))
    assert resp.status_code == 200, resp.text
    out = resp.json()
    assert out["mode"] == "async" and out["download_url"] is None
    async with tenant_session(rorg.tenant_id) as db:
        status = await builder.run_export(db, uuid.UUID(out["run_id"]))  # the worker body (no-op if already run)
    async with tenant_session(rorg.tenant_id) as db:
        run = await db.get(ReportRun, uuid.UUID(out["run_id"]))
        assert run is not None and run.status == "done" and run.row_count == 4 and run.file_id is not None
        note = (await db.execute(select(Notification).where(Notification.user_id == admin.user_id, Notification.type == "report_delivered"))).scalar_one()
    assert status in {"done", "running"} and note.link_path == f"/api/v1/report-runs/{run.id}/download"
    resp = await admin.client.get(f"{API}/report-runs/{run.id}/download", headers=h(admin))
    assert resp.status_code == 200 and resp.content[:2] == b"PK"
    # Expired links are gone (410) and the purge removes the file.
    async with tenant_session(rorg.tenant_id) as db:
        await db.execute(update(ReportRun).where(ReportRun.id == run.id).values(expires_at=datetime.now(UTC) - timedelta(minutes=1)))
    assert (await admin.client.get(f"{API}/report-runs/{run.id}/download", headers=h(admin))).status_code == 410
    async with tenant_session(rorg.tenant_id) as db:
        from app.services.reporting.builder.governance import purge_expired_files

        assert await purge_expired_files(db) == 1


@pytest.mark.usefixtures("test_source")
async def test_timeout_is_a_limit_hit(rorg: ROrg, monkeypatch: pytest.MonkeyPatch) -> None:
    await seed_placements(rorg)
    admin = rorg.admin
    monkeypatch.setattr(semantic_executor, "STATEMENT_TIMEOUT", "500ms")
    defn = {"source": TEST_SRC, "filters": {"field": "slow", "op": "eq", "value": True}, "fields": ["id"]}
    resp = await admin.client.post(f"{BASE}/preview", json={"definition": defn, "viz": {"type": "table"}}, headers=h(admin))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["limit_hit"]["kind"] == "timeout" and "seconds" in body["limit_hit"]["message"] and body["rows"] == []
    async with tenant_session(rorg.tenant_id) as db:
        run = (await db.execute(select(ReportRun).where(ReportRun.source_key == TEST_SRC))).scalar_one()
    assert (run.status, run.limit_kind) == ("limit_hit", "timeout")


@pytest.mark.usefixtures("test_source")
async def test_removed_field_needs_repair_and_pauses_schedules(rorg: ROrg) -> None:
    await seed_placements(rorg)
    admin = rorg.admin
    defn = {"source": TEST_SRC, "group_by": [{"field": "extra"}], "measures": [{"key": "n", "agg": "count"}]}
    report = await create_report(admin, defn, {"type": "bar", "x": "extra", "value": "n"}, name="Uses extra")
    resp = await admin.client.post(f"{API}/report-schedules", json={"report_id": report["id"], "cadence": "daily", "local_time": "08:00:00", "format": "xlsx", "recipient_user_ids": [str(admin.user_id)]}, headers=h(admin))
    assert resp.status_code == 201, resp.text
    sid = resp.json()["id"]
    # The field disappears from the registry; the nightly check flags the report and pauses its schedule.
    _test_source(with_extra=False)
    async with tenant_session(rorg.tenant_id) as db:
        result = await builder.validate_all(db)
    assert result["broken"] == 1
    async with tenant_session(rorg.tenant_id) as db:
        r = await db.get(ReportDefinition, uuid.UUID(report["id"]))
        s = await db.get(ReportSchedule, uuid.UUID(sid))
        notes = (await db.execute(select(func.count()).select_from(Notification).where(Notification.user_id == admin.user_id, Notification.type == "report_needs_repair"))).scalar_one()
    assert r is not None and r.status == "needs_repair"
    assert r.repair_issues == [{"path": "group_by[0].field", "field": "extra", "reason": "semantic_unknown_field"}]
    assert s is not None and (s.status, s.paused_reason) == ("paused", "needs_repair")
    assert notes == 1
    # Runs return 409; the list shows the badge state; resuming is refused until it is fixed.
    resp = await admin.client.post(f"{BASE}/{report['id']}/run", headers=h(admin))
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "report_needs_repair"
    listed = (await admin.client.get(BASE, params={"status": "needs_repair"}, headers=h(admin))).json()["items"]
    assert [x["id"] for x in listed] == [report["id"]] and listed[0]["repair_issues"][0]["field"] == "extra"
    assert (await admin.client.post(f"{API}/report-schedules/{sid}/resume", headers=h(admin))).status_code == 409
    # Saving a fixed definition clears the flag; the schedule can then be resumed.
    fixed = {"source": TEST_SRC, "group_by": [{"field": "role_title"}], "measures": [{"key": "n", "agg": "count"}]}
    resp = await admin.client.patch(f"{BASE}/{report['id']}", json={"definition": fixed, "viz": {"type": "bar", "x": "role_title", "value": "n"}}, headers=h(admin))
    assert resp.status_code == 200 and resp.json()["status"] == "ok" and resp.json()["repair_issues"] == []
    assert (await admin.client.post(f"{API}/report-schedules/{sid}/resume", headers=h(admin))).status_code == 200


async def test_preview_masks_nothing_for_builder_but_refuses_masked_measures(rorg: ROrg) -> None:
    """An author without compensation:view cannot build measures over masked fields (author validation)."""
    await seed_placements(rorg)
    # Give the Recruiter report:build to act as an author lacking compensation:view.
    from tests.reporting.builder.helpers import grant

    await grant(rorg, "recruiter", report__build="tenant")
    rec = rorg.recruiter
    resp = await rec.client.post(f"{BASE}/preview", json={"definition": example_definition(), "viz": {"type": "table"}}, headers=h(rec))
    assert resp.status_code == 422 and resp.json()["error"]["code"] == "semantic_field_forbidden"
    rows_def = {"source": SRC, "fields": ["human_id", "bill_rate"]}
    resp = await rec.client.post(f"{BASE}/preview", json={"definition": rows_def, "viz": {"type": "table"}}, headers=h(rec))
    assert resp.status_code == 200 and resp.json()["masked_fields"] == ["bill_rate"]
