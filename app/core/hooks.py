"""``hooks.py`` autodiscovery (AD-31).

Every Phase 2 domain registers its triggers, actions, notification types, approval subjects / resolvers,
posting sources, close checks, connector adapters, ... from ``app/services/<domain>/hooks.py``.
``autodiscover()`` imports each module whose last dotted segment is ``hooks`` exactly once per process. It
is called when the API app is created and from the Celery ``worker_init`` / ``beat_init`` signals.

The package tree is walked on the file system (``pkgutil.iter_modules`` per directory), so only packages
that actually contain a ``hooks`` module get imported. ``core`` never imports a service module statically.
"""

import importlib
import pkgutil
import threading
from collections.abc import Callable
from pathlib import Path

from loguru import logger

_lock = threading.Lock()
_done: dict[str, list[str]] = {}
# Epic 53: callbacks run once after the first full ``autodiscover("app.services")`` (e.g. the agent tool catalogue
# validation). A callback may raise to fail startup.
_post_discovery: list[Callable[[], None]] = []


def _hook_modules(package: str) -> list[str]:
    root = importlib.import_module(package)
    found: list[str] = []
    stack: list[tuple[str, list[str]]] = [(package, list(getattr(root, "__path__", [])))]
    while stack:
        prefix, paths = stack.pop()
        for info in pkgutil.iter_modules(paths):
            name = f"{prefix}.{info.name}"
            if info.ispkg:
                stack.append(
                    (name, [str(Path(p) / info.name) for p in paths if (Path(p) / info.name).is_dir()])
                )
            elif info.name == "hooks":
                found.append(name)
    return sorted(found)


def autodiscover(package: str = "app.services") -> list[str]:
    """Import every ``<package>.**.hooks`` module once (guarded); returns the module names."""
    with _lock:
        if package in _done:
            return list(_done[package])
        modules = _hook_modules(package)
        for name in modules:
            importlib.import_module(name)
        _done[package] = modules
        callbacks = list(_post_discovery) if package == "app.services" else []
    for fn in callbacks:
        fn()
    logger.debug("Autodiscovered {} hooks modules under {}", len(modules), package)
    return list(modules)


def register_post_discovery(fn: Callable[[], None]) -> None:
    """Run ``fn`` after hooks autodiscovery of ``app.services`` (immediately if it already ran)."""
    if fn in _post_discovery:
        return
    _post_discovery.append(fn)
    if "app.services" in _done:
        fn()


def discovered(package: str = "app.services") -> list[str]:
    return list(_done.get(package, []))
