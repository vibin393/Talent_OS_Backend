# ruff: noqa: E501
"""The external-id map ``connector_links`` (Epic 66, spec 0.10): local entity <-> provider id per Installation, unique
both ways. ``link`` upserts by the local side; an external id already linked to another local entity -> 409
``link_conflict``. ``unlink_candidate`` serves the erasure handler (unlink + null ``external_url``)."""

import uuid
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, ValidationFailed
from app.models.integrations import LINK_STATUSES, ConnectorLink
from app.schemas.integrations import LinkOut

__all__ = ["find_link", "link", "unlink_candidate"]

_FIELDS = (
    "external_url",
    "local_version",
    "external_version",
    "content_sha256",
    "status",
    "last_error_code",
    "candidate_id",
    "last_synced_at",
)


async def link(
    db: AsyncSession,
    installation_id: uuid.UUID,
    entity_type: str,
    local_id: uuid.UUID,
    external_id: str,
    **kw: Any,
) -> None:
    """Upsert the link of ``(installation, entity_type, local_id)``; ``kw`` sets ``external_url``, ``local_version``,
    ``external_version``, ``content_sha256``, ``status``, ``last_error_code``, ``candidate_id``, ``last_synced_at``
    (default now)."""
    unknown = sorted(set(kw) - set(_FIELDS))
    if unknown:
        msg = f"unknown link fields {unknown}"
        raise TypeError(msg)
    if kw.get("status", "linked") not in LINK_STATUSES:
        raise ValidationFailed("Invalid link status", code="link_status_invalid")
    tenant_id = uuid.UUID(str(db.sync_session.info["tenant_id"]))
    values: dict[str, Any] = {"last_synced_at": datetime.now(UTC), **kw, "external_id": external_id[:200]}
    stmt = pg_insert(ConnectorLink).values(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        installation_id=installation_id,
        entity_type=entity_type,
        local_id=local_id,
        **values,
    )
    stmt = stmt.on_conflict_do_update(
        constraint="uq_connector_links_local",
        set_={**{k: stmt.excluded[k] for k in values}, "updated_at": datetime.now(UTC)},
    )
    try:
        async with db.begin_nested():
            await db.execute(stmt)
    except IntegrityError as exc:
        raise ConflictError(
            "This external record is already linked to another record",
            code="link_conflict",
            details={"entity_type": entity_type},
        ) from exc


async def find_link(
    db: AsyncSession,
    installation_id: uuid.UUID,
    entity_type: str,
    *,
    local_id: uuid.UUID | None = None,
    external_id: str | None = None,
) -> LinkOut | None:
    if (local_id is None) == (external_id is None):
        msg = "pass exactly one of local_id / external_id"
        raise TypeError(msg)
    stmt = select(ConnectorLink).where(
        ConnectorLink.installation_id == installation_id, ConnectorLink.entity_type == entity_type
    )
    stmt = stmt.where(
        ConnectorLink.local_id == local_id
        if local_id is not None
        else ConnectorLink.external_id == external_id
    )
    row = (await db.execute(stmt)).scalar_one_or_none()
    return LinkOut.model_validate(row) if row is not None else None


async def unlink_candidate(db: AsyncSession, candidate_ids: Iterable[uuid.UUID]) -> int:
    ids = list(candidate_ids)
    if not ids:
        return 0
    result = await db.execute(
        update(ConnectorLink)
        .where(ConnectorLink.candidate_id.in_(ids))
        .values(status="unlinked", external_url=None, updated_at=datetime.now(UTC))
    )
    await db.flush()
    return int(getattr(result, "rowcount", 0) or 0)
