"""Test data helpers (real rows, so tenant_id foreign keys are satisfied)."""

import contextlib
import uuid
from collections.abc import Iterator
from types import ModuleType

from app.core.database import platform_session
from app.models.identity import Tenant


async def create_tenant_row(name: str = "Test Tenant") -> uuid.UUID:
    """Insert a bare tenant (platform table) and return its id."""
    async with platform_session() as db:
        tenant = Tenant(
            name=name,
            slug=f"t-{uuid.uuid4().hex[:16]}",
            timezone="Asia/Kolkata",
            currency="INR",
            locale="en-IN",
            seat_limit=10,
        )
        db.add(tenant)
        await db.flush()
        return tenant.id


@contextlib.contextmanager
def deferred_enqueues(*handler_modules: ModuleType) -> Iterator[None]:
    """Hold the after-commit Celery enqueues of ``handler_modules`` (each exposes ``_enqueue``, or is
    ``app.tasks.runtime`` with its shared ``enqueue`` used by the Phase 2 hooks) until the block ends, then
    run them.

    For tests that race concurrent requests: in eager Celery mode an enqueued task runs in a helper thread
    that the event loop joins, so it can wait on a row lock held by the other, then stalled, request (a
    deadlock no real worker can cause). Deferring keeps the race itself intact and makes it deterministic.
    """
    deferred: list[tuple[ModuleType, object, tuple[object, ...], dict[str, object]]] = []
    attrs = {m: "_enqueue" if hasattr(m, "_enqueue") else "enqueue" for m in handler_modules}
    originals = {m: getattr(m, attrs[m]) for m in handler_modules}
    for module in handler_modules:
        setattr(
            module,
            attrs[module],
            lambda target, *args, _m=module, **kwargs: deferred.append((_m, target, args, kwargs)),
        )
    try:
        yield
    finally:
        for module, original in originals.items():
            setattr(module, attrs[module], original)
    for module, target, args, kwargs in deferred:
        originals[module](target, *args, **kwargs)
