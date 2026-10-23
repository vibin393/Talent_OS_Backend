# ruff: file-ignore[line-too-long]
"""``SecretBag``: the lazy, logged view of one owner's secrets (Epic 66, spec 0.11, NFR-63).

* ``get(name)`` decrypts on demand, adds one ``vault_access_log`` row (caller = the calling module, purpose, actor
  kind) and registers the value with ``core.logger`` so it is masked in later log lines of this context.
* ``repr`` / ``str`` print ``SecretBag(****)``; pickling, copying and pydantic serialisation raise (like
  ``core.encrypted.Sealed``), so a bag cannot leak into a task payload, an event or an API response.
"""

import json
import sys
import uuid
from collections.abc import Mapping
from typing import Any

from pydantic_core import core_schema
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logger import register_secret_value
from app.models.vault import VaultSecret
from app.services.vault import store

_MIN_LEAF = 8


def register_value(value: str) -> None:
    register_secret_value(value)
    if value[:1] in "{[":  # JSON credentials: mask their string leaves too
        try:
            doc = json.loads(value)
        except ValueError:
            return
        stack: list[Any] = [doc]
        while stack:
            item = stack.pop()
            if isinstance(item, Mapping):
                stack.extend(item.values())
            elif isinstance(item, list):
                stack.extend(item)
            elif isinstance(item, str) and len(item) >= _MIN_LEAF:
                register_secret_value(item)


class SecretBag:
    """Secrets of one owner. ``get(name) -> str | None``; never printed, pickled or serialised."""

    __slots__ = ("_actor_kind", "_db", "_deks", "_purpose", "_rows", "_tenant_id")

    def __init__(
        self,
        db: AsyncSession,
        tenant_id: uuid.UUID,
        rows: Mapping[str, VaultSecret],
        deks: Mapping[int, bytes],
        *,
        purpose: str,
        actor_kind: str,
    ) -> None:
        self._db = db
        self._tenant_id = tenant_id
        self._rows = dict(rows)
        self._deks = dict(deks)
        self._purpose = purpose
        self._actor_kind = actor_kind

    def names(self) -> frozenset[str]:
        """Names of the live secrets (no access is logged)."""
        return frozenset(n for n, r in self._rows.items() if store.is_live(r))

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and name in self.names()

    def get(self, name: str) -> str | None:
        row = self._rows.get(name)
        if row is None or not store.is_live(row):
            return None
        dek = self._deks.get(row.dek_version)
        if dek is None:
            msg = f"tenant data key v{row.dek_version} is missing"
            raise LookupError(msg)
        value = store.decrypt_row(self._tenant_id, row, dek).decode("utf-8")
        frame = sys._getframe(1)
        store.log_access(
            self._db,
            self._tenant_id,
            row,
            purpose=self._purpose,
            caller=str(frame.f_globals.get("__name__", "unknown")),
            actor_kind=self._actor_kind,
        )
        register_value(value)
        return value

    def __repr__(self) -> str:
        return "SecretBag(****)"

    __str__ = __repr__

    def __reduce__(self) -> Any:
        msg = "SecretBag is never pickled"
        raise TypeError(msg)

    def __reduce_ex__(self, protocol: Any) -> Any:
        return self.__reduce__()

    def __copy__(self) -> "SecretBag":
        msg = "SecretBag is never copied"
        raise TypeError(msg)

    def __deepcopy__(self, memo: Any) -> "SecretBag":
        return self.__copy__()

    @classmethod
    def __get_pydantic_core_schema__(cls, source: Any, handler: Any) -> core_schema.CoreSchema:
        def _refuse(value: Any) -> Any:  # ruff: ignore[unused-function-argument]
            msg = "SecretBag values are never serialised"
            raise TypeError(msg)

        return core_schema.is_instance_schema(
            cls, serialization=core_schema.plain_serializer_function_ser_schema(_refuse)
        )
