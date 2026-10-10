"""Epic 22 (P2-9.1 / P2-9.2): document templates, template versions, documents and document versions.

Branch ``p2_documents`` forks from the foundation contract layer. Templates are internal-only; documents and
their versions are visible to a Worker through ``worker_candidate_id`` and to client / vendor organisations
through ``org_grants`` (subject ``document``). Versions are append-only; a signed document only allows
``archived_at`` / ``superseded_by_id`` / ``expires_on`` to change.

Revision ID: 20261003_209_01_doc_templates
Revises: 20261002_200_07
Create Date: 2026-10-03

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_helpers import (
    GrantsRef,
    InheritRef,
    disable_immutability,
    disable_org_rls,
    disable_rls,
    enable_immutability,
    enable_org_rls,
    enable_rls,
)

revision: str = "20261003_209_01_doc_templates"
down_revision: str | tuple[str, ...] | None = "20261002_200_07"
branch_labels: str | Sequence[str] | None = ("p2_documents",)
depends_on: str | Sequence[str] | None = None

TABLES = ("document_templates", "document_template_versions", "documents", "document_versions")


def _base() -> list[sa.Column]:  # type: ignore[type-arg]
    return [sa.Column("id", sa.Uuid(), nullable=False), sa.Column("tenant_id", sa.Uuid(), nullable=False)]


def _ts() -> list[sa.Column]:  # type: ignore[type-arg]
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    ]


def _tenant(table: str) -> list[sa.Constraint]:
    return [
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=f"fk_{table}_tenant_id_tenants", ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name=f"pk_{table}"),
        sa.UniqueConstraint("tenant_id", "id", name=f"uq_{table}_tenant_id_id"),
    ]


def upgrade() -> None:
    op.create_table(
        "document_templates",
        *_base(),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("kind", sa.String(30), nullable=False),
        sa.Column("legal_entity_id", sa.Uuid(), nullable=True),
        sa.Column("signer_roles", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("esign_config_id", sa.Uuid(), nullable=True),
        sa.Column("current_version_id", sa.Uuid(), nullable=True),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        *_ts(),
        *_tenant("document_templates"),
        sa.CheckConstraint(
            "kind IN ('employment_agreement', 'contractor_agreement', 'nda', 'policy', 'offer_letter', "
            "'extension_letter', 'relieving_letter', 'experience_letter', 'client_form', 'other')",
            name="ck_document_templates_kind",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "legal_entity_id"],
            ["legal_entities.tenant_id", "legal_entities.id"],
            name="fk_document_templates_legal_entity_id",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "esign_config_id"],
            ["provider_connector_configs.tenant_id", "provider_connector_configs.id"],
            name="fk_document_templates_esign_config_id",
            ondelete="RESTRICT",
        ),
    )
    op.create_index("ix_document_templates_tenant_id", "document_templates", ["tenant_id"])
    op.create_index("ix_document_templates_kind", "document_templates", ["kind", "legal_entity_id"])

    op.create_table(
        "document_template_versions",
        *_base(),
        sa.Column("template_id", sa.Uuid(), nullable=False),
        sa.Column("version_no", sa.Integer(), nullable=False),
        sa.Column("body_md", sa.Text(), nullable=False),
        sa.Column(
            "merge_fields",
            postgresql.ARRAY(sa.Text()),
            server_default=sa.text("'{}'::text[]"),
            nullable=False,
        ),
        sa.Column("status", sa.String(12), server_default=sa.text("'draft'"), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("published_by", sa.Uuid(), nullable=True),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        *_ts(),
        *_tenant("document_template_versions"),
        sa.CheckConstraint(
            "status IN ('draft', 'published', 'retired')", name="ck_document_template_versions_status"
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "template_id"],
            ["document_templates.tenant_id", "document_templates.id"],
            name="fk_document_template_versions_template_id",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "template_id", "version_no", name="uq_document_template_versions_template_id_version_no"
        ),
    )
    op.create_index("ix_document_template_versions_tenant_id", "document_template_versions", ["tenant_id"])
    op.create_index(
        "ix_document_template_versions_template_id", "document_template_versions", ["template_id"]
    )

    op.create_table(
        "documents",
        *_base(),
        sa.Column("human_id", sa.String(20), nullable=False),
        sa.Column("owner_kind", sa.String(40), nullable=False),
        sa.Column("owner_id", sa.Uuid(), nullable=False),
        sa.Column("worker_candidate_id", sa.Uuid(), nullable=True),
        sa.Column("category", sa.String(40), nullable=False),
        sa.Column("title", sa.String(300), nullable=False),
        sa.Column("access_level", sa.String(10), server_default=sa.text("'entity'"), nullable=False),
        sa.Column("retention_class", sa.String(40), nullable=True),
        sa.Column("expires_on", sa.Date(), nullable=True),
        sa.Column("current_version_id", sa.Uuid(), nullable=True),
        sa.Column("signed", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("superseded_by_id", sa.Uuid(), nullable=True),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        *_ts(),
        *_tenant("documents"),
        sa.CheckConstraint("access_level IN ('entity', 'hr', 'finance')", name="ck_documents_access_level"),
        sa.UniqueConstraint("tenant_id", "human_id", name="uq_documents_tenant_id_human_id"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "worker_candidate_id"],
            ["candidates.tenant_id", "candidates.id"],
            name="fk_documents_worker_candidate_id",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "superseded_by_id"],
            ["documents.tenant_id", "documents.id"],
            name="fk_documents_superseded_by_id",
            ondelete="RESTRICT",
        ),
    )
    op.create_index("ix_documents_tenant_id", "documents", ["tenant_id"])
    op.create_index("ix_documents_owner", "documents", ["owner_kind", "owner_id"])
    op.create_index("ix_documents_worker_candidate_id", "documents", ["worker_candidate_id"])

    op.create_table(
        "document_versions",
        *_base(),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("worker_candidate_id", sa.Uuid(), nullable=True),
        sa.Column("version_no", sa.Integer(), nullable=False),
        sa.Column("file_id", sa.Uuid(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("size", sa.BigInteger(), nullable=False),
        sa.Column("source", sa.String(10), nullable=False),
        sa.Column("template_version_id", sa.Uuid(), nullable=True),
        sa.Column("signature_request_id", sa.Uuid(), nullable=True),
        sa.Column("created_by_kind", sa.String(20), server_default=sa.text("'system'"), nullable=False),
        sa.Column("created_by_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        *_tenant("document_versions"),
        sa.CheckConstraint(
            "source IN ('upload', 'generated', 'signed', 'migrated')", name="ck_document_versions_source"
        ),
        sa.UniqueConstraint("document_id", "version_no", name="uq_document_versions_document_id_version_no"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "document_id"],
            ["documents.tenant_id", "documents.id"],
            name="fk_document_versions_document_id",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "file_id"],
            ["stored_files.tenant_id", "stored_files.id"],
            name="fk_document_versions_file_id",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "template_version_id"],
            ["document_template_versions.tenant_id", "document_template_versions.id"],
            name="fk_document_versions_template_version_id",
            ondelete="RESTRICT",
        ),
    )
    op.create_index("ix_document_versions_tenant_id", "document_versions", ["tenant_id"])
    op.create_index("ix_document_versions_document_id", "document_versions", ["document_id"])
    op.create_index("ix_document_versions_sha256", "document_versions", ["sha256"])

    for table in TABLES:
        enable_rls(table)
    enable_org_rls("document_templates")
    enable_org_rls("document_template_versions")
    enable_org_rls("documents", worker_col="worker_candidate_id", grants=GrantsRef("document"))
    enable_org_rls(
        "document_versions",
        worker_col="worker_candidate_id",
        grants=GrantsRef("document", "document_id"),
        inherit_from=InheritRef("documents", "document_id", ("worker_candidate_id",)),
    )
    enable_immutability("document_versions", status_column=None)
    enable_immutability(
        "documents",
        status_column="signed",
        mutable_states=("false",),
        allowed_columns=("archived_at", "superseded_by_id", "expires_on"),
    )


def downgrade() -> None:
    disable_immutability("documents")
    disable_immutability("document_versions")
    for table in reversed(TABLES):
        disable_org_rls(table)
        disable_rls(table)
        op.drop_table(table)
