"""Offer domain events (Epic 8; dispatched after commit, AD-9). Ids and enums only, no PII.

Public events (Epic 10 subscribes later): ``OfferCreated``, ``OfferVersionCreated``, ``OfferStatusChanged``,
``OfferAccepted``, ``OfferDeclined``, ``OfferExpired``.

Internal events (the offers domain's own after-commit work): ``OfferLetterRequested`` (render a version's
PDF letter) and ``OfferEmailRequested`` (``kind``: ``released`` | ``updated`` | ``withdrawn`` |
``declined_owner`` | ``expired_owner``).
"""

import dataclasses
import uuid


@dataclasses.dataclass(frozen=True, slots=True)
class OfferCreated:
    tenant_id: uuid.UUID
    offer_id: uuid.UUID
    application_id: uuid.UUID
    demand_id: uuid.UUID
    candidate_id: uuid.UUID


@dataclasses.dataclass(frozen=True, slots=True)
class OfferVersionCreated:
    tenant_id: uuid.UUID
    offer_id: uuid.UUID
    version_no: int
    requires_approval: bool


@dataclasses.dataclass(frozen=True, slots=True)
class OfferStatusChanged:
    tenant_id: uuid.UUID
    offer_id: uuid.UUID
    application_id: uuid.UUID
    demand_id: uuid.UUID
    candidate_id: uuid.UUID
    from_status: str | None
    to_status: str
    actor_kind: str
    version_no: int | None = None


@dataclasses.dataclass(frozen=True, slots=True)
class OfferAccepted:
    tenant_id: uuid.UUID
    offer_id: uuid.UUID
    application_id: uuid.UUID
    demand_id: uuid.UUID
    candidate_id: uuid.UUID
    version_no: int


@dataclasses.dataclass(frozen=True, slots=True)
class OfferDeclined:
    tenant_id: uuid.UUID
    offer_id: uuid.UUID
    application_id: uuid.UUID
    demand_id: uuid.UUID
    candidate_id: uuid.UUID
    version_no: int


@dataclasses.dataclass(frozen=True, slots=True)
class OfferExpired:
    tenant_id: uuid.UUID
    offer_id: uuid.UUID
    application_id: uuid.UUID
    demand_id: uuid.UUID
    candidate_id: uuid.UUID


@dataclasses.dataclass(frozen=True, slots=True)
class OfferLetterRequested:
    """Internal: render (idempotently) the PDF letter of one version."""

    tenant_id: uuid.UUID
    offer_id: uuid.UUID
    version_no: int


@dataclasses.dataclass(frozen=True, slots=True)
class OfferEmailRequested:
    """Internal: send one offer email (``kind``) from a Celery task after commit."""

    tenant_id: uuid.UUID
    offer_id: uuid.UUID
    kind: str
