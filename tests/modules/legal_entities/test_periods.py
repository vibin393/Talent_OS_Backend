"""AD-22: accounting periods (Epic 13, story 13.3)."""

import uuid
from datetime import date

import pytest
from sqlalchemy import func, select

from app.core.actors import ActorRef
from app.core.context import RequestContext
from app.core.database import tenant_session
from app.core.errors import ConflictError, Forbidden
from app.core.events import subscribe, unsubscribe
from app.core.fiscal import today_for
from app.events.legal_entities import AccountingPeriodClosed, AccountingPeriodReopened
from app.models.legal_entities import AccountingPeriod
from app.services import legal_entities_service as svc
from app.services.legal_entities import periods
from tests.modules.identity.conftest import make_tenant
from tests.modules.legal_entities.conftest import provision


async def _entity() -> tuple[uuid.UUID, uuid.UUID]:
    tenant = await make_tenant()
    return tenant.id, await provision(tenant)


def _ctx(tenant_id: uuid.UUID) -> RequestContext:
    return RequestContext(tenant_id=tenant_id)


async def test_ensure_periods_is_idempotent() -> None:
    tenant_id, le = await _entity()
    async with tenant_session(tenant_id) as db:
        assert await svc.ensure_periods(db, le, date(2025, 11, 5), date(2026, 2, 1)) == 4
        assert await svc.ensure_periods(db, le, date(2025, 11, 5), date(2026, 2, 1)) == 0
        assert await svc.ensure_periods(db, le, date(2026, 2, 1), date(2025, 1, 1)) == 0
        count = (await db.execute(select(func.count()).where(AccountingPeriod.legal_entity_id == le, AccountingPeriod.year == 2025))).scalar_one()
        assert count == 2


async def test_assert_open_missing_and_closed() -> None:
    tenant_id, le = await _entity()
    async with tenant_session(tenant_id) as db:
        with pytest.raises(ConflictError) as missing:
            await svc.assert_open(db, le, date(2031, 1, 15))
        assert missing.value.code == "period_closed"
        assert missing.value.details == {"legal_entity_id": str(le), "period": "2031-01", "state": "missing"}
        today = today_for("Asia/Kolkata")
        await svc.assert_open(db, le, today)
        await svc.close(_ctx(tenant_id), db, le, today.year, today.month, actor=ActorRef("user", uuid.uuid4()))
        with pytest.raises(ConflictError) as closed:
            await svc.assert_open(db, le, today)
        assert closed.value.details["state"] == "closed"


@pytest.mark.parametrize("kind", ["ai", "workflow", "system", "api_key"])
async def test_close_and_reopen_are_human_only(kind: str) -> None:
    tenant_id, le = await _entity()
    today = today_for("Asia/Kolkata")
    async with tenant_session(tenant_id) as db:
        with pytest.raises(Forbidden) as exc:
            await svc.close(_ctx(tenant_id), db, le, today.year, today.month, actor=ActorRef(kind, None))  # type: ignore[arg-type]
        assert exc.value.code == "human_only_action"
        await svc.close(_ctx(tenant_id), db, le, today.year, today.month, actor=ActorRef("user", uuid.uuid4()))
        with pytest.raises(Forbidden):
            await svc.reopen(_ctx(tenant_id), db, le, today.year, today.month, actor=ActorRef(kind, None), reason="x")  # type: ignore[arg-type]


async def test_close_reopen_state_machine_and_events() -> None:
    tenant_id, le = await _entity()
    today = today_for("Asia/Kolkata")
    seen: list[object] = []
    subscribe(AccountingPeriodClosed, seen.append)
    subscribe(AccountingPeriodReopened, seen.append)
    user = uuid.uuid4()
    try:
        async with tenant_session(tenant_id) as db:
            closed = await svc.close(_ctx(tenant_id), db, le, today.year, today.month, actor=ActorRef("user", user))
            assert closed.status == "closed" and closed.closed_by == user
            with pytest.raises(ConflictError) as twice:
                await svc.close(_ctx(tenant_id), db, le, today.year, today.month, actor=ActorRef("user", user))
            assert twice.value.code == "period_state"
        async with tenant_session(tenant_id) as db:
            reopened = await svc.reopen(
                _ctx(tenant_id), db, le, today.year, today.month, actor=ActorRef("recorded_by_user", user), reason="late invoice"
            )
            assert reopened.status == "open" and reopened.reopened_count == 1
            with pytest.raises(ConflictError):
                await svc.reopen(_ctx(tenant_id), db, le, today.year, today.month, actor=ActorRef("user", user), reason="x")
    finally:
        unsubscribe(AccountingPeriodClosed, seen.append)
        unsubscribe(AccountingPeriodReopened, seen.append)
    assert [type(e).__name__ for e in seen] == ["AccountingPeriodClosed", "AccountingPeriodReopened"]
    assert seen[1].reason == "late invoice" and seen[1].actor_kind == "recorded_by_user"  # type: ignore[attr-defined]


async def test_close_check_registry_blocks_close() -> None:
    tenant_id, le = await _entity()
    today = today_for("Asia/Kolkata")
    blocking: dict[str, int] = {"count": 2}

    async def open_drafts(ctx: RequestContext, db: object, legal_entity_id: uuid.UUID, start: date, end: date) -> periods.CloseCheckResult:
        assert start.day == 1 and end.month == start.month
        return periods.CloseCheckResult("t13.drafts", "Draft invoices", True, blocking["count"], "/billing/drafts")

    periods.register_close_check(f"t13.drafts.{uuid.uuid4().hex[:6]}", open_drafts)  # type: ignore[arg-type]
    async with tenant_session(tenant_id) as db:
        results = await svc.checklist(_ctx(tenant_id), db, le, today.year, today.month)
        assert any(r.name == "t13.drafts" and r.count == 2 for r in results)
        with pytest.raises(ConflictError) as exc:
            await svc.close(_ctx(tenant_id), db, le, today.year, today.month, actor=ActorRef("user", uuid.uuid4()))
        assert exc.value.code == "period_close_blocked"
        blocking["count"] = 0
        assert (await svc.close(_ctx(tenant_id), db, le, today.year, today.month, actor=ActorRef("user", uuid.uuid4()))).status == "closed"
    blocking["count"] = 0  # keep later tests' closes unblocked


async def test_ensure_periods_beat_task() -> None:
    from app.tasks import legal_entities as tasks

    tenant_id, le = await _entity()
    async with tenant_session(tenant_id) as db:
        await db.execute(AccountingPeriod.__table__.delete().where(AccountingPeriod.legal_entity_id == le))  # type: ignore[attr-defined]
    assert tasks.ensure_periods(str(tenant_id)) == 3
    assert tasks.ensure_periods(str(tenant_id)) == 0
