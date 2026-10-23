# ruff: file-ignore[line-too-long, non-imperative-mood, unused-function-argument]
"""Facade of the ``payroll`` domain (Epic 13 contract layer; filled by Epic 28).

Epic 71 (D-18, additive): ``export_file_bytes`` (the approved export file as generated for the default mapping,
masked identity columns) and ``set_provider_status`` (connector push outcome) for the Phase 5 payroll push.
"""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.context import RequestContext
from app.core.errors import ConflictError, ValidationFailed
from app.services.payroll.service import pay_period_for


async def export_file_bytes(ctx: RequestContext, db: AsyncSession, version_id: uuid.UUID) -> tuple[bytes, str]:
    """The finalised export version's file (default mapping and format) as ``(bytes, media_type)``. Previews refused."""
    from app.services.payroll import exports, files

    v = await exports.load_version(db, version_id)
    if v.kind == "preview":
        raise ConflictError("Only a final or supplementary version can be pushed", code="not_finalized")
    download = await files.build_file(ctx, db, version_id, None, None)
    return download.data, download.content_type


async def set_provider_status(
    ctx: RequestContext,
    db: AsyncSession,
    version_id: uuid.UUID,
    status: str,
    external_ref: str | None = None,
) -> None:
    """Record a connector push outcome (``sent`` | ``failed`` | ``not_sent``); never downgrades ``processed``."""
    from app.models.payroll import PROVIDER_STATUSES
    from app.services.payroll import exports

    if status not in PROVIDER_STATUSES or status == "processed":
        raise ValidationFailed("Unknown provider status", code="invalid_provider_status")
    v = await exports.load_version(db, version_id, lock=True)
    if v.provider_status == "processed":
        return
    v.provider_status = status
    if external_ref:
        v.provider_reference = external_ref[:200]
    await db.flush()
    audit.record("payroll.provider_status_set", "payroll_export_version", v.id, session=db, status=status)


__all__ = ["export_file_bytes", "pay_period_for", "set_provider_status"]
