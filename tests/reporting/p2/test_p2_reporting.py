# ruff: noqa: E501
"""Epic 34 (Phase 2 dashboards, reporting): facts, summaries + reconciliation, dashboards/tiles, executive grid,
Phase 2 list kinds + exports, global search groups, cross-tenant isolation."""

import dataclasses
import random
import uuid
from collections.abc import AsyncIterator
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from fastapi import FastAPI
from sqlalchemy import select, text, update

from app.core.database import tenant_session
from app.models.reporting import ReportingFinanceMonthly, ReportingReconciliationIssue
from app.services.reporting import common, facts, summaries
from tests.modules.identity.conftest import Member, csrf, join, make_tenant
from tests.reporting.p2 import seed

API = "/api/v1"
D = Decimal
OCT, NOV = date(2026, 10, 1), date(2026, 11, 1)


@dataclasses.dataclass
class FOrg:
    tenant_id: uuid.UUID
    admin: Member
    finance: Member
    recruiter: Member
    account: Member
    le: uuid.UUID
    le2: uuid.UUID
    client: uuid.UUID

    async def close(self) -> None:
        for m in (self.admin, self.finance, self.recruiter, self.account):
            await m.client.aclose()


async def _forg(app: FastAPI) -> FOrg:
    tenant = await make_tenant(seat_limit=50)
    admin = await join(app, tenant, name="Ada Admin")
    finance = await join(app, tenant, "finance_admin", name="Fin Admin")
    recruiter = await join(app, tenant, "recruiter", name="Rita Recruiter")
    account = await join(app, tenant, "account_manager", name="Anu Account")
    async with tenant_session(tenant.id) as db:
        le = await seed.legal_entity(db, tenant.id)
        le2 = await seed.legal_entity(db, tenant.id, "Initech Global")
        client = await seed.client(db, tenant.id, admin.user_id)
    return FOrg(tenant.id, admin, finance, recruiter, account, le, le2, client)


@pytest.fixture
async def forg(app: FastAPI) -> AsyncIterator[FOrg]:
    org = await _forg(app)
    yield org
    await org.close()


@pytest.fixture
async def other_forg(app: FastAPI) -> AsyncIterator[FOrg]:
    org = await _forg(app)
    yield org
    await org.close()


async def _month_docs(org: FOrg) -> dict[str, Any]:
    """October: issued 1000 (cost 600) + 500 (cost 300); a partial CN of 200 (cost 120) dated in November;
    a full-CN-cancelled invoice 400 (counts), an IRN-cancelled 700 (excluded), an imported 900 (excluded)."""
    t = org.tenant_id
    async with tenant_session(t) as db:
        inv1, lines1 = await seed.invoice(
            db, t, org.le, org.client, date(2026, 10, 31), [(D("1000.00"), D("600.00"))]
        )
        await seed.invoice(db, t, org.le, org.client, date(2026, 10, 5), [(D("500.00"), D("300.00"))])
        await seed.invoice(db, t, org.le, org.client, date(2026, 10, 6), [(D("400.00"), D("100.00"))],
                           status="cancelled", cancel_basis="full_credit_note")  # fmt: skip
        await seed.invoice(db, t, org.le, org.client, date(2026, 10, 7), [(D("700.00"), D("10.00"))],
                           status="cancelled", cancel_basis="irn_cancelled")  # fmt: skip
        await seed.invoice(
            db, t, org.le, org.client, date(2026, 10, 8), [(D("900.00"), D("0"))], imported=True
        )
        await seed.credit_note(
            db, t, org.le, org.client, inv1, lines1[0], date(2026, 11, 2), D("200.00"), D("120.00")
        )
        await seed.payment(db, t, org.le, org.client, date(2026, 10, 20), D("1180.00"))
    return {"invoice": inv1}


# ================================================================ facts
async def test_revenue_facts_posted_documents(forg: FOrg) -> None:
    await _month_docs(forg)
    async with tenant_session(forg.tenant_id) as db:
        oct_rows = await facts.finance_by_month(db, [forg.le], OCT, facts.month_end(OCT))
        nov_rows = await facts.finance_by_month(db, [forg.le], NOV, facts.month_end(NOV))
        coll = await facts.collections(db, [forg.le], OCT, facts.month_end(OCT))
    october = facts.total(oct_rows)
    # 1000 + 500 + 400 (full-CN-cancelled counts); IRN-cancelled and imported excluded. 23:30 IST on the 31st
    # is a business date (doc_date), so it stays in October.
    assert october.revenue == D("1900.00")
    assert october.cost == D("1000.00")
    assert october.margin == D("900.00")
    assert october.margin_pct == D("47.37")
    assert october.invoice_count == 3
    november = facts.total(nov_rows)
    assert november.revenue == D("-200.00")  # the CN by its own doc_date; October unchanged
    assert november.credit == D("200.00")
    assert november.credit_note_count == 1
    assert coll == D("1180.00")


def test_margin_pct_null_at_zero_revenue() -> None:
    row = facts.FinanceRow()
    assert row.margin == D("0.00")
    assert row.margin_pct is None
    assert facts.money(D("12.345")) == {"amount": "12.35", "currency": "INR"}


async def test_le_filter_isolates_entities(forg: FOrg) -> None:
    t = forg.tenant_id
    async with tenant_session(t) as db:
        await seed.invoice(db, t, forg.le2, forg.client, date(2026, 10, 3), [(D("250.00"), D("0"))])
        await seed.invoice(db, t, forg.le, forg.client, date(2026, 10, 3), [(D("100.00"), D("0"))])
        only = facts.total(await facts.finance_by_month(db, [forg.le2], OCT, facts.month_end(OCT)))
    assert only.revenue == D("250.00")


# ================================================================ summaries + reconciliation
async def test_rebuild_verify_and_tamper(forg: FOrg) -> None:
    await _month_docs(forg)
    async with tenant_session(forg.tenant_id) as db:
        await summaries.rebuild(db, forg.le, OCT)
        await summaries.rebuild(db, forg.le, NOV)
        result = await summaries.verify(db, [OCT, NOV])
        assert result == {"checked": 4, "issues": 0, "persistent": 0}  # two LEs x two months
        row = (
            await db.execute(select(ReportingFinanceMonthly).where(ReportingFinanceMonthly.month == OCT))
        ).scalar_one()
        assert row.revenue_inr == D("1900.00")
        # Tamper -> detected, rebuilt and resolved.
        await db.execute(
            update(ReportingFinanceMonthly)
            .where(ReportingFinanceMonthly.id == row.id)
            .values(revenue_inr=D("1.00"))
        )
        result = await summaries.verify(db, [OCT])
        assert result["issues"] == 1 and result["persistent"] == 0
        issues = list((await db.execute(select(ReportingReconciliationIssue))).scalars())
        assert issues and all(i.resolved_at is not None for i in issues)
        fixed = (
            await db.execute(
                select(ReportingFinanceMonthly.revenue_inr).where(ReportingFinanceMonthly.month == OCT)
            )
        ).scalar_one()
        assert fixed == D("1900.00")


async def test_summary_equals_live_property(forg: FOrg) -> None:
    """Property (seeded random document sets): Σ monthly summary rows == live facts, exactly."""
    rnd = random.Random(34)
    t = forg.tenant_id
    async with tenant_session(t) as db:
        for _ in range(12):
            day = date(2026, rnd.choice((9, 10)), rnd.randint(1, 28))
            amount = D(rnd.randint(1, 99_999)) / 100
            await seed.invoice(
                db, t, forg.le, forg.client, day, [(amount, (amount * D("0.6")).quantize(D("0.01")))]
            )
        for m in (date(2026, 9, 1), OCT):
            await summaries.rebuild(db, forg.le, m)
            live = facts.total(await facts.finance_by_month(db, [forg.le], m, facts.month_end(m)))
            stored = facts.total(list((await summaries.summary_finance(db, forg.le, m)).values()))
            assert stored == live


async def test_executive_grid_endpoint(forg: FOrg) -> None:
    await _month_docs(forg)
    async with tenant_session(forg.tenant_id) as db:
        await summaries.rebuild(db, forg.le, OCT)
    resp = await forg.admin.client.get(
        f"{API}/dashboards/executive/grid",
        params={"legal_entity_id": str(forg.le), "month_from": "2026-09-01", "month_to": "2026-10-01"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    rev = next(r for r in body["rows"] if r["key"] == "revenue")
    assert rev["cells"][1]["value"] == {"amount": "1900.00", "currency": "INR"}
    assert rev["cells"][1]["source"] == "summary"
    # Range > 24 months -> 422; recruiter (no executive_dashboard:read) -> 403.
    bad = await forg.admin.client.get(
        f"{API}/dashboards/executive/grid", params={"month_from": "2024-01-01", "month_to": "2026-10-01"}
    )
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "invalid_range"
    assert (await forg.recruiter.client.get(f"{API}/dashboards/executive/grid")).status_code == 403


async def test_open_issue_serves_live(forg: FOrg) -> None:
    await _month_docs(forg)
    async with tenant_session(forg.tenant_id) as db:
        await summaries.rebuild(db, forg.le, OCT)
        db.add(
            ReportingReconciliationIssue(
                tenant_id=forg.tenant_id, legal_entity_id=forg.le, month=OCT, figure="revenue"
            )
        )
    resp = await forg.admin.client.get(
        f"{API}/dashboards/executive/grid",
        params={"legal_entity_id": str(forg.le), "month_from": "2026-10-01", "month_to": "2026-10-01"},
    )
    rev = next(r for r in resp.json()["rows"] if r["key"] == "revenue")
    assert rev["cells"][0]["source"] == "live"
    recon = await forg.admin.client.get(f"{API}/reports/reconciliation")
    assert recon.status_code == 200 and len(recon.json()["items"]) == 1


# ================================================================ dashboards / tiles
async def test_gating_and_default_order(forg: FOrg) -> None:
    fin = (await forg.finance.client.get(f"{API}/dashboards")).json()["items"]
    assert fin[0]["key"] == "finance" and fin[0]["default"]
    assert "executive" in [d["key"] for d in fin]
    rec = [d["key"] for d in (await forg.recruiter.client.get(f"{API}/dashboards")).json()["items"]]
    assert "finance" not in rec and "executive" not in rec
    assert (
        await forg.recruiter.client.get(f"{API}/dashboards/finance/tiles/invoices_issued")
    ).status_code == 404


async def test_invoices_issued_tile_money_and_le_filter(forg: FOrg, other_forg: FOrg) -> None:
    await _month_docs(forg)
    base = f"{API}/dashboards/finance/tiles/invoices_issued"
    resp = await forg.finance.client.get(
        base, params={"from": "2026-10-01", "to": "2026-10-31", "legal_entity_ids": str(forg.le)}
    )
    assert resp.status_code == 200, resp.text
    tile = resp.json()
    assert tile["status"] == "ok"
    assert tile["value"] == 3
    assert tile["money"] == {"amount": "1900.00", "currency": "INR"}
    other = await forg.finance.client.get(base, params={"legal_entity_ids": str(other_forg.le)})
    assert other.status_code == 422 and other.json()["error"]["code"] == "unknown_legal_entity"
    coll = (
        await forg.finance.client.get(
            f"{API}/dashboards/finance/tiles/collections", params={"from": "2026-10-01", "to": "2026-10-31"}
        )
    ).json()
    assert coll["money"] == {"amount": "1180.00", "currency": "INR"}


async def test_ar_aging_tile_uses_receivables(forg: FOrg) -> None:
    await _month_docs(forg)
    resp = await forg.finance.client.get(
        f"{API}/dashboards/finance/tiles/ar_aging", params={"as_of": "2026-11-30", "to": "2026-11-30"}
    )
    assert resp.status_code == 200, resp.text
    tile = resp.json()
    assert tile["status"] == "ok", tile  # Epic 30 aging_query is built
    keys = [r["key"] for r in tile["rows"]]
    assert keys[0] == "not_due" and keys[-1] == "disputed"
    assert all(isinstance(r["money"]["amount"], str) for r in tile["rows"])
    total = sum(D(r["money"]["amount"]) for r in tile["rows"] if r["key"] != "disputed")
    assert total > 0


async def test_missing_table_is_unavailable(forg: FOrg, monkeypatch: pytest.MonkeyPatch) -> None:
    real = common.available

    async def without(db: Any) -> frozenset[str]:
        return (await real(db)) - {"payments"}

    from app.services.reporting import dashboards

    monkeypatch.setattr(dashboards, "available", without)
    tile = (await forg.finance.client.get(f"{API}/dashboards/finance/tiles/collections")).json()
    assert tile["status"] == "unavailable"


async def test_compensation_masking_wip(forg: FOrg) -> None:
    tile = (await forg.finance.client.get(f"{API}/dashboards/finance/tiles/missing_timesheets_value")).json()
    assert tile["status"] in ("ok", "unavailable")
    if tile["status"] == "ok":
        assert tile["estimate"] is True


# ================================================================ lists + exports
async def test_placements_export_masks_rates(forg: FOrg) -> None:
    t = forg.tenant_id
    async with tenant_session(t) as db:
        cand = await seed.candidate(db, t, forg.admin.user_id)
        await seed.placement(
            db, t, forg.le, forg.client, cand, start=date(2026, 10, 1), human_id="PLC-777001"
        )
    lst = await forg.admin.client.get(f"{API}/reports/lists/placements", params={"q": "PLC-777001"})
    assert lst.status_code == 200, lst.text
    assert lst.json()["total"] == 1
    # A principal without compensation:view: rate / margin columns removed from the export, listed as masked.
    from app.core.context import RequestContext
    from app.core.permissions import Principal, Scope
    from app.services.reporting import lists

    principal = Principal(
        type="user", id=forg.recruiter.user_id, tenant_id=t, permissions={"placement:read": Scope.TENANT}
    )
    ctx = RequestContext(tenant_id=t, principal=principal)
    async with tenant_session(t) as db:
        query = await lists.build(ctx, db, "placements", lists.parse_filters("placements", {}), export=True)
    assert {"pay_rate", "bill_rate", "vendor_rate", "margin", "margin_pct"} <= set(query.masked_columns)
    assert not any(c.compensation for c in query.columns)
    no_fin = Principal(
        type="user", id=forg.recruiter.user_id, tenant_id=t, permissions={"billing:read": Scope.TENANT}
    )
    async with tenant_session(t) as db:
        q2 = await lists.build(
            RequestContext(tenant_id=t, principal=no_fin),
            db,
            "invoices",
            lists.parse_filters("invoices", {}),
            export=True,
        )
    assert {"taxable_inr", "total", "total_inr"} <= set(q2.masked_columns)


async def test_invoice_export_requires_finance_export_for_amounts(forg: FOrg) -> None:
    await _month_docs(forg)
    admin = await forg.admin.client.post(
        f"{API}/exports", json={"kind": "invoices", "format": "csv"}, headers=csrf(forg.admin.client)
    )
    assert admin.status_code == 200, admin.text
    assert "total" not in admin.headers.get("X-TOS-Masked-Columns", "")
    body = admin.content.decode()
    assert "1000.00" in body or "1180.00" in body


async def test_list_kind_permission(forg: FOrg) -> None:
    resp = await forg.recruiter.client.get(f"{API}/reports/lists/invoices")
    assert resp.status_code == 403


# ================================================================ search
async def test_search_exact_invoice_pinned(forg: FOrg) -> None:
    t = forg.tenant_id
    async with tenant_session(t) as db:
        await seed.invoice(
            db, t, forg.le, forg.client, date(2026, 10, 2), [(D("10.00"), D("0"))], number="INV/26-27/0042"
        )
    resp = await forg.admin.client.get(f"{API}/search/global", params={"q": "inv/26-27/0042"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["exact"]["type"] == "invoice" and body["exact"]["exact"] is True
    assert any(g["type"] == "invoice" for g in body["groups"])
    rec = (await forg.recruiter.client.get(f"{API}/search/global", params={"q": "INV/26-27/0042"})).json()
    assert all(g["type"] != "invoice" for g in rec["groups"])


# ================================================================ cross-tenant
async def test_cross_tenant(forg: FOrg, other_forg: FOrg) -> None:
    await _month_docs(forg)
    other = other_forg.admin.client
    grid = await other.get(f"{API}/dashboards/executive/grid", params={"legal_entity_id": str(forg.le)})
    assert grid.status_code == 422
    lst = (await other.get(f"{API}/reports/lists/invoices")).json()
    assert lst["total"] == 0
    tile = (
        await other.get(
            f"{API}/dashboards/finance/tiles/invoices_issued",
            params={"from": "2026-10-01", "to": "2026-10-31"},
        )
    ).json()
    assert tile["value"] == 0
    async with tenant_session(other_forg.tenant_id) as db:
        assert (await db.execute(text("SELECT count(*) FROM reporting_finance_monthly"))).scalar_one() == 0


async def test_every_p2_tile_renders(forg: FOrg) -> None:
    """Smoke: every Phase 2 tile answers 200 for the Tenant Admin (ok / unavailable, never a 500)."""
    await _month_docs(forg)
    t = forg.tenant_id
    async with tenant_session(t) as db:
        cand = await seed.candidate(db, t, forg.admin.user_id)
        await seed.placement(
            db, t, forg.le, forg.client, cand, start=date(2026, 9, 1), end=date(2026, 9, 20), status="ended"
        )
        await summaries.rebuild(db, forg.le, OCT)
    from app.services.reporting.dashboards_p2 import P2_DASHBOARDS

    for key, dash in P2_DASHBOARDS.items():
        for tile in dash.tiles:
            resp = await forg.admin.client.get(
                f"{API}/dashboards/{key}/tiles/{tile.key}", params={"from": "2026-09-01", "to": "2026-10-31"}
            )
            assert resp.status_code == 200, (key, tile.key, resp.text)
            assert resp.json()["status"] in ("ok", "unavailable"), (key, tile.key, resp.json())
