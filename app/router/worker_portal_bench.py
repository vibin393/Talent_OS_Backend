"""Worker "My Work" bench (Epic 24 A5; ``/api/v1/portal/work/bench``).

Candidate principal with the worker org scope (AD-27, R17): org RLS confines every read to the Worker's own
bench entry. Responses (``schemas/bench_portal.py``) never carry cost fields.
"""

from typing import Any

from fastapi import APIRouter

from app.core.context import get_context
from app.core.field_visibility import portal_guard_route
from app.dependencies.worker import WorkerDBSessionDep, WorkerDep
from app.schemas.bench_portal import OptInIn, WorkerBenchState
from app.services.placements import bench

router = APIRouter(prefix="/portal/work", tags=["portal-work"], route_class=portal_guard_route("worker"))


@router.get("/bench", response_model=WorkerBenchState)
async def my_bench(principal: WorkerDep, db: WorkerDBSessionDep) -> Any:
    return await bench.portal_state(db, principal.tenant_id, principal.candidate_id)


@router.post("/bench/opt-in", response_model=WorkerBenchState)
async def opt_in(data: OptInIn, principal: WorkerDep, db: WorkerDBSessionDep) -> Any:
    return await bench.portal_opt_in(get_context(), db, principal.tenant_id, principal.candidate_id, data)


@router.post("/bench/opt-out", response_model=WorkerBenchState)
async def opt_out(principal: WorkerDep, db: WorkerDBSessionDep) -> Any:
    return await bench.portal_opt_out(db, principal.tenant_id, principal.candidate_id)
