# ruff: noqa: E501
"""Seed + API helpers for the Epic 47 report builder tests.

``seed_placements(org)`` inserts (directly, RLS applies) one legal entity, two clients (Acme, Globex) and four
placements with active rate segments:

==== ====== ============ =========== ====== ==== ====
key  client recruiter    acct mgr    start  bill pay
==== ====== ============ =========== ====== ==== ====
p1   Acme   recruiter    account     03-31  100  80
p2   Acme   recruiter_b  account     04-01  200  150
p3   Globex recruiter    --          04-15  300  200
p4   Globex recruiter_b  --          05-10  50   40
==== ====== ============ =========== ====== ==== ====

Scope (``placement:read`` over recruiter OR account manager): admin 4 rows; manager (BU A users) 3 (p1-p3);
account (owner of AM rows) 2 (p1, p2); recruiter 2 (p1, p3); recruiter_b 2 (p2, p4).
"""

import dataclasses
import uuid
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import text

from app.core.database import tenant_session
from tests.reporting.helpers import API, ROrg, h
from tests.reporting.p2 import seed as S

BASE = f"{API}/reports/custom"
SRC = "placements.placement"


@dataclasses.dataclass
class Seeded:
    le: uuid.UUID
    acme: uuid.UUID
    globex: uuid.UUID
    acme_name: str
    globex_name: str
    p: dict[str, uuid.UUID]


async def _rate(db: Any, tenant_id: uuid.UUID, placement_id: uuid.UUID, start: date, bill: str, pay: str) -> None:
    await S._insert(
        db, "rate_segments",
        {"tenant_id": tenant_id, "placement_id": placement_id, "effective_from": start, "rate_unit": "hour",
         "currency": "INR", "bill_rate": Decimal(bill), "pay_rate": Decimal(pay), "status": "active"},
    )  # fmt: skip


async def seed_placements(org: ROrg) -> Seeded:
    t = org.tenant_id
    async with tenant_session(t) as db:
        le = await S.legal_entity(db, t)
        acme = await S.client(db, t, org.account.user_id, "Acme")
        globex = await S.client(db, t, org.manager.user_id, "Globex")
        names = dict((await db.execute(text("SELECT id, name FROM clients WHERE id IN (:a, :b)"), {"a": acme, "b": globex})).tuples().all())
        cand = await S.candidate(db, t, org.recruiter.user_id)
        rows = {
            "p1": (acme, org.recruiter.user_id, org.account.user_id, date(2026, 3, 31), "100", "80"),
            "p2": (acme, org.recruiter_b.user_id, org.account.user_id, date(2026, 4, 1), "200", "150"),
            "p3": (globex, org.recruiter.user_id, None, date(2026, 4, 15), "300", "200"),
            "p4": (globex, org.recruiter_b.user_id, None, date(2026, 5, 10), "50", "40"),
        }
        ids: dict[str, uuid.UUID] = {}
        for key, (client, rec, am, start, bill, pay) in rows.items():
            pid = await S.placement(db, t, le, client, cand, start=start, am=am)
            await db.execute(text("UPDATE placements SET recruiter_user_id = :r WHERE id = :id"), {"r": rec, "id": pid})
            await _rate(db, t, pid, start, bill, pay)
            ids[key] = pid
    return Seeded(le, acme, globex, names[acme], names[globex], ids)


def example_definition(**over: Any) -> dict[str, Any]:
    """47.2: Placements by Client x month -- count, sum(bill), sum(pay), bill/pay ratio."""
    return {
        "source": SRC,
        "group_by": [{"field": "client.name"}, {"field": "planned_start_date", "bucket": "month"}],
        "measures": [
            {"key": "placements", "agg": "count"},
            {"key": "bill", "agg": "sum", "field": "bill_rate"},
            {"key": "pay", "agg": "sum", "field": "pay_rate"},
            {"key": "bill_pay", "agg": "ratio", "of": ["bill", "pay"]},
        ],
        "sort": [{"key": "client.name"}],
        **over,
    }


async def create_report(member: Any, definition: dict[str, Any] | None = None, viz: dict[str, Any] | None = None, name: str = "Placements by client") -> dict[str, Any]:
    body = {"name": name, "definition": definition or example_definition(), "viz": viz or {"type": "bar", "x": "planned_start_date", "series": "client.name", "value": "bill"}}
    resp = await member.client.post(BASE, json=body, headers=h(member))
    assert resp.status_code == 201, resp.text
    return dict(resp.json())


async def share_tenant(admin: Any, report_id: str) -> None:
    resp = await admin.client.put(f"{BASE}/{report_id}/shares", json=[{"target_kind": "tenant"}], headers=h(admin))
    assert resp.status_code == 200, resp.text


async def grant(org: ROrg, role_key: str, **perms: str) -> None:
    """Widen a seeded system role of this test tenant (``report__export="tenant"``)."""
    import json

    extra = {k.replace("__", ":"): v for k, v in perms.items()}
    async with tenant_session(org.tenant_id) as db:
        await db.execute(
            text("UPDATE roles SET permissions = permissions || CAST(:p AS jsonb) WHERE key = :k"),
            {"p": json.dumps(extra), "k": role_key},
        )


async def revoke(org: ROrg, role_key: str, *keys: str) -> None:
    async with tenant_session(org.tenant_id) as db:
        for key in keys:
            await db.execute(text("UPDATE roles SET permissions = permissions - :p WHERE key = :k"), {"p": key, "k": role_key})
