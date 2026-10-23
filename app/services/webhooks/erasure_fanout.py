# ruff: file-ignore[line-too-long]
"""The ``subject.erased`` fan-out (Epic 68, D-11, §0.6 step 3). Every Installation that received the subject
(``subject_exposures``) gets the thin event, WHATEVER its scopes: each active Subscription of the Installation that
subscribes to ``subject.erased`` or ``*``; else its oldest active Subscription; with none, the audit records
``webhook.erasure_no_endpoint`` for the 79 DSR listing. Delivery is logged; compliance cannot be enforced."""

from collections.abc import Sequence
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.context import RequestContext
from app.models.webhooks import WebhookSubscription

ERASED = "subject.erased"


def targets(installation_id: Any, subs: Sequence[WebhookSubscription]) -> list[WebhookSubscription]:
    own = sorted(
        (
            s
            for s in subs
            if s.installation_id == installation_id and s.status == "active" and s.deleted_at is None
        ),
        key=lambda s: (s.created_at, str(s.id)),
    )
    chosen = [s for s in own if s.all_events or ERASED in (s.event_types or [])]
    if not chosen and own:
        chosen = [own[0]]
    return chosen


async def fan_out(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument] - sink signature
    db: AsyncSession,
    row: Any,
    subs: Sequence[WebhookSubscription],
) -> int:
    from app.services import exposure_service
    from app.services.webhooks.sink import insert_pair

    created = 0
    for installation_id in await exposure_service.installations_exposed(db, row.subject_type, row.subject_id):
        chosen = targets(installation_id, subs)
        if not chosen:
            audit.record(
                "webhook.erasure_no_endpoint", "app_installation", installation_id, session=db,
                installation_id=str(installation_id), subject_type=row.subject_type, event_id=str(row.id),
            )  # fmt: skip
            continue
        for sub in chosen:
            if await insert_pair(db, sub, row) is not None:
                created += 1
    return created


__all__ = ["fan_out", "targets"]
