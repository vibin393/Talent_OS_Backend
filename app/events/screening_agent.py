# ruff: noqa: E501
"""Domain-local events of the Screening Agent (Epic 57). ``ScreeningConversationCompleted`` is the foundation's
(``app.events.conversations``). Frozen, slotted; ids and scalars only."""

import dataclasses
import uuid


@dataclasses.dataclass(frozen=True, slots=True)
class ScreeningAnswersReviewed:
    """A person completed the review of a conversation's (or voice session's) answers (FR-291)."""

    tenant_id: uuid.UUID
    conversation_id: uuid.UUID | None
    application_id: uuid.UUID
    accepted: int
    edited: int
    low_confidence: int
    reviewer_id: uuid.UUID | None
    voice_session_id: uuid.UUID | None = None


@dataclasses.dataclass(frozen=True, slots=True)
class ScreeningChatInvited:
    """A Candidate was invited to a portal screening chat (portal notification)."""

    tenant_id: uuid.UUID
    conversation_id: uuid.UUID
    candidate_id: uuid.UUID
    application_id: uuid.UUID


@dataclasses.dataclass(frozen=True, slots=True)
class ScreeningRunNeeded:
    """After commit: start a run for a conversation (workflow supersede, consent resume fallback, reminder)."""

    tenant_id: uuid.UUID
    conversation_id: uuid.UUID
    trigger_kind: str
    trigger_key: str
    mode: str
    payload: str = ""


__all__ = ["ScreeningAnswersReviewed", "ScreeningChatInvited", "ScreeningRunNeeded"]
