"""Approval domain events (dispatched after commit). Ids only, no PII."""

import dataclasses
import uuid


@dataclasses.dataclass(frozen=True, slots=True)
class ApprovalStepActivated:
    tenant_id: uuid.UUID
    request_id: uuid.UUID
    subject_type: str
    subject_id: uuid.UUID
    position: int
    approver_user_ids: tuple[uuid.UUID, ...]
    approver_role_id: uuid.UUID | None
    # Epic 13 (AD-24): the resolved approver kind of the step and, for Client User steps, the Client User.
    # Defaults keep Phase 1 handlers working.
    approver_kind: str = "user"
    approver_client_user_id: uuid.UUID | None = None


@dataclasses.dataclass(frozen=True, slots=True)
class ApprovalCompleted:
    tenant_id: uuid.UUID
    request_id: uuid.UUID
    subject_type: str
    subject_id: uuid.UUID
    status: str
