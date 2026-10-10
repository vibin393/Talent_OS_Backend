"""Epic 8 offers core (tenant-owned, RLS): offer settings, letter templates (+ seed), offers (+ partial unique
index: one non-terminal offer per application), offer versions, offer status history.

Branch ``offers``. Default templates for existing tenants are seeded before RLS is enabled; new tenants get
them from ``offers_service.ensure_default_templates`` (provisioning task, lazily on first use). The seed
bodies are a frozen copy of ``app.services.offers.templates.DEFAULT_TEMPLATES`` at the time of writing.

Revision ID: 20261001_80
Revises: 20261001_72
Create Date: 2026-10-01

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_helpers import disable_rls, enable_rls

revision: str = "20261001_80"
down_revision: str | tuple[str, ...] | None = "20261001_72"
branch_labels: str | Sequence[str] | None = ("offers",)
# applications / secure links (Epic 6 / Epic 12) and approval_requests (Epic 4).
depends_on: str | Sequence[str] | None = ("20261001_61", "20261001_42")

TABLES: tuple[str, ...] = (
    "offer_settings",
    "offer_templates",
    "offers",
    "offer_versions",
    "offer_status_history",
)
TERMINAL = "('accepted', 'declined', 'expired', 'withdrawn')"
STATUSES = "('draft', 'pending_approval', 'approved', 'released', 'viewed', 'accepted', 'declined', 'expired', 'withdrawn')"
EMPLOYMENT = "('contract', 'contract_to_hire', 'permanent', 'freelance')"
UNITS = "('hour', 'day', 'month', 'year')"

_LABELS = {
    "contract": "Contract",
    "contract_to_hire": "Contract to hire",
    "permanent": "Permanent",
    "freelance": "Freelance",
}
_INTRO = {
    "permanent": "We are delighted to offer you a permanent position",
    "contract": "We are pleased to offer you a contract engagement",
    "contract_to_hire": "We are pleased to offer you a contract-to-hire engagement",
    "freelance": "We are pleased to offer you a freelance engagement",
}


def _body(employment_type: str) -> str:
    end_row = (
        "| End date | {{ offer.end_date }} |\n" if employment_type in ("contract", "contract_to_hire") else ""
    )
    return (
        "# Offer of employment\n\n"
        "{{ today }}\n\n"
        "Dear {{ candidate.full_name }},\n\n"
        f"{_INTRO[employment_type]} as **{{{{ offer.title }}}}** with {{{{ tenant.name }}}}.\n\n"
        "## Key terms\n\n"
        "| Term | Detail |\n"
        "| --- | --- |\n"
        "| Position | {{ offer.title }} |\n"
        "| Employment type | {{ offer.employment_type }} |\n"
        "| Compensation | {{ offer.pay_rate }} {{ offer.pay_rate_unit }} |\n"
        "| Start date | {{ offer.start_date }} |\n"
        f"{end_row}"
        "| Location | {{ offer.location }} ({{ offer.work_mode }}) |\n\n"
        "## Benefits\n\n"
        "{{ offer.benefits_summary }}\n\n"
        "## Acceptance\n\n"
        "This offer is valid until **{{ offer.expiry_date }}**. To accept, sign electronically by typing your "
        "full name on the offer page.\n\n"
        "Sincerely,\n\n"
        "{{ signatory.name }}  \n"
        "{{ signatory.title }}, {{ tenant.name }}\n\n"
        "*Reference: {{ offer.reference }}*\n"
    )


def _timestamps() -> list[sa.Column]:  # type: ignore[type-arg]
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    ]


def _tenant_fk(table: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["tenant_id"], ["tenants.id"], name=op.f(f"fk_{table}_tenant_id_tenants"), ondelete="RESTRICT"
    )


def upgrade() -> None:
    op.create_table(
        "offer_settings",
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("default_expiry_days", sa.Integer(), server_default=sa.text("7"), nullable=False),
        sa.Column("signatory_name", sa.String(length=200), nullable=True),
        sa.Column("signatory_title", sa.String(length=200), nullable=True),
        sa.Column("response_link_grace_hours", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "attach_letter_to_release_email", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        *_timestamps(),
        sa.CheckConstraint(
            "default_expiry_days BETWEEN 1 AND 90", name=op.f("ck_offer_settings_default_expiry_days")
        ),
        sa.CheckConstraint(
            "response_link_grace_hours BETWEEN 0 AND 168", name=op.f("ck_offer_settings_grace_hours")
        ),
        _tenant_fk("offer_settings"),
        sa.PrimaryKeyConstraint("tenant_id", name=op.f("pk_offer_settings")),
    )

    op.create_table(
        "offer_templates",
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column(
            "employment_types",
            postgresql.ARRAY(sa.String(length=20)),
            server_default=sa.text("'{}'::varchar[]"),
            nullable=False,
        ),
        sa.Column("body_md", sa.Text(), nullable=False),
        sa.Column("is_default", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        *_timestamps(),
        _tenant_fk("offer_templates"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_offer_templates")),
        sa.UniqueConstraint("tenant_id", "id", name=op.f("uq_offer_templates_tenant_id_id")),
    )
    op.create_index(op.f("ix_offer_templates_tenant_id"), "offer_templates", ["tenant_id"], unique=False)

    op.create_table(
        "offers",
        sa.Column("human_id", sa.String(length=20), nullable=False),
        sa.Column("application_id", sa.Uuid(), nullable=False),
        sa.Column("demand_id", sa.Uuid(), nullable=False),
        sa.Column("candidate_id", sa.Uuid(), nullable=False),
        sa.Column("client_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=20), server_default=sa.text("'draft'"), nullable=False),
        sa.Column("current_version_id", sa.Uuid(), nullable=True),
        sa.Column("released_version_id", sa.Uuid(), nullable=True),
        sa.Column("owner_id", sa.Uuid(), nullable=False),
        sa.Column("approval_request_id", sa.Uuid(), nullable=True),
        sa.Column("withdrawn_reason", sa.Text(), nullable=True),
        sa.Column("status_comment", sa.Text(), nullable=True),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("viewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("responded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lock_version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        *_timestamps(),
        sa.CheckConstraint(f"status IN {STATUSES}", name=op.f("ck_offers_status")),
        sa.ForeignKeyConstraint(
            ["tenant_id", "application_id"],
            ["applications.tenant_id", "applications.id"],
            name="fk_offers_application_id_applications",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "demand_id"],
            ["demands.tenant_id", "demands.id"],
            name="fk_offers_demand_id_demands",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "candidate_id"],
            ["candidates.tenant_id", "candidates.id"],
            name="fk_offers_candidate_id_candidates",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "client_id"],
            ["clients.tenant_id", "clients.id"],
            name="fk_offers_client_id_clients",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "owner_id"],
            ["users.tenant_id", "users.id"],
            name="fk_offers_owner_id_users",
            ondelete="RESTRICT",
        ),
        _tenant_fk("offers"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_offers")),
        sa.UniqueConstraint("tenant_id", "human_id", name=op.f("uq_offers_tenant_id_human_id")),
        sa.UniqueConstraint("tenant_id", "id", name=op.f("uq_offers_tenant_id_id")),
    )
    op.create_index(op.f("ix_offers_tenant_id"), "offers", ["tenant_id"], unique=False)
    op.create_index("ix_offers_demand_id", "offers", ["demand_id"], unique=False)
    op.create_index("ix_offers_candidate_id", "offers", ["candidate_id"], unique=False)
    op.create_index("ix_offers_status_expires_at", "offers", ["status", "expires_at"], unique=False)
    op.create_index(
        "uq_offers_open_per_application",
        "offers",
        ["tenant_id", "application_id"],
        unique=True,
        postgresql_where=sa.text(f"NOT (status IN {TERMINAL})"),
    )

    op.create_table(
        "offer_versions",
        sa.Column("offer_id", sa.Uuid(), nullable=False),
        sa.Column("version_no", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("employment_type", sa.String(length=20), nullable=False),
        sa.Column("pay_rate", sa.Numeric(precision=14, scale=2), nullable=False),
        sa.Column("pay_currency", sa.String(length=3), nullable=False),
        sa.Column("pay_rate_unit", sa.String(length=10), nullable=False),
        sa.Column("bill_rate", sa.Numeric(precision=14, scale=2), nullable=True),
        sa.Column("bill_currency", sa.String(length=3), nullable=True),
        sa.Column("bill_rate_unit", sa.String(length=10), nullable=True),
        sa.Column("burden_pct", sa.Numeric(precision=5, scale=2), nullable=True),
        sa.Column("margin_amount", sa.Numeric(precision=14, scale=2), nullable=True),
        sa.Column("margin_pct", sa.Numeric(precision=7, scale=2), nullable=True),
        sa.Column("bonus_amount", sa.Numeric(precision=14, scale=2), nullable=True),
        sa.Column("bonus_currency", sa.String(length=3), nullable=True),
        sa.Column("benefits_summary", sa.Text(), nullable=True),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=True),
        sa.Column("location", sa.String(length=300), nullable=True),
        sa.Column("work_mode", sa.String(length=10), nullable=True),
        sa.Column("expiry_date", sa.Date(), nullable=False),
        sa.Column("template_id", sa.Uuid(), nullable=True),
        sa.Column("template_version", sa.Integer(), nullable=True),
        sa.Column("rendered_body", sa.Text(), nullable=True),
        sa.Column("letter_file_id", sa.Uuid(), nullable=True),
        sa.Column("letter_sha256", sa.String(length=64), nullable=True),
        sa.Column("letter_input_hash", sa.String(length=64), nullable=True),
        sa.Column(
            "approval_state", sa.String(length=20), server_default=sa.text("'pending'"), nullable=False
        ),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("frozen", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        *_timestamps(),
        sa.CheckConstraint(
            f"employment_type IN {EMPLOYMENT}", name=op.f("ck_offer_versions_employment_type")
        ),
        sa.CheckConstraint(f"pay_rate_unit IN {UNITS}", name=op.f("ck_offer_versions_pay_rate_unit")),
        sa.CheckConstraint(
            f"bill_rate_unit IS NULL OR bill_rate_unit IN {UNITS}",
            name=op.f("ck_offer_versions_bill_rate_unit"),
        ),
        sa.CheckConstraint(
            "work_mode IS NULL OR work_mode IN ('onsite', 'hybrid', 'remote')",
            name=op.f("ck_offer_versions_work_mode"),
        ),
        sa.CheckConstraint(
            "approval_state IN ('not_required', 'pending', 'approved', 'returned')",
            name=op.f("ck_offer_versions_approval_state"),
        ),
        sa.CheckConstraint("pay_rate > 0", name=op.f("ck_offer_versions_pay_rate_positive")),
        sa.CheckConstraint(
            "bill_rate IS NULL OR bill_rate > 0", name=op.f("ck_offer_versions_bill_rate_positive")
        ),
        sa.CheckConstraint(
            "bonus_amount IS NULL OR bonus_amount > 0", name=op.f("ck_offer_versions_bonus_positive")
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "offer_id"],
            ["offers.tenant_id", "offers.id"],
            name="fk_offer_versions_offer_id_offers",
            ondelete="CASCADE",
        ),
        _tenant_fk("offer_versions"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_offer_versions")),
        sa.UniqueConstraint("offer_id", "version_no", name=op.f("uq_offer_versions_offer_id_version_no")),
        sa.UniqueConstraint("tenant_id", "id", name=op.f("uq_offer_versions_tenant_id_id")),
    )
    op.create_index(op.f("ix_offer_versions_tenant_id"), "offer_versions", ["tenant_id"], unique=False)
    op.create_index(op.f("ix_offer_versions_offer_id"), "offer_versions", ["offer_id"], unique=False)

    op.create_table(
        "offer_status_history",
        sa.Column("offer_id", sa.Uuid(), nullable=False),
        sa.Column("from_status", sa.String(length=20), nullable=True),
        sa.Column("to_status", sa.String(length=20), nullable=False),
        sa.Column("actor_kind", sa.String(length=20), nullable=False),
        sa.Column("actor_id", sa.Uuid(), nullable=True),
        sa.Column("version_no", sa.Integer(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "actor_kind IN ('user', 'candidate', 'system', 'workflow', 'ai', 'api_key')",
            name=op.f("ck_offer_status_history_actor_kind"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "offer_id"],
            ["offers.tenant_id", "offers.id"],
            name="fk_offer_status_history_offer_id_offers",
            ondelete="CASCADE",
        ),
        _tenant_fk("offer_status_history"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_offer_status_history")),
    )
    op.create_index(
        op.f("ix_offer_status_history_tenant_id"), "offer_status_history", ["tenant_id"], unique=False
    )
    op.create_index(
        "ix_offer_status_history_offer_id_at", "offer_status_history", ["offer_id", "at"], unique=False
    )

    # Seed one default template per employment type for existing tenants (before RLS is forced).
    bind = op.get_bind()
    for employment_type, label in _LABELS.items():
        bind.execute(
            sa.text(
                "INSERT INTO offer_templates (id, tenant_id, name, employment_types, body_md, is_default, version) "
                "SELECT gen_random_uuid(), t.id, :name, ARRAY[:etype]::varchar[], :body, true, 1 FROM tenants t"
            ),
            {"name": f"{label} offer", "etype": employment_type, "body": _body(employment_type)},
        )

    for table in TABLES:
        enable_rls(table)


def downgrade() -> None:
    for table in reversed(TABLES):
        disable_rls(table)
        op.drop_table(table)
