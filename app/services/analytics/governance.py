# ruff: file-ignore[line-too-long]
"""Analytics settings, model enablement and model cards (Epic 48 A1 / A4 / A6, FR-244, PRD OQ7).

* ``analytics_settings`` (one row per tenant, lazily created, audited): thresholds can only be **raised** above
  the platform defaults (``min_samples >= 200``, ``min_minority >= 30``; 422 ``threshold_below_platform``).
* Disabling a kind sets its active model to ``disabled`` (reads return ``disabled``; no training or scoring).
  Re-enabling trains on the next night.
* Model cards: ``model:manage`` sees the full card; ``prediction:read`` holders get a reduced card (status,
  version, progress, feature list without metrics).
"""

import uuid
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.errors import ValidationFailed
from app.core.permissions import Principal
from app.models.analytics import (
    DEFAULT_BAND_THRESHOLDS,
    DEFAULT_ENABLED_KINDS,
    DEFAULT_HISTORY_MONTHS,
    MODEL_KINDS,
    PLATFORM_MIN_MINORITY,
    PLATFORM_MIN_SAMPLES,
    AnalyticsSettings,
    MlModel,
)
from app.schemas.analytics import (
    AnalyticsSettingsIn,
    AnalyticsSettingsOut,
    CalibrationBin,
    FeatureRank,
    HistoryRow,
    ModelCard,
    ModelDetail,
    ModelRef,
    Progress,
)
from app.services.analytics.features import denylist
from app.services.analytics.features.specs import KIND_LABELS, SPECS

READ = "prediction:read"
READ_ATTRITION = "prediction:read_attrition"
MANAGE = "model:manage"


def kind_permission(kind: str) -> str:
    return READ_ATTRITION if kind == "attrition" else READ


def can_read_kind(principal: Any, kind: str) -> bool:
    return (
        isinstance(principal, Principal) and principal.type == "user" and principal.has(kind_permission(kind))
    )


# ---------------------------------------------------------------- settings
async def settings_row(db: AsyncSession, tenant_id: uuid.UUID | None = None) -> AnalyticsSettings:
    row = (await db.execute(select(AnalyticsSettings))).scalar_one_or_none()
    if row is not None:
        return row
    if tenant_id is None:
        from app.services import entity_refs

        tenant_id = entity_refs.session_tenant(db)
    row = AnalyticsSettings(
        tenant_id=tenant_id,
        enabled_kinds=dict(DEFAULT_ENABLED_KINDS),
        min_samples=PLATFORM_MIN_SAMPLES,
        min_minority=PLATFORM_MIN_MINORITY,
        band_thresholds={k: dict(v) for k, v in DEFAULT_BAND_THRESHOLDS.items()},
        history_months=DEFAULT_HISTORY_MONTHS,
    )
    db.add(row)
    await db.flush()
    return row


def enabled(row: AnalyticsSettings, kind: str) -> bool:
    return bool((row.enabled_kinds or {}).get(kind, True))


def thresholds(row: AnalyticsSettings, kind: str) -> tuple[Decimal, Decimal] | None:
    if kind not in DEFAULT_BAND_THRESHOLDS:
        return None
    t = (row.band_thresholds or {}).get(kind) or DEFAULT_BAND_THRESHOLDS[kind]
    return Decimal(str(t["medium"])), Decimal(str(t["high"]))


def settings_out(row: AnalyticsSettings) -> AnalyticsSettingsOut:
    bands = {
        k: {**DEFAULT_BAND_THRESHOLDS[k], **((row.band_thresholds or {}).get(k) or {})}
        for k in DEFAULT_BAND_THRESHOLDS
    }
    return AnalyticsSettingsOut.model_validate(
        {
            "enabled_kinds": {k: enabled(row, k) for k in MODEL_KINDS},
            "min_samples": row.min_samples,
            "min_minority": row.min_minority,
            "band_thresholds": bands,
            "history_months": row.history_months,
            "platform_min_samples": PLATFORM_MIN_SAMPLES,
            "platform_min_minority": PLATFORM_MIN_MINORITY,
        }
    )


async def get_settings(ctx: RequestContext, db: AsyncSession) -> AnalyticsSettingsOut:
    return settings_out(await settings_row(db, ctx.tenant_id))


def _threshold(value: str, path: str) -> Decimal:
    try:
        d = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValidationFailed(
            "Band thresholds must be decimals", code="invalid_band_thresholds", details={"path": path}
        ) from exc
    if not Decimal(0) < d < Decimal(1):
        raise ValidationFailed(
            "Band thresholds must be between 0 and 1", code="invalid_band_thresholds", details={"path": path}
        )
    return d


async def update_settings(
    ctx: RequestContext, db: AsyncSession, data: AnalyticsSettingsIn
) -> AnalyticsSettingsOut:
    row = await settings_row(db, ctx.tenant_id)
    if data.min_samples is not None:
        if data.min_samples < PLATFORM_MIN_SAMPLES:
            raise ValidationFailed(
                f"Minimum samples cannot be below the platform default ({PLATFORM_MIN_SAMPLES})",
                code="threshold_below_platform",
                details={"path": "min_samples", "minimum": PLATFORM_MIN_SAMPLES},
            )
        row.min_samples = data.min_samples
    if data.min_minority is not None:
        if data.min_minority < PLATFORM_MIN_MINORITY:
            raise ValidationFailed(
                f"Minimum minority-class samples cannot be below the platform default ({PLATFORM_MIN_MINORITY})",
                code="threshold_below_platform",
                details={"path": "min_minority", "minimum": PLATFORM_MIN_MINORITY},
            )
        row.min_minority = data.min_minority
    if data.history_months is not None:
        row.history_months = data.history_months
    if data.band_thresholds is not None:
        bands: dict[str, dict[str, str]] = {}
        for kind in ("offer_drop", "attrition"):
            t = getattr(data.band_thresholds, kind)
            medium, high = (
                _threshold(t.medium, f"band_thresholds.{kind}.medium"),
                _threshold(t.high, f"band_thresholds.{kind}.high"),
            )
            if not medium < high:
                raise ValidationFailed(
                    "The medium threshold must be below the high threshold",
                    code="invalid_band_thresholds",
                    details={"path": f"band_thresholds.{kind}"},
                )
            bands[kind] = {"medium": str(medium), "high": str(high)}
        row.band_thresholds = bands
    if data.enabled_kinds is not None:
        kinds = dict(row.enabled_kinds or DEFAULT_ENABLED_KINDS)
        for kind, value in data.enabled_kinds.model_dump(exclude_none=True).items():
            if kinds.get(kind, True) and not value:
                await _disable_active(db, kind)
            kinds[kind] = bool(value)
        row.enabled_kinds = kinds
    await db.flush()
    return settings_out(row)


async def _disable_active(db: AsyncSession, kind: str) -> None:
    for model in (
        await db.execute(select(MlModel).where(MlModel.kind == kind, MlModel.status == "active"))
    ).scalars():
        model.status = "disabled"
    await db.flush()


# ---------------------------------------------------------------- models
async def active_model(db: AsyncSession, kind: str) -> MlModel | None:
    return (
        await db.execute(select(MlModel).where(MlModel.kind == kind, MlModel.status == "active"))
    ).scalar_one_or_none()


async def latest_model(db: AsyncSession, kind: str) -> MlModel | None:
    return (
        await db.execute(
            select(MlModel).where(MlModel.kind == kind).order_by(MlModel.version.desc()).limit(1)
        )
    ).scalar_one_or_none()


def progress_of(model: MlModel | None) -> Progress | None:
    if model is None or model.status != "insufficient_data":
        return None
    p = (model.metrics or {}).get("progress") or {}
    return Progress(
        n=int(p.get("n", model.n_samples or 0)),
        required=int(p.get("required", PLATFORM_MIN_SAMPLES)),
        n_minority=p.get("n_minority"),
        required_minority=p.get("required_minority"),
    )


def _features(model: MlModel) -> list[FeatureRank]:
    from app.services.analytics import models as ml

    labels = SPECS[model.kind].by_key()
    return [
        FeatureRank(
            key=key,
            label=labels[key].label if key in labels else key,
            sign="+" if w >= 0 else "-",
            rank=i + 1,
        )
        for i, (key, w) in enumerate(ml.coefficient_ranks(model.params or {}))
    ]


def _detail(model: MlModel, reduced: bool) -> ModelDetail:
    metrics = model.metrics or {}
    hold = metrics.get("holdout") or {}
    return ModelDetail(
        id=model.id,
        version=model.version,
        trained_at=model.trained_at,
        n_samples=model.n_samples,
        n_minority=model.n_minority,
        metrics={}
        if reduced
        else {k: str(v) for k, v in hold.items() if v is not None and not isinstance(v, list | dict)},
        baseline={}
        if reduced
        else {k: str(v) for k, v in (metrics.get("baseline") or {}).items() if v is not None},
        calibration=[] if reduced else [CalibrationBin(**b) for b in hold.get("calibration") or []],
        features=_features(model),
    )


def _card_status(model: MlModel | None, active: MlModel | None, on: bool) -> str:
    if not on:
        return "disabled"
    if active is not None:
        return "active"
    if model is None:
        return "no_model"
    if model.status in ("insufficient_data", "failed", "training"):
        return model.status
    return "no_model"


async def model_cards(ctx: RequestContext, db: AsyncSession, kind: str | None = None) -> list[ModelCard]:
    principal = ctx.principal
    full = isinstance(principal, Principal) and principal.has(MANAGE)
    row = await settings_row(db, ctx.tenant_id)
    kinds = [k for k in MODEL_KINDS if kind in (None, k)]
    if not full:
        kinds = [k for k in kinds if can_read_kind(principal, k)]
    cards: list[ModelCard] = []
    for k in kinds:
        history = list(
            (
                await db.execute(
                    select(MlModel).where(MlModel.kind == k).order_by(MlModel.version.desc()).limit(10)
                )
            ).scalars()
        )
        active = next((m for m in history if m.status == "active"), None) or await active_model(db, k)
        latest = history[0] if history else None
        on = enabled(row, k)
        cards.append(
            ModelCard(
                kind=k,  # type: ignore[arg-type]
                kind_label=KIND_LABELS[k],
                enabled=on,
                status=_card_status(latest, active, on),  # type: ignore[arg-type]
                reduced=not full,
                active=_detail(active, not full) if active is not None else None,
                latest=ModelRef(
                    id=latest.id,
                    version=latest.version,
                    status=latest.status,
                    trained_at=latest.trained_at,
                    failure_code=latest.failure_code,
                )
                if latest
                else None,
                progress=progress_of(latest),
                excluded_features=list(denylist.EXCLUDED_CATEGORIES),
                history=[]
                if not full
                else [
                    HistoryRow(
                        id=m.id,
                        version=m.version,
                        status=m.status,
                        trained_at=m.trained_at,
                        n_samples=m.n_samples,
                        failure_code=m.failure_code,
                    )
                    for m in history
                ],
            )
        )
    return cards
