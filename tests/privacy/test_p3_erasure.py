# ruff: noqa: E501, F811
"""Epic 52 (A1, FR-258): erasure behaviour across the Phase 3 handlers.

* Every handler that claims a Phase 3 personal table erases a seeded Candidate and its ``verify()`` returns ``[]``.
* The self-ID row is deleted (never anonymised); past ``fairness_runs`` are untouched.
* Predictions are deleted, ``ml_models`` untouched (AD-45).
* Withdrawing the ``demographic_self_id`` consent deletes the self-ID row.
"""

import uuid
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.context import RequestContext
from app.core.database import tenant_session
from app.core.hooks import autodiscover
from app.models.analytics import MlModel, MlPrediction
from app.services.privacy import registry
from app.services.privacy.registry import ErasureContext
from tests.modules.demands.helpers import DOrg
from tests.modules.fairness.helpers import enable, make_candidate
from tests.modules.fairness.test_reports import seed_answers
from tests.modules.placements.conftest import dorg  # noqa: F401 - fixture
from tests.privacy.test_erasure_coverage import P3_RETENTION

autodiscover("app.services")


def _p3_handlers() -> list[registry.ErasureHandler]:
    return [h for h in registry.handlers() if set(h.tables) & set(P3_RETENTION)]


async def _count(owner_db: AsyncEngine, sql: str, **params: object) -> int:
    async with owner_db.connect() as conn:
        return int((await conn.execute(text(sql), params)).scalar_one())


async def _seed_prediction(org: DOrg, candidate_id: uuid.UUID) -> uuid.UUID:
    async with tenant_session(org.tenant_id) as db:
        model = MlModel(
            id=uuid.uuid4(), tenant_id=org.tenant_id, kind="attrition", version=900 + int(uuid.uuid4().int % 99),
            status="superseded", n_samples=0, seed=1, feature_spec_version=1, metrics={}, params={},
        )  # fmt: skip
        db.add(model)
        await db.flush()
        db.add(
            MlPrediction(
                id=uuid.uuid4(), tenant_id=org.tenant_id, model_id=model.id, kind="attrition", subject_kind="placement",
                subject_id=uuid.uuid4(), candidate_id=candidate_id, score=Decimal("0.7"), band="high", contributions=[],
                explanation_status="none", computed_at=datetime.now(UTC),
            )
        )  # fmt: skip
        return model.id


async def test_every_p3_handler_erases_and_verifies(dorg: DOrg, owner_db: AsyncEngine) -> None:
    await enable(dorg)
    cid = await make_candidate(dorg, "Phase Three Subject")
    await seed_answers(dorg.tenant_id, {cid: {"gender": "woman", "disability": "no"}})
    model_id = await _seed_prediction(dorg, cid)
    runs_before = await _count(
        owner_db, "SELECT count(*) FROM fairness_runs WHERE tenant_id = :t", t=dorg.tenant_id
    )

    handlers = _p3_handlers()
    names = {h.name for h in handlers}
    assert {
        "fairness.self_id",
        "analytics.predictions",
        "campaigns.marketing",
        "assessments.attempts",
    } <= names
    ctx = ErasureContext(tenant_id=dorg.tenant_id, candidate_id=cid, run_id=uuid.uuid4(), reason="dsr")
    for handler in handlers:
        async with tenant_session(dorg.tenant_id) as db:
            await handler.erase(db, ctx)
    for handler in handlers:
        async with tenant_session(dorg.tenant_id) as db:
            assert await handler.verify(db, ctx) == [], handler.name

    assert (
        await _count(owner_db, "SELECT count(*) FROM demographic_self_ids WHERE candidate_id = :c", c=cid)
        == 0
    )
    assert (
        await _count(owner_db, "SELECT count(*) FROM fairness_runs WHERE tenant_id = :t", t=dorg.tenant_id)
        == runs_before
    )
    async with tenant_session(dorg.tenant_id) as db:
        assert (
            await db.execute(
                select(func.count()).select_from(MlPrediction).where(MlPrediction.candidate_id == cid)
            )
        ).scalar_one() == 0
        assert await db.get(MlModel, model_id) is not None  # models are never erased (AD-45)


async def test_self_id_consent_withdrawal_deletes_row(dorg: DOrg, owner_db: AsyncEngine) -> None:
    from app.services import privacy

    await enable(dorg)
    cid = await make_candidate(dorg, "Withdrawing Subject")
    await seed_answers(dorg.tenant_id, {cid: {"gender": "man"}})
    assert (
        await _count(owner_db, "SELECT count(*) FROM demographic_self_ids WHERE candidate_id = :c", c=cid)
        == 1
    )
    async with tenant_session(dorg.tenant_id) as db:
        await privacy.consent.record(
            db, cid, "demographic_self_id", "self_id_form", 1, RequestContext(tenant_id=dorg.tenant_id)
        )
    async with tenant_session(dorg.tenant_id) as db:
        await privacy.consent.withdraw_consent(
            RequestContext(tenant_id=dorg.tenant_id), db, cid, "demographic_self_id"
        )
    assert (
        await _count(owner_db, "SELECT count(*) FROM demographic_self_ids WHERE candidate_id = :c", c=cid)
        == 0
    )
