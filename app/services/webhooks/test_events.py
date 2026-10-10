# ruff: noqa: E501
"""Test events (Epic 68, story 68.5): a signed sample of any subscribed type (``spec.example`` or a sample built from
the thin ``data_model``; random ids, never a real record), ``livemode=false``, ``TalentOS-Test: true``. Sent at once
(the result carries the response code and latency); 10 per minute per Subscription."""

import datetime as dt
import types
import typing
import uuid
from typing import Any

from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.context import RequestContext
from app.core.errors import RateLimited, ValidationFailed
from app.models.webhooks import WebhookDelivery
from app.schemas.webhooks import DeliveryOut, TestEventIn
from app.services.webhooks import common, log, signing

PER_MINUTE = 10


def _sample_value(name: str, annotation: Any) -> Any:
    origin = typing.get_origin(annotation)
    if origin in (typing.Union, types.UnionType):
        args = [a for a in typing.get_args(annotation) if a is not type(None)]
        return _sample_value(name, args[0]) if args else None
    if origin is list:
        return []
    if annotation is uuid.UUID:
        return str(uuid.uuid4())
    if annotation is dt.date:
        return dt.date.today().isoformat()
    if annotation is dt.datetime:
        return common.now().replace(microsecond=0).isoformat().replace("+00:00", "Z")
    if annotation is int:
        return 1
    if annotation is bool:
        return True
    return {"to_stage": "interview", "to_status": "open", "from_status": "draft", "status": "open", "outcome": "approved",
            "change": "granted", "number": "INV-0001", "subject_type": "candidate", "tool_key": "example.tool"}.get(name, "example")  # fmt: skip


def sample_data(model: type[BaseModel]) -> dict[str, Any]:
    return {name: _sample_value(name, f.annotation) for name, f in model.model_fields.items()}


def sample_envelope(event_type: str, tenant_id: uuid.UUID, slug: str) -> tuple[uuid.UUID, str, bytes]:
    spec = common.public_types()[event_type]
    data = dict(spec.example or {}) or sample_data(spec.data_model)
    event_id = uuid.uuid4()
    subject_type = spec.subject_type if spec.subject_type != "*" else str(data.get("subject_type") or "candidate")
    doc = {
        "id": str(event_id), "type": event_type, "version": spec.version, "api_version": "v1",
        "occurred_at": common.now().strftime("%Y-%m-%dT%H:%M:%SZ"), "sequence": 0,
        "tenant_ref": {"id": str(tenant_id), "slug": slug}, "livemode": False,
        "subject": {"type": subject_type, "id": str(uuid.uuid4())}, "data": data,
    }  # fmt: skip
    return event_id, subject_type, signing.canonical(doc)


async def send_test_event(ctx: RequestContext, db: AsyncSession, subscription_id: uuid.UUID, data: TestEventIn) -> DeliveryOut:
    from app.services import public_events_service
    from app.services.webhooks.scheduler import attempt

    sub = await common.load(ctx, db, subscription_id, lock=True)
    if data.event_type not in common.public_types() or not common.type_matches(sub, data.event_type):
        raise ValidationFailed("Pick one of the subscription's event types", code="invalid_event_types")
    since = common.now() - dt.timedelta(minutes=1)
    recent = (
        await db.execute(select(func.count()).select_from(WebhookDelivery).where(WebhookDelivery.subscription_id == sub.id, WebhookDelivery.kind == "test", WebhookDelivery.created_at >= since))
    ).scalar_one()
    if recent >= PER_MINUTE:
        raise RateLimited("At most 10 test events per minute", code="rate_limited")
    slug, _live = await public_events_service.tenant_ref(db, sub.tenant_id)
    event_id, subject_type, body = sample_envelope(data.event_type, sub.tenant_id, slug)
    at = common.now()
    row = WebhookDelivery(
        id=uuid.uuid4(), tenant_id=sub.tenant_id, created_at=at, updated_at=at, subscription_id=sub.id, event_id=event_id,
        event_type=data.event_type, subject_type=subject_type, kind="test", body=body, status="in_flight",
        created_by_user_id=common.user_id_of(ctx),
    )  # fmt: skip
    db.add(row)
    await db.flush()
    await attempt(ctx, db, row, sub)
    if row.status == "retrying":  # a test is one attempt; no ladder
        row.status, row.next_attempt_at = "failed", None
    audit.record("webhook.test_sent", "webhook_subscription", sub.id, session=db, event_type=data.event_type, status_code=row.last_status_code)
    await db.flush()
    return log.out(row)


__all__ = ["sample_data", "sample_envelope", "send_test_event"]
