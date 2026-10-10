"""``fx`` rate source port (AD-29, Epic 36 contract 0.8; owned by Epic 51). ``manual`` returns no rates
(tenant-entered ``fx_rates`` stay authoritative); ``fake`` returns deterministic rates. Every adapter also
implements the generic ``ConnectorPort``. Signatures are frozen.
"""

import dataclasses
from collections.abc import Sequence
from datetime import date
from decimal import Decimal
from typing import Protocol, runtime_checkable

__all__ = ["FxRateDTO", "FxRateSource"]


@dataclasses.dataclass(frozen=True, slots=True)
class FxRateDTO:
    from_ccy: str
    to_ccy: str
    rate: Decimal  # 6 dp
    rate_date: date
    source: str


@runtime_checkable
class FxRateSource(Protocol):
    key: str

    async def fetch_rates(self, on: date, pairs: Sequence[tuple[str, str]]) -> Sequence[FxRateDTO]: ...
