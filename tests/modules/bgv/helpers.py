"""Shared helpers for the Epic 20 BGV tests."""

import re
import uuid
from typing import Any

from fastapi import FastAPI

from app.core.context import RequestContext
from app.core.database import tenant_session
from app.core.identity_docs import verhoeff_check_digit
from app.core.mailer import OutboxMailer, get_mailer
from app.core.security import CSRF_HEADER
from app.services.bgv import cases
from tests.modules.demands.helpers import DOrg, h
from tests.modules.placements.helpers import direct_placement

API = "/api/v1"
PORTAL = "/api/v1/portal/work"
CSRF = {CSRF_HEADER: "csrf-token"}
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
PDF = b"%PDF-1.4\n%test\n" + b"0" * 64


def full_aadhaar() -> str:
    base = "23456789012"
    return base + verhoeff_check_digit(base)


def mails(tenant_id: uuid.UUID, kind: str) -> list[Any]:
    mailer = get_mailer()
    assert isinstance(mailer, OutboxMailer)
    return [m for m in mailer.sent if m.tenant_id == tenant_id and m.kind == kind]


def link_token(message: Any) -> str:
    match = re.search(r"/l/([A-Za-z0-9_\-]+)", message.text)
    assert match, message.text
    return match.group(1)


async def make_package(org: DOrg, checks: list[dict[str, Any]] | None = None, **extra: Any) -> dict[str, Any]:
    body = {
        "name": f"Package {uuid.uuid4().hex[:6]}",
        "checks": checks
        or [
            {
                "type": "employment",
                "count": 1,
                "required_documents": ["employment_history", "relieving_letter"],
                "turnaround_days": 5,
            },
            {"type": "identity", "count": 1, "required_documents": ["aadhaar"], "turnaround_days": 2},
        ],
        **extra,
    }
    resp = await org.admin.client.post(f"{API}/bgv/packages", json=body, headers=h(org.admin))
    assert resp.status_code == 201, resp.text
    return dict(resp.json())


async def make_provider(org: DOrg, email: str | None = "verify@provider.example.com") -> dict[str, Any]:
    resp = await org.admin.client.post(
        f"{API}/bgv/providers",
        json={"name": "Veritas Checks", "contact_name": "Vera", "contact_email": email},
        headers=h(org.admin),
    )
    assert resp.status_code == 201, resp.text
    return dict(resp.json())


def sys_ctx(org: DOrg) -> RequestContext:
    return RequestContext(tenant_id=org.tenant_id, actor_kind="system")


async def evaluated(org: DOrg, placement: dict[str, Any], package_ids: list[str]) -> int:
    payload = {
        "subject_kind": "placement",
        "subject_id": placement["id"],
        "candidate_id": placement["candidate_id"],
        "placement_id": placement["id"],
        "bgv_package_ids": package_ids,
    }
    async with tenant_session(org.tenant_id) as db:
        return await cases.handle_requirements_evaluated(sys_ctx(org), db, payload)


async def case_for(org: DOrg, placement_id: str) -> dict[str, Any]:
    resp = await org.admin.client.get(f"{API}/bgv-cases", params={"placement_id": placement_id})
    assert resp.status_code == 200, resp.text
    items = resp.json()
    assert len(items) == 1, items
    detail = await org.admin.client.get(f"{API}/bgv-cases/{items[0]['id']}")
    assert detail.status_code == 200, detail.text
    return dict(detail.json())


async def placement_with_case(org: DOrg, **pkg: Any) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    package = await make_package(org, **pkg)
    placement = await direct_placement(org)
    assert await evaluated(org, placement, [package["id"]]) == 1
    return package, placement, await case_for(org, placement["id"])


async def consent_and_documents(app: FastAPI, org: DOrg, case: dict[str, Any], worker: Any) -> None:
    """Worker gives consent and submits every requested document."""
    cid = case["id"]
    view = await worker.get(f"{PORTAL}/bgv/cases/{cid}/consent")
    assert view.status_code == 200, view.text
    resp = await worker.post(
        f"{PORTAL}/bgv/cases/{cid}/consent",
        json={"agreed": True, "notice_id": view.json()["notice_id"]},
        headers=CSRF,
    )
    assert resp.status_code == 200, resp.text
    for doc in case["documents"]:
        key = doc["key"]
        data: dict[str, Any] = {}
        files = None
        if key == "aadhaar":
            data["aadhaar_last4"] = "1234"
            files = [("files", ("aadhaar-redacted.png", PNG, "image/png"))]
        elif doc["kind"] == "declaration":
            data["declaration"] = (
                '{"entries": [{"employer": "Infosys", "from_date": "2019-01-01", "to_date": "2023-06-30", '
                '"hr_contact_email": "hr@infosys.example.com"}]}'
            )
        else:
            files = [("files", (f"{key}.pdf", PDF, "application/pdf"))]
        r = await worker.post(
            f"{PORTAL}/bgv/cases/{cid}/documents/{key}", data=data, files=files, headers=CSRF
        )
        assert r.status_code == 200, r.text
    sub = await worker.post(f"{PORTAL}/bgv/cases/{cid}/submit", headers=CSRF)
    assert sub.status_code == 200, sub.text
