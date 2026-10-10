"""Bulk seeding for Epic 5 scale tests: candidates, skills, search projection rows and (fake) embeddings are
written with set-based SQL (no ORM, so no per-row events) inside the tenant's RLS session."""

import uuid

import numpy as np
from sqlalchemy import insert, text

from app.core.database import tenant_session
from app.models.embeddings import CandidateEmbedding
from app.services.ai import embeddings

TITLES = ["Data Engineer", "Java Developer", "QA Engineer", "Data Analyst"]
SKILL_GROUPS = {
    0: ["databricks", "pyspark", "sql"],
    1: ["java", "spring-boot"],
    2: ["selenium", "java"],
    3: ["sql", "power-bi"],
}


async def seed(tenant_id: uuid.UUID, owner_id: uuid.UUID, n: int, *, with_embeddings: bool = True) -> int:
    async with tenant_session(tenant_id) as db:
        await db.execute(
            text(
                """
                INSERT INTO candidates (id, tenant_id, human_id, full_name, location, current_title,
                    current_employer, total_experience_months, notice_period_days, source, owner_id,
                    ai_consent_state, created_at, updated_at)
                SELECT gen_random_uuid(), CAST(:tid AS uuid), 'SEED-' || lpad(g::text, 7, '0'),
                    'Seed Person ' || g,
                    jsonb_build_object('city', (ARRAY['Chennai','Pune','Bengaluru','Hyderabad'])[1 + g % 4],
                                       'country', 'India'),
                    (ARRAY['Data Engineer','Java Developer','QA Engineer','Data Analyst'])[1 + g % 4],
                    'Employer ' || (g % 50), 12 + (g % 120), (g % 4) * 30, 'manual', CAST(:owner AS uuid),
                    'granted', now() - make_interval(mins => g), now() - make_interval(mins => g)
                FROM generate_series(1, :n) AS g
                """
            ),
            {"tid": str(tenant_id), "owner": str(owner_id), "n": n},
        )
        for group, slugs in SKILL_GROUPS.items():
            await db.execute(
                text(
                    """
                    INSERT INTO candidate_skills (id, tenant_id, candidate_id, skill_id, source, years, created_at, updated_at)
                    SELECT gen_random_uuid(), c.tenant_id, c.id, s.id, 'manual',
                           1 + (abs(hashtext(c.id::text)) % 8), now(), now()
                    FROM candidates c JOIN skills s ON s.slug = ANY(:slugs)
                    WHERE c.human_id LIKE 'SEED-%'
                      AND (substring(c.human_id from 6)::int % 4) = :grp
                    """
                ),
                {"slugs": slugs, "grp": group},
            )
        await db.execute(
            text(
                """
                INSERT INTO candidate_search_docs (tenant_id, candidate_id, human_id, owner_id, display_name,
                    name_norm, headline, current_title, current_employer, skill_ids, skill_names, source,
                    country, city, experience_years, notice_days, ai_consent, is_archived,
                    candidate_updated_at, doc)
                SELECT c.tenant_id, c.id, c.human_id, c.owner_id, c.full_name, f_unaccent(lower(c.full_name)),
                    c.current_title, c.current_title, c.current_employer,
                    coalesce(array_agg(s.id) FILTER (WHERE s.id IS NOT NULL), '{}'),
                    coalesce(array_agg(s.name) FILTER (WHERE s.id IS NOT NULL), '{}'),
                    c.source, c.location->>'country', c.location->>'city',
                    round(c.total_experience_months / 12.0, 1), c.notice_period_days, true, false, c.updated_at,
                    setweight(to_tsvector('talent_en', c.full_name || ' ' || coalesce(c.current_title, '')), 'A')
                    || setweight(to_tsvector('talent_en', coalesce(string_agg(s.name, ' '), '')), 'B')
                    || setweight(to_tsvector('talent_en', coalesce(c.current_employer, '')), 'C')
                FROM candidates c
                LEFT JOIN candidate_skills cs ON cs.candidate_id = c.id
                LEFT JOIN skills s ON s.id = cs.skill_id
                WHERE c.human_id LIKE 'SEED-%'
                GROUP BY c.id
                """
            )
        )
        if with_embeddings:
            rows = (
                await db.execute(
                    text(
                        "SELECT candidate_id, current_title || ' ' || array_to_string(skill_names, ' ') "
                        "FROM candidate_search_docs WHERE human_id LIKE 'SEED-%'"
                    )
                )
            ).all()
            vectors = embeddings.embed([r[1] for r in rows])
            values = [
                {
                    "tenant_id": tenant_id,
                    "candidate_id": r[0],
                    "kind": "profile",
                    "vector": np.asarray(v, dtype=np.float32).tolist(),
                    "model": embeddings.FAKE_MODEL,
                    "content_hash": "seed",
                }
                for r, v in zip(rows, vectors, strict=True)
            ]
            for start in range(0, len(values), 2000):
                await db.execute(insert(CandidateEmbedding.__table__), values[start : start + 2000])
    return n


async def purge(tenant_id: uuid.UUID) -> None:
    """Delete the seeded rows as the owner role with FK triggers off (a cascading DELETE of 100k candidates
    through every referencing table is far too slow for the shared test database)."""
    from app.core.database import owner_engine

    engine = owner_engine()
    try:
        async with engine.begin() as conn:
            await conn.execute(text("SET LOCAL session_replication_role = replica"))
            ids = "SELECT id FROM candidates WHERE tenant_id = CAST(:tid AS uuid) AND human_id LIKE 'SEED-%'"
            for table in ("candidate_search_docs", "candidate_embeddings", "matches", "candidate_skills"):
                await conn.execute(
                    text(f"DELETE FROM {table} WHERE candidate_id IN ({ids})"), {"tid": str(tenant_id)}
                )
            await conn.execute(
                text(
                    "DELETE FROM candidates WHERE tenant_id = CAST(:tid AS uuid) AND human_id LIKE 'SEED-%'"
                ),
                {"tid": str(tenant_id)},
            )
    finally:
        await engine.dispose()
