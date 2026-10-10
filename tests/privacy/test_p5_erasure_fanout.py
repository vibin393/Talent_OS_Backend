# ruff: noqa: E501
"""Epic 79 story 79.5 (FR-410, D-11): the erasure downstream fan-out register (``dsr_downstream_items``).

Statutory connectors (HRMS / accounting / payroll) are recorded ``statutory_retained``; an Installation with no erasure
capability is ``listed_only`` / ``manual_required``. Enforcement of partner erasure is out of scope (D-11): the row is
the record, not a guarantee.
"""

import uuid

from sqlalchemy import select, text

from app.core.database import tenant_session
from app.models.ecosystem_governance import DsrDownstreamItem
from app.services.ecosystem_governance import erasure_fanout
from tests.factories import create_tenant_row


async def _seed_connector(tenant_id: uuid.UUID, connector_key: str) -> uuid.UUID:
    inst_id = uuid.uuid4()
    async with tenant_session(tenant_id) as db:
        await db.execute(
            text(
                "INSERT INTO app_installations (id, tenant_id, kind, connector_key, instance_no, display_name, "
                "manifest_version, manifest_sha256, status, granted_scopes, created_at, updated_at) VALUES "
                "(:id, :tid, 'connector', :ck, 1, :name, 1, :sha, 'active', CAST('{}' AS varchar[]), now(), now())"
            ),
            {"id": inst_id, "tid": tenant_id, "ck": connector_key, "name": f"{connector_key} connector", "sha": "0" * 64},
        )
        await db.commit()
    return inst_id


async def _expose(tenant_id: uuid.UUID, installation_id: uuid.UUID, subject_id: uuid.UUID) -> None:
    async with tenant_session(tenant_id) as db:
        await db.execute(
            text(
                "INSERT INTO subject_exposures (id, tenant_id, installation_id, subject_type, subject_id, channel, "
                "first_at, last_at) VALUES (:id, :tid, :inst, 'candidate', :sid, 'connector', now(), now())"
            ),
            {"id": uuid.uuid4(), "tid": tenant_id, "inst": installation_id, "sid": subject_id},
        )
        await db.commit()


async def test_fan_out_records_statutory_and_listed_only() -> None:
    tenant_id = await create_tenant_row("Erasure fan-out tenant")
    subject_id = uuid.uuid4()
    dsr_id = uuid.uuid4()
    hrms_inst = await _seed_connector(tenant_id, "hrms")
    # A connector with no erasure capability and not statutory -> listed_only (a real row: subject_exposures FKs it).
    plain_inst = await _seed_connector(tenant_id, "madeup_connector")
    await _expose(tenant_id, hrms_inst, subject_id)
    await _expose(tenant_id, plain_inst, subject_id)

    async with tenant_session(tenant_id) as db:
        created = await erasure_fanout.fan_out(
            db, tenant_id, dsr_id, subject_type="candidate", subject_id=subject_id
        )
        await db.commit()
        assert len(created) == 2

    async with tenant_session(tenant_id) as db:
        rows = (
            await db.execute(
                select(DsrDownstreamItem).where(DsrDownstreamItem.dsr_id == dsr_id)
            )
        ).scalars().all()
    by_inst = {r.installation_id: r for r in rows}
    assert by_inst[hrms_inst].status == "statutory_retained"
    assert by_inst[hrms_inst].reason is not None
    assert by_inst[plain_inst].channel == "listed_only"
    assert by_inst[plain_inst].status == "manual_required"


async def test_fan_out_is_idempotent_per_dsr() -> None:
    tenant_id = await create_tenant_row("Erasure fan-out idempotent tenant")
    subject_id = uuid.uuid4()
    dsr_id = uuid.uuid4()
    inst = await _seed_connector(tenant_id, "esign")
    await _expose(tenant_id, inst, subject_id)
    async with tenant_session(tenant_id) as db:
        await erasure_fanout.fan_out(db, tenant_id, dsr_id, subject_type="candidate", subject_id=subject_id)
        await db.commit()
    async with tenant_session(tenant_id) as db:
        again = await erasure_fanout.fan_out(db, tenant_id, dsr_id, subject_type="candidate", subject_id=subject_id)
        await db.commit()
        assert again == []  # no duplicate rows
