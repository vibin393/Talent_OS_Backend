# ruff: noqa: E501
"""Public API v1 webhook shapes (Epic 68, spec A4): an App manages its OWN Subscriptions (``installation_id`` is the
token's Installation; sending one is refused). Outputs reuse the internal DTOs (secrets only on create / rotate)."""

from pydantic import ConfigDict, Field

from app.schemas.webhooks import (
    DeliveryOut,
    SecretRotated,
    SubscriptionCreate,
    SubscriptionCreated,
    SubscriptionOut,
    SubscriptionUpdate,
    TestEventIn,
)


class PublicSubscriptionCreate(SubscriptionCreate):
    """``installation_id`` is not accepted on the public surface (422)."""

    model_config = ConfigDict(extra="forbid")

    installation_id: None = Field(default=None, exclude=True)


__all__ = [
    "DeliveryOut",
    "PublicSubscriptionCreate",
    "SecretRotated",
    "SubscriptionCreated",
    "SubscriptionOut",
    "SubscriptionUpdate",
    "TestEventIn",
]
