"""Unified-platform inbound data loops on the Public API v1.

* ``POST /public/v1/assessments`` (Croar -> ATS): ``candidates:write``. Idempotent by ``event_id`` and by
  ``Idempotency-Key``. 422 when the candidate cannot be resolved (never creates a candidate).
* ``POST /public/v1/engagement`` (Feedback IQ -> ATS): ``candidates:write``. Best-effort email match;
  stored even when unmatched. Idempotent by ``event_id`` and by ``Idempotency-Key``.

Same auth, tenant-scoping and ``Idempotency-Key`` requirement as every other Public API v1 write."""

from typing import Any

from fastapi import status

from app.core.public_api import public_router
from app.dependencies.public import PublicDBSessionDep
from app.router.public_v1._common import Idem, scope
from app.schemas.public_v1.ingest import AssessmentIngestIn, AssessmentOut, EngagementIngestIn, EngagementOut
from app.services import public_api_service as svc

assessments_router = public_router("assessments")
engagement_router = public_router("engagement")
Write = scope("candidates:write")


@assessments_router.post("/assessments", response_model=AssessmentOut, status_code=status.HTTP_201_CREATED)
async def ingest_assessment(body: AssessmentIngestIn, _: Write, db: PublicDBSessionDep, idem: Idem) -> Any:
    return await idem.run(lambda: svc.ingest.create_assessment(db, body))


@engagement_router.post("/engagement", response_model=EngagementOut, status_code=status.HTTP_201_CREATED)
async def ingest_engagement(body: EngagementIngestIn, _: Write, db: PublicDBSessionDep, idem: Idem) -> Any:
    return await idem.run(lambda: svc.ingest.create_engagement(db, body))
