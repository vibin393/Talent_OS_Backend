"""Epic 17 A8: vendor payable statements over the payable ledger (FR-98, AD-23) with a fake posting source
(time / expense sources land with Epics 25 / 27; re-run after B3 / B4)."""

import uuid
from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from fastapi import FastAPI
from hypothesis import given, settings
from hypothesis import strategies as st
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from app.core.context import RequestContext
from app.core.database import tenant_session
from app.core.money import line_taxable
from app.models.placements import Placement, RateSegment
from app.models.postings import PayablePosting
from app.services import postings
from tests.epic3 import create_candidate
from tests.modules.demands.helpers import DOrg, h
from tests.modules.legal_entities.conftest import provision
from tests.modules.vendors.conftest import PORTAL, active_vendor, vendor_user

SOURCE = "test_vendor_entry"
ITEMS: list[postings.SourceItem] = []


async def _loader(ctx: RequestContext, db: Any, query: postings.SourceQuery) -> list[postings.SourceItem]:
    ids = set(query.placement_ids or [])
    return [i for i in ITEMS if i.placement_id in ids and i.item_date <= query.period_end]


@pytest.fixture(autouse=True)
def _source() -> Iterator[None]:
    if SOURCE not in postings.posting_sources():
        postings.register_posting_source(SOURCE, _loader, ledgers=frozenset({"payable"}))
    ITEMS.clear()
    yield
    ITEMS.clear()


async def _placement(org: DOrg, le: uuid.UUID, vendor_id: uuid.UUID | None, rate: str = "1300") -> uuid.UUID:
    cand = await create_candidate(org.admin, full_name=f"Worker {uuid.uuid4().hex[:4]}")
    pid = uuid.uuid4()
    async with tenant_session(org.tenant_id) as db:
        db.add(
            Placement(
                id=pid,
                tenant_id=org.tenant_id,
                human_id=f"PLC-{uuid.uuid4().hex[:6]}",
                candidate_id=uuid.UUID(cand["id"]),
                client_id=org.client_id,
                vendor_id=vendor_id,
                legal_entity_id=le,
                worker_type="vendor_contractor" if vendor_id else "employee",
                payee_kind="vendor" if vendor_id else "worker",
                planned_start_date=date(2026, 9, 1),
                currency="INR",
            )
        )
        await db.flush()
        db.add(
            RateSegment(
                id=uuid.uuid4(),
                tenant_id=org.tenant_id,
                placement_id=pid,
                effective_from=date(2026, 9, 1),
                rate_unit="hour",
                currency="INR",
                vendor_rate=Decimal(rate),
                pay_rate=Decimal("900") if not vendor_id else None,
                bill_rate=Decimal("1800"),
            )
        )
    return pid


def _item(pid: uuid.UUID, day: int, qty: str, **attrs: Any) -> postings.SourceItem:
    return postings.SourceItem(
        source_kind=SOURCE,
        source_id=uuid.uuid4(),
        placement_id=pid,
        item_date=date(2026, 9, day),
        quantity=Decimal(qty),
        amount=None,
        currency="INR",
        attributes={"category": "regular", **attrs},
    )


async def _world(app: FastAPI, org: DOrg) -> tuple[dict[str, Any], uuid.UUID, uuid.UUID, uuid.UUID]:
    le = await provision(org.tenant)
    v = await active_vendor(org, "TalentBridge")
    vp = await _placement(org, le, uuid.UUID(v["id"]))
    wp = await _placement(org, le, None)
    return v, le, vp, wp


async def _build(org: DOrg, vendor_id: str, le: uuid.UUID, end: str = "2026-09-30") -> Any:
    return await org.admin.client.post(
        "/api/v1/vendor-statements/build",
        json={
            "vendor_ids": [vendor_id],
            "legal_entity_id": str(le),
            "period_start": "2026-09-01",
            "period_end": end,
        },
        headers=h(org.admin),
    )


async def test_build_finalize_and_never_restated(app: FastAPI, dorg: DOrg) -> None:
    v, le, vp, wp = await _world(app, dorg)
    ITEMS.extend(
        [_item(vp, 2, "8"), _item(vp, 3, "7.5"), _item(wp, 2, "8")]
    )  # worker-payee item never stated
    resp = await _build(dorg, v["id"], le)
    assert resp.status_code == 201, resp.text
    [st_] = resp.json()["statements"]
    assert st_["status"] == "draft" and st_["total_amount"] == "20150.00" and st_["line_count"] == 2
    async with tenant_session(dorg.tenant_id) as db:  # a draft creates no postings
        assert (await db.execute(select(PayablePosting))).scalars().all() == []
    resp = await dorg.admin.client.post(
        f"/api/v1/vendor-statements/{st_['id']}/finalize", headers=h(dorg.admin)
    )
    assert resp.status_code == 200, resp.text
    fin = resp.json()
    assert fin["status"] == "finalized" and fin["content_hash"] and len(fin["lines"]) == 2
    async with tenant_session(dorg.tenant_id) as db:
        posted = (await db.execute(select(PayablePosting))).scalars().all()
        assert len(posted) == 2 and all(
            p.payee_kind == "vendor" and p.target_kind == "vendor_statement_line" for p in posted
        )
        # Immutable once finalized.
        with pytest.raises(DBAPIError):
            async with db.begin_nested():
                await db.execute(
                    text("UPDATE vendor_statement_lines SET amount = 1 WHERE statement_id = :s"),
                    {"s": fin["id"]},
                )
    # The next period never repeats those items.
    again = await _build(dorg, v["id"], le, end="2026-10-31")
    assert again.json()["statements"] == [] and again.json()["skipped"][0]["reason"] == "nothing_to_state"
    # The vendor downloads its finalized statement; drafts are never visible in the portal.
    ext = await vendor_user(app, dorg, v["id"])
    try:
        rows = (await ext.client.get(f"{PORTAL}/statements")).json()
        assert [r["id"] for r in rows] == [fin["id"]]
        dl = await ext.client.get(f"{PORTAL}/statements/{fin['id']}/download")
        assert dl.status_code == 200 and dl.content.startswith(b"%PDF")
        csv = await ext.client.get(f"{PORTAL}/statements/{fin['id']}/download", params={"format": "csv"})
        assert b"TOTAL" in csv.content
    finally:
        await ext.client.aclose()


async def test_stale_finalize_and_draft_delete(app: FastAPI, dorg: DOrg) -> None:
    v, le, vp, _ = await _world(app, dorg)
    ITEMS.append(_item(vp, 2, "8"))
    st_ = (await _build(dorg, v["id"], le)).json()["statements"][0]
    ITEMS.append(_item(vp, 4, "2"))  # approved after the build
    resp = await dorg.admin.client.post(
        f"/api/v1/vendor-statements/{st_['id']}/finalize", headers=h(dorg.admin)
    )
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "statement_stale"
    rebuilt = (await _build(dorg, v["id"], le)).json()["statements"][0]
    assert rebuilt["id"] == st_["id"] and rebuilt["line_count"] == 2
    assert (
        await dorg.admin.client.delete(f"/api/v1/vendor-statements/{st_['id']}", headers=h(dorg.admin))
    ).status_code == 204


@settings(max_examples=40, deadline=None)
@given(
    st.lists(
        st.tuples(st.decimals(min_value=0, max_value=24, places=2), st.sampled_from(["1", "1.5", "2"])),
        min_size=1,
        max_size=15,
    ),
    st.decimals(min_value=1, max_value=99999, places=4),
)
def test_total_is_sum_of_per_line_rounding(lines: list[tuple[Decimal, str]], rate: Decimal) -> None:
    from app.services.payroll.vendor_statements import _price

    pid = uuid.uuid4()
    total = Decimal(0)
    expected = Decimal(0)
    for qty, mult in lines:
        item = postings.SourceItem(
            SOURCE, uuid.uuid4(), pid, date(2026, 9, 1), qty, None, "INR", {"category": "x"}
        )
        kind, amount, _ = _price(item, rate, Decimal(mult), "INR")
        assert kind == "time"
        total += amount
        expected += line_taxable(qty, rate, Decimal(mult), "INR")
    assert total == expected
    assert total == total.quantize(Decimal("0.01"))
