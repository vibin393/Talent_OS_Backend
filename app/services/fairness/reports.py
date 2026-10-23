# ruff: file-ignore[line-too-long, ambiguous-unicode-character-string]
"""Adverse-impact report runs (Epic 49 A5, FR-247).

``create_run`` stores a ``queued`` row and enqueues ``fairness.run_report`` (queue ``analytics``, per-tenant
concurrency 1 through a transaction advisory lock). ``run_report_job``:

1. builds the selection set (ids in memory only);
2. sets ``app.fairness_access = 'aggregate'``, reads the sealed answers of those candidates and decrypts them in
   memory (missing row / ``prefer_not_to_say`` -> unknown);
3. computes the tables (``aggregate.build_snapshot``) with small-cell + complementary suppression;
4. persists the aggregate snapshot (no candidate ids -- ``assert_no_ids`` walks it) and notifies the requester.

Only counts are logged. Every run, view and export is audited with parameters only.
"""

import re
import uuid
from typing import Any

from loguru import logger
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.context import RequestContext
from app.models.fairness import PREFER_NOT, FairnessRun
from app.schemas.fairness import PackOut, RunCreate, RunOut, RunSummaryOut
from app.services.fairness import aggregate, categories, crypto, selection_points, self_id
from app.services.fairness import settings as settings_mod
from app.services.fairness.common import not_found, now, tenant_of, user_id

UUID_RE = re.compile(r"[0-9a-fA-F]{8}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{12}")
INTERSECTIONS = {"gender×age_band": ("gender", "age_band"), "gender×disability": ("gender", "disability")}
NOTIFICATION_TYPE = "fairness.run_completed"


class SnapshotLeakError(RuntimeError):
    pass


def assert_no_ids(value: Any) -> None:
    """Walk the snapshot JSON: no UUID-looking value or key may appear (aggregate-only invariant)."""
    if isinstance(value, dict):
        for k, v in value.items():
            assert_no_ids(k)
            assert_no_ids(v)
    elif isinstance(value, list | tuple):
        for v in value:
            assert_no_ids(v)
    elif isinstance(value, str | uuid.UUID) and UUID_RE.search(str(value)):
        msg = "snapshot contains an identifier"
        raise SnapshotLeakError(msg)


def summary_out(r: FairnessRun) -> RunSummaryOut:
    return RunSummaryOut(
        id=r.id,
        selection_point=r.selection_point,
        params=dict(r.params or {}),
        date_from=r.date_from,  # type: ignore[arg-type]
        date_to=r.date_to,
        k=r.k,
        method=r.method,
        status=r.status,
        total_applicants=r.total_applicants,
        created_at=r.created_at,
        completed_at=r.completed_at,
        error_code=r.error_code,
    )


def run_out(r: FairnessRun) -> RunOut:
    pack = PackOut.model_validate(r.pack) if r.pack else None
    return RunOut(
        **summary_out(r).model_dump(),
        filters=dict(r.filters or {}),
        intersections=list(r.intersections or []),
        snapshot=r.snapshot,
        unknown_counts=r.unknown_counts,
        pack=pack,
    )


async def create_run(ctx: RequestContext, db: AsyncSession, data: RunCreate) -> RunOut:
    s = await settings_mod.current(db)
    if data.selection_point == "assessment_pass" and data.params.assessment_id is None:
        pass  # every assessment
    if data.selection_point.startswith("agent_"):  # Epic 65: a Phase 4 point needs its owner's tables
        from app.services.fairness.aedt_p4 import point_available
        from app.services.fairness.common import invalid

        if not await point_available(db, data.selection_point):
            raise invalid("fairness_point_unavailable", "This selection point is not available", "selection_point")
    run = FairnessRun(
        id=uuid.uuid4(),
        tenant_id=tenant_of(db),
        selection_point=data.selection_point,
        params=data.params.model_dump(mode="json", exclude_none=True),
        filters=data.filters.model_dump(mode="json", exclude_none=True),
        intersections=list(dict.fromkeys(data.intersections)),
        date_from=data.date_from,
        date_to=data.date_to,
        k=s.k,
        method="both" if data.selection_point in selection_points.SCORE_BASED else "selection_rate",
        status="queued",
        created_by=user_id(ctx),
    )
    db.add(run)
    await db.flush()
    await db.refresh(run)
    audit.record(
        "fairness.run_requested",
        "fairness_run",
        run.id,
        session=db,
        selection_point=run.selection_point,
        params=run.params,
        filters=run.filters,
        date_from=str(run.date_from),
        date_to=str(run.date_to),
        k=run.k,
    )
    from app.core.events import emit
    from app.services.fairness.events import RunRequested

    emit(db, RunRequested(tenant_id=run.tenant_id, run_id=run.id))
    return run_out(run)


async def list_runs(ctx: RequestContext, db: AsyncSession) -> list[RunSummaryOut]:  # ruff: ignore[unused-function-argument]
    rows = (
        await db.execute(select(FairnessRun).order_by(FairnessRun.created_at.desc()).limit(200))
    ).scalars()
    return [summary_out(r) for r in rows]


async def load(db: AsyncSession, run_id: uuid.UUID) -> FairnessRun:
    run = await db.get(FairnessRun, run_id)
    if run is None:
        raise not_found("Report run")
    return run


async def get_run(ctx: RequestContext, db: AsyncSession, run_id: uuid.UUID) -> RunOut:  # ruff: ignore[unused-function-argument]
    run = await load(db, run_id)
    audit.record(
        "fairness.run_viewed", "fairness_run", run.id, session=db, selection_point=run.selection_point
    )
    return run_out(run)


# ---------------------------------------------------------------- the aggregate job (queue ``analytics``)
async def _try_lock(db: AsyncSession, tenant_id: uuid.UUID) -> bool:
    return bool(
        (
            await db.execute(
                text("SELECT pg_try_advisory_xact_lock(hashtext(:k))"), {"k": f"fairness:run:{tenant_id}"}
            )
        ).scalar_one()
    )


def _categories_of(opened: dict[str, Any], known: dict[str, set[str]]) -> dict[str, str | None]:
    out: dict[str, str | None] = {}
    for dim, options in known.items():
        value = opened.get(dim)
        out[dim] = value if isinstance(value, str) and value != PREFER_NOT and value in options else None
    return out


async def compute(db: AsyncSession, run: FairnessRun) -> dict[str, Any]:
    """Build the snapshot of ``run`` (inside the task only)."""
    tenant_id = tenant_of(db)
    cset = await categories.active_set(db)
    dims = [d for d in await categories.dimensions(db, cset.id) if d.enabled]
    dimensions = [
        aggregate.Dimension(
            d.key,
            d.label,
            [(str(o["key"]), str(o["label"])) for o in d.options or [] if o["key"] != PREFER_NOT],
        )
        for d in dims
    ]
    known = {d.key: {o for o, _ in d.options} for d in dimensions}
    points = await selection_points.collect(
        db, run.selection_point, dict(run.params or {}), run.date_from, run.date_to, dict(run.filters or {})
    )
    sealed = await self_id.sealed_for(db, list({p.candidate_id for p in points}))
    cats: dict[uuid.UUID, dict[str, str | None]] = {}
    for cid, value in sealed.items():
        cats[cid] = _categories_of(crypto.open_answers(tenant_id, value), known)
    empty: dict[str, str | None] = dict.fromkeys(known)
    records = [aggregate.Record(cats.get(p.candidate_id, empty), p.selected, p.score) for p in points]
    n_points, n_answers = len(points), len(sealed)
    del points, sealed, cats  # ids and answers leave memory with the task
    pairs = [INTERSECTIONS[i] for i in run.intersections or [] if i in INTERSECTIONS]
    snapshot = aggregate.build_snapshot(
        records,
        dimensions,
        pairs,
        run.k,
        score_based=run.selection_point in selection_points.SCORE_BASED,
        category_set_version=cset.version,
        notes=[
            selection_points.DEFINITIONS[run.selection_point],
            "Self-identification is voluntary; missing answers and 'Prefer not to say' are reported as unknown.",
            "Race/ethnicity is not collected; gender is reported in the LL144 'sex' position.",
            f"Cells with fewer than {run.k} people are suppressed, plus complementary cells so they cannot be recomputed.",
        ],
    )
    del records
    logger.info("fairness run {}: {} applicants, {} self-ID rows aggregated", run.id, n_points, n_answers)
    assert_no_ids(snapshot)
    return snapshot


async def run_report_job(db: AsyncSession, run_id: uuid.UUID) -> str:
    run = await db.get(FairnessRun, run_id, with_for_update=True)
    if run is None or run.status not in ("queued", "running"):
        return "skipped"
    if not await _try_lock(db, run.tenant_id):
        return "locked"
    run.status = "running"
    await db.flush()
    try:
        async with db.begin_nested():
            snapshot = await compute(db, run)
    except Exception as exc:
        logger.warning("fairness run {} failed ({})", run.id, type(exc).__name__)
        run.status = "failed"
        run.error_code = (
            "invalid_parameters" if getattr(exc, "status_code", None) == 422 else "compute_failed"
        )
        run.completed_at = now()
        await db.flush()
        return "failed"
    run.snapshot = snapshot
    run.total_applicants = int(snapshot["total_applicants"])
    run.unknown_counts = dict(snapshot["unknown_counts"])
    run.status = "completed"
    run.completed_at = now()
    await db.flush()
    await _notify(db, run)
    return "completed"


async def _notify(db: AsyncSession, run: FairnessRun) -> None:
    if run.created_by is None:
        return
    try:
        from app.services import notifications_service as ns

        if NOTIFICATION_TYPE not in ns.NOTIFICATION_TYPES:
            return
        async with db.begin_nested():
            await ns.deliver(
                db,
                NOTIFICATION_TYPE,
                [run.created_by],
                ns.Content(
                    title="Your adverse-impact report is ready",
                    body="Aggregate results only.",
                    link_path=f"/fairness/runs/{run.id}",
                ),
                event_id=f"fairness_run:{run.id}",
            )
    except Exception:
        logger.exception("fairness: run notification failed")
