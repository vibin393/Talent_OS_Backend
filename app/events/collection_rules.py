# ruff: noqa: E501
"""Collections Agent domain events (Epic 63; dispatched after commit). Ids / scalars only.

* ``CollectionsAgentHoldChanged`` -- a person paused / took over (``active=True``) or released a hold.
* ``CollectionMessageSent`` -- the Collections Agent (or an approved Proposal) sent a Client message (workflow trigger
  ``collection_rules.message_sent``).
"""

import dataclasses
import uuid


@dataclasses.dataclass(frozen=True, slots=True)
class CollectionsAgentHoldChanged:
    tenant_id: uuid.UUID
    client_id: uuid.UUID
    document_id: uuid.UUID | None
    mode: str
    active: bool
    actor_id: uuid.UUID


@dataclasses.dataclass(frozen=True, slots=True)
class CollectionMessageSent:
    tenant_id: uuid.UUID
    client_id: uuid.UUID
    document_ids: tuple[uuid.UUID, ...]
    email_message_id: uuid.UUID
    tone: str
    via_proposal_id: uuid.UUID | None


__all__ = ["CollectionMessageSent", "CollectionsAgentHoldChanged"]
