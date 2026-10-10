"""Reconciliation check registry (R3, NFR-25). Registry only: the runner, tables and alerts are Epic 30's;
Epic 29 registers its ``billing.*`` checks here."""

import dataclasses
import uuid
from collections.abc import Awaitable, Callable, Mapping
from datetime import date
from typing import Any, Literal

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.registry import Registry


@dataclasses.dataclass(frozen=True)
class Finding:
    check: str
    severity: Literal["error", "warning"]
    subject_kind: str
    subject_id: uuid.UUID | None
    details: Mapping[str, Any]


ReconciliationCheck = Callable[[RequestContext, AsyncSession, date], Awaitable[list[Finding]]]

_checks: Registry[str, ReconciliationCheck] = Registry("reconciliation check")


def register_reconciliation_check(name: str, fn: ReconciliationCheck) -> None:
    _checks.register(name, fn)


def reconciliation_checks() -> Mapping[str, ReconciliationCheck]:
    return _checks.view()
