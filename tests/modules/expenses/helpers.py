"""Shared helpers for the Epic 27 expense tests."""

import uuid
from datetime import date
from decimal import Decimal
from typing import Any

import httpx

from app.core.database import tenant_session
from app.schemas.legal_entities import FxRateIn
from app.services import legal_entities_service, postings
from tests.modules.demands.helpers import DOrg, admin_ctx, h
from tests.modules.time.helpers import CSRF, Setup

EXP = "/api/v1/expenses"
WORK = "/api/v1/portal/work/expense-claims"
RECEIPTS = "/api/v1/portal/work/expense-receipts"
CLIENT = "/api/v1/client-portal/expenses"
PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"


def cat(category: str, **kw: Any) -> dict[str, Any]:
    return {"category": category, **kw}


async def policy(org: DOrg, *categories: dict[str, Any], **body: Any) -> dict[str, Any]:
    payload = {"name": "Standard", "scope": "tenant", "categories": list(categories), **body}
    resp = await org.admin.client.post(f"{EXP}/policies", json=payload, headers=h(org.admin))
    assert resp.status_code == 201, resp.text
    return dict(resp.json())


STANDARD = (
    cat("travel", default_billable=True, rebill_method="at_cost"),
    cat("meals", daily_limit="2000.00"),
    cat("mileage", mileage_rate_per_km="9.5000"),
    cat("accommodation", default_billable=True, rebill_method="markup", markup_pct="10"),
    cat("other"),
)


async def fx(org: DOrg, from_ccy: str, to_ccy: str, on: date, rate: str) -> None:
    async with tenant_session(org.tenant_id) as db:
        await legal_entities_service.upsert_rate(
            admin_ctx(org),
            db,
            FxRateIn(
                from_currency=from_ccy, to_currency=to_ccy, rate_date=on, rate=Decimal(rate), source="Finance"
            ),
        )


async def new_claim(s: Setup, title: str = "Client visit") -> dict[str, Any]:
    resp = await s.worker.post(WORK, json={"placement_id": s.pid, "title": title}, headers=CSRF)
    assert resp.status_code == 201, resp.text
    return dict(resp.json())


async def add_line(s: Setup, claim_id: str, **line: Any) -> httpx.Response:
    return await s.worker.post(f"{WORK}/{claim_id}/lines", json=line, headers=CSRF)


async def ok_line(s: Setup, claim_id: str, **line: Any) -> dict[str, Any]:
    resp = await add_line(s, claim_id, **line)
    assert resp.status_code == 200, resp.text
    return dict(resp.json())


async def submit(s: Setup, claim_id: str) -> httpx.Response:
    return await s.worker.post(f"{WORK}/{claim_id}/submit", headers=CSRF)


async def detail(org: DOrg, claim_id: str) -> dict[str, Any]:
    resp = await org.admin.client.get(f"{EXP}/claims/{claim_id}")
    assert resp.status_code == 200, resp.text
    return dict(resp.json())


async def decide(
    org: DOrg, claim_id: str, action: str = "approve", comment: str | None = None, **body: Any
) -> httpx.Response:
    return await org.admin.client.post(
        f"{EXP}/claims/{claim_id}/decide",
        json={"action": action, "comment": comment, **body},
        headers=h(org.admin),
    )


async def upload_receipt(s: Setup, data: bytes = PDF, name: str = "taxi.pdf") -> httpx.Response:
    return await s.worker.post(RECEIPTS, files={"file": (name, data, "application/pdf")}, headers=CSRF)


def line_by(d: dict[str, Any], category: str) -> dict[str, Any]:
    return next(ln for ln in d["lines"] if ln["category"] == category)


async def post_payable(
    org: DOrg, line_ids: list[str], placement_id: str, payee: str = "worker"
) -> list[uuid.UUID]:
    target = "payroll_row" if payee == "worker" else "vendor_statement_line"
    async with tenant_session(org.tenant_id) as db:
        return await postings.record_posted(
            admin_ctx(org),
            db,
            "payable",
            [
                postings.PostingLine(
                    "expense_line",
                    uuid.UUID(lid),
                    target,
                    uuid.uuid4(),
                    None,
                    Decimal("1"),
                    Decimal("1.00"),
                    "INR",
                    uuid.UUID(placement_id),
                    payee,
                )
                for lid in line_ids
            ],
        )
