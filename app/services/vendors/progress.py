"""Vendor-facing progress (Epic 17 A7, FR-97): after-commit handlers denormalise pipeline, interview and
placement
events onto ``vendor_submissions`` (``progress_key`` / ``progress_label`` / ``next_interview_at`` /
``placement_id``) and the vendor-visible timeline. Idempotent; run as ``system`` in an internal session;
they only
ever update ``vendor_submissions`` / ``vendor_submission_events``.
"""

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.models.external_auth import VendorUser
from app.models.interviews import Interview
from app.models.vendors import VendorSubmission
from app.services.vendors.common import labels_of, load_settings, now
from app.services.vendors.submissions import add_timeline

NOTIFY_KEYS = frozenset({"shortlisted", "submitted", "interviewing", "offered", "hired"})


async def _by_application(db: AsyncSession, application_id: uuid.UUID) -> VendorSubmission | None:
    return (
        await db.execute(
            select(VendorSubmission)
            .where(VendorSubmission.application_id == application_id)
            .with_for_update()
        )
    ).scalar_one_or_none()


async def vendor_user_recipients(db: AsyncSession, vendor_id: uuid.UUID) -> list[uuid.UUID]:
    return list(
        (
            await db.execute(
                select(VendorUser.id).where(VendorUser.vendor_id == vendor_id, VendorUser.status == "active")
            )
        ).scalars()
    )


async def _notify(db: AsyncSession, sub: VendorSubmission, label: str) -> None:
    from app.services import notifications_service as ns

    recipients = [ns.Recipient("vendor_user", uid) for uid in await vendor_user_recipients(db, sub.vendor_id)]
    if not recipients:
        return
    await ns.deliver(
        db,
        "vendors.submission_progress",
        recipients,
        ns.Content(title=f"{sub.human_id}: {label}", link_path=f"/vendor/submissions/{sub.id}"),
        event_id=f"vsub-progress:{sub.id}:{label}",
    )


async def on_stage_changed(
    ctx: RequestContext,  # noqa: ARG001
    db: AsyncSession,
    application_id: uuid.UUID,
    to_key: str,
) -> bool:
    sub = await _by_application(db, application_id)
    if sub is None or sub.status != "accepted":
        return False
    labels = labels_of(await load_settings(db))
    label = labels.get(to_key)
    if label is None or (sub.progress_key == to_key and sub.progress_label == label):
        return False
    changed = sub.progress_label != label
    sub.progress_key, sub.progress_label, sub.progress_updated_at = to_key, label, now()
    if changed:
        add_timeline(db, sub, label)
    await db.flush()
    if changed and to_key in NOTIFY_KEYS:
        await _notify(db, sub, label)
    return True


async def on_interview_changed(ctx: RequestContext, db: AsyncSession, application_id: uuid.UUID) -> bool:  # noqa: ARG001
    """Recompute the next interview (date / time only; never interviewer names)."""
    sub = await _by_application(db, application_id)
    if sub is None:
        return False
    nxt = (
        await db.execute(
            select(func.min(Interview.starts_at)).where(
                Interview.application_id == application_id,
                Interview.status == "scheduled",
                Interview.starts_at > now(),
            )
        )
    ).scalar_one_or_none()
    if sub.next_interview_at == nxt:
        return False
    sub.next_interview_at = nxt
    await db.flush()
    return True


async def on_placement(
    ctx: RequestContext,  # noqa: ARG001
    db: AsyncSession,
    placement_id: uuid.UUID,
    *,
    dropped: bool = False,
) -> bool:
    from app.models.placements import Placement

    p = (await db.execute(select(Placement).where(Placement.id == placement_id))).scalar_one_or_none()
    if p is None or p.vendor_id is None or p.application_id is None:
        return False
    sub = await _by_application(db, p.application_id)
    if sub is None or sub.vendor_id != p.vendor_id:
        return False
    labels = labels_of(await load_settings(db))
    key = "rejected" if dropped or p.status == "dropped" else "placement"
    label = labels.get(key, "Placed" if key == "placement" else "Not selected")
    sub.placement_id = p.id
    if sub.progress_label != label:
        sub.progress_key, sub.progress_label, sub.progress_updated_at = key, label, now()
        add_timeline(db, sub, label)
        await db.flush()
        if key == "placement":
            await _notify(db, sub, label)
    await db.flush()
    return True
