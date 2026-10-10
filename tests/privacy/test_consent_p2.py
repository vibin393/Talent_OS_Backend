"""Epic 35 story 35.1 (FR-179): Phase 2 consent purposes, notices, ``require`` / ``record``, the ``engagement``
basis (auto-recorded on placement creation, not withdrawable while on assignment) and purpose isolation."""

import uuid
from pathlib import Path

import pytest
from sqlalchemy import select

from app.core.context import RequestContext
from app.core.database import tenant_session
from app.core.errors import ConflictError
from app.models.privacy import Consent, ConsentNotice
from app.services.privacy import consent
from tests.epic12 import Org3, create_candidate, h

P2_PURPOSES = ("bgv", "reference_check", "vendor_collection", "whatsapp", "engagement")


async def test_notices_seeded_for_new_purposes(org3: Org3) -> None:
    async with tenant_session(org3.tenant_id) as db:
        await consent.ensure_default_notices(db, org3.tenant_id)
        purposes = set(
            (await db.execute(select(ConsentNotice.purpose).where(ConsentNotice.active.is_(True)))).scalars()
        )
    assert set(P2_PURPOSES) <= purposes
    assert consent.PURPOSE_BASIS["engagement"] == "legitimate_use"
    assert all(consent.PURPOSE_BASIS[p] == "consent" for p in P2_PURPOSES if p != "engagement")


async def test_require_and_record(org3: Org3) -> None:
    cand = await create_candidate(org3.admin)
    cid = uuid.UUID(cand["id"])
    async with tenant_session(org3.tenant_id) as db:
        await consent.ensure_default_notices(db, org3.tenant_id)
        with pytest.raises(ConflictError) as exc:
            await consent.require(db, cid, "bgv")
        assert exc.value.code == "consent_required"
        assert exc.value.details["purpose"] == "bgv"
        row = await consent.record(
            db, cid, "bgv", "worker_portal", 1, RequestContext(tenant_id=org3.tenant_id)
        )
        assert row.notice_id is not None and row.source == "worker_portal"
        again = await consent.record(db, cid, "bgv", "worker_portal", 1, None)
        assert again.id == row.id  # idempotent
        assert (await consent.require(db, cid, "bgv")).id == row.id


async def test_engagement_recorded_and_not_withdrawable_while_active(org3: Org3) -> None:
    cand = await create_candidate(org3.admin)
    cid = uuid.UUID(cand["id"])
    async with tenant_session(org3.tenant_id) as db:
        await consent.record_engagement(db, cid)
        rows = list((await db.execute(select(Consent).where(Consent.candidate_id == cid))).scalars())
    assert [(r.purpose, r.source, r.status) for r in rows] == [("engagement", "placement", "granted")]

    async def active(_db: object, _cid: uuid.UUID) -> bool:
        return True

    orig = consent.has_active_engagement
    consent.has_active_engagement = active  # type: ignore[assignment]
    try:
        resp = await org3.admin.client.post(f"/api/v1/consents/{rows[0].id}/withdraw", headers=h(org3.admin))
        assert (resp.status_code, resp.json()["error"]["code"]) == (409, "engagement_active")
    finally:
        consent.has_active_engagement = orig  # type: ignore[assignment]
    # no active placement: withdrawable
    resp = await org3.admin.client.post(f"/api/v1/consents/{rows[0].id}/withdraw", headers=h(org3.admin))
    assert resp.status_code == 200, resp.text


async def test_has_active_engagement_reads_placements(org3: Org3) -> None:
    cand = await create_candidate(org3.admin)
    async with tenant_session(org3.tenant_id) as db:
        assert await consent.has_active_engagement(db, uuid.UUID(cand["id"])) is False


def test_engagement_paths_never_require_marketing_ai_or_whatsapp() -> None:
    """FR-179 isolation: placement, timesheet, payroll and invoice code never gates on these purposes."""
    root = Path(__file__).resolve().parents[2] / "app" / "services"
    offenders = []
    for pkg in ("placements", "time", "billing", "payroll", "receivables", "expenses"):
        for path in (root / pkg).rglob("*.py"):
            src = path.read_text(encoding="utf-8")
            for purpose in ("marketing", "ai_processing", "whatsapp"):
                for needle in (f'require(db, candidate_id, "{purpose}")', f'require_consent("{purpose}"'):
                    if needle in src:
                        offenders.append(f"{path.name}:{purpose}")
    assert offenders == []


async def test_placement_created_handler_is_subscribed() -> None:
    from app.core import events
    from app.events.placements import PlacementCreated
    from app.services.privacy import handlers

    handlers.install()
    assert handlers._placement_created in events._handlers[PlacementCreated]
