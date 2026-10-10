# ruff: noqa: E501
"""Shared helpers for the Epic 23 onboarding tests."""

import uuid
from datetime import date, timedelta
from typing import Any

import httpx
from fastapi import FastAPI
from sqlalchemy import select

from app.core.context import RequestContext
from app.core.database import tenant_session
from app.core.security import CSRF_HEADER
from app.models.onboarding import OnboardingCase, OnboardingTask
from tests.modules.demands.helpers import DOrg, h
from tests.modules.offers.helpers import create_candidate
from tests.modules.placements.helpers import direct_placement
from tests.p2 import sign_in_candidate

TPL = "/api/v1/onboarding/templates"
CASES = "/api/v1/onboarding-cases"
TASKS = "/api/v1/onboarding-tasks"
PORTAL = "/api/v1/portal/work/onboarding"
CSRF = {CSRF_HEADER: "csrf-token"}
ACCOUNT = "123456789012"
PAN = "ABCPE1234F"
BANK = {
    "holder_name": "Asha Rao",
    "account_number": ACCOUNT,
    "account_number_confirm": ACCOUNT,
    "ifsc": "HDFC0001234",
}
NDA = "# Non-disclosure agreement\n\nThe worker keeps client information confidential.\n"
POLICY = "# IT policy\n\nUse company laptops for work only.\n"


async def doc_templates(org: DOrg) -> tuple[str, str]:
    nda = await _tpl(org, "NDA", "nda", NDA)
    pol = await _tpl(org, "IT policy", "policy", POLICY)
    return nda, pol


async def _tpl(org: DOrg, name: str, kind: str, body: str) -> str:
    m = org.admin
    resp = await m.client.post(
        "/api/v1/documents/templates", json={"name": name, "kind": kind, "body_md": body}, headers=h(m)
    )
    assert resp.status_code == 201, resp.text
    vid = resp.json()["versions"][0]["id"]
    pub = await m.client.post(f"/api/v1/documents/templates/versions/{vid}/publish", headers=h(m))
    assert pub.status_code == 200, pub.text
    return str(resp.json()["id"])


def full_tasks(nda: str, policy: str) -> list[dict[str, Any]]:
    return [
        {"key": "personal", "kind": "personal_details", "label": "Personal details", "reusable": True},
        {"key": "bank", "kind": "bank_details", "label": "Bank details", "reusable": True},
        {"key": "tax", "kind": "tax_details", "label": "PAN and declaration", "reusable": True, "config": {"declaration_mode": "form"}},
        {"key": "emergency", "kind": "emergency_contact", "label": "Emergency contact", "reusable": True},
        {"key": "nda", "kind": "esign", "label": "Sign the NDA", "reusable": True, "config": {"document_template_id": nda}},
        {"key": "it_policy", "kind": "policy_ack", "label": "IT policy", "reusable": True, "config": {"document_template_id": policy}},
        {
            "key": "laptop",
            "kind": "internal",
            "label": "Issue laptop",
            "owner_kind": "case_owner",
            "config": {"internal_type": "equipment"},
        },
    ]  # fmt: skip


async def create_template(org: DOrg, tasks: list[dict[str, Any]], **match: Any) -> dict[str, Any]:
    body = {
        "name": f"Template {uuid.uuid4().hex[:4]}",
        "tasks": tasks,
        **{k: str(v) for k, v in match.items()},
    }
    resp = await org.admin.client.post(TPL, json=body, headers=h(org.admin))
    assert resp.status_code == 201, resp.text
    return dict(resp.json())


async def set_owner(org: DOrg) -> None:
    resp = await org.admin.client.patch(
        "/api/v1/onboarding/settings",
        json={"default_hr_owner_id": str(org.admin.user_id)},
        headers=h(org.admin),
    )
    assert resp.status_code == 200, resp.text


async def placement(org: DOrg, candidate_id: uuid.UUID | None = None, days: int = 10) -> dict[str, Any]:
    cid = candidate_id or await create_candidate(org.admin, f"Asha Rao {uuid.uuid4().hex[:4]}")
    return await direct_placement(
        org, cid, planned_start_date=(date.today() + timedelta(days=days)).isoformat()
    )


async def case_of(org: DOrg, placement_id: Any) -> OnboardingCase | None:
    async with tenant_session(org.tenant_id) as db:
        return (
            await db.execute(
                select(OnboardingCase).where(OnboardingCase.placement_id == uuid.UUID(str(placement_id)))
            )
        ).scalar_one_or_none()


async def tasks_of(org: DOrg, case_id: Any) -> dict[str, OnboardingTask]:
    async with tenant_session(org.tenant_id) as db:
        rows = (
            await db.execute(select(OnboardingTask).where(OnboardingTask.case_id == uuid.UUID(str(case_id))))
        ).scalars()
        return {t.key: t for t in rows}


async def worker(app: FastAPI, org: DOrg, candidate_id: Any) -> httpx.AsyncClient:
    return await sign_in_candidate(app, uuid.UUID(str(candidate_id)), org.tenant_id)


async def event(org: DOrg, name: str, payload: dict[str, Any]) -> Any:
    from app.services import onboarding

    async with tenant_session(org.tenant_id) as db:
        return await onboarding.handle_event(
            RequestContext(tenant_id=org.tenant_id, actor_kind="system"), db, name, payload
        )
