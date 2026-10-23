# ruff: file-ignore[line-too-long]
"""Transcripts (Epic 53, AD-66; migration ``20261008_400_03_p4_hubs``). Owner: ``transcripts``.

``transcripts`` is the audited header; ``transcript_segments`` are append-only and NOT audited, so no transcript text is
ever copied into ``audit_logs``. Erasure / retention delete the segments and keep the header as ``erased``.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.models import AuditedMixin, Base, TenantMixin, Timestamps, UUIDPk

SOURCE_KINDS = ("screening_conversation", "voice_session", "interview_capture", "upload")
TRANSCRIPT_STATUSES = ("pending", "ready", "failed", "erased")
SPEAKER_ROLES = ("agent", "candidate", "interviewer", "recruiter", "unknown")
MAX_SEGMENT_CHARS = 8000


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _tenant_fk(column: str, target: str, name: str, ondelete: str = "RESTRICT") -> ForeignKeyConstraint:
    return ForeignKeyConstraint(
        ["tenant_id", column], [f"{target}.tenant_id", f"{target}.id"], name=name, ondelete=ondelete
    )


class Transcript(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "transcripts"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        _tenant_fk("candidate_id", "candidates", "fk_transcripts_candidate_id_candidates"),
        _tenant_fk("application_id", "applications", "fk_transcripts_application_id_applications"),
        _tenant_fk("interview_id", "interviews", "fk_transcripts_interview_id_interviews"),
        _tenant_fk("file_id", "stored_files", "fk_transcripts_file_id_stored_files"),
        _tenant_fk("attested_by", "users", "fk_transcripts_attested_by_users"),
        CheckConstraint(_in("source_kind", SOURCE_KINDS), name="source_kind"),
        CheckConstraint(_in("status", TRANSCRIPT_STATUSES), name="status"),
        Index("ix_transcripts_source", "source_kind", "source_id"),
        Index("ix_transcripts_candidate_id", "candidate_id"),
    )

    source_kind: Mapped[str] = mapped_column(String(24), nullable=False)
    source_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    candidate_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    application_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    interview_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    language: Mapped[str | None] = mapped_column(String(10), nullable=True)
    status: Mapped[str] = mapped_column(String(10), nullable=False, server_default=text("'pending'"))
    segment_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    file_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    created_by_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    attested_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    attested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    erased_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class TranscriptSegment(UUIDPk, TenantMixin, Timestamps, Base):
    __tablename__ = "transcript_segments"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "transcript_id", "seq"),
        _tenant_fk(
            "transcript_id",
            "transcripts",
            "fk_transcript_segments_transcript_id_transcripts",
            ondelete="CASCADE",
        ),
        CheckConstraint(_in("speaker_role", SPEAKER_ROLES), name="speaker_role"),
        CheckConstraint("end_ms IS NULL OR start_ms IS NULL OR end_ms >= start_ms", name="times"),
        CheckConstraint(f"char_length(text) <= {MAX_SEGMENT_CHARS}", name="text_length"),
    )
    # Segment text can hold candidate personal data; claimed by ``transcripts.records``.
    __candidate_pii__ = True

    transcript_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    speaker_role: Mapped[str] = mapped_column(String(12), nullable=False)
    speaker_ref: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    start_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    end_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    text: Mapped[str] = mapped_column(Text, nullable=False)


__all__ = [
    "MAX_SEGMENT_CHARS",
    "SOURCE_KINDS",
    "SPEAKER_ROLES",
    "TRANSCRIPT_STATUSES",
    "Transcript",
    "TranscriptSegment",
]
