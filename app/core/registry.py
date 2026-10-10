"""Generic keyed registries (AD-31). Phase 2 domains fill them from ``services/<domain>/hooks.py``.

``view()`` is a live read-only mapping, used for the Phase 1 names that used to be plain dicts
(``TRIGGERS``, ``ACTIONS``, ``NOTIFICATION_TYPES``, ``FIELD_REGISTRY``). ``override`` swaps an entry for the
duration of a ``with`` block (tests only).
"""

import contextlib
from collections.abc import Iterator, Mapping
from types import MappingProxyType

from loguru import logger

_MISSING = object()


class RegistryError(KeyError):
    """Duplicate registration or unknown key."""

    def __str__(self) -> str:
        return str(self.args[0]) if self.args else "registry error"


class Registry[K, V]:
    def __init__(self, name: str, *, allow_replace: bool = False) -> None:
        self.name = name
        self.allow_replace = allow_replace
        self._items: dict[K, V] = {}
        self._view: Mapping[K, V] = MappingProxyType(self._items)

    def register(self, key: K, value: V) -> None:
        if key in self._items:
            if not self.allow_replace:
                msg = f"duplicate {self.name} {key!r}"
                raise RegistryError(msg)
            logger.debug("Replacing {} {!r}", self.name, key)
        self._items[key] = value

    def unregister(self, key: K) -> None:
        self._items.pop(key, None)

    def get(self, key: K) -> V:
        try:
            return self._items[key]
        except KeyError as exc:
            msg = f"unknown {self.name} {key!r}"
            raise RegistryError(msg) from exc

    def find(self, key: K) -> V | None:
        return self._items.get(key)

    def __contains__(self, key: object) -> bool:
        return key in self._items

    def view(self) -> Mapping[K, V]:
        return self._view

    @contextlib.contextmanager
    def override(self, key: K, value: V) -> Iterator[None]:
        previous = self._items.get(key, _MISSING)  # type: ignore[arg-type]
        self._items[key] = value
        try:
            yield
        finally:
            if previous is _MISSING:
                self._items.pop(key, None)
            else:
                self._items[key] = previous  # type: ignore[assignment]
