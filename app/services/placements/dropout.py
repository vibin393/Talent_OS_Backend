"""Rule-based Dropout-risk flag (Epic 19 A6, FR-105). Pure functions -- **not AI** (no ``ai`` import; an
import-graph test enforces it).

Rules (tenant ``dropout_rules``):

* ``no_response``: at least ``no_response_touchpoints`` sent touchpoints are unanswered;
* ``low_onboarding``: within 7 days of the start, onboarding is below ``onboarding_pct_at_t7`` percent;
* ``counter_offer``: a counter-offer was recorded (when ``counter_offer`` is true).
"""

import dataclasses
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any


@dataclasses.dataclass(frozen=True, slots=True)
class TouchpointFact:
    sent: bool
    responded: bool


def evaluate(
    rules: Mapping[str, Any],
    touchpoints: Sequence[TouchpointFact],
    onboarding_pct: int | None,
    days_to_start: int,
    counter_offer_at: datetime | None,
) -> list[str]:
    fired: list[str] = []
    threshold = rules.get("no_response_touchpoints")
    if threshold:
        unanswered = sum(1 for t in touchpoints if t.sent and not t.responded)
        if unanswered >= int(threshold):
            fired.append("no_response")
    pct_rule = rules.get("onboarding_pct_at_t7")
    if (
        pct_rule is not None
        and onboarding_pct is not None
        and days_to_start <= 7
        and onboarding_pct < int(pct_rule)
    ):
        fired.append("low_onboarding")
    if rules.get("counter_offer", True) and counter_offer_at is not None:
        fired.append("counter_offer")
    return fired


def newly_fired(previous: Sequence[str], current: Sequence[str]) -> list[str]:
    """Rules firing now that did not fire before (the flag re-raises only on a new rule)."""
    return [r for r in current if r not in set(previous)]
