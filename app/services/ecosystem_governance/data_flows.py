# ruff: file-ignore[line-too-long]
"""Data-flow register (Epic 79 stories 79.1 / 79.5, FR-405 / FR-408, NFR-69).

One row per (Installation, direction, entity, field set, purpose, destination country) from the current
``app_grants.data_flows`` snapshot (Apps) and connector manifests' ``data_flows`` (connectors). Restricted destination
countries (setting ``restricted_transfer_countries``, empty by default) are flagged. The CSV export is masked (no
subject ids) and streamed from the console (spec deviation note: not the P1 export engine).
"""

from __future__ import annotations

import csv
import io
from typing import TYPE_CHECKING, Any

from loguru import logger
from sqlalchemy import select

from app.models.oauth import AppInstallation
from app.schemas.ecosystem_governance import DataFlowRow

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.core.context import RequestContext

CSV_COLUMNS = (
    "installation", "publisher", "direction", "entity", "fields", "personal_data", "purpose",
    "destination_country", "since", "subjects_exposed_90d", "processor_role", "restricted_transfer",
)  # fmt: skip


def _restricted(country: str | None) -> bool:
    if not country:
        return False
    try:
        from app.core.settings import get_settings

        restricted = getattr(get_settings(), "restricted_transfer_countries", []) or []
        return country in restricted
    except Exception:  # pragma: no cover
        return False


async def register(ctx: RequestContext, db: AsyncSession, **_: Any) -> list[DataFlowRow]:
    rows: list[DataFlowRow] = []
    insts = (
        await db.execute(
            select(AppInstallation).where(
                AppInstallation.tenant_id == ctx.tenant_id, AppInstallation.status != "uninstalled"
            )
        )
    ).scalars().all()
    for inst in insts:
        flows = await _flows_for(db, inst)
        publisher = "platform" if inst.kind == "connector" else "partner"
        for flow in flows:
            country = flow.get("destination_country")
            rows.append(
                DataFlowRow(
                    installation=inst.display_name,
                    publisher=publisher,
                    direction=flow.get("direction", "out"),
                    entity=flow.get("entity", ""),
                    fields=list(flow.get("fields", []) or []),
                    personal_data=bool(flow.get("personal_data", False)),
                    purpose=flow.get("purpose"),
                    destination_country=country,
                    since=inst.granted_at.date() if inst.granted_at else None,
                    subjects_exposed_90d=0,
                    processor_role=flow.get("processor_role"),
                    restricted_transfer=_restricted(country),
                )
            )
    return rows


async def _flows_for(db: AsyncSession, inst: AppInstallation) -> list[dict[str, Any]]:
    """Resolve the Installation's declared data flows (Apps: ``app_grants.data_flows``; connectors: manifest)."""
    try:
        from app.services import marketplace_service

        fn = getattr(marketplace_service, "data_flows", None)
        if callable(fn):
            result = fn(inst.connector_key) if inst.kind == "connector" else None
            if result:
                return list(result)
    except Exception:  # pragma: no cover
        logger.debug("data_flows: marketplace lookup failed for %s", inst.id)
    # Fallback: the grant snapshot stored on the installation config.
    config = getattr(inst, "config", {}) or {}
    return list(config.get("data_flows", []) or [])


def to_csv(rows: list[DataFlowRow]) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(CSV_COLUMNS)
    for r in rows:
        writer.writerow(
            [
                r.installation, r.publisher or "", r.direction, r.entity, "; ".join(r.fields),
                "yes" if r.personal_data else "no", r.purpose or "", r.destination_country or "",
                r.since.isoformat() if r.since else "", r.subjects_exposed_90d, r.processor_role or "",
                "yes" if r.restricted_transfer else "no",
            ]
        )
    return buf.getvalue()


__all__ = ["CSV_COLUMNS", "register", "to_csv"]
