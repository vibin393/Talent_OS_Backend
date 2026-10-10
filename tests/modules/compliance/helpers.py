"""Shared helpers for the Epic 21 compliance tests."""

import uuid
from datetime import date
from typing import Any

from sqlalchemy import select, text

from app.core.database import tenant_session
from app.core.identity_docs import verhoeff_check_digit
from app.models.compliance import Credential, PlacementComplianceItem
from tests.epic3 import make_pdf
from tests.modules.demands.helpers import DOrg, h
from tests.modules.offers.helpers import create_candidate
from tests.modules.placements.helpers import direct_placement

COMP = "/api/v1/compliance"
CREDS = "/api/v1/credentials"
CANDS = "/api/v1/candidates"
PLACEMENTS = "/api/v1/placements"
WORK = "/api/v1/portal/work/credentials"
PDF = make_pdf("credential scan")


def full_aadhaar(prefix: str = "23456789012") -> str:
    """Return a Verhoeff-valid 12-digit Aadhaar-like number."""
    return prefix + verhoeff_check_digit(prefix)


async def set_industry(org: DOrg, industry: str, client_id: uuid.UUID | None = None) -> None:
    async with tenant_session(org.tenant_id) as db:
        await db.execute(
            text("UPDATE clients SET industry = :i WHERE id = :c"),
            {"i": industry, "c": client_id or org.client_id},
        )


async def requirement(org: DOrg, items: list[dict[str, Any]], **body: Any) -> dict[str, Any]:
    payload = {"name": body.pop("name", "Banking clients"), "items": items, **body}
    resp = await org.admin.client.post(f"{COMP}/requirements", json=payload, headers=h(org.admin))
    assert resp.status_code == 201, resp.text
    return dict(resp.json())


async def record(
    org: DOrg, candidate_id: uuid.UUID, type_key: str, *, file: bool = True, client: Any = None, **form: str
) -> Any:
    c = client or org.admin.client
    return await c.post(
        f"{CANDS}/{candidate_id}/credentials",
        data={"type_key": type_key, **form},
        files={"file": ("scan.pdf", PDF, "application/pdf")} if file else None,
        headers=h(org.admin) if client is None else {"X-CSRF-Token": "csrf-token"},
    )


async def verify(org: DOrg, credential_id: str, decision: str = "valid") -> Any:
    return await org.admin.client.post(
        f"{CREDS}/{credential_id}/verify", json={"decision": decision}, headers=h(org.admin)
    )


async def placement(org: DOrg, candidate_id: uuid.UUID | None = None, **body: Any) -> dict[str, Any]:
    cand = candidate_id or await create_candidate(org.admin, f"Wren Worker {uuid.uuid4().hex[:4]}")
    return await direct_placement(org, cand, **body)


async def items(org: DOrg, placement_id: str | uuid.UUID) -> dict[str, PlacementComplianceItem]:
    async with tenant_session(org.tenant_id) as db:
        rows = (
            await db.execute(
                select(PlacementComplianceItem).where(
                    PlacementComplianceItem.placement_id == uuid.UUID(str(placement_id))
                )
            )
        ).scalars()
        return {r.ref: r for r in rows}


async def credential(org: DOrg, credential_id: str | uuid.UUID) -> Credential:
    async with tenant_session(org.tenant_id) as db:
        c = await db.get(Credential, uuid.UUID(str(credential_id)))
        assert c is not None
        return c


async def set_expiry(org: DOrg, credential_id: str | uuid.UUID, expires_on: date) -> None:
    async with tenant_session(org.tenant_id) as db:
        await db.execute(
            text("UPDATE credentials SET expires_on = :d WHERE id = :i"),
            {"d": expires_on, "i": uuid.UUID(str(credential_id))},
        )
