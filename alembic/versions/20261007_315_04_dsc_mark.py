# ruff: noqa: E501
"""Phase 3 Epic 51 (FR-253) 315_04: server-side DSC marker on issue.

``tos_dsc_mark`` (BEFORE UPDATE on ``invoices`` / ``credit_notes``) sets ``dsc_status='pending'`` in the same
statement that assigns the document number (``number`` NULL -> NOT NULL, still a draft, so the immutability guard
allows it) when the resolved DSC policy (exact Client row, else the NULL-Client row of the supplier GST
registration; active only) is required. No extra round trip inside the issue transaction (the gap-free numbering
concurrency suite is unaffected); the issue code reads the refreshed column to decide whether to queue signing.

Revision ID: 20261007_315_04_dsc_mark
Revises: 20261007_315_03_fx_feed
Create Date: 2026-10-07
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20261007_315_04_dsc_mark"
down_revision: str | tuple[str, ...] | None = "20261007_315_03_fx_feed"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = ("invoices", "credit_notes")

FUNCTION = """
CREATE OR REPLACE FUNCTION tos_dsc_mark() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    req boolean;
BEGIN
    IF NEW.number IS NOT NULL AND OLD.number IS NULL
       AND NEW.supplier_gst_registration_id IS NOT NULL
       AND NEW.dsc_status = 'not_required' THEN
        SELECT p.required INTO req
          FROM dsc_policies p
         WHERE p.tenant_id = NEW.tenant_id
           AND p.gst_registration_id = NEW.supplier_gst_registration_id
           AND p.active
           AND (p.client_id = NEW.client_id OR p.client_id IS NULL)
         ORDER BY (p.client_id IS NULL)
         LIMIT 1;
        IF req THEN
            NEW.dsc_status := 'pending';
        END IF;
    END IF;
    RETURN NEW;
END;
$$
"""


def upgrade() -> None:
    op.execute(FUNCTION)
    for t in TABLES:
        op.execute(f"DROP TRIGGER IF EXISTS tos_dsc_mark ON {t}")
        op.execute(
            f"CREATE TRIGGER tos_dsc_mark BEFORE UPDATE OF number ON {t} FOR EACH ROW EXECUTE FUNCTION tos_dsc_mark()"
        )


def downgrade() -> None:
    for t in TABLES:
        op.execute(f"DROP TRIGGER IF EXISTS tos_dsc_mark ON {t}")
    op.execute("DROP FUNCTION IF EXISTS tos_dsc_mark()")
