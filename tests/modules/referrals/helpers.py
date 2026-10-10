# ruff: noqa: E501
"""Shared helpers for the Epic 42 referral tests."""

import re
import uuid
from typing import Any

from fastapi import FastAPI
from sqlalchemy import text

from app.core.database import tenant_session
from tests.epic3 import make_pdf
from tests.epic12 import sent
from tests.modules.demands.helpers import DOrg, h, open_demand
from tests.modules.identity.conftest import Member, join

API = "/api/v1"
ME = f"{API}/me/referrals"
WORK = f"{API}/portal/work/referrals"
PUBLIC = f"{API}/public/links"
PDF = make_pdf("Priya Sharma resume Python data engineer")
TOKEN_RE = re.compile(r"/l/([A-Za-z0-9_\-]+)")


def program_body(**kw: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "name": "Engineering referrals",
        "valid_from": "2026-01-01",
        "eligible_referrers": "both",
        "eligible_demands": {"all": True},
        "reward_rules": [{"match": {"seniority_level": "senior"}, "amount": "25000.00", "currency": "INR"}],
        "default_amount": "10000.00",
        "currency": "INR",
        "joining_condition_days": 90,
        "exclusions": {"assigned_recruiters": True, "demand_owner": True, "excluded_role_keys": []},
        "priority": 10,
    }
    body.update(kw)
    return body


async def create_program(org: DOrg, **kw: Any) -> dict[str, Any]:
    r = await org.admin.client.post(f"{API}/referral-programs", json=program_body(**kw), headers=h(org.admin))
    assert r.status_code == 201, r.text
    return dict(r.json())


async def senior_demand(org: DOrg, **kw: Any) -> dict[str, Any]:
    return await open_demand(org.admin, org.client_id, min_experience_years="8", **kw)


async def employee(app: FastAPI, org: DOrg, name: str = "Esha Employee", role: str = "viewer") -> Member:
    return await join(app, org.tenant, role, name=name)


def refer_form(demand_id: Any = None, email: str | None = None, name: str = "Priya Sharma", **kw: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "referee_name": name,
        "referee_email": email or f"priya-{uuid.uuid4().hex[:6]}@friends.example.com",
        **kw,
    }
    if demand_id is not None:
        data["demand_id"] = str(demand_id)
    return data


async def refer(client: Any, demand_id: Any = None, *, url: str = ME, headers: Any = None, **kw: Any) -> Any:
    data = refer_form(demand_id, **kw)
    return await client.post(
        url,
        data=data,
        files={"resume": ("resume.pdf", PDF, "application/pdf")},
        headers=headers if headers is not None else {"X-CSRF-Token": "csrf-token"},
    )


async def refer_ok(member: Member, demand_id: Any = None, **kw: Any) -> dict[str, Any]:
    r = await refer(member.client, demand_id, headers=member.h(), **kw)
    assert r.status_code == 201, r.text
    return dict(r.json())


def referee_token(email: str) -> str:
    msgs = sent(kind="referral_notice", to=email)
    assert msgs, f"no referral notice sent to {email}"
    m = TOKEN_RE.search(msgs[-1].text)
    assert m is not None
    return m.group(1)


async def accept(client: Any, token: str) -> Any:
    view = await client.get(f"{PUBLIC}/{token}/referral")
    assert view.status_code == 200, view.text
    return await client.post(
        f"{PUBLIC}/{token}/referral/accept", json={"notice_id": view.json()["notice"]["notice_id"], "agreed": True}
    )


async def rows(org: DOrg, stmt: str, **params: Any) -> list[Any]:
    async with tenant_session(org.tenant_id) as db:
        return list((await db.execute(text(stmt), params)).all())


async def scalar(org: DOrg, stmt: str, **params: Any) -> Any:
    async with tenant_session(org.tenant_id) as db:
        return (await db.execute(text(stmt), params)).scalar()


async def execute(org: DOrg, stmt: str, **params: Any) -> None:
    async with tenant_session(org.tenant_id) as db:
        await db.execute(text(stmt), params)
