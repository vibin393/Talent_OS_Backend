"""Worker ("My Work") dependencies (Epic 13, AD-27): a Worker is a Candidate with a Placement -- no worker
principal or table. ``require_worker`` reuses ``require_candidate``; ``get_worker_db`` runs with
``OrgScope.of("worker", candidate_id)`` (never internal)."""

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import OrgScope, tenant_session
from app.dependencies.candidate import CandidatePrincipal, require_candidate

require_worker = require_candidate
WorkerDep = Annotated[CandidatePrincipal, Depends(require_candidate)]


async def get_worker_db(principal: WorkerDep) -> AsyncIterator[AsyncSession]:
    async with tenant_session(principal.tenant_id, org=OrgScope.of("worker", principal.candidate_id)) as session:
        yield session


WorkerDBSessionDep = Annotated[AsyncSession, Depends(get_worker_db)]
