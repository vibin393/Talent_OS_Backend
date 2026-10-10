# ruff: noqa: E501
"""Epic 62 (story 62.4, FR-310): Knowledge Sources, versions and searchable chunks.

* ``knowledge_sources`` (audited) -- article / file / onboarding template / policy document for an audience.
* ``knowledge_source_versions`` (append-only, audited) -- markdown body or an uploaded file + extracted text.
* ``knowledge_chunks`` (not audited) -- the chunked text of each version (generated ``text_tsv`` + optional local
  embedding); only ``active`` chunks are searchable.

Internal tables only: tenant RLS + internal-only org RLS.

Revision ID: 20261008_409_01_knowledge
Revises: 20261008_400_04_p4_alters
Create Date: 2026-10-08
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_helpers import disable_org_rls, disable_rls, enable_org_rls, enable_rls

revision: str = "20261008_409_01_knowledge"
down_revision: str | tuple[str, ...] | None = "20261008_400_04_p4_alters"
branch_labels: str | Sequence[str] | None = ("p4_compliance_agents",)
depends_on: str | Sequence[str] | None = None

KINDS = ("article", "file", "onboarding_template", "policy_document")
AUDIENCES = ("worker", "candidate", "both", "internal")
STATUSES = ("draft", "published", "unpublished", "archived")
EXTRACT = ("pending", "ok", "failed", "unsupported")
TABLES = ("knowledge_sources", "knowledge_source_versions", "knowledge_chunks")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _base(table: str) -> list[Any]:
    return [
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=f"pk_{table}"),
        sa.UniqueConstraint("tenant_id", "id", name=f"uq_{table}_tenant_id_id"),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=f"fk_{table}_tenant_id_tenants", ondelete="RESTRICT"
        ),
    ]


def _fk(column: str, target: str, name: str, ondelete: str = "RESTRICT") -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["tenant_id", column], [f"{target}.tenant_id", f"{target}.id"], name=name, ondelete=ondelete
    )


def _ck(table: str, name: str, condition: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(condition, name=op.f(f"ck_{table}_{name}"))


def _uuid(name: str, nullable: bool = True) -> sa.Column:
    return sa.Column(name, sa.Uuid(), nullable=nullable)


def upgrade() -> None:
    t = "knowledge_sources"
    op.create_table(
        t,
        *_base(t),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("audience", sa.String(12), nullable=False),
        sa.Column("status", sa.String(12), server_default=sa.text("'draft'"), nullable=False),
        sa.Column("current_version_no", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("published_version_no", sa.Integer(), nullable=True),
        sa.Column("ref_kind", sa.String(30), nullable=True),
        _uuid("ref_id"),
        _uuid("legal_entity_id"),
        _uuid("client_id"),
        _uuid("owner_id"),
        sa.Column(
            "tags", postgresql.ARRAY(sa.String(40)), server_default=sa.text("'{}'::varchar[]"), nullable=False
        ),
        sa.Column("last_reviewed_on", sa.Date(), nullable=True),
        _fk("owner_id", "users", "fk_knowledge_sources_owner_id_users"),
        _ck(t, "kind", _in("kind", KINDS)),
        _ck(t, "audience", _in("audience", AUDIENCES)),
        _ck(t, "status", _in("status", STATUSES)),
    )
    op.create_index("ix_knowledge_sources_status_audience", t, ["tenant_id", "status", "audience"])

    t = "knowledge_source_versions"
    op.create_table(
        t,
        *_base(t),
        _uuid("source_id", False),
        sa.Column("version_no", sa.Integer(), nullable=False),
        sa.Column("body_md", sa.Text(), nullable=True),
        _uuid("file_id"),
        sa.Column("extracted_text", sa.Text(), nullable=True),
        sa.Column("extract_status", sa.String(12), server_default=sa.text("'pending'"), nullable=False),
        sa.Column("content_sha256", sa.String(64), nullable=False),
        _uuid("created_by"),
        sa.UniqueConstraint("tenant_id", "source_id", "version_no", name="uq_knowledge_source_versions_no"),
        _fk("source_id", "knowledge_sources", "fk_knowledge_source_versions_source_id", "CASCADE"),
        _ck(t, "extract_status", _in("extract_status", EXTRACT)),
        _ck(t, "body_length", "body_md IS NULL OR char_length(body_md) <= 100000"),
    )

    t = "knowledge_chunks"
    op.create_table(
        t,
        *_base(t),
        _uuid("source_id", False),
        sa.Column("version_no", sa.Integer(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("heading", sa.String(300), nullable=True),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column(
            "text_tsv",
            postgresql.TSVECTOR(),
            sa.Computed(
                "to_tsvector('english'::regconfig, coalesce(heading, '') || ' ' || text)", persisted=True
            ),
            nullable=True,
        ),
        sa.Column("embedding", postgresql.ARRAY(sa.Float(precision=24)), nullable=True),
        sa.Column("embedding_model", sa.String(100), nullable=True),
        sa.Column("audience", sa.String(12), nullable=False),
        _uuid("legal_entity_id"),
        _uuid("client_id"),
        sa.Column("active", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.UniqueConstraint("tenant_id", "source_id", "version_no", "seq", name="uq_knowledge_chunks_seq"),
        _fk("source_id", "knowledge_sources", "fk_knowledge_chunks_source_id", "CASCADE"),
        _ck(t, "audience", _in("audience", AUDIENCES)),
        _ck(t, "text_length", "char_length(text) <= 2000"),
    )
    op.create_index("ix_knowledge_chunks_active_audience", t, ["tenant_id", "active", "audience"])
    op.create_index("ix_knowledge_chunks_text_tsv", t, ["text_tsv"], postgresql_using="gin")

    for table in TABLES:
        op.create_index(f"ix_{table}_tenant_id", table, ["tenant_id"])
        enable_rls(table)
        enable_org_rls(table)


def downgrade() -> None:
    for table in reversed(TABLES):
        disable_org_rls(table)
        disable_rls(table)
        op.drop_table(table)
