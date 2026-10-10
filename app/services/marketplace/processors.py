# ruff: noqa: E501, ARG001
"""Processor view and data-flow read model (Epic 69, spec A6). ``list_processors``: the platform sub-processors (P1,
unchanged) plus every non-uninstalled Installation ("Installed by your organisation"). ``data_flows``: one row per data
flow of each live Installation's current grant -- the read model Epic 79's data-flow register and FR-408 consume."""

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.database import platform_session
from app.models.oauth import App, AppGrant, AppInstallation
from app.models.platform import SubProcessor
from app.schemas.marketplace import DataFlowQuery, DataFlowRow, ProcessorOut
from app.services.marketplace.legacy_catalog import listing_category


async def _live(db: AsyncSession) -> list[tuple[AppInstallation, AppGrant | None]]:
    rows = list(
        (await db.execute(select(AppInstallation).where(AppInstallation.status != "uninstalled"))).scalars()
    )
    grants = (
        {
            g.installation_id: g
            for g in (
                await db.execute(
                    select(AppGrant).where(
                        AppGrant.installation_id.in_([r.id for r in rows]), AppGrant.superseded_at.is_(None)
                    )
                )
            ).scalars()
        }
        if rows
        else {}
    )
    return [(r, grants.get(r.id)) for r in rows]


def _flows(grant: AppGrant | None) -> list[dict[str, Any]]:
    flows = grant.data_flows if grant is not None else []
    return [f for f in flows or [] if isinstance(f, dict)]


async def list_processors(ctx: RequestContext, db: AsyncSession) -> list[ProcessorOut]:
    from app.core.integrations import find_manifest

    out: list[ProcessorOut] = []
    async with platform_session() as pdb:
        subs = list(
            (
                await pdb.execute(
                    select(SubProcessor).where(SubProcessor.status != "retired").order_by(SubProcessor.name)
                )
            ).scalars()
        )
        out.extend(
            ProcessorOut(
                name=s.name,
                source="platform",
                publisher="Talent OS",
                purpose=s.purpose,
                countries=[s.country],
                status=s.status,
            )
            for s in subs
        )
        live = await _live(db)
        app_ids = [i.app_id for i, _ in live if i.app_id is not None]
        apps = (
            {a.id: a for a in (await pdb.execute(select(App).where(App.id.in_(app_ids)))).scalars()}
            if app_ids
            else {}
        )
        publishers = {a.id: ("Private" if a.publisher_kind == "tenant" else "Partner") for a in apps.values()}
    for inst, grant in live:
        countries = sorted({str(f.get("destination_country") or "IN") for f in _flows(grant)}) or ["IN"]
        if inst.kind == "connector":
            m = find_manifest(str(inst.connector_key))
            category, publisher = (listing_category(m.category) if m else "other"), "Talent OS"
        else:
            category = (
                str((apps[inst.app_id].manifest or {}).get("category") or "other")
                if inst.app_id in apps
                else "other"
            )
            publisher = publishers.get(inst.app_id, "Partner")  # type: ignore[arg-type]
        out.append(ProcessorOut(
            name=inst.display_name, source="installed", publisher=publisher, category=category, countries=countries,
            installed_on=inst.created_at, status=inst.status, installation_id=inst.id,
        ))  # fmt: skip
    return out


async def data_flows(
    ctx: RequestContext, db: AsyncSession, q: DataFlowQuery | None = None
) -> list[DataFlowRow]:
    q = q or DataFlowQuery()
    rows: list[DataFlowRow] = []
    for inst, grant in await _live(db):
        if q.installation_id is not None and inst.id != q.installation_id:
            continue
        for f in _flows(grant):
            row = DataFlowRow(
                installation_id=inst.id, name=inst.display_name, direction=str(f.get("direction") or "out"),
                entity=str(f.get("entity") or ""), fields=[str(x) for x in f.get("fields") or []],
                purpose=str(f.get("purpose") or ""), destination_country=str(f.get("destination_country") or "IN"),
                personal_data=bool(f.get("personal_data", True)), granted_at=grant.granted_at if grant else inst.created_at,
            )  # fmt: skip
            if q.personal_data is not None and row.personal_data != q.personal_data:
                continue
            if q.destination_country and row.destination_country.upper() != q.destination_country.upper():
                continue
            rows.append(row)
    return rows


__all__ = ["data_flows", "list_processors"]
