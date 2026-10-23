# ruff: file-ignore[line-too-long]
"""Knowledge Sources and Onboarding Q&A (Epic 62, FR-310; migrations ``20261008_409_01_knowledge`` /
``20261008_409_02_onb_qa``). Owner: ``knowledge``.

* ``knowledge_sources`` (audited) -- an article, file, imported onboarding template or policy document the onboarding
  assistant may cite once **published** for an audience (``worker`` / ``candidate`` / ``both`` / ``internal``).
* ``knowledge_source_versions`` (append-only, audited) -- every edit is a new version; one version is published.
* ``knowledge_chunks`` (not audited) -- the searchable text of each version; only ``active`` chunks (those of the
  published version) are ever returned by search.
* ``onboarding_qa_threads`` (audited header; worker-visible) -- a Worker's / pre-joining Candidate's questions.
* ``onboarding_qa_messages`` (not audited; worker-visible) -- the messages. Epic 78 (FR-404, AD-90; migrations
  ``20261010_512_02_qa_sealed`` / ``20261010_512_03_qa_plain_drop``): the text and the feedback note are sealed under the
  Tenant DEK (``text_sealed`` / ``feedback_note_sealed``, ``vault_service.seal_field``; decrypted only in
  ``services/knowledge/sealing.py``); ``topic_label`` (non-reversible) and ``question_fingerprint`` (HMAC) feed the gap
  report. ``plain_text`` / ``plain_feedback_note`` are transient (filled by ``sealing.open_messages``).
"""

import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Computed,
    Date,
    DateTime,
    Float,
    ForeignKeyConstraint,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy import text as sql_text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column

from app.core.models import AuditedMixin, Base, TenantMixin, Timestamps, UUIDPk

SOURCE_KINDS = ("article", "file", "onboarding_template", "policy_document")
AUDIENCES = ("worker", "candidate", "both", "internal")
SOURCE_STATUSES = ("draft", "published", "unpublished", "archived")
EXTRACT_STATUSES = ("pending", "ok", "failed", "unsupported")
THREAD_AUDIENCES = ("worker", "candidate")
THREAD_STATUSES = ("open", "answered", "handed_off", "closed")
MESSAGE_ROLES = ("asker", "assistant", "coordinator", "system")
MESSAGE_OUTCOMES = ("answered", "handed_off", "ungrounded", "pending_review", "blocked")
TOPICS = ("pay", "tax", "legal", "immigration", "documents", "policy", "it", "other")
FEEDBACK = ("helpful", "not_helpful", "incorrect")
MAX_BODY = 100_000
MAX_CHUNK = 2000
MAX_ASKER_TEXT = 2000


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _tfk(column: str, target: str, name: str, ondelete: str = "RESTRICT") -> ForeignKeyConstraint:
    return ForeignKeyConstraint(
        ["tenant_id", column], [f"{target}.tenant_id", f"{target}.id"], name=name, ondelete=ondelete
    )


class KnowledgeSource(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "knowledge_sources"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        CheckConstraint(_in("kind", SOURCE_KINDS), name="kind"),
        CheckConstraint(_in("audience", AUDIENCES), name="audience"),
        CheckConstraint(_in("status", SOURCE_STATUSES), name="status"),
        _tfk("owner_id", "users", "fk_knowledge_sources_owner_id_users"),
        Index("ix_knowledge_sources_status_audience", "tenant_id", "status", "audience"),
    )

    title: Mapped[str] = mapped_column(String(200), nullable=False)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    audience: Mapped[str] = mapped_column(String(12), nullable=False)
    status: Mapped[str] = mapped_column(String(12), nullable=False, server_default=sql_text("'draft'"))
    current_version_no: Mapped[int] = mapped_column(Integer, nullable=False, server_default=sql_text("0"))
    published_version_no: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ref_kind: Mapped[str | None] = mapped_column(String(30), nullable=True)
    ref_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    legal_entity_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    client_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    owner_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    tags: Mapped[list[str]] = mapped_column(
        ARRAY(String(40)), nullable=False, server_default=sql_text("'{}'::varchar[]")
    )
    last_reviewed_on: Mapped[date | None] = mapped_column(Date, nullable=True)


class KnowledgeSourceVersion(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "knowledge_source_versions"
    __audit_exclude__ = ("body_md", "extracted_text")
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "source_id", "version_no", name="uq_knowledge_source_versions_no"),
        _tfk("source_id", "knowledge_sources", "fk_knowledge_source_versions_source_id", "CASCADE"),
        CheckConstraint(_in("extract_status", EXTRACT_STATUSES), name="extract_status"),
        CheckConstraint(f"body_md IS NULL OR char_length(body_md) <= {MAX_BODY}", name="body_length"),
    )

    source_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    body_md: Mapped[str | None] = mapped_column(Text, nullable=True)
    file_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    extracted_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    extract_status: Mapped[str] = mapped_column(
        String(12), nullable=False, server_default=sql_text("'pending'")
    )
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)


class KnowledgeChunk(UUIDPk, TenantMixin, Timestamps, Base):
    __tablename__ = "knowledge_chunks"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "source_id", "version_no", "seq", name="uq_knowledge_chunks_seq"),
        _tfk("source_id", "knowledge_sources", "fk_knowledge_chunks_source_id", "CASCADE"),
        CheckConstraint(_in("audience", AUDIENCES), name="audience"),
        CheckConstraint(f"char_length(text) <= {MAX_CHUNK}", name="text_length"),
        Index("ix_knowledge_chunks_active_audience", "tenant_id", "active", "audience"),
        Index("ix_knowledge_chunks_text_tsv", "text_tsv", postgresql_using="gin"),
    )

    source_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    heading: Mapped[str | None] = mapped_column(String(300), nullable=True)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    text_tsv: Mapped[Any] = mapped_column(
        TSVECTOR,
        Computed("to_tsvector('english'::regconfig, coalesce(heading, '') || ' ' || text)", persisted=True),
    )
    embedding: Mapped[list[float] | None] = mapped_column(ARRAY(Float(precision=24)), nullable=True)
    embedding_model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    audience: Mapped[str] = mapped_column(String(12), nullable=False)
    legal_entity_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    client_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=sql_text("false"))


class OnboardingQaThread(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "onboarding_qa_threads"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        _tfk("candidate_id", "candidates", "fk_onboarding_qa_threads_candidate_id"),
        _tfk("coordinator_user_id", "users", "fk_onboarding_qa_threads_coordinator"),
        CheckConstraint(_in("audience", THREAD_AUDIENCES), name="audience"),
        CheckConstraint(_in("status", THREAD_STATUSES), name="status"),
        Index("ix_onboarding_qa_threads_candidate_created", "candidate_id", "created_at"),
    )

    candidate_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    onboarding_case_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    placement_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    audience: Mapped[str] = mapped_column(String(10), nullable=False)
    status: Mapped[str] = mapped_column(String(12), nullable=False, server_default=sql_text("'open'"))
    coordinator_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    consent_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    agent_run_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    task_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class OnboardingQaMessage(UUIDPk, TenantMixin, Timestamps, Base):
    __tablename__ = "onboarding_qa_messages"
    __pii_fields__ = ("text_sealed", "feedback_note_sealed")
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "thread_id", "seq", name="uq_onboarding_qa_messages_seq"),
        _tfk("thread_id", "onboarding_qa_threads", "fk_onboarding_qa_messages_thread_id", "CASCADE"),
        CheckConstraint(_in("role", MESSAGE_ROLES), name="role"),
        CheckConstraint(f"outcome IS NULL OR {_in('outcome', MESSAGE_OUTCOMES)}", name="outcome"),
        CheckConstraint(f"topic IS NULL OR {_in('topic', TOPICS)}", name="topic"),
        CheckConstraint(f"feedback IS NULL OR {_in('feedback', FEEDBACK)}", name="feedback"),
        Index("ix_onboarding_qa_messages_candidate_id", "candidate_id"),
        Index("ix_onboarding_qa_messages_topic_label", "tenant_id", "topic_label"),
        Index("ix_onboarding_qa_messages_fingerprint", "tenant_id", "question_fingerprint"),
    )
    # Transient decrypted values (never persisted; ``sealing.open_messages``).
    plain_text = None
    plain_feedback_note = None

    thread_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    candidate_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    role: Mapped[str] = mapped_column(String(12), nullable=False)
    text_sealed: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    citations: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, server_default=sql_text("'[]'::jsonb")
    )
    outcome: Mapped[str | None] = mapped_column(String(16), nullable=True)
    topic: Mapped[str | None] = mapped_column(String(12), nullable=True)
    agent_run_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    proposal_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    author_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    feedback: Mapped[str | None] = mapped_column(String(12), nullable=True)
    feedback_note_sealed: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    topic_label: Mapped[str | None] = mapped_column(String(60), nullable=True)
    question_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)


__all__ = [
    "AUDIENCES",
    "FEEDBACK",
    "MESSAGE_OUTCOMES",
    "SOURCE_KINDS",
    "SOURCE_STATUSES",
    "THREAD_STATUSES",
    "TOPICS",
    "KnowledgeChunk",
    "KnowledgeSource",
    "KnowledgeSourceVersion",
    "OnboardingQaMessage",
    "OnboardingQaThread",
]
