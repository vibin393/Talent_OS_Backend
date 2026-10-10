# ruff: noqa: E501
"""Synthetic, seeded training data for the Epic 48 tests (no cross-test randomness)."""

import math
import random
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from app.core.database import tenant_session
from app.models.analytics import MlModel
from app.services.analytics import training
from app.services.analytics.features.common import TrainingRow

API = "/api/v1"
START = datetime(2025, 1, 1, tzinfo=UTC)


def offer_features(rnd: random.Random, notice: float) -> dict[str, Any]:
    return {
        "notice_period": notice,
        "days_since_acceptance": rnd.uniform(0, 7),
        "joining_lead_time": rnd.uniform(5, 60),
        "offer_revisions": float(rnd.randint(0, 3)),
        "compensation_change": rnd.uniform(-10, 60),
        "prejoining_completion": rnd.random(),
        "bgv_pending": float(rnd.randint(0, 1)),
        "counter_offer": 0.0,
        "different_city": None,
    }


def offer_rows(n: int, positives: int, *, signal: bool = True, seed: int = 7) -> list[TrainingRow]:
    """``n`` labelled offer outcomes with exactly ``positives`` drops. With ``signal`` the drops are the longest
    notice periods (plus noise); otherwise labels are random."""
    rnd = random.Random(seed)
    notices = [rnd.uniform(0, 90) for _ in range(n)]
    if signal:
        ranked = sorted(range(n), key=lambda i: -(notices[i] + rnd.gauss(0, 8)))
    else:
        ranked = list(range(n))
        rnd.shuffle(ranked)
    drops = set(ranked[:positives])
    rows = []
    for i in range(n):
        outcome = START + timedelta(days=i)
        rows.append(
            TrainingRow(
                uuid.uuid4(),
                uuid.uuid4(),
                outcome - timedelta(days=20),
                outcome,
                offer_features(rnd, notices[i]),
                1.0 if i in drops else 0.0,
            )
        )
    return rows


def ttf_rows(n: int = 300, seed: int = 11) -> list[TrainingRow]:
    rnd = random.Random(seed)
    rows = []
    for i in range(n):
        skills = rnd.randint(1, 10)
        days = math.exp(1.5 + 0.25 * skills + rnd.gauss(0, 0.15))
        feats = {
            "required_skills": float(skills),
            "skill_rarity": rnd.uniform(1, 20),
            "min_experience": rnd.uniform(0, 12),
            "openings": float(rnd.randint(1, 4)),
            "priority": float(rnd.randint(0, 3)),
            "employment_type": rnd.choice(["contract", "permanent"]),
            "remote": float(rnd.randint(0, 1)),
            "city": rnd.choice(["chennai", "pune", "bengaluru"]),
            "client_feedback_delay": rnd.uniform(1, 10),
            "client_fill_rate": rnd.random(),
            "rate_vs_median": rnd.uniform(0.7, 1.3),
            "recruiter_load": float(rnd.randint(0, 8)),
        }
        opened = START + timedelta(days=i)
        rows.append(TrainingRow(uuid.uuid4(), None, opened, opened + timedelta(days=days), feats, days))
    return rows


def attrition_rows(n: int = 300, positives: int = 60, seed: int = 5) -> list[TrainingRow]:
    rnd = random.Random(seed)
    rejections = [float(rnd.randint(0, 6)) for _ in range(n)]
    ranked = sorted(range(n), key=lambda i: -(rejections[i] + rnd.gauss(0, 0.8)))
    leavers = set(ranked[:positives])
    rows = []
    for i in range(n):
        feats = {
            "tenure": rnd.uniform(30, 400),
            "timesheet_rejections": rejections[i],
            "bench_entries": float(rnd.randint(0, 2)),
            "pay_rate_vs_median": rnd.uniform(0.7, 1.3),
            "leave_days": rnd.uniform(0, 10),
            "pending_leave": float(rnd.randint(0, 2)),
            "prior_extensions": float(rnd.randint(0, 3)),
            "client_early_end_rate": rnd.random(),
            "days_to_planned_end": rnd.uniform(0, 300),
        }
        outcome = START + timedelta(days=i)
        rows.append(
            TrainingRow(
                uuid.uuid4(),
                uuid.uuid4(),
                outcome - timedelta(days=60),
                outcome,
                feats,
                1.0 if i in leavers else 0.0,
            )
        )
    return rows


async def train_with(tenant_id: uuid.UUID, kind: str, rows: list[TrainingRow]) -> MlModel:
    async with tenant_session(tenant_id) as db:
        model = await training.train(db, tenant_id, kind, rows=rows)
        assert model is not None
        await db.flush()
        await db.refresh(model)
        db.expunge(model)
        return model
