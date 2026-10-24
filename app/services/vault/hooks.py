"""Vault wiring (Epic 66), autodiscovered by ``core.hooks``.

* retention binding ``vault_access_log`` -> class ``vault_access_logs`` (12 months, rows deleted; no subject);
* audit action labels ``vault.secret_set``, ``vault.secret_deleted``, ``vault.dek_rotated`` (names only).
"""

import uuid
from collections.abc import Sequence
from typing import Any

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession


def _register_retention() -> None:
    from sqlalchemy import delete, select

    from app.models.vault import VaultAccessLog
    from app.services.privacy.retention_classes import register_retention_binding, retention_bindings

    async def _no_subject(db: AsyncSession, row_id: uuid.UUID, *_: Any, **__: Any) -> None:  # ruff: ignore[unused-function-argument]  # NOSONAR
        return None

    async def _anchor(db: AsyncSession, row_id: uuid.UUID, *_: Any, **__: Any) -> Any:
        return (
            await db.execute(select(VaultAccessLog.at).where(VaultAccessLog.id == row_id))
        ).scalar_one_or_none()

    async def _expire(db: AsyncSession, ids: Sequence[uuid.UUID], action: str = "delete") -> dict[str, int]:  # ruff: ignore[unused-function-argument]
        result = await db.execute(delete(VaultAccessLog).where(VaultAccessLog.id.in_(list(ids))))
        return {"deleted": int(getattr(result, "rowcount", 0) or 0)}

    if "vault_access_log" not in retention_bindings():
        register_retention_binding(
            "vault_access_log",
            "vault_access_logs",
            subject_resolver=_no_subject,
            anchor_resolver=_anchor,
            expire_fn=_expire,
        )


def _register_audit() -> None:
    from app.core import audit

    for key, text in (
        ("vault.secret_set", "A stored secret was set or rotated"),
        ("vault.secret_deleted", "A stored secret was deleted"),
        ("vault.dek_rotated", "The workspace secrets data key was rotated"),
    ):
        if key not in audit.audit_actions():
            audit.register_audit_action(key, text)


for _fn in (_register_retention, _register_audit):
    try:
        _fn()
    except Exception:  # pragma: no cover - surfaced in the logs
        logger.exception("vault hooks: {} failed", _fn.__name__)
        raise
