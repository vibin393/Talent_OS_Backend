"""Predictions port for readers outside ``analytics`` (Epic 48 / Epic 52, AD-40).

``analytics`` is a top-layer reader: no other service imports it
(``tests/modules/analytics/test_import_guard.py``). The analytics domain registers its read helpers here from
``hooks.py``; readers such as the Phase 3 dashboards use ``prediction_reader()``. Without a provider (analytics
not loaded) no prediction kind is readable and there are no model cards, so tiles show nothing rather than leak.
"""

from typing import Any, Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext

__all__ = ["PredictionReader", "prediction_reader", "register_prediction_reader"]


class PredictionReader(Protocol):
    def can_read_kind(self, principal: Any, kind: str) -> bool: ...

    async def model_cards(self, ctx: RequestContext, db: AsyncSession) -> list[Any]: ...


class _NoPredictions:
    def can_read_kind(self, principal: Any, kind: str) -> bool:  # ruff: ignore[unused-method-argument]
        return False

    async def model_cards(self, ctx: RequestContext, db: AsyncSession) -> list[Any]:  # ruff: ignore[unused-method-argument]  # NOSONAR
        return []


_provider: PredictionReader | None = None


def register_prediction_reader(provider: PredictionReader) -> None:
    global _provider  # process-wide registry, set once at start-up
    _provider = provider


def prediction_reader() -> PredictionReader:
    return _provider if _provider is not None else _NoPredictions()
