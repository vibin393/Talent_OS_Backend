"""Rate-card gate port (Epic 16, FR-91) for the Phase 1 proceed points.

Phase 1 modules (demands, pipeline, offers) must not import a Phase 2 domain (import contract
``p1-infra-below-p2`` is transitive), so they call this port; ``services/commercials/hooks.py`` registers the
implementations. Unregistered (e.g. a stripped-down test app) -> the gate passes.

Points: ``demand_submit(ctx, db, demand)``, ``submission_create(ctx, db, *, demand, application_id,
candidate_id, bill_rate, currency, rate_unit)``, ``offer_release(ctx, db, *, offer, version, demand)``.
Each raises 409 ``rate_exception_required|pending|rejected`` when the rate needs an approved exception.
"""

from collections.abc import Awaitable, Callable
from typing import Any

from app.core.registry import Registry

Gate = Callable[..., Awaitable[Any]]
_gates: Registry[str, Gate] = Registry("rate gate", allow_replace=True)
POINTS = ("demand_submit", "submission_create", "offer_release")


def register_rate_gate(point: str, fn: Gate) -> None:
    if point not in POINTS:
        msg = f"unknown rate gate point {point!r}"
        raise ValueError(msg)
    _gates.register(point, fn)


async def check(point: str, *args: Any, **kwargs: Any) -> Any:
    gate = _gates.find(point)
    if gate is None:
        return None
    return await gate(*args, **kwargs)
