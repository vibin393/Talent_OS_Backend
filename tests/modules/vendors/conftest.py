"""Shared fixtures / helpers for the Epic 17 (vendors & vendor portal) tests."""

import dataclasses
import uuid
from datetime import date
from typing import Any

import pytest
from fastapi import FastAPI

from app.core.database import tenant_session
from app.models.external_auth import VendorUser
from tests.epic3 import _isolated_files, _no_real_ai, fake_ai, make_pdf  # noqa: F401
from tests.modules.demands.helpers import DOrg, dorg, h, open_demand, other_dorg  # noqa: F401
from tests.p2 import External, sign_in_external

VENDORS = "/api/v1/vendors"
PORTAL = "/api/v1/vendor-portal"
ALL_PORTAL_PERMS = (
    "releases:view",
    "submissions:create",
    "placements:view",
    "documents:manage",
    "statements:view",
    "users:manage",
)


def pdf(text: str = "Agreement") -> bytes:
    return make_pdf(text)


async def create_vendor(org: DOrg, name: str | None = None, **body: Any) -> dict[str, Any]:
    resp = await org.admin.client.post(
        VENDORS,
        json={"legal_name": name or f"Vendor {uuid.uuid4().hex[:6]}", "tier": "gold", **body},
        headers=h(org.admin),
    )
    assert resp.status_code == 201, resp.text
    return dict(resp.json())


async def upload_doc(
    org: DOrg, vendor_id: str, category: str, expires_on: date | None = None
) -> dict[str, Any]:
    data = {"category": category}
    if expires_on is not None:
        data["expires_on"] = expires_on.isoformat()
    resp = await org.admin.client.post(
        f"{VENDORS}/{vendor_id}/documents",
        data=data,
        files={"file": (f"{category}.pdf", pdf(category), "application/pdf")},
        headers=h(org.admin),
    )
    assert resp.status_code == 201, resp.text
    return dict(resp.json())


async def transition(org: DOrg, vendor_id: str, to: str, reason: str | None = None) -> Any:
    return await org.admin.client.post(
        f"{VENDORS}/{vendor_id}/transition", json={"to": to, "reason": reason}, headers=h(org.admin)
    )


async def active_vendor(org: DOrg, name: str | None = None) -> dict[str, Any]:
    v = await create_vendor(org, name)
    await upload_doc(org, v["id"], "agreement")
    await upload_doc(org, v["id"], "registration")
    assert (await transition(org, v["id"], "onboarding")).status_code == 200
    resp = await transition(org, v["id"], "active")
    assert resp.status_code == 200, resp.text
    return dict(resp.json())


async def vendor_user(
    app: FastAPI, org: DOrg, vendor_id: str, *, permissions: tuple[str, ...] = ALL_PORTAL_PERMS
) -> External:
    vu_id = uuid.uuid4()
    async with tenant_session(org.tenant_id) as db:
        db.add(
            VendorUser(
                id=vu_id,
                tenant_id=org.tenant_id,
                vendor_id=uuid.UUID(vendor_id),
                email=f"v-{uuid.uuid4().hex[:8]}@vendor.example.com",
                display_name="Sunita Vendor",
                status="active",
                portal_permissions=list(permissions),
            )
        )
    return await sign_in_external(app, "vendor_user", vu_id, org.tenant_id, uuid.UUID(vendor_id))


async def release(org: DOrg, demand_id: str, vendor_ids: list[str], **body: Any) -> dict[str, Any]:
    payload = {"vendor_ids": vendor_ids, "max_vendor_rate": "1350", "submission_limit": 3, **body}
    resp = await org.admin.client.post(
        f"/api/v1/demands/{demand_id}/vendor-releases", json=payload, headers=h(org.admin)
    )
    assert resp.status_code == 201, resp.text
    return dict(resp.json())


async def submit(ext: External, release_id: str, **fields: Any) -> Any:
    data = {
        "full_name": "Ravi Kumar",
        "email": f"ravi-{uuid.uuid4().hex[:6]}@mail.example.com",
        "vendor_rate": "1300",
        "attestation": "true",
        "phone_e164": "",
        "current_location": "Chennai",
    }
    data.update({k: str(v) for k, v in fields.items()})
    files = {"resume": ("cv.pdf", make_pdf("Ravi Kumar resume Python"), "application/pdf")}
    return await ext.client.post(
        f"{PORTAL}/releases/{release_id}/submissions", data=data, files=files, headers=ext.h()
    )


@dataclasses.dataclass
class Setup:
    org: DOrg
    vendor: dict[str, Any]
    demand: dict[str, Any]
    release: dict[str, Any]
    user: External


@pytest.fixture
async def setup(app: FastAPI, dorg: DOrg) -> Any:  # noqa: F811
    vendor = await active_vendor(dorg, "TalentBridge")
    demand = await open_demand(dorg.admin, dorg.client_id)
    rel = await release(dorg, demand["id"], [vendor["id"]])
    item = rel["items"][0]
    assert item["status"] == "released", item
    ext = await vendor_user(app, dorg, vendor["id"])
    yield Setup(dorg, vendor, demand, item["release"], ext)
    await ext.client.aclose()
