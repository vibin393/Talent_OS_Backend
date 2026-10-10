# ruff: noqa: E501
"""Developer session dependencies (Epic 74, spec 0.1 / A4).

* ``tos_developer`` (httpOnly session cookie, ``tos_dev_<token>``) is **not** in ``core.security.SESSION_COOKIES``:
  ``require()`` and every tenant route ignore it (a developer cookie on ``/api/v1/candidates`` -> 401).
* Full sessions exist only after MFA, so ``require_developer`` is also the MFA gate (``require_developer_mfa``).
* The selected org comes from the ``{org_id}`` path parameter, else the ``X-Partner-Org`` header, validated against a
  live membership (the role from ``partner_org_members``); a non-member gets ``partner_org_id = None`` -> 403 / 404.
* CSRF on mutations: ``X-CSRF-Token`` == ``tos_csrf`` == HMAC of a session cookie the request presents (Story 22.9 rule;
  the developer cookie or the pending MFA cookie count as session cookies here).
"""

import hmac
import uuid
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy import select

from app.core.context import update_context
from app.core.database import platform_session
from app.core.developer import DeveloperContext
from app.core.errors import Unauthorized
from app.core.permissions import Principal
from app.core.security import (
    CSRF_BOUND_COOKIES,
    CSRF_COOKIE,
    CSRF_HEADER,
    SAFE_METHODS,
    csrf_failed,
    csrf_token_for,
)
from app.models.developer import DeveloperSandbox, PartnerOrgMember
from app.models.oauth import PartnerOrg
from app.services import developer_portal_service

DEV_COOKIE = "tos_developer"
DEV_MFA_COOKIE = "tos_dev_mfa"
ORG_HEADER = "X-Partner-Org"
_STATE = "developer_context"


def verify_developer_csrf(request: Request) -> None:
    if request.method.upper() in SAFE_METHODS:
        return
    cookie = request.cookies.get(CSRF_COOKIE) or ""
    header = request.headers.get(CSRF_HEADER) or ""
    if not cookie or not header or not hmac.compare_digest(cookie.encode(), header.encode()):
        raise csrf_failed()
    names = (DEV_COOKIE, DEV_MFA_COOKIE, *CSRF_BOUND_COOKIES)
    bound = [csrf_token_for(t) for name in names if (t := request.cookies.get(name))]
    if not any(hmac.compare_digest(cookie.encode(), b.encode()) for b in bound):
        raise csrf_failed()


def _org_param(request: Request) -> uuid.UUID | None:
    raw = request.path_params.get("org_id") or request.headers.get(ORG_HEADER)
    try:
        return uuid.UUID(str(raw)) if raw else None
    except ValueError:
        return None


async def require_developer(request: Request) -> DeveloperContext:
    cached = getattr(request.state, _STATE, None)
    if isinstance(cached, DeveloperContext):
        return cached
    token = request.cookies.get(DEV_COOKIE)
    if not token:
        raise Unauthorized()
    org_id = _org_param(request)
    async with platform_session() as db:
        found = await developer_portal_service.find_session(db, token, pending=False)
        if found is None:
            raise Unauthorized("Your session has ended. Sign in again.", code="session_expired")
        session, account = found
        if account.mfa_enabled_at is None or not session.mfa_passed:
            raise Unauthorized("Complete two-step verification", code="mfa_required")
        verify_developer_csrf(request)
        role: str | None = None
        verified = False
        sandbox_ids: frozenset[uuid.UUID] = frozenset()
        if org_id is not None:
            member = (
                await db.execute(
                    select(PartnerOrgMember.role, PartnerOrg.verification_status, PartnerOrg.status)
                    .join(PartnerOrg, PartnerOrg.id == PartnerOrgMember.partner_org_id)
                    .where(PartnerOrgMember.partner_org_id == org_id, PartnerOrgMember.account_id == account.id, PartnerOrgMember.removed_at.is_(None))
                )
            ).first()
            if member is None or member[2] != "active":
                org_id = None
            else:
                role, verified = member[0], member[1] == "verified"
                sandbox_ids = frozenset(
                    t for t in (await db.execute(select(DeveloperSandbox.tenant_id).where(DeveloperSandbox.partner_org_id == org_id, DeveloperSandbox.status != "deleted"))).scalars() if t
                )
        dctx = DeveloperContext(
            developer_id=account.id, partner_org_id=org_id, role=role, partner_verified=verified,  # type: ignore[arg-type]
            mfa_verified=True, session_id=session.id, sandbox_tenant_ids=sandbox_ids,
        )  # fmt: skip
    update_context(principal=Principal(type="platform", id=account.id))
    setattr(request.state, _STATE, dctx)
    return dctx


require_developer_mfa = require_developer
DeveloperDep = Annotated[DeveloperContext, Depends(require_developer)]


__all__ = ["DEV_COOKIE", "DEV_MFA_COOKIE", "ORG_HEADER", "DeveloperDep", "require_developer", "require_developer_mfa", "verify_developer_csrf"]
