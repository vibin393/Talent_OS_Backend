"""Notification events (Epic 10, dispatched after commit). Ids only."""

import dataclasses
import uuid


@dataclasses.dataclass(frozen=True, slots=True)
class NotificationsCreated:
    """A fan-out inserted in-app notifications (lets later real-time transports push; polling today)."""

    tenant_id: uuid.UUID
    type: str
    user_ids: tuple[uuid.UUID, ...]
