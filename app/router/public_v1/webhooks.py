# ruff: noqa: E501
"""``/public/v1/webhooks`` (Epic 68, spec A4): an App's OWN Subscriptions (scope ``webhooks:manage``, app tokens only --
API keys get 403 ``insufficient_scope``, D-24). Another Installation's or a tenant-owned Subscription -> 404."""

import uuid
from typing import Annotated, Any

from fastapi import Query, Request, Response, status

from app.core.context import get_context
from app.core.public_api import public_router
from app.dependencies.public import PublicDBSessionDep
from app.router.public_v1._common import Idem, guarded_update, scope
from app.schemas.public_events import PublicPage
from app.schemas.public_v1.webhooks import (
    DeliveryOut,
    PublicSubscriptionCreate,
    SecretRotated,
    SubscriptionCreated,
    SubscriptionOut,
    SubscriptionUpdate,
    TestEventIn,
)
from app.schemas.webhooks import DeliveryQuery, SubscriptionCreate, SubscriptionQuery
from app.services import webhooks_service as svc

router = public_router("webhooks")
Manage = scope("webhooks:manage")


@router.get("/webhooks", response_model=PublicPage[SubscriptionOut])
async def list_webhooks(
    _: Manage, db: PublicDBSessionDep, limit: Annotated[int, Query(ge=1, le=200)] = 50, cursor: Annotated[str | None, Query(max_length=1000)] = None,
) -> Any:  # fmt: skip
    return await svc.list_subscriptions(get_context(), db, SubscriptionQuery(limit=limit, cursor=cursor))


@router.post("/webhooks", response_model=SubscriptionCreated, status_code=status.HTTP_201_CREATED)
async def create_webhook(body: PublicSubscriptionCreate, _: Manage, db: PublicDBSessionDep) -> Any:
    # Not replayed through the Idempotency-Key store: the response carries the show-once signing secret, which is
    # never persisted outside the vault (NFR-63). A retried create makes a second Subscription (limits apply).
    data = SubscriptionCreate.model_validate(body.model_dump(exclude={"installation_id"}))
    return await svc.create_subscription(get_context(), db, data)


@router.get("/webhooks/{subscription_id}", response_model=SubscriptionOut)
async def get_webhook(subscription_id: uuid.UUID, _: Manage, db: PublicDBSessionDep, response: Response) -> Any:
    out = await svc.get_subscription(get_context(), db, subscription_id)
    response.headers["ETag"] = f'W/"{out.version}"'
    return out


@router.patch("/webhooks/{subscription_id}", response_model=SubscriptionOut)
async def update_webhook(subscription_id: uuid.UUID, body: SubscriptionUpdate, request: Request, _: Manage, db: PublicDBSessionDep) -> Any:
    async def current() -> str:
        return (await svc.get_subscription(get_context(), db, subscription_id)).version

    async def apply() -> dict[str, Any]:
        return (await svc.update_subscription(get_context(), db, subscription_id, body)).model_dump(mode="json")

    return await guarded_update(request, current, apply)


@router.delete("/webhooks/{subscription_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_webhook(subscription_id: uuid.UUID, _: Manage, db: PublicDBSessionDep) -> Response:
    await svc.delete_subscription(get_context(), db, subscription_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/webhooks/{subscription_id}/rotate_secret", response_model=SecretRotated)
async def rotate_secret(subscription_id: uuid.UUID, _: Manage, db: PublicDBSessionDep) -> Any:
    # Show-once secret: never stored in the Idempotency-Key response store (NFR-63).
    return await svc.rotate_secret(get_context(), db, subscription_id)


@router.post("/webhooks/{subscription_id}/test", response_model=DeliveryOut)
async def send_test(subscription_id: uuid.UUID, body: TestEventIn, _: Manage, db: PublicDBSessionDep, idem: Idem) -> Any:
    async def go() -> dict[str, Any]:
        return (await svc.send_test_event(get_context(), db, subscription_id, body)).model_dump(mode="json")

    return await idem.run(go)


@router.get("/webhooks/{subscription_id}/deliveries", response_model=PublicPage[DeliveryOut])
async def list_deliveries(
    subscription_id: uuid.UUID, _: Manage, db: PublicDBSessionDep, limit: Annotated[int, Query(ge=1, le=200)] = 50,
    cursor: Annotated[str | None, Query(max_length=1000)] = None,
) -> Any:  # fmt: skip
    return await svc.list_deliveries(get_context(), db, subscription_id, DeliveryQuery(limit=limit, cursor=cursor))


@router.post("/webhooks/deliveries/{delivery_id}/replay", response_model=DeliveryOut)
async def replay_delivery(delivery_id: uuid.UUID, _: Manage, db: PublicDBSessionDep, idem: Idem) -> Any:
    async def go() -> dict[str, Any]:
        return (await svc.replay_delivery(get_context(), db, delivery_id)).model_dump(mode="json")

    return await idem.run(go)
