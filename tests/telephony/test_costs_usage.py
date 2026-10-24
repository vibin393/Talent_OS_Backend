"""Regression: telephony usage grouped by day must not raise a Postgres GroupingError.

The STT day expression (``date(timezone(tz, created_at))``) must be built once and reused in
both SELECT and GROUP BY. Inlining it twice made SQLAlchemy emit two bind params for ``tz``,
which Postgres treated as different expressions -> ``stt_jobs.created_at must appear in the
GROUP BY clause``, crashing ``GET /telephony/usage?group_by=day`` (and ``/telephony/call-options``)
with a 500. See app/services/telephony/costs.py::usage.
"""

from datetime import date

from app.core.database import tenant_session
from app.services.telephony import costs
from tests.telephony.conftest import TOrg


async def test_usage_day_grouping_does_not_raise(torg: TOrg) -> None:
    async with tenant_session(torg.tenant_id) as db:
        rows = await costs.usage(db, date(2026, 1, 1), date(2026, 12, 31), "day")
    assert isinstance(rows, list)


async def test_usage_user_and_legal_entity_grouping(torg: TOrg) -> None:
    async with tenant_session(torg.tenant_id) as db:
        assert isinstance(await costs.usage(db, date(2026, 1, 1), date(2026, 12, 31), "user"), list)
        assert isinstance(await costs.usage(db, date(2026, 1, 1), date(2026, 12, 31), "legal_entity"), list)
