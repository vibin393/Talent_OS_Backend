# ruff: noqa: E501
"""Submission pack domain events (Epic 61; dispatched after commit). Ids / scalars only.

* ``SubmissionPackReady`` -- a pack was (re)built and its status changed (notification ``submission_pack_ready``,
  workflow trigger ``submission_packs.pack_ready``).
* ``SubmissionPackUsed`` -- a person created a Submission from the pack (``changed_fields``: what they changed;
  ``note_change``: ``unchanged`` | ``edited`` | ``replaced``).
"""

import dataclasses
import uuid


@dataclasses.dataclass(frozen=True, slots=True)
class SubmissionPackReady:
    tenant_id: uuid.UUID
    pack_id: uuid.UUID
    application_id: uuid.UUID
    demand_id: uuid.UUID
    client_id: uuid.UUID
    status: str
    blocker_count: int
    agent_run_id: uuid.UUID | None


@dataclasses.dataclass(frozen=True, slots=True)
class SubmissionPackUsed:
    tenant_id: uuid.UUID
    pack_id: uuid.UUID
    submission_id: uuid.UUID
    application_id: uuid.UUID
    changed_fields: tuple[str, ...]
    note_change: str


__all__ = ["SubmissionPackReady", "SubmissionPackUsed"]
