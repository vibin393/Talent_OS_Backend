"""Candidate principal dependencies (Epic 9 story 9.3, AD-10).

* ``require_candidate`` (alias ``current_candidate``, the name Epic 8's portal offer routes use) authenticates
  **only** the ``tos_candidate`` cookie: session live (idle ``CANDIDATE_SESSION_IDLE_MINUTES``, absolute
  ``CANDIDATE_SESSION_ABSOLUTE_HOURS``), tenant active, candidate not merged / erased, CSRF on mutations. A
  user's ``tos_session`` cookie is never read here (401), and the user dependency never reads
  ``tos_candidate`` -- principals do not cross.
* ``get_candidate_db`` / ``CandidateDBDep``: an RLS session scoped to the candidate's tenant and, since Epic 13
  (AD-26/AD-27), to the worker organization ``OrgScope.of("worker", candidate_id)``: external principals are
  never internal, so org-isolated tables show only the candidate's own rows (fail-closed).

The resolved principal is also put on the request context as ``core.permissions.Principal(type="candidate")``
so audit rows and pipeline transitions are attributed to the candidate.
"""

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import OrgScope, tenant_session
from app.services.portal.common import CandidatePrincipal

__all__ = [
    "CandidateDBDep",
    "CandidateDep",
    "CandidatePrincipal",
    "current_candidate",
    "get_candidate_db",
    "require_candidate",
]


async def require_candidate(request: Request) -> CandidatePrincipal:
    from app.services.portal import candidate_auth

    return await candidate_auth.authenticate_candidate(request)


# Epic 8 (``app/router/portal_offers.py``) depends on this name.
current_candidate = require_candidate

CandidateDep = Annotated[CandidatePrincipal, Depends(require_candidate)]


async def get_candidate_db(principal: CandidateDep) -> AsyncIterator[AsyncSession]:
    async with tenant_session(principal.tenant_id, org=OrgScope.of("worker", principal.candidate_id)) as session:
        yield session


CandidateDBDep = Annotated[AsyncSession, Depends(get_candidate_db)]
