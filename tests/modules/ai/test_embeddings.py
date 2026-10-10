"""Story 5.2: embeddings (fake backend), VectorIndex (ArrayVectorIndex parity, tenant isolation, selection),
hash skip and consent handling. No model is ever loaded (``EMBEDDING_BACKEND=fake``)."""

import uuid
from collections.abc import Iterator

import numpy as np
import pytest
from sqlalchemy import select

from app.core.database import tenant_session
from app.core.settings import get_settings
from app.models.embeddings import CandidateEmbedding
from app.services import embeddings_service
from app.services.ai import embeddings, vector_index
from app.services.ai.vector_index import ArrayVectorIndex, PgVectorIndex, get_vector_index, set_vector_index
from tests.epic5 import DATA_ENGINEER, make_candidate, patch_candidate
from tests.modules.demands.helpers import DOrg


@pytest.fixture(autouse=True)
def _reset_index() -> Iterator[None]:
    set_vector_index(None)
    yield
    set_vector_index(None)


def test_fake_backend_shape_normalisation_and_determinism() -> None:
    assert get_settings().embedding_backend == "fake"
    vecs = embeddings.embed(["Data engineer Databricks", "Data engineer Databricks", "Java developer"])
    assert len(vecs) == 3 and all(len(v) == 384 for v in vecs)
    for v in vecs:
        assert abs(float(np.linalg.norm(v)) - 1.0) < 1e-5
    assert vecs[0] == vecs[1]
    related = vector_index.cosine(vecs[0], embeddings.embed(["Databricks engineer"])[0])
    unrelated = vector_index.cosine(vecs[0], vecs[2])
    assert related > unrelated
    # queries get the bge retrieval prefix, so they differ from passages
    assert embeddings.embed(["x"], kind="query")[0] != embeddings.embed(["x"])[0]
    assert embeddings.embed([]) == []


def test_embedding_text_uses_structured_fields_only() -> None:
    from tests.modules.matching.test_scoring import cand, demand

    text = embeddings.candidate_text(cand())
    assert "Databricks (4 yrs)" in text and "Data Engineer" in text
    assert "HDFC" not in text  # employer names are not part of the profile text
    dtext = embeddings.demand_text(demand())
    assert "Databricks (mandatory, 3+ yrs)" in dtext and "4-8 years" in dtext


async def test_index_selection(dorg: DOrg) -> None:
    from sqlalchemy import text

    async with tenant_session(dorg.tenant_id) as db:
        has_pgvector = (await db.execute(text("SELECT 1 FROM pg_extension WHERE extname = 'vector'"))).first()
        index = await get_vector_index(db)
    expected = PgVectorIndex if has_pgvector else ArrayVectorIndex  # local PG18 has no pgvector
    assert isinstance(index, expected)
    set_vector_index(ArrayVectorIndex())
    async with tenant_session(dorg.tenant_id) as db:
        assert isinstance(await get_vector_index(db), ArrayVectorIndex)


async def test_candidates_are_embedded_and_hash_skipped(dorg: DOrg) -> None:
    c = await make_candidate(dorg.admin, "Priya Raman", **DATA_ENGINEER)
    cid = uuid.UUID(c["id"])
    async with tenant_session(dorg.tenant_id) as db:
        row = (
            await db.execute(select(CandidateEmbedding).where(CandidateEmbedding.candidate_id == cid))
        ).scalar_one()
        assert len(row.vector) == 384 and row.model == embeddings.FAKE_MODEL
        first_hash, first_updated = row.content_hash, row.updated_at
        # unchanged profile -> no re-embedding
        assert await embeddings_service.sync_candidates(db, dorg.tenant_id, [cid]) == []
    # a change of a non-embedded field (notice period) keeps the vector
    await patch_candidate(dorg.admin, c["id"], notice_period_days=10)
    async with tenant_session(dorg.tenant_id) as db:
        row = (
            await db.execute(select(CandidateEmbedding).where(CandidateEmbedding.candidate_id == cid))
        ).scalar_one()
        assert (row.content_hash, row.updated_at) == (first_hash, first_updated)
    await patch_candidate(dorg.admin, c["id"], skills=["Java"])
    async with tenant_session(dorg.tenant_id) as db:
        row = (
            await db.execute(select(CandidateEmbedding).where(CandidateEmbedding.candidate_id == cid))
        ).scalar_one()
        assert row.content_hash != first_hash


async def test_array_index_parity_and_tenant_isolation(dorg: DOrg, other_dorg: DOrg) -> None:
    ids = []
    for i, skills in enumerate((["Databricks", "PySpark"], ["Java", "Spring Boot"], ["Databricks"], ["Go"])):
        ids.append(uuid.UUID((await make_candidate(dorg.admin, f"Cand {i}", skills=skills))["id"]))
    theirs = uuid.UUID((await make_candidate(other_dorg.admin, "Other Tenant", skills=["Databricks"]))["id"])
    query = embeddings.embed(["Databricks PySpark"], kind="query")[0]
    index = ArrayVectorIndex()
    async with tenant_session(dorg.tenant_id) as db:
        stored = await index.vectors(db, "candidate", ids)
        brute = sorted(((cid, float(np.dot(query, v))) for cid, v in stored.items()), key=lambda x: -x[1])
        hits = await index.search(db, dorg.tenant_id, query, None, k=3)
        assert [h[0] for h in hits] == [b[0] for b in brute[:3]]
        for (_, a), (_, b) in zip(hits, brute[:3], strict=True):
            assert abs(a - b) < 1e-5
        filtered = await index.search(db, dorg.tenant_id, query, [ids[1], ids[3]], k=5)
        assert {h[0] for h in filtered} == {ids[1], ids[3]}
        assert theirs not in {h[0] for h in await index.search(db, dorg.tenant_id, query, None, k=50)}
        assert await index.search(db, dorg.tenant_id, query, [theirs], k=5) == []
        assert await index.search(db, dorg.tenant_id, query, [], k=5) == []


async def test_consent_withdrawn_candidates_have_no_embedding(dorg: DOrg) -> None:
    from app.services.candidates import privacy_port

    c = await make_candidate(dorg.admin, "No Consent", skills=["Databricks"])
    cid = uuid.UUID(c["id"])
    async with tenant_session(dorg.tenant_id) as db:
        await privacy_port.set_ai_consent_state(db, {cid: "withdrawn"})
    async with tenant_session(dorg.tenant_id) as db:
        assert (
            await db.execute(select(CandidateEmbedding).where(CandidateEmbedding.candidate_id == cid))
        ).first() is None
        assert await embeddings_service.sync_candidates(db, dorg.tenant_id, [cid]) == []
    async with tenant_session(dorg.tenant_id) as db:
        await privacy_port.set_ai_consent_state(db, {cid: "granted"})
    async with tenant_session(dorg.tenant_id) as db:
        assert (
            await db.execute(select(CandidateEmbedding).where(CandidateEmbedding.candidate_id == cid))
        ).first()


def test_only_the_ai_package_imports_model_libraries() -> None:
    from pathlib import Path

    root = Path(__file__).resolve().parents[3] / "app"
    offenders = []
    for path in root.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        rel = path.relative_to(root).as_posix()
        if (
            "import sentence_transformers" in text or "from sentence_transformers" in text
        ) and rel != "services/ai/embeddings.py":
            offenders.append(rel)
        if ("import anthropic" in text or "from anthropic" in text) and not rel.startswith("services/ai/"):
            offenders.append(rel)
    assert offenders == []
    scoring_src = (root / "services/matching/scoring.py").read_text(encoding="utf-8")
    assert "app.services.ai" not in scoring_src and "sqlalchemy" not in scoring_src
