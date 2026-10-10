# ruff: noqa: E501
"""Epic 30 operations: period close / reopen, collections (PTP, reminders, queue), disputes, bank import,
accounting export, reconciliation runs and the client-portal statement."""

import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import select

from app.models.receivables import CollectionActivity, PeriodCloseEvent
from app.models.tasks import Task
from app.schemas.receivables import ActivityIn, AllocationIn, PaymentIn
from app.services.receivables import collections, payments
from tests.modules.receivables.conftest import API, R, ROrg, err, invoice, ok

D = Decimal


async def _period_id(org: ROrg, year: int, month: int) -> str:
    rows = await ok(await org.c.get(f"{API}/periods", params={"legal_entity_id": str(org.entity_id), "year": year}))
    return next(r["id"] for r in rows if r["month"] == month)


async def test_close_with_warnings_blocks_backdated_payment_and_reopen(rorg: ROrg) -> None:
    await invoice(rorg, "5000.00", doc_date=date(2026, 6, 3))
    # an unallocated payment in June -> warning
    body = {"legal_entity_id": str(rorg.entity_id), "client_id": str(rorg.client_id), "payment_date": "2026-06-10", "amount": "100.00", "mode": "neft", "allocations": "none"}
    await ok(await rorg.c.post(f"{R}/payments", json=body, headers=rorg.h(str(uuid.uuid4()))), 201)
    pid = await _period_id(rorg, 2026, 6)
    checks = await ok(await rorg.c.get(f"{API}/periods/{pid}/checklist"))
    warn = next(c for c in checks if c["name"] == "receivables.unallocated_payments")
    assert warn["severity"] == "warning" and warn["count"] == 1
    err(await rorg.c.post(f"{API}/periods/{pid}/close", json={}, headers=rorg.h(str(uuid.uuid4()))), 422, "warnings_not_acknowledged")
    acks = [c["name"] for c in checks if c["severity"] == "warning" and c["count"] > 0]
    closed = await ok(await rorg.c.post(f"{API}/periods/{pid}/close", json={"acknowledged_warnings": acks}, headers=rorg.h(str(uuid.uuid4()))))
    assert closed["status"] == "closed"
    body2 = {**body, "payment_date": "2026-06-20"}
    e = err(await rorg.c.post(f"{R}/payments", json=body2, headers=rorg.h(str(uuid.uuid4()))), 409, "period_closed")
    assert e["details"]["period"] == "2026-06"
    reopen = await ok(await rorg.c.post(f"{API}/periods/{pid}/reopen", json={"reason": "late bank entry"}, headers=rorg.h()), 202)
    assert reopen["action"] == "reopen_requested"
    rows = await ok(await rorg.c.get(f"{API}/periods", params={"legal_entity_id": str(rorg.entity_id), "year": 2026}))
    assert next(r for r in rows if r["id"] == pid)["status"] == "open"
    await ok(await rorg.c.post(f"{API}/periods/{pid}/close", json={"acknowledged_warnings": acks}, headers=rorg.h(str(uuid.uuid4()))))
    report = await ok(await rorg.c.get(f"{API}/periods/report", params={"legal_entity_id": str(rorg.entity_id)}))
    actions = sorted(e["action"] for e in report["events"])
    assert actions == ["close", "reclosed", "reopen_requested", "reopened"]


async def test_close_blocked_by_blocker(rorg: ROrg) -> None:
    from app.services.legal_entities.periods import CloseCheckResult, _close_checks

    async def blocker(ctx, db, le, start, end) -> CloseCheckResult:  # type: ignore[no-untyped-def]  # noqa: ANN001
        return CloseCheckResult("test.blocker", "Test blocker", True, 1 if le == rorg.entity_id else 0, None)

    _close_checks.register("test.blocker", blocker)
    try:
        pid = await _period_id(rorg, 2026, 5)
        e = err(await rorg.c.post(f"{API}/periods/{pid}/close", json={}, headers=rorg.h(str(uuid.uuid4()))), 409, "close_blocked")
        assert e["details"]["checks"][0]["name"] == "test.blocker"
    finally:
        _close_checks.unregister("test.blocker")


async def test_ptp_broken_creates_task_and_reminders_skip_disputes(rorg: ROrg) -> None:
    inv = await invoice(rorg, "9000.00", doc_date=date(2026, 7, 1), due=date(2026, 7, 31))
    inv2 = await invoice(rorg, "4000.00", doc_date=date(2026, 7, 1), due=date(2026, 7, 31))
    async with rorg.session() as db:
        await collections.log_activity(rorg.ctx(), db, ActivityIn(client_id=rorg.client_id, document_kind="invoice", document_id=inv, kind="promise_to_pay", ptp_amount=D("9000.00"), ptp_date=date(2026, 8, 15)))
    async with rorg.session() as db:
        await payments.record(rorg.ctx(), db, PaymentIn(legal_entity_id=rorg.entity_id, client_id=rorg.client_id, payment_date=date(2026, 8, 20), amount=D("1000.00"), mode="upi", allocations=[AllocationIn(document_kind="invoice", document_id=inv, amount=D("1000.00"))]))
    async with rorg.session() as db:
        assert await collections.ptp_check(rorg.ctx(), db, date(2026, 9, 1)) == 1
    async with rorg.session() as db:
        ptp = (await db.execute(select(CollectionActivity).where(CollectionActivity.kind == "promise_to_pay"))).scalar_one()
        assert ptp.ptp_status == "broken" and ptp.task_id is not None
        assert (await db.execute(select(Task).where(Task.id == ptp.task_id))).scalar_one() is not None
    # queue + dispute + do-not-remind
    queue = await ok(await rorg.c.get(f"{R}/collections/queue"))
    assert {r["document_id"] for r in queue} >= {str(inv), str(inv2)}
    await ok(await rorg.c.post(f"{R}/disputes", json={"document_kind": "invoice", "document_id": str(inv2), "amount": "4000.00", "reason": "rate dispute"}, headers=rorg.h()), 201)
    out = await ok(await rorg.c.post(f"{R}/collections/reminders", json={"document_ids": [str(inv2)]}, headers=rorg.h()))
    assert out["sent"] == [] and out["skipped"][0]["reason"] == "disputed"
    await ok(await rorg.c.put(f"{R}/collections/clients/{rorg.client_id}/settings", json={"do_not_remind": True}, headers=rorg.h()))
    out = await ok(await rorg.c.post(f"{R}/collections/reminders", json={"document_ids": [str(inv)]}, headers=rorg.h()))
    assert out["skipped"][0]["reason"] == "do_not_remind"
    # auto allocation skips disputed amounts
    body = {"legal_entity_id": str(rorg.entity_id), "client_id": str(rorg.client_id), "payment_date": "2026-09-05", "amount": "20000.00", "mode": "neft"}
    pay = await ok(await rorg.c.post(f"{R}/payments", json=body, headers=rorg.h(str(uuid.uuid4()))), 201)
    assert {a["document_id"] for a in pay["allocations"]} == {str(inv)}
    disputes = await ok(await rorg.c.get(f"{R}/disputes", params={"client_id": str(rorg.client_id)}))
    resolved = await ok(await rorg.c.post(f"{R}/disputes/{disputes[0]['id']}/resolve", json={"status": "rejected", "resolution_note": "rate is per contract"}, headers=rorg.h()))
    assert resolved["status"] == "rejected"


async def test_bank_import_dedupe_and_apply(rorg: ROrg) -> None:
    inv = await invoice(rorg, "2500.00")
    async with rorg.session() as db:
        from app.models.billing import Invoice

        number = (await db.execute(select(Invoice.number).where(Invoice.id == inv))).scalar_one()
    csv = f"date,amount,reference,narrative\n2026-09-12,2500.00,UTR1,NEFT {number} ACME\n2026-09-12,99.00,UTR2,unknown\nbad,1,x,y\n".encode()
    files = {"file": ("stmt.csv", csv, "text/csv")}
    first = await ok(await rorg.c.post(f"{R}/bank-imports", data={"legal_entity_id": str(rorg.entity_id)}, files=files, headers=rorg.h(str(uuid.uuid4()))), 201)
    kinds = [r["match_kind"] for r in first["rows"]]
    assert kinds == ["matched", "unmatched", "failed"]
    again = await ok(await rorg.c.post(f"{R}/bank-imports", data={"legal_entity_id": str(rorg.entity_id)}, files={"file": ("stmt.csv", csv, "text/csv")}, headers=rorg.h(str(uuid.uuid4()))), 201)
    assert again["batch_id"] == first["batch_id"]
    applied = await ok(await rorg.c.post(f"{R}/bank-imports/{first['batch_id']}/apply", json={"row_ids": [first["rows"][0]["id"]]}, headers=rorg.h(str(uuid.uuid4()))))
    assert applied["rows"][0]["match_kind"] == "applied" and applied["rows"][0]["payment_id"]


async def test_accounting_export_flags_overlap_and_reconciliation_run(rorg: ROrg) -> None:
    inv = await invoice(rorg, "1180.00", taxable="1000.00")
    body = {"legal_entity_id": str(rorg.entity_id), "client_id": str(rorg.client_id), "payment_date": "2026-09-12", "amount": "1180.00", "mode": "neft"}
    await ok(await rorg.c.post(f"{R}/payments", json=body, headers=rorg.h(str(uuid.uuid4()))), 201)
    exp = {"legal_entity_id": str(rorg.entity_id), "from_date": "2026-09-01", "to_date": "2026-09-30"}
    first = await ok(await rorg.c.post(f"{R}/accounting-exports", json=exp, headers=rorg.h(str(uuid.uuid4()))), 201)
    assert first["row_count"] == 3 and first["flagged_count"] == 0  # invoice, payment, allocation
    second = await ok(await rorg.c.post(f"{R}/accounting-exports", json={**exp, "format": "xlsx"}, headers=rorg.h(str(uuid.uuid4()))), 201)
    assert second["flagged_count"] == 3 and second["version"] == 2
    dl = await rorg.c.get(f"{R}/accounting-exports/{first['id']}/download")
    assert dl.status_code == 200 and b"realized_fx" in dl.content and str(inv).encode() not in dl.content[:0]
    run = await ok(await rorg.c.post(f"{API}/reconciliation/runs", headers=rorg.h()), 201)
    findings = await ok(await rorg.c.get(f"{API}/reconciliation/runs/{run['id']}/findings"))
    assert [f for f in findings if f["check"].startswith("receivables.")] == []
    aging = await ok(await rorg.c.get(f"{R}/aging", params={"legal_entity_id": str(rorg.entity_id), "as_of": "2026-09-11", "group_by": "client"}))
    assert aging["rows"][0]["total_open"] == "1180.00"
    csv = await rorg.c.get(f"{R}/aging", params={"legal_entity_id": str(rorg.entity_id), "format": "csv"})
    assert csv.status_code == 200 and csv.headers["content-type"].startswith("text/csv")
    dso = await ok(await rorg.c.get(f"{R}/dso", params={"legal_entity_id": str(rorg.entity_id), "period_start": "2026-09-01", "period_end": "2026-09-30"}))
    assert dso["dso"] == "0.00" and dso["net_sales_inr"] == "1180.00"
    stmt = await rorg.c.get(f"{R}/statements", params={"client_id": str(rorg.client_id), "start": "2026-09-01", "end": "2026-09-30", "format": "pdf"})
    assert stmt.status_code == 200 and stmt.content[:4] == b"%PDF"


async def test_portal_schema_has_no_collection_fields() -> None:
    from app.schemas.receivables_portal import PortalPaymentStatus, PortalStatement

    fields = set(PortalStatement.model_fields) | set(PortalPaymentStatus.model_fields)
    assert not fields & {"ptp_amount", "body", "owner_id", "kind_activity", "do_not_remind"}


async def test_period_events_audit(rorg: ROrg) -> None:
    async with rorg.session() as db:
        assert (await db.execute(select(PeriodCloseEvent))).scalars().all() == []
