"""Exactly-once posting, ledger locks, reopen, adjustment timesheets, retro rate adjustments and CSV import
(Epic 25 A7-A9, AD-21, AD-23)."""

import asyncio
import uuid
from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest
from fastapi import FastAPI
from sqlalchemy import select, text

from app.core.database import tenant_session
from app.core.errors import ConflictError
from app.models.time import RateAdjustment, TimesheetEntry
from app.services import postings
from app.services.postings import SourceQuery, load_unposted
from tests.modules.demands.helpers import DOrg, admin_ctx, h
from tests.modules.time.helpers import (
    AM,
    TIME,
    Setup,
    active,
    admin_decide,
    detail,
    first_timesheet,
    post_payable,
    week,
    worker_save,
    worker_submit,
)


async def _approved(app: FastAPI, org: DOrg, *hours: str, **kw: Any) -> tuple[Setup, dict[str, Any]]:
    s = await active(app, org, route=[AM], **kw)
    ts = await first_timesheet(s)
    assert (await worker_save(s, ts["id"], week(s.start, *hours))).status_code == 200
    assert (await worker_submit(s, ts["id"])).status_code == 200
    assert (await admin_decide(org, ts["id"])).status_code == 200
    d = await detail(org, ts["id"])
    assert d["status"] == "approved"
    return s, d


def _line(
    kind: str, sid: uuid.UUID, target: str, pid: str, payee: str | None = "worker"
) -> postings.PostingLine:
    return postings.PostingLine(
        kind, sid, target, uuid.uuid4(), None, Decimal("8"), Decimal("8000.00"), "INR", uuid.UUID(pid), payee
    )


async def test_exactly_once_per_ledger_and_locks(app: FastAPI, dorg: DOrg) -> None:
    # (1) loader excludes non-approved timesheets
    s0 = await active(app, dorg, route=[AM])
    draft = await first_timesheet(s0)
    await worker_save(s0, draft["id"], week(s0.start, "8"))
    async with tenant_session(dorg.tenant_id) as db:
        items = await load_unposted(
            admin_ctx(dorg),
            db,
            SourceQuery(
                ledger="billing", period_end=s0.start + timedelta(days=6), placement_ids=[uuid.UUID(s0.pid)]
            ),
        )
    assert items == []

    s, d = await _approved(app, dorg, "8")
    entry = next(e for e in d["entries"] if e["category"] == "regular")
    sid = uuid.UUID(entry["id"])
    ctx = admin_ctx(dorg)
    # (3) one payable and one billing posting of the same entry are allowed
    async with tenant_session(dorg.tenant_id) as db:
        await postings.record_posted(
            ctx, db, "payable", [_line("timesheet_entry", sid, "payroll_row", s.pid)]
        )
        await postings.reserve(
            ctx, db, "billing", [_line("timesheet_entry", sid, "invoice_line", s.pid, None)]
        )
    # (2) a second payroll posting in the same ledger -> 409 source_already_posted
    async with tenant_session(dorg.tenant_id) as db:
        with pytest.raises(ConflictError) as exc:
            await postings.record_posted(
                ctx, db, "payable", [_line("timesheet_entry", sid, "payroll_row", s.pid)]
            )
        assert exc.value.code == "source_already_posted"
    locks = (await detail(dorg, d["id"]))["locks"]
    assert locks == {"payroll_locked": True, "billing_locked": False, "in_draft_invoice": True}
    # (6) once posted, raw UPDATE / DELETE of the entry as the runtime role is refused by the trigger
    async with tenant_session(dorg.tenant_id) as db:
        with pytest.raises(Exception, match="document_immutable"):
            async with db.begin_nested():
                await db.execute(text("UPDATE timesheet_entries SET hours = 1 WHERE id = :i"), {"i": sid})
        with pytest.raises(Exception, match="document_immutable"):
            async with db.begin_nested():
                await db.execute(text("DELETE FROM timesheet_entries WHERE id = :i"), {"i": sid})
    resp = await dorg.admin.client.post(
        f"{TIME}/timesheets/{d['id']}/reopen", json={"reason": "fix"}, headers=h(dorg.admin)
    )
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "timesheet_locked"
    assert resp.json()["error"]["details"]["payroll_locked"] is True
    # (5) release then repost -> one active + one released
    async with tenant_session(dorg.tenant_id) as db:
        active_rows = await postings.active_postings_for(db, "timesheet_entry", [sid], ledger="payable")
        await postings.release(
            ctx, db, "payable", reason="payroll void", posting_ids=[p.id for p in active_rows[sid]]
        )
        await postings.record_posted(
            ctx, db, "payable", [_line("timesheet_entry", sid, "payroll_row", s.pid)]
        )
    async with tenant_session(dorg.tenant_id) as db:
        all_rows = await postings.targets_for(db, "payable", "timesheet_entry", sid)
    assert sorted(r.state for r in all_rows) == ["posted", "released"]


async def test_reserved_only_is_in_draft_invoice_and_reopen_when_free(app: FastAPI, dorg: DOrg) -> None:
    s, d = await _approved(app, dorg, "8")
    sid = uuid.UUID(next(e["id"] for e in d["entries"] if e["category"] == "regular"))
    ctx = admin_ctx(dorg)
    async with tenant_session(dorg.tenant_id) as db:
        ids = await postings.reserve(
            ctx, db, "billing", [_line("timesheet_entry", sid, "invoice_line", s.pid, None)]
        )
    resp = await dorg.admin.client.post(
        f"{TIME}/timesheets/{d['id']}/reopen", json={"reason": "fix"}, headers=h(dorg.admin)
    )
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "in_draft_invoice"
    async with tenant_session(dorg.tenant_id) as db:
        await postings.release(ctx, db, "billing", reason="draft discarded", posting_ids=ids)
    resp = await dorg.admin.client.post(
        f"{TIME}/timesheets/{d['id']}/reopen", json={"reason": "fix"}, headers=h(dorg.admin)
    )
    assert resp.status_code == 200 and resp.json()["status"] == "draft"


async def test_concurrent_posting_single_active_row(app: FastAPI, dorg: DOrg) -> None:
    s, d = await _approved(app, dorg, "8")
    sid = uuid.UUID(next(e["id"] for e in d["entries"] if e["category"] == "regular"))
    ctx = admin_ctx(dorg)

    async def one() -> bool:
        try:
            async with tenant_session(dorg.tenant_id) as db:
                await postings.record_posted(
                    ctx, db, "payable", [_line("timesheet_entry", sid, "payroll_row", s.pid)]
                )
            return True
        except Exception:
            return False

    results = await asyncio.gather(*(one() for _ in range(20)))
    assert results.count(True) == 1
    async with tenant_session(dorg.tenant_id) as db:
        assert (
            len((await postings.active_postings_for(db, "timesheet_entry", [sid], ledger="payable"))[sid])
            == 1
        )


async def test_adjustment_timesheet_is_a_new_source(app: FastAPI, dorg: DOrg) -> None:
    s, d = await _approved(app, dorg, "10.50", "8", config={"ot_daily_threshold": "9"})
    await post_payable(dorg, "timesheet_entry", [e["id"] for e in d["entries"]], s.pid)
    resp = await dorg.admin.client.post(
        f"{TIME}/timesheets/{d['id']}/adjustments",
        json={
            "reason": "Too much OT",
            "entries": [{"date": s.start.isoformat(), "category": "ot", "delta_hours": "-2.00"}],
        },
        headers=h(dorg.admin),
    )
    assert resp.status_code == 422 and resp.json()["error"]["code"] == "adjustment_below_zero"
    resp = await dorg.admin.client.post(
        f"{TIME}/timesheets/{d['id']}/adjustments",
        json={
            "reason": "OT correction",
            "entries": [
                {"date": s.start.isoformat(), "category": "ot", "delta_hours": "-1.00"},
                {
                    "date": (s.start + timedelta(days=1)).isoformat(),
                    "category": "regular",
                    "delta_hours": "2.00",
                },
            ],
        },
        headers=h(dorg.admin),
    )
    assert resp.status_code == 201, resp.text
    adj = resp.json()
    assert adj["kind"] == "adjustment" and adj["adjustment_seq"] == 1 and adj["status"] == "draft"
    resp = await dorg.admin.client.post(
        f"{TIME}/timesheets/{adj['id']}/submit", json={"reason": "adjustment"}, headers=h(dorg.admin)
    )
    assert resp.status_code == 200, resp.text
    assert (await admin_decide(dorg, adj["id"])).status_code == 200
    async with tenant_session(dorg.tenant_id) as db:
        items = await load_unposted(
            admin_ctx(dorg),
            db,
            SourceQuery(
                ledger="payable", period_end=s.start + timedelta(days=6), placement_ids=[uuid.UUID(s.pid)]
            ),
        )
    adj_items = sorted(
        (i.attributes["category"], i.quantity) for i in items if i.source_kind == "adjustment_entry"
    )
    assert adj_items == [("ot", Decimal("-1.00")), ("regular", Decimal("2.00"))]
    assert not [i for i in items if i.source_kind == "timesheet_entry"]  # originals are posted, untouched
    original = await detail(dorg, d["id"])
    assert {t["category"]: t["hours"] for t in original["totals"]} == {"regular": "17.00", "ot": "1.50"}


async def test_retro_rate_segments_create_rate_adjustments_once(app: FastAPI, dorg: DOrg) -> None:
    s, d = await _approved(app, dorg, "0", "0", "0", "8", pay_rate="500.00", bill_rate="900.00", weeks_ago=3)
    entry_id = next(e["id"] for e in d["entries"] if e["category"] == "regular")
    await post_payable(dorg, "timesheet_entry", [entry_id], s.pid)
    # Retro segments are written directly (deterministic ordering of the EXCLUDE-guarded rows) and the
    # ``RateSegmentAdded`` handler is invoked as the event would.
    from app.services.time import rate_adjustments

    prev_id = None
    async with tenant_session(dorg.tenant_id) as db:
        prev_id = (
            await db.execute(
                text("SELECT id FROM rate_segments WHERE placement_id = :p AND status = 'active'"),
                {"p": uuid.UUID(s.pid)},
            )
        ).scalar_one()
    for day, rate in ((1, "550.00"), (2, "520.00")):
        frm = s.start + timedelta(days=day)
        new_id = uuid.uuid4()
        async with tenant_session(dorg.tenant_id) as db:
            await db.execute(
                text("UPDATE rate_segments SET effective_to = :t WHERE id = :i"),
                {"t": frm - timedelta(days=1), "i": prev_id},
            )
            await db.execute(
                text(
                    "INSERT INTO rate_segments (id, tenant_id, placement_id, effective_from, rate_unit, "
                    "currency, "
                    "pay_rate, status) "
                    "VALUES (:i, :t, :p, :f, 'hour', 'INR', :r, 'active')"
                ),
                {"i": new_id, "t": dorg.tenant_id, "p": uuid.UUID(s.pid), "f": frm, "r": Decimal(rate)},
            )
            await rate_adjustments.handle_segment_added(
                admin_ctx(dorg), db, uuid.UUID(s.pid), new_id, [(prev_id, frm, None)]
            )
        prev_id = new_id
    async with tenant_session(dorg.tenant_id) as db:
        rows = list(
            (
                await db.execute(
                    select(RateAdjustment)
                    .where(RateAdjustment.original_source_id == uuid.UUID(entry_id))
                    .order_by(RateAdjustment.created_at)
                )
            ).scalars()
        )
    assert [(r.ledger, r.rate_delta) for r in rows] == [
        ("payable", Decimal("50.0000")),
        ("payable", Decimal("-30.0000")),
    ]
    # delivered twice -> still one per (ledger, source, segment)
    async with tenant_session(dorg.tenant_id) as db:
        seg = rows[-1].segment_id
        prev = rows[0].segment_id
        again = await rate_adjustments.handle_segment_added(
            admin_ctx(dorg), db, uuid.UUID(s.pid), seg, [(prev, s.start + timedelta(days=2), None)]
        )
    assert again == 0
    # unposted entries get none; loader exposes rate_adjustment items for the payable ledger
    async with tenant_session(dorg.tenant_id) as db:
        n = (
            (await db.execute(select(RateAdjustment).where(RateAdjustment.placement_id == uuid.UUID(s.pid))))
            .scalars()
            .all()
        )
        items = await load_unposted(
            admin_ctx(dorg),
            db,
            SourceQuery(
                ledger="payable", period_end=s.start + timedelta(days=30), placement_ids=[uuid.UUID(s.pid)]
            ),
        )
    assert len(n) == 2
    ra = [i for i in items if i.source_kind == "rate_adjustment"]
    assert sorted(i.attributes["rate_delta"] for i in ra) == [Decimal("-30.0000"), Decimal("50.0000")]


async def test_import_preview_duplicate_and_apply_idempotent(app: FastAPI, dorg: DOrg) -> None:
    s = await active(app, dorg, route=[AM])
    ts = await first_timesheet(s)
    d0 = s.start.isoformat()
    d1 = (s.start + timedelta(days=1)).isoformat()
    csv_text = (
        "placement_id,date,category,hours\n"
        f"{s.pid},{d0},regular,8\n"
        f'{s.pid},{d1},regular,"7,5"\n'
        f"{uuid.uuid4()},{d0},regular,8\n"
        f"{s.pid},{d1},ot,1\n"
    )
    files = {"file": ("ts.csv", csv_text.encode(), "text/csv")}
    resp = await dorg.admin.client.post(f"{TIME}/imports", files=files, headers=h(dorg.admin))
    assert resp.status_code == 201, resp.text
    preview = resp.json()
    errors = [r["errors"] for r in preview["rows"]]
    assert errors == [[], ["bad_decimal"], ["unknown_placement"], ["category_disabled"]]
    dup = await dorg.admin.client.post(f"{TIME}/imports", files=files, headers=h(dorg.admin))
    assert dup.status_code == 409 and dup.json()["error"]["code"] == "import_duplicate_file"
    key = {"Idempotency-Key": f"k-{uuid.uuid4().hex}", **h(dorg.admin)}
    first = await dorg.admin.client.post(f"{TIME}/imports/{preview['batch_id']}/apply", headers=key)
    assert first.status_code == 200, first.text
    assert first.json()["applied_rows"] == 1 and first.json()["submitted_timesheet_ids"] == [ts["id"]]
    replay = await dorg.admin.client.post(f"{TIME}/imports/{preview['batch_id']}/apply", headers=key)
    assert replay.status_code == 200 and replay.json() == first.json()
    d = await detail(dorg, ts["id"])
    assert d["status"] == "submitted" and d["entries"][0]["is_proxy"] is True
    async with tenant_session(dorg.tenant_id) as db:
        count = len(
            (
                await db.execute(
                    select(TimesheetEntry).where(TimesheetEntry.timesheet_id == uuid.UUID(ts["id"]))
                )
            )
            .scalars()
            .all()
        )
    assert count == 1
