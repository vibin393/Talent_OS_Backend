"""Domain event dataclasses, one module per domain (AD-1 / AD-9).

``subscribe_optional(module_path, class_name, handler)`` (Epic 10) subscribes ``handler`` to an event class
that may not exist yet (an upstream epic not merged): a missing module or class logs ONE warning and the
subscription is skipped, so consumers can land before their producers.
"""

import importlib
from collections.abc import Callable
from typing import Any

from loguru import logger

_warned: set[tuple[str, str]] = set()


def resolve_event_class(module_path: str, class_name: str) -> type | None:
    """Return the event class, or None when its module / class is not available (no warning)."""
    try:
        module = importlib.import_module(module_path)
    except ImportError:
        return None
    cls = getattr(module, class_name, None)
    return cls if isinstance(cls, type) else None


def subscribe_optional(module_path: str, class_name: str, handler: Callable[[Any], None]) -> bool:
    """Subscribe ``handler`` to ``module_path.class_name`` if it exists; returns whether it did."""
    from app.core.events import subscribe

    cls = resolve_event_class(module_path, class_name)
    if cls is None:
        key = (module_path, class_name)
        if key not in _warned:
            _warned.add(key)
            logger.warning("Event {}.{} not available yet; subscription skipped", module_path, class_name)
        return False
    subscribe(cls, handler)
    return True
