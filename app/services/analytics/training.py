# ruff: file-ignore[line-too-long]
"""Per-tenant training (Epic 48 A4, FR-244, NFR-37/39). Runs inside one tenant RLS session (one tenant per task).

``train(db, tenant_id, kind)``:

1. inserts ``ml_models(status='training')`` (next version of the kind);
2. builds the labelled set over ``history_months`` (a row is labelled only when its outcome is known);
3. threshold: ``n < min_samples`` or (classification) ``n_minority < min_minority`` -> ``insufficient_data`` with
   ``metrics.progress`` (no snapshot kept; the previous active model stays);
4. stores the snapshot, sorts by outcome date, time-ordered holdout = the last 20 %;
5. fits (``models.fit``, fixed seed) on the training part only;
6. compares with the naive baseline on the holdout;
7. promotes: beats baseline -> ``active`` and the previous active -> ``superseded`` (same transaction); otherwise
   ``failed`` (``below_baseline``) and the previous active stays. Exceptions -> ``failed`` (``error``).

``refit(db, model)`` refits from the stored snapshot + seed (reproducibility; byte-identical ``params``).
"""

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

from loguru import logger
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.analytics import MlFeatureSpec, MlModel, MlTrainingSet
from app.services.analytics import governance, snapshots
from app.services.analytics.features import denylist
from app.services.analytics.features.common import TrainingRow
from app.services.analytics.features.specs import SPECS, FeatureSpec

SEED = 4848
HOLDOUT_SHARE = 0.2


async def _ensure_spec(db: AsyncSession, tenant_id: uuid.UUID, spec: FeatureSpec) -> None:
    exists = (
        await db.execute(
            select(MlFeatureSpec.id).where(
                MlFeatureSpec.kind == spec.kind, MlFeatureSpec.version == spec.version
            )
        )
    ).first()
    if exists is None:
        db.add(
            MlFeatureSpec(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                kind=spec.kind,
                version=spec.version,
                features=spec.as_json(),
            )
        )
        await db.flush()


def split(rows: Sequence[TrainingRow]) -> tuple[list[TrainingRow], list[TrainingRow]]:
    """Time-ordered holdout: sort by outcome date (subject id breaks ties), the last 20 % is the holdout."""
    ordered = sorted(rows, key=lambda r: (r.outcome_at, str(r.subject_id)))
    cut = max(1, round(len(ordered) * (1 - HOLDOUT_SHARE)))
    cut = min(cut, len(ordered) - 1)
    return ordered[:cut], ordered[cut:]


def minority(rows: Sequence[TrainingRow]) -> int:
    pos = sum(1 for r in rows if r.label >= 0.5)
    return min(pos, len(rows) - pos)


def fit_rows(spec: FeatureSpec, rows: Sequence[TrainingRow], seed: int):  # type: ignore[no-untyped-def]  # ruff: ignore[missing-return-type-undocumented-public-function]
    from app.services.analytics import models as ml

    train, holdout = split(rows)
    return ml.fit(
        spec.task,
        spec.as_json(),
        [(r.features, r.label) for r in train],
        [(r.features, r.label) for r in holdout],
        seed,
    ), holdout


async def _next_version(db: AsyncSession, kind: str) -> int:
    return (
        int(
            (
                await db.execute(
                    select(func.coalesce(func.max(MlModel.version), 0)).where(MlModel.kind == kind)
                )
            ).scalar_one()
        )
        + 1
    )


async def train(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    kind: str,
    *,
    now: datetime | None = None,
    seed: int = SEED,
    rows: Sequence[TrainingRow] | None = None,
) -> MlModel | None:
    """Train one kind for the session's tenant. ``rows`` (tests / perf) bypasses the extractor."""
    from app.services.analytics import features

    settings = await governance.settings_row(db, tenant_id)
    if not governance.enabled(settings, kind):
        return None
    spec = SPECS[kind]
    denylist.check_spec(spec.as_json())
    now = now or datetime.now(UTC)
    model = MlModel(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        kind=kind,
        version=await _next_version(db, kind),
        status="training",
        seed=seed,
        feature_spec_version=spec.version,
    )
    db.add(model)
    await db.flush()
    try:
        async with db.begin_nested():
            await _ensure_spec(db, tenant_id, spec)
            built = (
                list(rows)
                if rows is not None
                else await features.training_rows(db, kind, now, settings.history_months)
            )
            await _train(db, tenant_id, model, spec, built, settings.min_samples, settings.min_minority, now)
    except Exception:
        logger.exception("analytics: training {} failed for tenant {}", kind, tenant_id)
        await db.refresh(model)
        model.status = "failed"
        model.failure_code = "error"
        model.trained_at = now
        await db.flush()
    return model


async def _train(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    model: MlModel,
    spec: FeatureSpec,
    rows: Sequence[TrainingRow],
    min_samples: int,
    min_minority: int,
    now: datetime,
) -> None:
    n = len(rows)
    n_min = minority(rows) if spec.task == "classification" else None
    model.n_samples = n
    model.n_minority = n_min
    model.trained_at = now
    if n < min_samples or (n_min is not None and n_min < min_minority):
        model.status = "insufficient_data"
        model.metrics = {
            "progress": {
                "n": n,
                "required": min_samples,
                "n_minority": n_min,
                "required_minority": min_minority if spec.task == "classification" else None,
            }
        }
        await db.flush()
        return
    stored = snapshots.roundtrip(rows)  # fit on exactly what the snapshot returns
    result, holdout = fit_rows(spec, stored, model.seed)
    holdout_from = min(r.outcome_at for r in holdout).date() if holdout else None
    ts = await snapshots.write(
        db, tenant_id, spec.kind, spec.version, stored, n_minority=n_min, holdout_from=holdout_from, now=now
    )
    model.training_set_id = ts.id
    model.params = result.params
    model.metrics = {
        "holdout": result.metrics,
        "baseline": result.baseline,
        "beats_baseline": result.beats_baseline,
    }
    if not result.beats_baseline:
        model.status = "failed"
        model.failure_code = "below_baseline"
        await db.flush()
        return
    for previous in (
        await db.execute(
            select(MlModel).where(
                MlModel.kind == spec.kind, MlModel.status == "active", MlModel.id != model.id
            )
        )
    ).scalars():
        previous.status = "superseded"
        previous.superseded_at = now
    await db.flush()
    model.status = "active"
    await db.flush()


async def refit(db: AsyncSession, model: MlModel) -> dict:  # type: ignore[type-arg]
    """Params of a refit from the model's stored snapshot and seed (reproducibility check)."""
    if model.training_set_id is None:
        msg = "model has no stored snapshot"
        raise ValueError(msg)
    ts = await db.get(MlTrainingSet, model.training_set_id)
    if ts is None:
        msg = "snapshot expired"
        raise ValueError(msg)
    rows = await snapshots.read(ts)
    result, _ = fit_rows(SPECS[model.kind], rows, model.seed)
    return result.params


async def train_all(db: AsyncSession, tenant_id: uuid.UUID, now: datetime | None = None) -> dict[str, str]:
    """Every enabled kind, sequentially (nightly task body)."""
    out: dict[str, str] = {}
    for kind in SPECS:
        model = await train(db, tenant_id, kind, now=now)
        out[kind] = model.status if model is not None else "disabled"
    return out
