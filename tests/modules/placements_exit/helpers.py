"""Shared helpers for the Epic 31 extension / conversion / offboarding tests."""

import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import date, timedelta
from typing import Any

import pytest
from sqlalchemy import text

from app.core.database import tenant_session
from app.services.placements import registry
from app.services.placements.registry import VerifierResult
from tests.modules.bench.helpers import placement as bench_placement
from tests.modules.demands.helpers import DOrg, h

API = "/api/v1"
PL = f"{API}/placements"


async def active(
    org: DOrg,
    *,
    end: date | None = None,
    start: date | None = None,
    worker_type: str = "employee",
    **body: Any,
) -> dict[str, Any]:
    body.setdefault("account_manager_user_id", str(org.admin.user_id))
    return await bench_placement(
        org,
        start=start or date.today() - timedelta(days=90),
        end=end if end is not None else date.today() + timedelta(days=45),
        worker_type=worker_type,
        **body,
    )


async def sql(org: DOrg, stmt: str, **params: Any) -> list[dict[str, Any]]:
    async with tenant_session(org.tenant_id) as db:
        res = await db.execute(text(stmt), params)
        try:
            return [dict(r._mapping) for r in res]
        except Exception:
            return []


async def placement_row(org: DOrg, pid: str) -> dict[str, Any]:
    (row,) = await sql(org, "SELECT * FROM placements WHERE id = :i", i=uuid.UUID(pid))
    return row


async def post(org: DOrg, path: str, body: Any | None = None, **headers: str) -> Any:
    return await org.admin.client.post(f"{API}{path}", json=body or {}, headers={**h(org.admin), **headers})


@contextmanager
def fake_verifiers(monkeypatch: pytest.MonkeyPatch, state: dict[str, bool]) -> Iterator[None]:
    """Replace the verifier registry with fakes whose ``done`` comes from ``state`` (mutable)."""

    def make(kind: str) -> Any:
        async def fn(session: Any, placement_id: uuid.UUID, lwd: date) -> VerifierResult:
            return VerifierResult(done=state.get(kind, False), details={"fake": kind})

        return fn

    fakes: Mapping[str, Any] = {k: make(k) for k in state}
    monkeypatch.setattr(registry, "offboarding_verifiers", lambda: fakes)
    yield


SYSTEM_KINDS = (
    "final_timesheet_approved",
    "final_expense_settlement",
    "final_payroll_inclusion",
    "final_invoice",
)


async def case_detail(org: DOrg, case_id: str) -> dict[str, Any]:
    resp = await org.admin.client.get(f"{API}/offboarding-cases/{case_id}")
    assert resp.status_code == 200, resp.text
    return dict(resp.json())


async def complete_manual(org: DOrg, case: dict[str, Any]) -> dict[str, Any]:
    """Complete every pending mandatory manual item (no registered verifier)."""
    for item in case["items"]:
        if item["mandatory"] and item["status"] == "pending" and item["kind"] not in SYSTEM_KINDS:
            resp = await post(org, f"/offboarding-items/{item['id']}/complete", {"note": "done"})
            assert resp.status_code == 200, resp.text
            case = resp.json()
    return case
