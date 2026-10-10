# ruff: noqa: E501
"""Epic 52 (A1, FR-256): every Phase 3 consent purpose has an English default notice, seeded lazily for new tenants
and for tenants created before Phase 3."""

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.database import tenant_session
from app.core.hooks import autodiscover
from app.services.privacy import consent
from tests.factories import create_tenant_row

# purpose -> owner epic (the epic whose hooks.py must register the notice)
P3_NOTICE_OWNERS: dict[str, str] = {
    "tracking": "Epic 39",
    "assessment": "Epic 40",
    "referral": "Epic 42",
    "campus": "Epic 43",
    "immigration": "Epic 46",
    "demographic_self_id": "Epic 49",
}


@pytest.fixture(scope="module", autouse=True)
def _hooks() -> None:
    autodiscover("app.services")


@pytest.mark.parametrize("purpose", sorted(P3_NOTICE_OWNERS))
def test_default_notice_registered(purpose: str) -> None:
    owner = P3_NOTICE_OWNERS[purpose]
    assert purpose in consent.DEFAULT_NOTICES, f"{owner} must register_default_notice({purpose!r}, ...)"
    title, body = consent.DEFAULT_NOTICES[purpose]
    assert title.strip() and body.strip(), f"{owner}: empty notice text for {purpose!r}"


def test_self_id_notice_states_the_promises() -> None:
    body = consent.DEFAULT_NOTICES["demographic_self_id"][1].lower()
    for needle in ("optional", "gender", "age band", "disability", "never used in any decision", "aggregate"):
        assert needle in body, f"demographic_self_id notice must state: {needle!r} (Epic 49)"


async def _assert_active(tenant_id: uuid.UUID) -> None:
    async with tenant_session(tenant_id) as db:
        for purpose, owner in P3_NOTICE_OWNERS.items():
            notice = await consent.active_notice(db, tenant_id, purpose)
            assert notice is not None, f"no active en notice for {purpose!r} ({owner})"
            assert notice.locale == "en" and notice.active and notice.body_md.strip()


async def test_fresh_tenant_gets_p3_notices() -> None:
    await _assert_active(await create_tenant_row("P3 notices fresh"))


async def test_pre_phase3_tenant_gets_p3_notices_lazily(owner_db: AsyncEngine) -> None:
    tenant_id = await create_tenant_row("P3 notices legacy")
    async with tenant_session(tenant_id) as db:
        await consent.ensure_default_notices(db, tenant_id)
    # Simulate a tenant seeded before Phase 3: only P1/P2 notices exist.
    async with owner_db.begin() as conn:
        await conn.execute(
            text("DELETE FROM consent_notices WHERE tenant_id = :t AND purpose = ANY(:p)"),
            {"t": tenant_id, "p": list(P3_NOTICE_OWNERS)},
        )
        left = (
            await conn.execute(
                text("SELECT count(*) FROM consent_notices WHERE tenant_id = :t"), {"t": tenant_id}
            )
        ).scalar_one()
    assert left > 0
    await _assert_active(tenant_id)
