# ruff: file-ignore[line-too-long]
"""Webhooks for tenant users (Epic 68, spec A4): ``/api/v1/webhooks/*``. Reads need ``webhook:manage`` or
``integration:view``; every mutation needs ``webhook:manage`` and a person (403 ``human_only_action`` for API keys and
automated actors, FR-413)."""

import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Response, status

from app.core.actors import actor_from_ctx, require_human
from app.core.context import get_context
from app.core.permissions import Principal, require, require_any
from app.dependencies.auth import TenantDBSessionDep
from app.schemas.public_events import PublicPage
from app.schemas.webhooks import (
    DeliveryDetailOut,
    DeliveryOut,
    DeliveryQuery,
    DeliveryStatus,
    EchoEndpointCreate,
    EchoEndpointCreated,
    EchoEndpointOut,
    EchoReceiptOut,
    EventTypeOut,
    ReplayRangeIn,
    ReplayRangeOut,
    SecretRotated,
    SubscriptionCreate,
    SubscriptionCreated,
    SubscriptionOut,
    SubscriptionQuery,
    SubscriptionStatus,
    SubscriptionUpdate,
    TestEventIn,
)
from app.services import webhooks_service as svc

router = APIRouter(prefix="/webhooks", tags=["webhooks"])
DB = TenantDBSessionDep
View = Annotated[Principal, Depends(require_any("webhook:manage", "integration:view"))]
Manage = Annotated[Principal, Depends(require("webhook:manage"))]


def _human() -> None:
    require_human(actor_from_ctx(get_context()))


@router.get("/subscriptions", response_model=PublicPage[SubscriptionOut])
async def list_subscriptions(
    _: View, db: DB, status_: Annotated[SubscriptionStatus | None, Query(alias="status")] = None,
    installation_id: uuid.UUID | None = None, limit: Annotated[int, Query(ge=1, le=200)] = 50, cursor: str | None = None,
) -> Any:  # fmt: skip
    return await svc.list_subscriptions(get_context(), db, SubscriptionQuery(status=status_, installation_id=installation_id, limit=limit, cursor=cursor))


@router.post("/subscriptions", response_model=SubscriptionCreated, status_code=status.HTTP_201_CREATED)
async def create_subscription(body: SubscriptionCreate, _: Manage, db: DB) -> Any:
    _human()
    return await svc.create_subscription(get_context(), db, body)


@router.get("/subscriptions/{subscription_id}", response_model=SubscriptionOut)
async def get_subscription(subscription_id: uuid.UUID, _: View, db: DB) -> Any:
    return await svc.get_subscription(get_context(), db, subscription_id)


@router.patch("/subscriptions/{subscription_id}", response_model=SubscriptionOut)
async def update_subscription(subscription_id: uuid.UUID, body: SubscriptionUpdate, _: Manage, db: DB) -> Any:
    _human()
    return await svc.update_subscription(get_context(), db, subscription_id, body)


@router.delete("/subscriptions/{subscription_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_subscription(subscription_id: uuid.UUID, _: Manage, db: DB) -> Response:
    _human()
    await svc.delete_subscription(get_context(), db, subscription_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/subscriptions/{subscription_id}/enable", response_model=SubscriptionOut)
async def enable(subscription_id: uuid.UUID, _: Manage, db: DB) -> Any:
    _human()
    return await svc.set_enabled(get_context(), db, subscription_id, enabled=True)


@router.post("/subscriptions/{subscription_id}/disable", response_model=SubscriptionOut)
async def disable(subscription_id: uuid.UUID, _: Manage, db: DB) -> Any:
    _human()
    return await svc.set_enabled(get_context(), db, subscription_id, enabled=False)


@router.post("/subscriptions/{subscription_id}/verify", response_model=SubscriptionOut)
async def verify(subscription_id: uuid.UUID, _: Manage, db: DB) -> Any:
    _human()
    return await svc.verify_now(get_context(), db, subscription_id)


@router.post("/subscriptions/{subscription_id}/rotate-secret", response_model=SecretRotated)
async def rotate_secret(subscription_id: uuid.UUID, _: Manage, db: DB) -> Any:
    _human()
    return await svc.rotate_secret(get_context(), db, subscription_id)


@router.post("/subscriptions/{subscription_id}/test", response_model=DeliveryOut)
async def send_test(subscription_id: uuid.UUID, body: TestEventIn, _: Manage, db: DB) -> Any:
    _human()
    return await svc.send_test_event(get_context(), db, subscription_id, body)


@router.get("/subscriptions/{subscription_id}/deliveries", response_model=PublicPage[DeliveryOut])
async def list_deliveries(
    subscription_id: uuid.UUID, _: View, db: DB, status_: Annotated[list[DeliveryStatus] | None, Query(alias="status")] = None,
    event_type: str | None = None, date_from: Annotated[datetime | None, Query(alias="from")] = None,
    date_to: Annotated[datetime | None, Query(alias="to")] = None, limit: Annotated[int, Query(ge=1, le=200)] = 50,
    cursor: str | None = None,
) -> Any:  # fmt: skip
    q = DeliveryQuery(status=status_, event_type=event_type, date_from=date_from, date_to=date_to, limit=limit, cursor=cursor)
    return await svc.list_deliveries(get_context(), db, subscription_id, q)


@router.post("/subscriptions/{subscription_id}/replay", response_model=ReplayRangeOut)
async def replay_range(subscription_id: uuid.UUID, body: ReplayRangeIn, _: Manage, db: DB) -> Any:
    _human()
    return await svc.replay_range(get_context(), db, subscription_id, body)


@router.get("/deliveries/export")
async def export_deliveries(
    _: Manage, db: DB, subscription_id: uuid.UUID | None = None, date_from: Annotated[datetime | None, Query(alias="from")] = None,
    date_to: Annotated[datetime | None, Query(alias="to")] = None,
) -> Response:  # fmt: skip
    csv_text = await svc.export_csv(get_context(), db, subscription_id, date_from, date_to)
    return Response(content=csv_text, media_type="text/csv", headers={"Content-Disposition": 'attachment; filename="webhook_deliveries.csv"'})


@router.get("/deliveries/{delivery_id}", response_model=DeliveryDetailOut)
async def get_delivery(delivery_id: uuid.UUID, _: View, db: DB) -> Any:
    return await svc.get_delivery(get_context(), db, delivery_id)


@router.post("/deliveries/{delivery_id}/replay", response_model=DeliveryOut)
async def replay_delivery(delivery_id: uuid.UUID, _: Manage, db: DB) -> Any:
    _human()
    return await svc.replay_delivery(get_context(), db, delivery_id)


@router.get("/event-types", response_model=list[EventTypeOut], response_model_by_alias=True)
async def event_types(_: Manage, db: DB, installation_id: uuid.UUID | None = None) -> Any:
    return await svc.allowed_event_types(get_context(), db, installation_id=installation_id)


@router.post("/echo-endpoints", response_model=EchoEndpointCreated, status_code=status.HTTP_201_CREATED)
async def create_echo(body: EchoEndpointCreate, _: Manage, db: DB) -> Any:
    _human()
    return await svc.create_echo_endpoint(get_context(), db, body.label)


@router.get("/echo-endpoints", response_model=list[EchoEndpointOut])
async def list_echo(_: Manage, db: DB) -> Any:
    return await svc.list_echo_endpoints(get_context(), db)


@router.get("/echo-endpoints/{endpoint_id}/receipts", response_model=list[EchoReceiptOut])
async def echo_receipts(endpoint_id: uuid.UUID, _: Manage, db: DB) -> Any:
    return await svc.list_echo_receipts(get_context(), db, endpoint_id)
