# ruff: file-ignore[line-too-long]
"""Public events hooks (Epic 66, AD-79): install the outbox row factory and the after-commit relay kick in
``core.public_events``, register the catalogue v1, the retention binding of ``outbox_events`` (class ``public_events``)
and the post-discovery catalogue validation (errors are logged; ``tests/public_events`` asserts an empty list)."""

import uuid
from collections.abc import Sequence
from typing import Any

from loguru import logger
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import public_events as registry
from app.core.hooks import register_post_discovery
from app.services.public_events import catalogue_v1, dispatch, outbox


def _install() -> None:
    registry.set_outbox_row_factory(outbox.row_factory)
    registry.set_dispatch_kicker(dispatch.kick)
    catalogue_v1.register_v1()


def _register_retention() -> None:
    from app.models.public_events import OutboxEvent
    from app.services.privacy.retention_classes import register_retention_binding, retention_bindings

    if "outbox_events" in retention_bindings():
        return

    async def _no_subject(db: AsyncSession, row_id: uuid.UUID, *_: Any, **__: Any) -> None:  # ruff: ignore[unused-function-argument]  # NOSONAR
        return None

    async def _anchor(db: AsyncSession, row_id: uuid.UUID, *_: Any, **__: Any) -> Any:
        return (
            await db.execute(select(OutboxEvent.occurred_at).where(OutboxEvent.id == row_id))
        ).scalar_one_or_none()

    async def _expire(db: AsyncSession, ids: Sequence[uuid.UUID], action: str = "delete") -> dict[str, int]:  # ruff: ignore[unused-function-argument]
        result = await db.execute(
            delete(OutboxEvent).where(
                OutboxEvent.id.in_(list(ids)), OutboxEvent.dispatch_status == "dispatched"
            )
        )
        return {"deleted": int(getattr(result, "rowcount", 0) or 0)}

    register_retention_binding(
        "outbox_events",
        "public_events",
        subject_resolver=_no_subject,
        anchor_resolver=_anchor,
        expire_fn=_expire,
    )


def _validate() -> None:
    for error in registry.validate_catalogue():
        logger.error("Public Event catalogue: {}", error)


_install()
_register_retention()
register_post_discovery(_validate)
