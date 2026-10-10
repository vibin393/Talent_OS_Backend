# ruff: noqa: E501
"""47.4: scheduled reports run once per recipient with the recipient's permissions; delivery; failures; pause."""

import uuid
from datetime import UTC, datetime, time, timedelta

from sqlalchemy import func, select, text, update

from app.core import mailer
from app.core.database import tenant_session
from app.models.notifications import Notification
from app.models.report_builder import ReportRun, ReportSchedule
from app.services.reporting import builder
from app.services.reporting.builder.delivery import delivery_kind
from app.services.reporting.builder.schedules import next_occurrence
from tests.reporting.builder.helpers import SRC, create_report, grant, revoke, seed_placements, share_tenant
from tests.reporting.helpers import API, ROrg, h

SCHED = f"{API}/report-schedules"


_slot_seq = iter(range(1, 10_000))


async def _make_due(org: ROrg, schedule_id: str) -> datetime:
    slot = datetime.now(UTC).replace(second=0, microsecond=0) - timedelta(minutes=10_000 - next(_slot_seq))  # increasing slots
    async with tenant_session(org.tenant_id) as db:
        await db.execute(update(ReportSchedule).where(ReportSchedule.id == uuid.UUID(schedule_id)).values(next_run_at=slot))
    return slot


async def _tick(org: ROrg) -> tuple[list[tuple[uuid.UUID, datetime, uuid.UUID]], dict[uuid.UUID, str]]:
    """What beat + workers do: evaluate (one transaction), then one transaction per recipient."""
    async with tenant_session(org.tenant_id) as db:
        jobs = await builder.evaluate_due(db)
    out: dict[uuid.UUID, str] = {}
    for schedule_id, slot, user_id in jobs:
        async with tenant_session(org.tenant_id) as db:
            out[user_id] = await builder.run_for_recipient(db, schedule_id, slot, user_id)
    return jobs, out


async def _runs(org: ROrg, schedule_id: str, slot: datetime) -> dict[uuid.UUID, ReportRun]:
    async with tenant_session(org.tenant_id) as db:
        rows = (await db.execute(select(ReportRun).where(ReportRun.schedule_id == uuid.UUID(schedule_id), ReportRun.slot_at == slot))).scalars().all()
        return {r.principal_user_id: r for r in rows}


async def _schedule(org: ROrg, schedule_id: str) -> ReportSchedule:
    async with tenant_session(org.tenant_id) as db:
        s = await db.get(ReportSchedule, uuid.UUID(schedule_id))
        assert s is not None
        return s


def test_next_occurrence_in_schedule_timezone() -> None:
    after = datetime(2026, 10, 6, 10, 0, tzinfo=UTC)  # Tuesday 15:30 IST
    weekly = next_occurrence("weekly", 0, None, time(9, 0), "Asia/Kolkata", after)
    assert weekly == datetime(2026, 10, 12, 3, 30, tzinfo=UTC)  # Monday 09:00 IST
    assert next_occurrence("daily", None, None, time(16, 0), "Asia/Kolkata", after) == datetime(2026, 10, 6, 10, 30, tzinfo=UTC)
    assert next_occurrence("monthly", None, -1, time(8, 0), "Asia/Kolkata", after) == datetime(2026, 10, 31, 2, 30, tzinfo=UTC)
    assert next_occurrence("monthly", None, 5, time(8, 0), "Asia/Kolkata", after) == datetime(2026, 11, 5, 2, 30, tzinfo=UTC)
    assert next_occurrence("weekly", 1, None, time(9, 0), "America/New_York", after).isoformat() == "2026-10-06T13:00:00+00:00"  # Tuesday 09:00 EDT


def test_delivery_kind_rules() -> None:
    assert delivery_kind(1000, frozenset({"none"}), 5) == "attachment"
    assert delivery_kind(6 * 1024 * 1024, frozenset({"none"}), 5) == "link"  # > 5 MB
    assert delivery_kind(1000, frozenset({"none", "compensation"}), 5) == "link"
    assert delivery_kind(1000, frozenset({"personal"}), 5) == "link"


async def test_weekly_schedule_runs_per_recipient(rorg: ROrg) -> None:
    await seed_placements(rorg)
    admin = rorg.admin
    for role in ("recruiter", "account_manager"):
        await grant(rorg, role, report__export="tenant")
    report = await create_report(admin, {"source": SRC, "fields": ["human_id", "bill_rate"], "sort": [{"key": "human_id"}]}, {"type": "table"}, name="Weekly rates")
    await share_tenant(admin, report["id"])
    members = [admin, rorg.manager, rorg.account, rorg.recruiter, rorg.recruiter_b]
    body = {"report_id": report["id"], "cadence": "weekly", "weekday": 0, "local_time": "09:00:00", "timezone": "Asia/Kolkata",
            "format": "xlsx", "recipient_user_ids": [str(m.user_id) for m in members]}  # fmt: skip
    # Recipients must be internal users of the tenant.
    bad = await admin.client.post(SCHED, json={**body, "recipient_user_ids": [str(uuid.uuid4())]}, headers=h(admin))
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "schedule_recipient_invalid"
    assert (await admin.client.post(SCHED, json={**body, "weekday": None}, headers=h(admin))).status_code == 422
    resp = await admin.client.post(SCHED, json=body, headers=h(admin))
    assert resp.status_code == 201, resp.text
    sched = resp.json()
    nxt = datetime.fromisoformat(sched["next_run_at"])
    assert nxt.weekday() == 0 and (nxt.hour, nxt.minute) == (3, 30)  # Monday 09:00 IST

    box = mailer.get_mailer()
    assert isinstance(box, mailer.OutboxMailer)
    box.sent.clear()
    slot = await _make_due(rorg, sched["id"])
    jobs, statuses = await _tick(rorg)
    assert len(jobs) == 5 and set(statuses.values()) == {"done"}
    runs = await _runs(rorg, sched["id"], slot)
    counts = {m.user_id: runs[m.user_id].row_count for m in members}
    assert counts == {admin.user_id: 4, rorg.manager.user_id: 3, rorg.account.user_id: 2, rorg.recruiter.user_id: 2, rorg.recruiter_b.user_id: 2}
    # Compensation columns -> a 7-day link; masked for the recruiter -> an attachment.
    admin_run, rec_run = runs[admin.user_id], runs[rorg.recruiter.user_id]
    assert admin_run.delivery == "link" and admin_run.masked_fields == []
    assert admin_run.expires_at is not None and (admin_run.expires_at - admin_run.finished_at).days == 7
    assert rec_run.delivery == "attachment" and rec_run.masked_fields == ["bill_rate"]
    mails = {m.to: m for m in box.sent if m.kind == "report_delivered"}
    assert len(mails) == 5
    rec_mail = next(m for m in mails.values() if m.attachments)
    assert rec_mail.attachments[0].filename.endswith(".xlsx")
    admin_mail = next(m for m in mails.values() if f"/report-runs/{admin_run.id}/download" in m.text)
    assert not admin_mail.attachments
    # The link works for its recipient only (404 for anyone else).
    resp = await admin.client.get(f"{API}/report-runs/{admin_run.id}/download", headers=h(admin))
    assert resp.status_code == 200 and resp.content[:2] == b"PK"
    resp = await rorg.manager.client.get(f"{API}/report-runs/{admin_run.id}/download", headers=h(rorg.manager))
    assert resp.status_code == 404
    # Idempotent: re-delivering the same slot is a no-op.
    async with tenant_session(rorg.tenant_id) as db:
        assert await builder.run_for_recipient(db, uuid.UUID(sched["id"]), slot, admin.user_id) == "duplicate"
    assert len(await _runs(rorg, sched["id"], slot)) == 5
    s = await _schedule(rorg, sched["id"])
    assert s.consecutive_failures == 0 and s.last_run_at == slot and s.next_run_at > datetime.now(UTC)

    # Second slot: Recruiter B deactivated, the Recruiter lost report:read -> both skipped.
    async with tenant_session(rorg.tenant_id) as db:
        await db.execute(text("UPDATE users SET is_active = false WHERE id = :id"), {"id": rorg.recruiter_b.user_id})
    await revoke(rorg, "recruiter", "report:read")
    slot2 = await _make_due(rorg, sched["id"])
    await _tick(rorg)
    runs = await _runs(rorg, sched["id"], slot2)
    assert (runs[rorg.recruiter_b.user_id].status, runs[rorg.recruiter_b.user_id].skip_reason) == ("skipped", "recipient_inactive")
    assert (runs[rorg.recruiter.user_id].status, runs[rorg.recruiter.user_id].skip_reason) == ("skipped", "not_authorised")
    assert runs[admin.user_id].status == "done"
    assert (await _schedule(rorg, sched["id"])).consecutive_failures == 0


async def test_three_failures_pause_and_notify_then_resume(rorg: ROrg) -> None:
    await seed_placements(rorg)
    admin = rorg.admin
    await grant(rorg, "recruiter", report__export="tenant")
    # Filters by a compensation field: fails for a recipient without compensation:view (never silently dropped).
    report = await create_report(admin, {"source": SRC, "filters": {"field": "bill_rate", "op": "gt", "value": 1}, "fields": ["human_id"]}, {"type": "table"}, name="Fails for recruiters")
    await share_tenant(admin, report["id"])
    resp = await admin.client.post(SCHED, json={"report_id": report["id"], "cadence": "daily", "local_time": "07:00:00", "format": "csv", "recipient_user_ids": [str(rorg.recruiter.user_id)]}, headers=h(admin))
    assert resp.status_code == 201, resp.text
    sid = resp.json()["id"]
    for i in range(3):
        slot = await _make_due(rorg, sid)
        _, statuses = await _tick(rorg)
        assert statuses == {rorg.recruiter.user_id: "failed"}
        run = (await _runs(rorg, sid, slot))[rorg.recruiter.user_id]
        assert run.error_code == "semantic_field_forbidden"
        s = await _schedule(rorg, sid)
        assert s.consecutive_failures == i + 1
    assert (s.status, s.paused_reason) == ("paused", "all_recipients_failed")
    async with tenant_session(rorg.tenant_id) as db:
        notes = (await db.execute(select(Notification.title).where(Notification.user_id == admin.user_id, Notification.type == "report_failed"))).scalars().all()
    assert len(notes) == 3 and any("paused" in t for t in notes)
    # A paused schedule is not evaluated; resume re-activates it with a fresh counter.
    await _make_due(rorg, sid)
    jobs, _ = await _tick(rorg)
    assert jobs == []
    resp = await admin.client.post(f"{SCHED}/{sid}/resume", headers=h(admin))
    assert resp.status_code == 200 and resp.json()["status"] == "active" and resp.json()["consecutive_failures"] == 0
    # Only the owner manages the schedule.
    assert (await rorg.manager.client.post(f"{SCHED}/{sid}/resume", headers=h(rorg.manager))).status_code == 404


async def test_owner_losing_schedule_permission_counts_as_failure(rorg: ROrg) -> None:
    await seed_placements(rorg)
    am = rorg.account
    report = await create_report(am, name="AM report")
    resp = await am.client.post(SCHED, json={"report_id": report["id"], "cadence": "monthly", "month_day": -1, "local_time": "06:00:00", "format": "pdf", "recipient_user_ids": [str(am.user_id)]}, headers=h(am))
    assert resp.status_code == 201, resp.text
    sid = resp.json()["id"]
    await revoke(rorg, "account_manager", "report:schedule")
    await _make_due(rorg, sid)
    jobs, _ = await _tick(rorg)
    assert jobs == []
    assert (await _schedule(rorg, sid)).consecutive_failures == 1
    async with tenant_session(rorg.tenant_id) as db:
        n = (await db.execute(select(func.count()).select_from(Notification).where(Notification.user_id == am.user_id, Notification.type == "report_failed"))).scalar_one()
    assert n == 1
