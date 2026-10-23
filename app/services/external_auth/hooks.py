"""Registrations of the ``external_auth`` domain (autodiscovered by ``core.hooks``; Epic 13).

* Notification recipient directories for ``client_user`` and ``vendor_user`` (email, name, organization).
* Approver resolvers ``client_user`` (``{"client_user_id"}``) and ``client_any`` (R5: ``{"client_id",
  "portal_permission"}`` -- any Client User of that client holding the permission decides).
"""

import uuid
from collections.abc import Mapping
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.models.external_auth import ClientUser, VendorUser
from app.services.approvals import ResolvedApprover, register_approver_resolver
from app.services.notifications_service import RecipientInfo, register_recipient_directory


async def _client_user_directory(db: AsyncSession, client_user_id: uuid.UUID) -> RecipientInfo | None:
    row = (await db.execute(select(ClientUser).where(ClientUser.id == client_user_id))).scalar_one_or_none()
    if row is None or row.status != "active":
        return None
    return RecipientInfo(str(row.email).lower(), row.display_name, ("client", row.client_id))


async def _vendor_user_directory(db: AsyncSession, vendor_user_id: uuid.UUID) -> RecipientInfo | None:
    row = (await db.execute(select(VendorUser).where(VendorUser.id == vendor_user_id))).scalar_one_or_none()
    if row is None or row.status != "active":
        return None
    return RecipientInfo(str(row.email).lower(), row.display_name, ("vendor", row.vendor_id))


async def _resolve_client_user(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,
    approver_ref: Mapping[str, Any],
    facts: Mapping[str, Any],  # ruff: ignore[unused-function-argument]
) -> ResolvedApprover:
    raw = approver_ref.get("client_user_id")
    if not raw:
        raise LookupError("client_user_id")
    row = (
        await db.execute(select(ClientUser).where(ClientUser.id == uuid.UUID(str(raw))))
    ).scalar_one_or_none()
    if row is None or row.status != "active":
        raise LookupError("client user")
    return ResolvedApprover("client_user", None, None, row.id, ("client", row.client_id), row.display_name)


async def _resolve_client_any(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,  # ruff: ignore[unused-function-argument]
    approver_ref: Mapping[str, Any],
    facts: Mapping[str, Any],  # ruff: ignore[unused-function-argument]
) -> ResolvedApprover:
    raw = approver_ref.get("client_id")
    if not raw:
        raise LookupError("client_id")
    client_id = uuid.UUID(str(raw))
    return ResolvedApprover("client_any", None, None, None, ("client", client_id), "client approver")


register_recipient_directory("client_user", _client_user_directory)
register_recipient_directory("vendor_user", _vendor_user_directory)
register_approver_resolver("client_user", _resolve_client_user)
register_approver_resolver("client_any", _resolve_client_any)

# Epic 14: a vendor that stops being active loses every Vendor User session (<= 1 minute).
from app.core.events import subscribe  # ruff: ignore[module-import-not-at-top-of-file]
from app.events.vendors import VendorStatusChanged  # ruff: ignore[module-import-not-at-top-of-file]
from app.services.external_auth.handlers import on_vendor_status_changed

subscribe(VendorStatusChanged, on_vendor_status_changed)
