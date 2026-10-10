# ruff: noqa: E501
"""Evaluation results store (Epic 65, story 65.5, FR-322, AD-69; branch ``p4_governance``, band 412).

Platform tables (non-RLS, listed in ``core.tenancy.PLATFORM_TABLES``; ``talent_app`` gets SELECT / INSERT / UPDATE).
Eval data is platform test data, never Tenant data: ``failures`` holds ``[{case_id, assertion, code}]`` only -- no case
inputs or outputs.

* ``ai_eval_runs`` -- one ``python -m evals.run --record`` invocation (mode, trigger, git sha, model ids per tier).
* ``ai_eval_results`` -- one row per (run, agent, suite): score = passed / total, threshold, fingerprint.
"""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.models import Base

EVAL_MODES = ("offline", "live")
EVAL_TRIGGERS = ("ci", "schedule", "manual", "release")
EVAL_RUN_STATUSES = ("running", "passed", "failed", "error", "skipped")
EVAL_SUITES = ("agent", "redteam")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


class AIEvalRun(Base):
    __tablename__ = "ai_eval_runs"
    __table_args__ = (
        CheckConstraint(_in("mode", EVAL_MODES), name="mode"),
        CheckConstraint(_in("trigger", EVAL_TRIGGERS), name="trigger"),
        CheckConstraint(_in("status", EVAL_RUN_STATUSES), name="status"),
        Index("ix_ai_eval_runs_created_at", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    mode: Mapped[str] = mapped_column(String(10), nullable=False)
    trigger: Mapped[str] = mapped_column(String(10), nullable=False)
    git_sha: Mapped[str | None] = mapped_column(String(40), nullable=True)
    sdk_version: Mapped[str | None] = mapped_column(String(20), nullable=True)
    models: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    status: Mapped[str] = mapped_column(String(10), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class AIEvalResult(Base):
    __tablename__ = "ai_eval_results"
    __table_args__ = (
        UniqueConstraint("run_id", "agent_key", "suite"),
        CheckConstraint(_in("suite", EVAL_SUITES), name="suite"),
        CheckConstraint("score >= 0 AND score <= 1", name="score"),
        Index("ix_ai_eval_results_agent_suite_created", "agent_key", "suite", "created_at"),
        Index("ix_ai_eval_results_fingerprint", "fingerprint"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("ai_eval_runs.id", ondelete="CASCADE"), nullable=False
    )
    agent_key: Mapped[str] = mapped_column(String(40), nullable=False)
    agent_version: Mapped[int] = mapped_column(Integer, nullable=False)
    suite: Mapped[str] = mapped_column(String(10), nullable=False)
    prompt_ref: Mapped[str | None] = mapped_column(String(200), nullable=True)
    model: Mapped[str | None] = mapped_column(String(60), nullable=True)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    cases_total: Mapped[int] = mapped_column(Integer, nullable=False)
    cases_passed: Mapped[int] = mapped_column(Integer, nullable=False)
    score: Mapped[Decimal] = mapped_column(Numeric(5, 4), nullable=False)
    threshold: Mapped[Decimal] = mapped_column(Numeric(3, 2), nullable=False)
    passed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    failures: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    input_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    output_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    cost_micros: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


__all__ = ["EVAL_MODES", "EVAL_RUN_STATUSES", "EVAL_SUITES", "EVAL_TRIGGERS", "AIEvalResult", "AIEvalRun"]
