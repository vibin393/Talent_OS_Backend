"""Epic 22 (P2-9.2): P1 attachments -> documents (data only, idempotent).

Every ``attachments`` row becomes a ``documents`` row **with the same id** (so the P1 attachment API keeps its
ids and shapes) plus ``document_versions(version_no=1, source='migrated')`` over the same ``stored_files`` row.
``attachments.migrated_document_id`` records the link and an insert trigger makes the table read-only (new
uploads go through the documents facade). Dropping ``attachments`` is decided at the phase-close merge.

Downgrade clears ``migrated_document_id`` (and drops the column / trigger) only; documents stay.

Revision ID: 20261003_209_03_doc_attach_mig
Revises: 20261003_209_02_doc_esign
Create Date: 2026-10-03

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20261003_209_03_doc_attach_mig"
down_revision: str | tuple[str, ...] | None = "20261003_209_02_doc_esign"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

READ_ONLY = r"""
CREATE OR REPLACE FUNCTION tos_attachments_read_only() RETURNS trigger LANGUAGE plpgsql AS $fn$
BEGIN
    RAISE EXCEPTION USING ERRCODE = 'TOS02', MESSAGE = 'document_immutable:attachments';
END
$fn$
"""

MIGRATE_DOCUMENTS = r"""
WITH src AS (
    SELECT a.id, a.tenant_id, a.entity_type, a.entity_id, a.category, a.expires_on, a.note, a.archived_at,
           a.uploaded_by, a.created_at, f.original_name,
           row_number() OVER (PARTITION BY a.tenant_id ORDER BY a.created_at, a.id) AS rn
    FROM attachments a
    JOIN stored_files f ON f.id = a.file_id AND f.tenant_id = a.tenant_id
    WHERE a.migrated_document_id IS NULL
      AND NOT EXISTS (SELECT 1 FROM documents d WHERE d.id = a.id)
)
INSERT INTO documents (id, tenant_id, human_id, owner_kind, owner_id, worker_candidate_id, category, title,
                       access_level, retention_class, expires_on, signed, archived_at, note, created_by,
                       created_at, updated_at)
SELECT s.id, s.tenant_id,
       'DOC-' || lpad((COALESCE(seq.value, 0) + s.rn)::text, 4, '0'),
       s.entity_type, s.entity_id,
       CASE WHEN s.entity_type = 'candidate'
                 AND EXISTS (SELECT 1 FROM candidates c WHERE c.id = s.entity_id AND c.tenant_id = s.tenant_id)
            THEN s.entity_id END,
       s.category, left(COALESCE(NULLIF(s.original_name, ''), 'Attachment'), 300), 'entity',
       CASE s.category WHEN 'payslip' THEN 'financial'
                       WHEN 'offer_letter' THEN 'signed_agreements'
                       WHEN 'relieving_letter' THEN 'signed_agreements' END,
       s.expires_on, false, s.archived_at, s.note, s.uploaded_by, s.created_at, s.created_at
FROM src s
LEFT JOIN tenant_sequences seq ON seq.tenant_id = s.tenant_id AND seq.key = 'DOC'
"""

BUMP_SEQUENCES = r"""
INSERT INTO tenant_sequences (tenant_id, key, value)
SELECT d.tenant_id, 'DOC', max(substr(d.human_id, 5)::bigint)
FROM documents d
WHERE d.human_id ~ '^DOC-[0-9]+$'
GROUP BY d.tenant_id
ON CONFLICT (tenant_id, key) DO UPDATE SET value = GREATEST(tenant_sequences.value, EXCLUDED.value)
"""

MIGRATE_VERSIONS = r"""
INSERT INTO document_versions (id, tenant_id, document_id, worker_candidate_id, version_no, file_id, sha256, size,
                               source, created_by_kind, created_by_id, created_at)
SELECT gen_random_uuid(), a.tenant_id, a.id, d.worker_candidate_id, 1, a.file_id, f.sha256, f.size_bytes,
       'migrated', CASE WHEN a.uploaded_by IS NULL THEN 'system' ELSE 'user' END, a.uploaded_by, a.created_at
FROM attachments a
JOIN documents d ON d.id = a.id
JOIN stored_files f ON f.id = a.file_id AND f.tenant_id = a.tenant_id
WHERE a.migrated_document_id IS NULL
  AND NOT EXISTS (SELECT 1 FROM document_versions v WHERE v.document_id = a.id)
"""

LINK: tuple[str, ...] = (
    """
UPDATE documents d SET current_version_id = v.id
FROM document_versions v
WHERE v.document_id = d.id AND v.version_no = 1 AND d.current_version_id IS NULL
""",
    """
UPDATE attachments a SET migrated_document_id = a.id
WHERE a.migrated_document_id IS NULL AND EXISTS (SELECT 1 FROM documents d WHERE d.id = a.id)
""",
)


def upgrade() -> None:
    op.add_column("attachments", sa.Column("migrated_document_id", sa.Uuid(), nullable=True))
    op.execute(MIGRATE_DOCUMENTS)
    op.execute(BUMP_SEQUENCES)
    op.execute(MIGRATE_VERSIONS)
    for stmt in LINK:
        op.execute(stmt)
    op.execute(READ_ONLY)
    op.execute("DROP TRIGGER IF EXISTS tos_attachments_read_only ON attachments")
    op.execute(
        "CREATE TRIGGER tos_attachments_read_only BEFORE INSERT ON attachments "
        "FOR EACH ROW EXECUTE FUNCTION tos_attachments_read_only()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS tos_attachments_read_only ON attachments")
    op.execute("DROP FUNCTION IF EXISTS tos_attachments_read_only()")
    op.execute("UPDATE attachments SET migrated_document_id = NULL")
    op.drop_column("attachments", "migrated_document_id")
