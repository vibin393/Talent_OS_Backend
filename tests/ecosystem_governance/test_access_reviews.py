# ruff: noqa: E501
"""Epic 79 story 79.2 (FR-406): quarterly access reviews -- cadence open, stale flag, decide, complete, auto-revoke."""

import datetime as dt
import os
import uuid

from sqlalchemy import select, text

from app.core.database import tenant_session
from app.models.ecosystem_governance import AccessReview, AccessReviewItem
from app.services.ecosystem_governance import access_reviews as ar
from tests.factories import create_tenant_row


async def _seed_api_key(tenant_id: uuid.UUID, *, last_used_days_ago: int | None) -> uuid.UUID:
    key_id = uuid.uuid4()
    last_used = (
        None if last_used_days_ago is None else dt.datetime.now(dt.UTC) - dt.timedelta(days=last_used_days_ago)
    )
    async with tenant_session(tenant_id) as db:
        await db.execute(
            text(
                "INSERT INTO api_keys (id, tenant_id, name, prefix, key_hash, scopes, rate_limit_per_min, "
                "last_used_at, created_at, updated_at) VALUES (:id, :tid, :name, :prefix, :kh, "
                "CAST(:scopes AS varchar[]), 600, CAST(:last AS timestamptz), now(), now())"
            ),
            {
                "id": key_id,
                "tid": tenant_id,
                "name": "Nightly export key",
                "prefix": "tos_ab12",
                "kh": os.urandom(32),
                "scopes": ["candidates:read"],
                "last": last_used,
            },
        )
        await db.commit()
    return key_id


async def test_open_review_flags_stale_api_key_and_decisions_flow() -> None:
    tenant_id = await create_tenant_row("Access review tenant")
    await _seed_api_key(tenant_id, last_used_days_ago=200)  # older than the 90-day default -> stale

    async with tenant_session(tenant_id) as db:
        review = await ar.open_review(db, tenant_id)
        await db.commit()
        review_id = review.id
        assert review.item_count == 1

    async with tenant_session(tenant_id) as db:
        items = await ar.list_items(db, tenant_id, review_id)
        assert len(items) == 1
        item = items[0]
        assert item.kind == "api_key" and item.stale is True
        assert "last_used_at" in item.snapshot
        await ar.decide_item(db, tenant_id, item.id, decision="confirmed", decided_by=None, note="Still needed")
        await db.commit()

    async with tenant_session(tenant_id) as db:
        reloaded = (await db.execute(select(AccessReview).where(AccessReview.id == review_id))).scalar_one()
        assert reloaded.decided_count == 1
        await ar.complete_review(db, tenant_id, review_id, completed_by=None)
        await db.commit()
        reloaded = (await db.execute(select(AccessReview).where(AccessReview.id == review_id))).scalar_one()
        assert reloaded.status == "completed" and reloaded.completed_at is not None


async def test_open_review_is_idempotent_while_open() -> None:
    tenant_id = await create_tenant_row("Idempotent review tenant")
    async with tenant_session(tenant_id) as db:
        first = await ar.open_review(db, tenant_id)
        await db.commit()
    async with tenant_session(tenant_id) as db:
        second = await ar.open_review(db, tenant_id)
        await db.commit()
    assert first.id == second.id


async def test_auto_revoke_marks_stale_undecided_items_after_14_days() -> None:
    tenant_id = await create_tenant_row("Auto-revoke tenant")
    await _seed_api_key(tenant_id, last_used_days_ago=None)  # never used -> stale
    async with tenant_session(tenant_id) as db:
        await ar.update_settings(
            db, tenant_id, cadence_months=3, stale_after_days=90, auto_revoke_stale=True,
            reviewer_role_key=None, updated_by=None,
        )
        review = await ar.open_review(db, tenant_id)
        # Backdate the review so it is older than the 14-day auto-revoke window.
        review.opened_at = dt.datetime.now(dt.UTC) - dt.timedelta(days=20)
        await db.commit()
        review_id = review.id

    async with tenant_session(tenant_id) as db:
        revoked = await ar.auto_revoke_due(db, tenant_id)
        await db.commit()
        assert len(revoked) == 1
        item = (
            await db.execute(select(AccessReviewItem).where(AccessReviewItem.review_id == review_id))
        ).scalar_one()
        assert item.decision == "auto_revoked"


async def test_auto_revoke_noop_when_disabled() -> None:
    tenant_id = await create_tenant_row("Auto-revoke off tenant")
    await _seed_api_key(tenant_id, last_used_days_ago=None)
    async with tenant_session(tenant_id) as db:
        await ar.open_review(db, tenant_id)
        await db.commit()
    async with tenant_session(tenant_id) as db:
        assert await ar.auto_revoke_due(db, tenant_id) == []
