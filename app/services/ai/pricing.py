"""Cost estimates for reporting (Epic 53, AD-58). Budgets stay token-denominated (FR-63); ``cost_micros`` is
micro-USD from ``settings.ai_prices`` (USD per million tokens): 1 token at $1/MTok = 1 micro-USD."""

import functools
import json
from typing import Any

from loguru import logger

from app.core.settings import get_settings
from app.schemas.agentic import StepUsage


@functools.cache
def _prices(raw: str) -> dict[str, dict[str, float]]:
    try:
        data: Any = json.loads(raw)
    except ValueError:
        logger.warning("AI_PRICES is not valid JSON; costs are reported as 0")
        return {}
    return {str(k): {str(f): float(v) for f, v in dict(p).items()} for k, p in dict(data).items()}


def cost_micros(model: str, usage: StepUsage) -> int:
    table = _prices(get_settings().ai_prices).get(model)
    if table is None:
        if model != "fake":
            logger.warning("No AI price for model {}; cost reported as 0", model)
        return 0
    rate_in = table.get("in", 0.0)
    total = (
        usage.input_tokens * rate_in
        + usage.output_tokens * table.get("out", 0.0)
        + usage.cache_read_tokens * table.get("cache_read", rate_in)
        + usage.cache_write_tokens * table.get("cache_write", rate_in)
    )
    return round(total)


__all__ = ["cost_micros"]
