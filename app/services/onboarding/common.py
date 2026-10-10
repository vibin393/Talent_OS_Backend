# ruff: noqa: E501
"""Shared helpers of the onboarding domain (Epic 23)."""

import re
import uuid
from collections.abc import Iterable, Mapping
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.errors import NotFound, ValidationFailed
from app.models.onboarding import OnboardingCase, OnboardingSettings, OnboardingTask

READ = "onboarding:read"
MANAGE = "onboarding:manage"
WAIVE = "onboarding:waive"
CONFIGURE = "onboarding:configure"

OPEN_TASK_STATUSES = ("pending", "submitted", "returned")
DONE_TASK_STATUSES = ("approved", "waived")
WORKER_OPEN = ("pending", "returned")
TERMINAL_CASE = ("complete", "cancelled")

# Keys that must never appear in onboarding ``data`` / ``draft`` (AD-35: placements / compliance own them).
_FORBIDDEN_KEY_RE = re.compile(r"^(account_number.*|pan|pan_number|aadhaar.*|passport.*)$", re.IGNORECASE)
_AADHAAR_RE = re.compile(r"(?<!\d)(\d{4}[\s-]?\d{4}[\s-]?\d{4})(?!\d)")


def now() -> datetime:
    return datetime.now(UTC)


def today() -> date:
    from app.core.fiscal import today_for

    return today_for("Asia/Kolkata")


def tenant_of(db: AsyncSession) -> uuid.UUID:
    return uuid.UUID(str(db.sync_session.info["tenant_id"]))


def as_uuid(value: Any) -> uuid.UUID | None:
    if value is None or value == "":
        return None
    try:
        return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None


def user_id(ctx: RequestContext) -> uuid.UUID | None:
    p = ctx.principal
    if p is not None and getattr(p, "type", "user") == "user":
        return getattr(p, "id", None)
    return None


def assert_no_full_aadhaar(payload: Any) -> None:
    """422 when a payload carries a full 12-digit Aadhaar number (same rule as Epic 21's
    ``core.identity_docs``, applied locally so this epic does not depend on its merge order)."""
    for text in _strings(payload):
        if _AADHAAR_RE.search(text):
            raise ValidationFailed(
                "Do not enter a full Aadhaar number -- only the last 4 digits",
                code="full_aadhaar_not_allowed",
            )


def _strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for k, v in value.items():
            if str(k).startswith("account_number"):  # bank account numbers are validated by placements
                continue
            yield from _strings(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            yield from _strings(v)


def assert_no_sensitive_keys(data: Mapping[str, Any] | None) -> None:
    """Defence in depth (AD-35): stored ``data`` / ``draft`` never carries account / PAN / identity numbers."""
    for key in _keys(data or {}):
        if _FORBIDDEN_KEY_RE.match(key):
            msg = f"onboarding must not store {key!r}"
            raise ValueError(msg)


def _keys(value: Any) -> Iterable[str]:
    if isinstance(value, Mapping):
        for k, v in value.items():
            yield str(k)
            yield from _keys(v)
    elif isinstance(value, list):
        for v in value:
            yield from _keys(v)


def scrub_draft(draft: Mapping[str, Any]) -> dict[str, Any]:
    """Drafts keep partial progress but never the sensitive numbers (they are re-entered on submit)."""
    return {k: v for k, v in draft.items() if not _FORBIDDEN_KEY_RE.match(str(k))}


async def get_settings_row(db: AsyncSession) -> OnboardingSettings:
    row = (await db.execute(select(OnboardingSettings))).scalar_one_or_none()
    if row is None:
        row = OnboardingSettings(tenant_id=tenant_of(db), default_owner_by_role={})
        async with db.begin_nested():
            db.add(row)
            await db.flush()
    return row


async def load_case(db: AsyncSession, case_id: uuid.UUID, *, for_update: bool = False) -> OnboardingCase:
    stmt = select(OnboardingCase).where(
        OnboardingCase.id == case_id, OnboardingCase.initialized_at.is_not(None)
    )
    if for_update:
        stmt = stmt.with_for_update()
    row = (await db.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise NotFound("Onboarding case not found")
    return row


async def load_task(db: AsyncSession, task_id: uuid.UUID, *, for_update: bool = False) -> OnboardingTask:
    stmt = select(OnboardingTask).where(OnboardingTask.id == task_id)
    if for_update:
        stmt = stmt.with_for_update()
    row = (await db.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise NotFound("Onboarding task not found")
    return row


async def case_tasks(db: AsyncSession, case_id: uuid.UUID) -> list[OnboardingTask]:
    return list(
        (
            await db.execute(
                select(OnboardingTask)
                .where(OnboardingTask.case_id == case_id)
                .order_by(OnboardingTask.position, OnboardingTask.created_at)
            )
        ).scalars()
    )


def is_overdue(task: OnboardingTask, on: date | None = None) -> bool:
    return task.status in WORKER_OPEN and task.due_on is not None and task.due_on < (on or today())
