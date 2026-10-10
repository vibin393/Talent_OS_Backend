"""Tenant matching settings (Epic 5, FR-30/FR-74): weights within bounds, thresholds, proxy fields, FX.

``GET|PUT /api/v1/settings/matching`` (``matching:configure``). Audited (``MatchingSettings`` is an
``AuditedMixin`` model). Out-of-bounds weights -> 422 with per-field messages.
"""

import dataclasses
import uuid
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.errors import ValidationFailed
from app.models.matching import MatchingSettings
from app.schemas.matching import MatchingSettingsIn, MatchingSettingsOut
from app.schemas.matching_profiles import PROXY_CAPABLE_FIELDS
from app.services.matching.scoring import DEFAULT_FX, DEFAULT_WEIGHTS, WeightsError, validate_weights


@dataclasses.dataclass(frozen=True, slots=True)
class EffectiveSettings:
    weights: dict[str, int]
    strong_threshold: int
    relevant_threshold: int
    proxy_fields: frozenset[str]
    explain_top_n: int
    fx: dict[str, Decimal]


DEFAULTS = EffectiveSettings(
    weights=dict(DEFAULT_WEIGHTS),
    strong_threshold=70,
    relevant_threshold=40,
    proxy_fields=frozenset(),
    explain_top_n=25,
    fx=dict(DEFAULT_FX),
)


def _fx(raw: dict[str, Any] | None) -> dict[str, Decimal]:
    out = dict(DEFAULT_FX)
    for ccy, rate in (raw or {}).items():
        try:
            value = Decimal(str(rate))
        except (InvalidOperation, ValueError):
            continue
        if value > 0:
            out[str(ccy).upper()] = value
    return out


async def _row(db: AsyncSession) -> MatchingSettings | None:
    return (await db.execute(select(MatchingSettings))).scalar_one_or_none()


async def effective(db: AsyncSession) -> EffectiveSettings:
    row = await _row(db)
    if row is None:
        return DEFAULTS
    try:
        weights = validate_weights(row.weights or {})
    except WeightsError:  # pragma: no cover - rows are validated on write
        weights = dict(DEFAULT_WEIGHTS)
    return EffectiveSettings(
        weights=weights,
        strong_threshold=row.strong_threshold,
        relevant_threshold=row.relevant_threshold,
        proxy_fields=frozenset(row.proxy_fields or ()),
        explain_top_n=row.explain_top_n,
        fx=_fx(row.fx_rates),
    )


async def proxy_fields(db: AsyncSession) -> frozenset[str]:
    return (await effective(db)).proxy_fields


def _out(s: EffectiveSettings) -> MatchingSettingsOut:
    return MatchingSettingsOut(
        weights=s.weights,
        strong_threshold=s.strong_threshold,
        relevant_threshold=s.relevant_threshold,
        proxy_fields=sorted(s.proxy_fields),
        explain_top_n=s.explain_top_n,
        fx_rates={k: str(v) for k, v in sorted(s.fx.items())},
        available_proxy_fields=sorted(PROXY_CAPABLE_FIELDS),
    )


async def get_settings(ctx: RequestContext, db: AsyncSession) -> MatchingSettingsOut:  # noqa: ARG001
    return _out(await effective(db))


async def update_settings(
    ctx: RequestContext, db: AsyncSession, data: MatchingSettingsIn
) -> MatchingSettingsOut:
    current = await effective(db)
    errors: list[dict[str, str]] = []
    weights = current.weights
    if data.weights is not None:
        try:
            weights = validate_weights({**current.weights, **data.weights})
        except WeightsError as exc:
            errors.extend(exc.fields)
    strong = data.strong_threshold if data.strong_threshold is not None else current.strong_threshold
    relevant = data.relevant_threshold if data.relevant_threshold is not None else current.relevant_threshold
    if relevant > strong:
        errors.append({"field": "relevant_threshold", "message": "Must not exceed the strong threshold"})
    proxies = current.proxy_fields
    if data.proxy_fields is not None:
        unknown = sorted(set(data.proxy_fields) - PROXY_CAPABLE_FIELDS)
        errors.extend({"field": "proxy_fields", "message": f"Unknown proxy field '{f}'"} for f in unknown)
        proxies = frozenset(data.proxy_fields)
    fx_raw: dict[str, Any] | None = None
    if data.fx_rates is not None:
        fx_raw = {}
        for ccy, rate in data.fx_rates.items():
            if len(ccy) != 3 or not ccy.isalpha() or rate <= 0:
                errors.append(
                    {"field": f"fx_rates.{ccy}", "message": "Use a 3-letter currency and a positive rate"}
                )
            else:
                fx_raw[ccy.upper()] = str(rate)
    if errors:
        raise ValidationFailed(
            "Invalid matching settings", code="invalid_matching_settings", details={"fields": errors}
        )
    row = await _row(db)
    if row is None:
        row = MatchingSettings(tenant_id=ctx.tenant_id or _tenant_of(db), weights=dict(weights))
        db.add(row)
    row.weights = dict(weights)
    row.strong_threshold = strong
    row.relevant_threshold = relevant
    row.proxy_fields = sorted(proxies)
    if data.explain_top_n is not None:
        row.explain_top_n = data.explain_top_n
    if fx_raw is not None:
        row.fx_rates = fx_raw
    await db.flush()
    return _out(await effective(db))


def _tenant_of(db: AsyncSession) -> uuid.UUID:
    return uuid.UUID(str(db.sync_session.info["tenant_id"]))
