# ruff: noqa: E501
"""Epic 71 (stories 71.1-71.3): HRMS manifests, mappings, eligibility, hire / change / exit push, the status pull,
sensitive-field gating, the Manual import file and the payroll push skip. Fake adapters only."""

import dataclasses
import uuid
from datetime import date
from typing import Any

import pytest
from sqlalchemy import select, update

from app.core.database import tenant_session
from app.core.errors import Forbidden
from app.events.placements import PlacementActivated, PlacementEnded, RateSegmentAdded
from app.models.connectors import ConnectorJob
from app.models.hrms_sync import HrmsSettings, HrmsSyncRecord
from app.models.integrations import ConnectorLink
from app.models.placements import Placement
from app.models.privacy import Consent
from app.models.tasks import Task
from app.schemas.hrms_sync import MappingIn
from app.services import hrms_sync_service as svc
from app.services.connectors.hrms.fake import FakeHrms
from app.services.hrms_sync.eligibility import eligible
from tests.hrms_sync.helpers import drain, fire, human, install, make_placement, system
from tests.modules.receivables.conftest import ROrg

BASE_MAP = [
    MappingIn(hrms_field="first_name", local_field="first_name"),
    MappingIn(hrms_field="last_name", local_field="last_name"),
    MappingIn(hrms_field="date_of_joining", local_field="date_of_joining", transform="date_iso"),
]


async def _setup(org: ROrg, key: str = "keka", mapping: list[MappingIn] | None = None) -> uuid.UUID:
    inst, _ = await install(org, key)
    async with tenant_session(org.tenant_id) as db:
        await svc.put_mappings(human(org), db, inst, org.entity_id, mapping if mapping is not None else BASE_MAP)
    return inst


async def _record(org: ROrg, placement_id: uuid.UUID) -> HrmsSyncRecord | None:
    async with tenant_session(org.tenant_id) as db:
        return (await db.execute(select(HrmsSyncRecord).where(HrmsSyncRecord.placement_id == placement_id))).scalar_one_or_none()


def test_eligibility_matrix() -> None:
    settings = HrmsSettings(push_worker_types=["employee"])
    p = Placement(payee_kind="worker", worker_type="employee")
    assert eligible(p, settings, None) == (True, None)
    assert eligible(Placement(payee_kind="worker", worker_type="contractor"), settings, None) == (False, "not_on_payroll")
    assert eligible(Placement(payee_kind="vendor", worker_type="vendor_contractor"), settings, HrmsSyncRecord(include_override=True)) == (False, "not_on_payroll")
    assert eligible(Placement(payee_kind="worker", worker_type=None), settings, None)[0] is False
    assert eligible(Placement(payee_kind="worker", worker_type="contractor"), settings, HrmsSyncRecord(include_override=True)) == (True, None)
    assert eligible(p, settings, HrmsSyncRecord(include_override=False))[0] is False
    assert eligible(Placement(payee_kind="worker", worker_type="contractor"), HrmsSettings(push_worker_types=["employee", "contractor"]), None)[0] is True


async def test_joined_pushes_hire_once_and_links(org: ROrg) -> None:
    inst = await _setup(org)
    p = await make_placement(org)
    await fire(org, PlacementActivated(tenant_id=org.tenant_id, placement_id=p.id, candidate_id=p.candidate_id, actual_start_date=date(2026, 10, 1)))
    await drain(org)
    rec = await _record(org, p.id)
    assert rec is not None and rec.status == "synced" and rec.employee_ref == "FK-1" and rec.last_push_kind == "hire"
    async with tenant_session(org.tenant_id) as db:
        link = (await db.execute(select(ConnectorLink).where(ConnectorLink.installation_id == inst, ConnectorLink.entity_type == "worker"))).scalar_one()
        assert (link.local_id, link.external_id, link.candidate_id) == (p.id, "FK-1", p.candidate_id)
        consent = (await db.execute(select(Consent).where(Consent.candidate_id == p.candidate_id, Consent.purpose == "hrms_transfer"))).scalar_one()
        assert consent.source == "placement"
        job = (await db.execute(select(ConnectorJob).where(ConnectorJob.op == "sync:push_hire"))).scalar_one()
        assert job.installation_id == inst and job.status == "succeeded"
    # replay: the same hire job (and a direct push) never creates a second employee
    await fire(org, PlacementActivated(tenant_id=org.tenant_id, placement_id=p.id, candidate_id=p.candidate_id, actual_start_date=date(2026, 10, 1)))
    await drain(org)
    async with tenant_session(org.tenant_id) as db:
        await svc.push_placement(system(org), db, inst, p.id, "hire")
    assert [c for c in FakeHrms.calls if c[0] == "push_hire"].__len__() == 1
    assert FakeHrms._seq == 1


async def test_unmapped_required_field_blocks_with_task(org: ROrg) -> None:
    inst = await _setup(org, mapping=BASE_MAP[:1])
    p = await make_placement(org, name="Ravi Kumar")
    async with tenant_session(org.tenant_id) as db:
        await svc.push_placement(system(org), db, inst, p.id, "hire")
    rec = await _record(org, p.id)
    assert rec is not None and rec.status == "blocked" and set(rec.blocked_fields) == {"last_name", "date_of_joining"}
    assert not [c for c in FakeHrms.calls if c[0] == "push_hire"]
    async with tenant_session(org.tenant_id) as db:
        task = (await db.execute(select(Task).where(Task.entity_type == "hrms_sync", Task.entity_id == rec.id))).scalar_one()
        assert task.title == "Map last_name, date_of_joining before pushing Ravi Kumar"


async def test_vendor_and_contractor_not_pushed(org: ROrg) -> None:
    inst = await _setup(org)
    contractor = await make_placement(org, worker_type="contractor")
    async with tenant_session(org.tenant_id) as db:
        out = await svc.push_placement(system(org), db, inst, contractor.id, "hire")
    assert out.skipped == 1 and await _record(org, contractor.id) is None
    async with tenant_session(org.tenant_id) as db:
        panel = await svc.placement_panel(human(org), db, contractor.id)
        assert panel.eligible is False and panel.reason == "not_on_payroll"
        await svc.include(human(org), db, contractor.id, True)
    await drain(org)
    rec = await _record(org, contractor.id)
    assert rec is not None and rec.include_override is True and rec.status == "synced"


async def test_change_pushes_changed_fields_only_and_exit(org: ROrg, monkeypatch: pytest.MonkeyPatch) -> None:
    inst = await _setup(org, mapping=[*BASE_MAP, MappingIn(hrms_field="department", local_field="designation", transform="lookup", lookup={"Data Engineer": "engineering", "Data Lead": "operations"})])
    p = await make_placement(org)
    async with tenant_session(org.tenant_id) as db:
        await svc.push_placement(system(org), db, inst, p.id, "hire")
    seen: list[Any] = []
    original = FakeHrms.push_change

    async def capture(self: FakeHrms, change: Any) -> Any:
        seen.append(change)
        return await original(self, change)

    monkeypatch.setattr(FakeHrms, "push_change", capture)
    async with tenant_session(org.tenant_id) as db:
        await db.execute(update(Placement).where(Placement.id == p.id).values(role_title="Data Lead", lock_version=Placement.lock_version + 1))
    await fire(org, RateSegmentAdded(tenant_id=org.tenant_id, placement_id=p.id, rate_segment_id=uuid.uuid4(), effective_from=date(2026, 11, 1), previous_segment_id=None))
    await drain(org)
    assert len(seen) == 1 and dict(seen[0].fields) == {"department": "operations"} and seen[0].employee_ref == "FK-1"
    # no change -> skipped silently
    async with tenant_session(org.tenant_id) as db:
        await svc.push_placement(system(org), db, inst, p.id, "change")
    assert len(seen) == 1
    async with tenant_session(org.tenant_id) as db:
        await db.execute(update(Placement).where(Placement.id == p.id).values(status="ended", end_date=date(2026, 12, 31)))
    await fire(org, PlacementEnded(tenant_id=org.tenant_id, placement_id=p.id, candidate_id=p.candidate_id, end_date=date(2026, 12, 31), reason=None, worker_type="employee", bench_eligible=False))
    await drain(org)
    rec = await _record(org, p.id)
    assert rec is not None and rec.last_push_kind == "exit" and rec.status == "synced"
    assert FakeHrms._employees["FK-1"]["exit_date"] == date(2026, 12, 31)


async def test_pull_flags_mismatch_without_state_change(org: ROrg) -> None:
    inst = await _setup(org)
    p = await make_placement(org, name="Neha Rao")
    async with tenant_session(org.tenant_id) as db:
        await svc.push_placement(system(org), db, inst, p.id, "hire")
    FakeHrms.script["exits"] = {"FK-1": "2026-11-30"}
    async with tenant_session(org.tenant_id) as db:
        await svc.pull_now(human(org), db, inst)
    await drain(org)
    rec = await _record(org, p.id)
    assert rec is not None and rec.hrms_status == "exited" and rec.hrms_exit_date == date(2026, 11, 30) and rec.employee_code == "FK-1"
    assert rec.mismatch == "status" and rec.mismatch_task_id is not None
    async with tenant_session(org.tenant_id) as db:
        task = await db.get(Task, rec.mismatch_task_id)
        assert task is not None and task.title == "HRMS shows Neha Rao exited on 30 Nov 2026; Talent OS shows Active"
        placement = await db.get(Placement, p.id)
        assert placement is not None and placement.status == "active" and placement.end_date is None


async def test_sensitive_mapping_needs_finance_and_never_leaks(org: ROrg) -> None:
    inst = await _setup(org, mapping=[*BASE_MAP, MappingIn(hrms_field="department", local_field="pan")])
    async with tenant_session(org.tenant_id) as db:
        page = await svc.get_mappings(human(org), db, inst, org.entity_id)
    row = next(f.mapping for f in page.fields if f.hrms_field == "department")
    assert row is not None and row.sensitive is False
    async with tenant_session(org.tenant_id) as db:
        with pytest.raises(Forbidden) as exc:
            await svc.enable_sensitive(human(org, "hrms:sync"), db, row.id, "Statutory payroll registration")
    assert exc.value.code == "sensitive_mapping_requires_finance"
    async with tenant_session(org.tenant_id) as db:
        with pytest.raises(Forbidden):
            await svc.enable_sensitive(human(org, "hrms:sync", "ledger:sync", principal_type="agent"), db, row.id, "Statutory payroll registration")
    # off by default: PAN never sent
    p = await make_placement(org)
    async with tenant_session(org.tenant_id) as db:
        await svc.push_placement(system(org), db, inst, p.id, "hire")
    async with tenant_session(org.tenant_id) as db:
        out = await svc.enable_sensitive(human(org, "hrms:sync", "ledger:sync"), db, row.id, "Statutory payroll registration")
    assert out.sensitive and out.purpose == "Statutory payroll registration"
    rec = await _record(org, p.id)
    assert rec is not None and "pan" not in str(dataclasses.asdict(_plain(rec))).lower()


def _plain(rec: HrmsSyncRecord) -> Any:
    @dataclasses.dataclass
    class R:
        message: str | None
        blocked: list[str]

    return R(rec.last_message, list(rec.blocked_fields))


async def test_manual_import_file(org: ROrg) -> None:
    inst = await _setup(org)
    p = await make_placement(org, name="Kiran Shah")
    async with tenant_session(org.tenant_id) as db:
        from app.services.hrms_sync.push import ensure_record

        placement = await db.get(Placement, p.id)
        assert placement is not None
        await ensure_record(db, inst, placement)
    async with tenant_session(org.tenant_id) as db:
        dl = await svc.build_import_file(human(org), db, inst, org.entity_id)
    text = dl.data.decode("utf-8")
    assert text.startswith("﻿") and dl.content_type == "text/csv"
    lines = text.lstrip("﻿").strip().splitlines()
    assert lines[0] == "first_name,last_name,date_of_joining" and lines[1] == "Kiran,Shah,2026-10-01"


async def test_payroll_queue_push_skips_installation_configs(org: ROrg) -> None:
    from types import SimpleNamespace

    from app.services.payroll import exports

    await install(org, "keka_payroll")
    v = SimpleNamespace(id=uuid.uuid4(), legal_entity_id=org.entity_id, kind="final", provider_status="not_sent")
    async with tenant_session(org.tenant_id) as db:
        await exports._queue_push(system(org), db, v)  # type: ignore[arg-type]
        jobs = (await db.execute(select(ConnectorJob).where(ConnectorJob.op == "push"))).scalars().all()
    assert v.provider_status == "not_sent" and not jobs


async def test_api_permissions_and_cross_tenant(org: ROrg, other: ROrg) -> None:
    inst = await _setup(org)
    p = await make_placement(org)
    r = await org.c.get(f"/api/v1/hrms/placements/{p.id}")
    assert r.status_code == 200 and r.json()["eligible"] is True
    assert (await other.c.get(f"/api/v1/hrms/placements/{p.id}")).status_code == 404
    assert (await other.c.get(f"/api/v1/hrms/installations/{inst}/entities/{org.entity_id}/mappings")).status_code == 404
    r = await org.c.get(f"/api/v1/hrms/installations/{inst}/entities/{org.entity_id}/mappings")
    assert r.status_code == 200 and r.json()["connector_key"] == "keka"
    r = await org.c.put("/api/v1/hrms/settings", json={"push_worker_types": ["employee", "contractor"]}, headers=org.h())
    assert r.status_code == 200 and r.json()["push_worker_types"] == ["contractor", "employee"]
    r = await org.c.get("/api/v1/hrms/records")
    assert r.status_code == 200
