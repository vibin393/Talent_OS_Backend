"""Story 35.7 (A8, FR-185): go-live import: templates, upload/validate/preview, idempotent upload, apply with
``skip_invalid``, dependency order, masked error CSV, cross-tenant 404 and opening invoices."""

import csv
import io
import uuid
from datetime import date, timedelta
from typing import Any

from sqlalchemy import select

from app.core.audit import AuditLog
from app.core.database import tenant_session
from app.models.billing import Invoice
from app.models.candidates import CandidateEmail
from app.models.placements import Placement, WorkerBankAccount
from app.models.privacy import Consent
from app.services import legal_entities_service
from tests.modules.demands.helpers import DOrg, h

GL = "/api/v1/golive"
WORKER_HEADERS = ["full_name", "email", "phone", "pan", "bank_account_number", "ifsc"]


def _csv(headers: list[str], rows: list[list[str]], *, bom: bool = False) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(headers)
    w.writerows(rows)
    return (b"\xef\xbb\xbf" if bom else b"") + buf.getvalue().encode()


async def _upload(org: DOrg, kind: str, data: bytes, name: str = "data.csv") -> Any:
    return await org.admin.client.post(
        f"{GL}/imports", data={"kind": kind}, files={"file": (name, data, "text/csv")}, headers=h(org.admin)
    )


async def _apply(org: DOrg, batch_id: str, *, skip_invalid: bool = False, key: str | None = None) -> Any:
    headers = {**h(org.admin), "Idempotency-Key": key or uuid.uuid4().hex}
    return await org.admin.client.post(
        f"{GL}/imports/{batch_id}/apply",
        json={"skip_invalid": skip_invalid, "confirm": "APPLY"},
        headers=headers,
    )


def _email() -> str:
    return f"w-{uuid.uuid4().hex[:10]}@example.com"


async def test_kinds_and_templates(dorg: DOrg) -> None:
    c = dorg.admin.client
    kinds = (await c.get(f"{GL}/kinds")).json()
    # Phase 2 kinds first; Epic 52 appends the Phase 3 kinds (tests/golive/test_p3_imports.py).
    assert [k["kind"] for k in kinds][:4] == ["workers", "placements", "opening_invoices", "leave_balances"]
    by = {k["kind"]: k for k in kinds}
    assert by["workers"]["available"] is True
    assert by["placements"]["depends_on"] == ["workers"]
    resp = await c.get(f"{GL}/templates/placements")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")
    header, example = list(csv.reader(io.StringIO(resp.text)))
    assert header[:2] == ["worker_email", "client_id"]
    assert len(example) == len(header)
    dictionary = (await c.get(f"{GL}/templates/workers/dictionary")).json()
    assert dictionary[0] == {
        "column": "full_name",
        "required": True,
        "description": "Worker's full name",
        "example": "Asha Rao",
    }
    assert (await c.get(f"{GL}/templates/nope")).status_code == 404


async def test_workers_preview_duplicate_skip_invalid_and_masked_errors(dorg: DOrg, other_dorg: DOrg) -> None:
    good, banked = _email(), _email()
    data = _csv(
        WORKER_HEADERS,
        [
            ["Asha Rao", good, "", "", "", ""],
            ["Bad Bank", _email(), "", "", "12AB567890", "HDFC0001234"],
            ["Ravi Kumar", banked, "", "ABCDE1234F", "123456789012", "HDFC0001234"],
            ["", "not-an-email", "", "", "", ""],
        ],
        bom=True,
    )
    resp = await _upload(dorg, "workers", data, "workers.csv")
    assert resp.status_code == 201, resp.text
    batch = resp.json()
    assert batch["status"] == "previewed"
    assert batch["kind"] == "workers"
    assert (batch["row_count"], batch["valid_count"], batch["error_count"]) == (4, 2, 2)
    codes = {(e["row"], e["column"], e["code"]) for e in batch["errors"]}
    assert (2, "bank_account_number", "invalid_account_number") in codes
    assert (4, "email", "invalid_email") in codes

    # Same file + kind -> the same batch (200).
    again = await _upload(dorg, "workers", data, "workers.csv")
    assert again.status_code == 200
    assert again.json()["id"] == batch["id"]

    # The error CSV never echoes bank / identity values.
    err = await dorg.admin.client.get(f"{GL}/imports/{batch['id']}/errors.csv")
    assert err.status_code == 200
    assert "12AB567890" not in err.text
    assert "****7890" in err.text
    lines = list(csv.DictReader(io.StringIO(err.text)))
    assert {"row", "column", "code", "message", "value"} <= set(lines[0])

    # Errors refuse a plain apply.
    refused = await _apply(dorg, batch["id"])
    assert refused.status_code == 409
    assert refused.json()["error"]["code"] == "has_errors"

    # Tenant B cannot see or apply tenant A's batch.
    assert (await other_dorg.admin.client.get(f"{GL}/imports/{batch['id']}")).status_code == 404
    assert (await _apply(other_dorg, batch["id"], skip_invalid=True)).status_code == 404

    key = uuid.uuid4().hex
    applied = await _apply(dorg, batch["id"], skip_invalid=True, key=key)
    assert applied.status_code == 200, applied.text
    out = applied.json()
    assert out["status"] == "applied"
    assert out["applied_at"]
    assert (out["applied_count"], out["skipped_count"], out["failed_count"]) == (2, 2, 0)
    replay = await _apply(dorg, batch["id"], skip_invalid=True, key=key)
    assert replay.status_code == 200
    assert replay.headers.get("Idempotency-Replayed") == "true"

    rows = (
        await dorg.admin.client.get(f"{GL}/imports/{batch['id']}/rows", params={"status": "applied"})
    ).json()
    assert [r["row_no"] for r in rows] == [1, 3]
    assert rows[0]["result_ref"].startswith("candidate:")

    async with tenant_session(dorg.tenant_id) as db:
        cid = (
            await db.execute(select(CandidateEmail.candidate_id).where(CandidateEmail.email == banked))
        ).scalar_one()
        consent = (
            await db.execute(
                select(Consent).where(Consent.candidate_id == cid, Consent.purpose == "engagement")
            )
        ).scalar_one()
        assert consent.source == "import"
        bank = (
            await db.execute(select(WorkerBankAccount).where(WorkerBankAccount.candidate_id == cid))
        ).scalar_one()
        assert bank.account_number_last4 == "9012"
        assert bank.source == "import"
        audit_rows = (
            (await db.execute(select(AuditLog).where(AuditLog.action == "golive.applied"))).scalars().all()
        )
        assert len(audit_rows) == 1
        assert audit_rows[0].data["applied"] == 2

    listed = (await dorg.admin.client.get(f"{GL}/imports", params={"kind": "workers"})).json()
    assert [b["id"] for b in listed] == [batch["id"]]

    # Re-importing an existing Worker matches by email (no duplicate Candidate).
    rematch = await _upload(dorg, "workers", _csv(["full_name", "email"], [["Asha R", good.upper()]]))
    assert (await _apply(dorg, rematch.json()["id"])).status_code == 200
    async with tenant_session(dorg.tenant_id) as db:
        ids = (
            await db.execute(select(CandidateEmail.candidate_id).where(CandidateEmail.email == good))
        ).all()
        assert len(ids) == 1


async def test_placements_need_workers_first(dorg: DOrg) -> None:
    email = _email()
    start = (date.today() - timedelta(days=30)).isoformat()
    placement_csv = _csv(
        ["worker_email", "client_id", "worker_type", "start_date", "rate_unit", "pay_rate", "bill_rate"],
        [[email, str(dorg.client_id), "employee", start, "hour", "1000.00", "1500.00"]],
    )
    up = await _upload(dorg, "placements", placement_csv)
    assert up.status_code == 201, up.text
    batch = up.json()
    assert batch["error_count"] == 0, batch
    blocked = await _apply(dorg, batch["id"])
    assert blocked.status_code == 409
    assert blocked.json()["error"]["code"] == "dependency_missing"

    workers = await _upload(dorg, "workers", _csv(["full_name", "email"], [["Wren Worker", email]]))
    assert (await _apply(dorg, workers.json()["id"])).status_code == 200

    done = await _apply(dorg, batch["id"])
    assert done.status_code == 200, done.text
    assert done.json()["applied_count"] == 1, done.json()["errors"]
    async with tenant_session(dorg.tenant_id) as db:
        cid = (
            await db.execute(select(CandidateEmail.candidate_id).where(CandidateEmail.email == email))
        ).scalar_one()
        p = (await db.execute(select(Placement).where(Placement.candidate_id == cid))).scalar_one()
        assert p.status == "active"
        assert p.actual_start_date is not None
        assert p.actual_start_date.isoformat() == start


async def test_placement_row_errors_are_previewed(dorg: DOrg) -> None:
    email = _email()
    workers = await _upload(dorg, "workers", _csv(["full_name", "email"], [["Ira Iyer", email]]))
    assert (await _apply(dorg, workers.json()["id"])).status_code == 200
    data = _csv(
        ["worker_email", "client_id", "worker_type", "start_date", "rate_unit", "pay_rate"],
        [
            [email, str(dorg.client_id), "employee", "01/04/2026", "hour", "1000"],
            [email, "not-an-id", "employee", "2026-04-01", "hour", "1000"],
            [email, str(dorg.client_id), "wizard", "2026-04-01", "hour", "1000"],
        ],
    )
    batch = (await _upload(dorg, "placements", data)).json()
    assert batch["error_count"] == 3, batch
    cols = {(e["row"], e["column"]) for e in batch["errors"]}
    assert (1, "start_date") in cols
    assert (2, "client_id") in cols
    assert (3, "worker_type") in cols


async def test_bad_files_refused(dorg: DOrg) -> None:
    missing = await _upload(dorg, "workers", _csv(["email"], [["a@example.com"]]))
    assert missing.status_code == 422
    assert missing.json()["error"]["code"] == "missing_columns"
    binary = await _upload(dorg, "workers", b"%PDF-1.4\x00\x00", "x.csv")
    assert binary.status_code == 422
    assert binary.json()["error"]["code"] == "unsupported_file_type"
    other = await _upload(dorg, "workers", b"full_name,email\n", "x.xlsx")
    assert other.status_code == 422


async def test_discard_then_reupload_creates_new_batch(dorg: DOrg) -> None:
    data = _csv(["full_name", "email"], [["Dev Das", _email()]])
    first = (await _upload(dorg, "workers", data)).json()
    gone = await dorg.admin.client.post(f"{GL}/imports/{first['id']}/discard", headers=h(dorg.admin))
    assert gone.status_code == 200
    assert gone.json()["status"] == "discarded"
    second = await _upload(dorg, "workers", data)
    assert second.status_code == 201
    assert second.json()["id"] != first["id"]


async def test_opening_invoices(dorg: DOrg) -> None:
    from app.core.context import RequestContext
    from app.core.permissions import CATALOG, Principal, Scope
    from app.services.golive.owners import default_legal_entity
    from tests.modules.billing.conftest import setup_entity

    principal = Principal(
        type="user",
        id=dorg.admin.user_id,
        tenant_id=dorg.tenant_id,
        permissions=dict.fromkeys(CATALOG, Scope.TENANT),
    )
    async with tenant_session(dorg.tenant_id) as db:
        entity_id = await default_legal_entity(db)
    assert entity_id is not None
    await setup_entity(
        dorg.tenant_id, entity_id, RequestContext(tenant_id=dorg.tenant_id, principal=principal)
    )
    async with tenant_session(dorg.tenant_id) as db:
        await legal_entities_service.ensure_periods(db, entity_id, date(2026, 3, 1), date(2026, 3, 1))
    number = f"OLD/{uuid.uuid4().hex[:6].upper()}"
    data = _csv(
        ["number", "doc_date", "client_id", "taxable_total", "tax_total", "document_total"],
        [
            [number, "2026-03-15", str(dorg.client_id), "100000.00", "18000.00", "118000.00"],
            [number, "2026-03-16", str(dorg.client_id), "1.00", "0", "1.00"],
            [f"{number}X", "2025-01-10", str(dorg.client_id), "1.00", "0", "1.00"],
        ],
    )
    up = await _upload(dorg, "opening_invoices", data)
    assert up.status_code == 201, up.text
    batch = up.json()
    assert batch["error_count"] == 1, batch["errors"]
    assert batch["errors"][0]["code"] == "duplicate_in_file"
    done = await _apply(dorg, batch["id"], skip_invalid=True)
    assert done.status_code == 200, done.text
    assert done.json()["applied_count"] == 1, done.json()["errors"]
    # No open Accounting Period for Jan 2025: the period guard refuses that row.
    failed = (
        await dorg.admin.client.get(f"{GL}/imports/{batch['id']}/rows", params={"status": "failed"})
    ).json()
    assert [(r["row_no"], r["errors"][0]["code"]) for r in failed] == [(3, "period_closed")]
    async with tenant_session(dorg.tenant_id) as db:
        inv = (await db.execute(select(Invoice).where(Invoice.number == number))).scalar_one()
        assert inv.is_imported
        assert inv.status == "issued"


async def test_leave_balances_dependency_and_row_errors(dorg: DOrg) -> None:
    headers = ["worker_email", "leave_type_code", "days"]
    pending = await _upload(dorg, "leave_balances", _csv(headers, [[_email(), "CL", "5"]]))
    assert pending.status_code == 201, pending.text
    blocked = await _apply(dorg, pending.json()["id"])
    assert blocked.status_code == 409
    assert blocked.json()["error"]["code"] == "dependency_missing"

    email = _email()
    workers = await _upload(dorg, "workers", _csv(["full_name", "email"], [["Lila Leave", email]]))
    assert (await _apply(dorg, workers.json()["id"])).status_code == 200
    bad = (await _upload(dorg, "leave_balances", _csv(headers, [[email, "NOPE", "1.234"]]))).json()
    codes = {(e["column"], e["code"]) for e in bad["errors"]}
    assert ("leave_type_code", "unknown_leave_type") in codes
    assert ("days", "invalid_days") in codes


async def test_unavailable_kind_refused(dorg: DOrg, monkeypatch: Any) -> None:
    from app.services.golive import owners

    real = owners.owner
    monkeypatch.setattr(owners, "owner", lambda kind: None if kind == "leave_balances" else real(kind))
    kinds = {k["kind"]: k["available"] for k in (await dorg.admin.client.get(f"{GL}/kinds")).json()}
    assert kinds["leave_balances"] is False
    resp = await _upload(
        dorg, "leave_balances", _csv(["worker_email", "leave_type_code", "days"], [["a@b.co", "CL", "1"]])
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "kind_unavailable"
