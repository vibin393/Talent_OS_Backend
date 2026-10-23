# ruff: file-ignore[line-too-long]
"""Webhook events (Epic 68, spec A5).

Contract layer (AD-36): frozen, slotted dataclasses; ``tenant_id`` first; ids and scalars only (no PII); dispatched
after commit. Field changes need a spine amendment (``tests/contracts/test_events_catalogue.py``).
"""

import dataclasses
import uuid

__all__ = ["WebhookDeliveryFailed", "WebhookSubscriptionChanged"]


@dataclasses.dataclass(frozen=True, slots=True)
class WebhookSubscriptionChanged:
    """``change`` in created | verified | verification_failed | updated | enabled | disabled | deleted |
    secret_rotated | failing (the 1 h failure-streak notice)."""

    tenant_id: uuid.UUID
    subscription_id: uuid.UUID
    owner_kind: str
    installation_id: uuid.UUID | None
    change: str
    status: str
    disabled_reason: str | None


@dataclasses.dataclass(frozen=True, slots=True)
class WebhookDeliveryFailed:
    """Emitted on a delivery's final failure and on the first failure of a streak (not every attempt)."""

    tenant_id: uuid.UUID
    subscription_id: uuid.UUID
    delivery_id: uuid.UUID
    event_type: str
    attempt: int
    final: bool
