"""Epic 13: the business-hours calculator moved to ``app.core.business_hours``; the Phase 1 path re-exports it."""

import uuid

from app.core import business_hours as core_bh
from app.core.database import tenant_session
from app.services.demands import business_hours as shim
from tests.factories import create_tenant_row


def test_shim_reexports_the_same_objects() -> None:
    for name in shim.__all__:
        assert getattr(shim, name) is getattr(core_bh, name)
    assert shim.Calendar is core_bh.Calendar
    assert callable(shim.add_business_hours)


async def test_tenant_calendar_defaults_to_no_working_days() -> None:
    tenant_id = await create_tenant_row()
    async with tenant_session(tenant_id) as db:
        cal, tz = await core_bh.tenant_calendar(db)
    assert tz == "Asia/Kolkata"
    assert cal.work_week == {}


async def test_demand_sla_calendar_delegates() -> None:
    from app.core.context import RequestContext
    from app.services.demands import sla

    tenant_id = await create_tenant_row()
    async with tenant_session(tenant_id) as db:
        cal, tz = await sla.calendar_for(RequestContext(tenant_id=tenant_id), db)
        cal2, tz2 = await core_bh.tenant_calendar(db)
    assert (cal, tz) == (cal2, tz2)
    assert uuid.UUID(str(tenant_id))
