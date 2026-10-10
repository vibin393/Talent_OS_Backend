"""Activity domain events (dispatched after commit, see ``app.core.events``)."""

import dataclasses
import uuid


@dataclasses.dataclass(frozen=True, slots=True)
class UserMentioned:
    """A note / logged call or meeting mentions a tenant user (Epic 10 turns this into a notification)."""

    tenant_id: uuid.UUID
    activity_id: uuid.UUID
    entity_type: str
    entity_id: uuid.UUID
    mentioned_user_id: uuid.UUID
    author_id: uuid.UUID | None


@dataclasses.dataclass(frozen=True, slots=True)
class ActivityLogged:
    """A note, logged call / meeting or email was added to a record's timeline (Epic 15: CRM moves a New Lead to
    Contacted on its first logged contact). ``actor_kind``: user | system."""

    tenant_id: uuid.UUID
    activity_id: uuid.UUID
    entity_type: str
    entity_id: uuid.UUID
    kind: str
    actor_kind: str
