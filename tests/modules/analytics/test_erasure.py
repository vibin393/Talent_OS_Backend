# ruff: noqa: E501
"""Epic 48 (A8, FR-258, AD-45): predictions deleted, snapshots rewritten without the subject, models retained."""

import dataclasses
import uuid
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select

from app.core.database import tenant_session
from app.models.analytics import MlModel, MlPrediction, MlTrainingSet
from app.services.analytics import snapshots
from app.services.analytics.models.serialize import dumps
from app.services.analytics_erasure import AnalyticsHandler
from app.services.privacy import registry
from app.services.privacy.registry import ErasureContext
from tests.modules.analytics.helpers import offer_rows, train_with
from tests.modules.demands.helpers import DOrg
from tests.modules.offers.helpers import create_candidate


def test_handler_registered_and_claims_tables() -> None:
    handlers = {h.name: h for h in registry.handlers()} if hasattr(registry, "handlers") else {}
    h = handlers.get("analytics.predictions") or AnalyticsHandler()
    assert set(h.tables) == {"ml_predictions", "ml_training_sets"}
    assert h.order == 48


async def test_erase_subject(dorg: DOrg) -> None:
    cand = await create_candidate(dorg.admin, f"Esme Erased {uuid.uuid4().hex[:4]}")
    rows = offer_rows(400, 60, seed=41)
    rows[5] = dataclasses.replace(rows[5], candidate_id=cand)
    model = await train_with(dorg.tenant_id, "offer_drop", rows)
    assert model.status == "active"
    async with tenant_session(dorg.tenant_id) as db:
        db.add(
            MlPrediction(
                id=uuid.uuid4(), tenant_id=dorg.tenant_id, model_id=model.id, kind="offer_drop", subject_kind="offer",
                subject_id=uuid.uuid4(), candidate_id=cand, score=Decimal("0.4"), band="medium", contributions=[],
                explanation_status="none", computed_at=datetime.now(UTC),
            )
        )  # fmt: skip
    ctx = ErasureContext(tenant_id=dorg.tenant_id, candidate_id=cand, run_id=uuid.uuid4(), reason="dsr")
    handler = AnalyticsHandler()
    async with tenant_session(dorg.tenant_id) as db:
        exported = await handler.export(db, ctx)
        assert exported is not None and exported["predictions"][0]["band"] == "medium"
        counts = await handler.erase(db, ctx)
        assert counts == {"predictions": 1, "snapshots": 1, "snapshot_rows": 1}
    async with tenant_session(dorg.tenant_id) as db:
        assert await handler.verify(db, ctx) == []
        assert (
            await db.execute(select(MlPrediction).where(MlPrediction.candidate_id == cand))
        ).first() is None
        ts = await db.get(MlTrainingSet, model.training_set_id)
        assert ts is not None and ts.rewritten_for_erasure_at is not None and ts.row_count == 399
        assert cand not in {r.candidate_id for r in await snapshots.read(ts)}
        kept = await db.get(MlModel, model.id)
        assert kept is not None and kept.status == "active" and dumps(kept.params) == dumps(model.params)
