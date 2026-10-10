"""Phase 2 foundation (Epic 13, stories 13.1 / 13.4): extensions, reference data, guard functions.

* ``btree_gist`` + ``pgcrypto`` extensions (AD-16).
* Consent purpose / source CHECK widening (Addendum R12).
* Platform tables ``gst_states`` / ``currencies`` seeded from ``app/core/data/*.json`` (SELECT-only for
  ``talent_app``).
* Generic trigger functions ``tos_immutable``, ``tos_period_guard``, ``tos_source_lock``, ``tos_inherit_org``
  (``app.core.migration_helpers``).
* ``audit_logs.actor_type`` CHECK gains ``client_user``, ``vendor_user`` and ``secure_link``.

Revision ID: 20261002_200_01
Revises: 20261001_901
Create Date: 2026-10-02

"""

import json
from collections.abc import Sequence
from pathlib import Path

import sqlalchemy as sa

from alembic import op
from app.core.migration_helpers import create_guard_functions, drop_guard_functions
from app.core.settings import get_settings

revision: str = "20261002_200_01"
down_revision: str | tuple[str, ...] | None = "20261001_901"
branch_labels: str | Sequence[str] | None = ("p2_foundation",)
depends_on: str | Sequence[str] | None = None

DATA_DIR = Path(__file__).resolve().parents[2] / "app" / "core" / "data"

P1_CONSENT_PURPOSES = ("data_processing", "ai_processing", "client_submission", "marketing")
P2_CONSENT_PURPOSES = (
    *P1_CONSENT_PURPOSES,
    "bgv",
    "reference_check",
    "vendor_collection",
    "whatsapp",
    "engagement",
)
P1_CONSENT_SOURCES = ("candidate_link", "candidate_portal", "application", "recruiter_attestation", "import")
P2_CONSENT_SOURCES = (
    *P1_CONSENT_SOURCES,
    "vendor_attestation",
    "vendor_notice",
    "worker_portal",
    "placement",
    "whatsapp_optin",
    "recipient_link",
)
P1_ACTOR_TYPES = ("user", "candidate", "platform", "system", "ai", "workflow", "api_key")
P2_ACTOR_TYPES = (*P1_ACTOR_TYPES, "client_user", "vendor_user", "secure_link")
GST_STATE_KINDS = ("state", "ut_legislature", "ut_no_legislature", "other_territory", "other_country")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _replace_check(table: str, name: str, condition: str) -> None:
    op.drop_constraint(op.f(name), table, type_="check")
    op.create_check_constraint(op.f(name), table, condition)


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")

    # R12: consent purposes / sources (P1 privacy CHECKs).
    _replace_check("consents", "ck_consents_purpose", _in("purpose", P2_CONSENT_PURPOSES))
    _replace_check("consent_notices", "ck_consent_notices_purpose", _in("purpose", P2_CONSENT_PURPOSES))
    _replace_check("consents", "ck_consents_source", _in("source", P2_CONSENT_SOURCES))

    # Reference data (platform tables).
    op.create_table(
        "gst_states",
        sa.Column("code", sa.CHAR(length=2), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.CheckConstraint(_in("kind", GST_STATE_KINDS), name=op.f("ck_gst_states_kind")),
        sa.PrimaryKeyConstraint("code", name=op.f("pk_gst_states")),
    )
    op.create_table(
        "currencies",
        sa.Column("code", sa.CHAR(length=3), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("minor_units", sa.SmallInteger(), nullable=False),
        sa.Column("document_allowed", sa.Boolean(), nullable=False),
        sa.CheckConstraint("minor_units BETWEEN 0 AND 4", name=op.f("ck_currencies_minor_units")),
        sa.PrimaryKeyConstraint("code", name=op.f("pk_currencies")),
    )
    states = json.loads((DATA_DIR / "gst_states.json").read_text(encoding="utf-8"))
    currencies = json.loads((DATA_DIR / "currencies.json").read_text(encoding="utf-8"))
    op.bulk_insert(
        sa.table("gst_states", sa.column("code"), sa.column("name"), sa.column("kind")),
        [{"code": r["code"], "name": r["name"], "kind": r["kind"]} for r in states],
    )
    op.bulk_insert(
        sa.table(
            "currencies",
            sa.column("code"),
            sa.column("name"),
            sa.column("minor_units"),
            sa.column("document_allowed"),
        ),
        [
            {
                "code": r["code"],
                "name": r["name"],
                "minor_units": int(r["minor_units"]),
                "document_allowed": bool(r["document_allowed"]),
            }
            for r in currencies
        ],
    )
    role = get_settings().db_app_user
    for table in ("gst_states", "currencies"):
        op.execute(f"REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON {table} FROM {role}")
        op.execute(f"GRANT SELECT ON {table} TO {role}")

    create_guard_functions()

    _replace_check("audit_logs", "ck_audit_logs_actor_type", _in("actor_type", P2_ACTOR_TYPES))


def downgrade() -> None:
    _replace_check("audit_logs", "ck_audit_logs_actor_type", _in("actor_type", P1_ACTOR_TYPES))
    drop_guard_functions()
    op.drop_table("currencies")
    op.drop_table("gst_states")
    _replace_check("consents", "ck_consents_source", _in("source", P1_CONSENT_SOURCES))
    _replace_check("consent_notices", "ck_consent_notices_purpose", _in("purpose", P1_CONSENT_PURPOSES))
    _replace_check("consents", "ck_consents_purpose", _in("purpose", P1_CONSENT_PURPOSES))
    # The extensions stay (other objects may use them; dropping btree_gist / pgcrypto is never required).
