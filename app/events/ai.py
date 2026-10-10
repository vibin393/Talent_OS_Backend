"""AI-service domain events (Epic 5; dispatched after commit, see ``app.core.events``). Ids only."""

import dataclasses
import uuid


@dataclasses.dataclass(frozen=True, slots=True)
class CandidateEmbeddingUpdated:
    tenant_id: uuid.UUID
    candidate_id: uuid.UUID
