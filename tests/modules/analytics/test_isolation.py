"""Epic 48 (A4, NFR-39): one tenant per task; tenant prefixes; tenant B never in tenant A's sets."""

import contextlib
import inspect
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import pytest

import app.tasks.analytics as tasks
from app.core.database import tenant_session
from app.services.analytics import features
from app.services.analytics.features import common
from tests.modules.demands.helpers import DOrg, open_demand
from tests.modules.placements.helpers import direct_placement


def test_every_task_takes_exactly_one_tenant() -> None:
    found = 0
    for name, obj in vars(tasks).items():
        task_name = getattr(obj, "name", None)
        if not (isinstance(task_name, str) and task_name.startswith("analytics.")):
            continue
        found += 1
        params = list(inspect.signature(obj.run).parameters)
        if task_name in tasks.FAN_OUT_TASKS:
            assert params == [], name
        else:
            assert params[0] == "tenant_id", name
            assert sum(1 for p in params if "tenant" in p) == 1, name
    assert found >= 5


async def test_task_opens_a_single_tenant_session(dorg: DOrg, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []
    real = tasks.tenant_session

    @contextlib.asynccontextmanager
    async def spy(tenant_id: Any, **kw: Any) -> AsyncIterator[Any]:
        seen.append(str(tenant_id))
        assert len(set(seen)) == 1, f"a second tenant was opened: {seen}"
        async with real(tenant_id, **kw) as db:
            yield db

    monkeypatch.setattr(tasks, "tenant_session", spy)
    out = await tasks.train_tenant_job(dorg.tenant_id)
    assert set(out["trained"]) == {"time_to_fill", "offer_drop", "attrition"}
    assert seen == [str(dorg.tenant_id)]
    await tasks.score_subject_job(dorg.tenant_id, "placement", uuid.uuid4())
    assert set(seen) == {str(dorg.tenant_id)}


def test_training_window() -> None:
    assert tasks.in_window(datetime(2026, 1, 1, 20, 0, tzinfo=UTC))  # 01:30 IST
    assert not tasks.in_window(datetime(2026, 1, 1, 0, 0, tzinfo=UTC))  # 05:30 IST -> deferred


async def test_other_tenant_rows_never_appear(dorg: DOrg, other_dorg: DOrg) -> None:
    mine = await open_demand(dorg.admin, dorg.client_id)
    theirs = await open_demand(other_dorg.admin, other_dorg.client_id)
    my_p = await direct_placement(dorg)
    their_p = await direct_placement(other_dorg)
    async with tenant_session(dorg.tenant_id) as db:
        rows = await features.scoring_rows(db, "time_to_fill", datetime.now(UTC))
        ids = {r.subject_id for r in rows}
        assert uuid.UUID(mine["id"]) in ids and uuid.UUID(theirs["id"]) not in ids
        placements = {p.id for p in await common.load_placements(db)}
        assert uuid.UUID(my_p["id"]) in placements and uuid.UUID(their_p["id"]) not in placements
        for kind in ("time_to_fill", "offer_drop", "attrition"):
            training = await features.training_rows(db, kind, datetime.now(UTC), 36)
            assert not {r.subject_id for r in training} & {uuid.UUID(theirs["id"]), uuid.UUID(their_p["id"])}
