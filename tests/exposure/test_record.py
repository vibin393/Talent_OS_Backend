"""Epic 66 (D-11, D-25(d)): ``exposure_service.record`` buffers, ``flush`` upserts ``subject_exposures`` (first_at /
last_at), and the erasure fan-out readers return the recipients."""

import uuid

import pytest
from sqlalchemy import select

from app.core.context import RequestContext
from app.core.database import tenant_session
from app.models.ecosystem import SubjectExposure
from app.models.oauth import AppInstallation
from app.services import exposure_service
from tests.factories import create_tenant_row


async def _installation(tenant: uuid.UUID) -> uuid.UUID:
    async with tenant_session(tenant) as db:
        inst = AppInstallation(
            tenant_id=tenant, kind="connector", connector_key="fake_hrms", display_name="Fake HRMS",
            manifest_version=1, manifest_sha256="0" * 64, status="active",
        )  # fmt: skip
        db.add(inst)
        await db.flush()
        return inst.id


async def test_record_and_flush_upsert() -> None:
    tenant = await create_tenant_row("Exposure Tenant")
    inst = await _installation(tenant)
    ctx = RequestContext(tenant_id=tenant)
    cand, other = uuid.uuid4(), uuid.uuid4()
    await exposure_service.flush()  # drain anything left by other tests
    await exposure_service.record(ctx, installation_id=inst, subject_type="candidate", subject_id=cand, channel="api")
    await exposure_service.record(ctx, installation_id=inst, subject_type="candidate", subject_id=cand, channel="api")
    await exposure_service.record(ctx, installation_id=inst, subject_type="candidate", subject_id=cand, channel="mcp")
    assert await exposure_service.flush() == 2
    async with tenant_session(tenant) as db:
        rows = list((await db.execute(select(SubjectExposure).order_by(SubjectExposure.channel))).scalars())
        assert [(r.channel, r.subject_id, r.installation_id) for r in rows] == [("api", cand, inst), ("mcp", cand, inst)]
        first_api = rows[0].first_at
        assert rows[0].first_at <= rows[0].last_at
    # a later exposure moves last_at only
    await exposure_service.record(ctx, installation_id=inst, subject_type="candidate", subject_id=cand, channel="api")
    assert await exposure_service.flush() == 1
    async with tenant_session(tenant) as db:
        api = (await db.execute(select(SubjectExposure).where(SubjectExposure.channel == "api"))).scalar_one()
        assert api.first_at == first_api and api.last_at > first_api
        assert await exposure_service.installations_exposed(db, "candidate", cand) == [inst]
        assert await exposure_service.installations_exposed(db, "candidate", other) == []
        assert await exposure_service.api_keys_exposed(db, "candidate", cand) == []


async def test_record_validates() -> None:
    ctx = RequestContext(tenant_id=uuid.uuid4())
    sid = uuid.uuid4()
    with pytest.raises(ValueError, match="exactly one"):
        await exposure_service.record(ctx, subject_type="candidate", subject_id=sid, channel="api")
    with pytest.raises(ValueError, match="exactly one"):
        await exposure_service.record(
            ctx, installation_id=sid, api_key_id=sid, subject_type="candidate", subject_id=sid, channel="api"
        )
    with pytest.raises(ValueError, match="channel"):
        await exposure_service.record(ctx, installation_id=sid, subject_type="candidate", subject_id=sid, channel="sms")  # type: ignore[arg-type]
