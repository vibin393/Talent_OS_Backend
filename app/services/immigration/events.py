"""Domain-local (not frozen) after-commit events of the ``immigration`` domain (Epic 46).

``ImmigrationCaseStep`` feeds the ``immigration_case_step`` notification type (foundation 0.7). The frozen,
cross-domain ``PermitRecorded`` lives in ``app/events/immigration.py``.
"""

import dataclasses
import uuid


@dataclasses.dataclass(frozen=True, slots=True)
class ImmigrationCaseStep:
    tenant_id: uuid.UUID
    case_id: uuid.UUID
    candidate_id: uuid.UUID
    step: str  # stage_changed | overdue | outcome_recorded | documents_requested
    outcome: str | None = None
    actor_user_id: uuid.UUID | None = None
