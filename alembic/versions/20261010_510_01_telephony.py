# ruff: noqa: E501
"""Epic 76 (Real Telephony & STT, stories 76.1 / 76.2 / 76.5, spec A1): ``telephony_settings``, ``telephony_numbers``,
``user_call_phones`` and ``voice_calls``. Tenant tables with RLS + org RLS (internal only).

Revision ID: 20261010_510_01_telephony
Revises: 20261009_500_06_p5_vault_drop
Create Date: 2026-10-10
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_helpers import disable_org_rls, disable_rls, enable_org_rls, enable_rls

revision: str = "20261010_510_01_telephony"
down_revision: str | tuple[str, ...] | None = "20261009_500_06_p5_vault_drop"
branch_labels: str | Sequence[str] | None = ("p5_telephony",)
depends_on: str | Sequence[str] | None = None

TABLES = ("telephony_settings", "telephony_numbers", "user_call_phones", "voice_calls")
CALL_STATUSES = (
    "requested", "refused", "dialing_recruiter", "dialing_candidate", "in_progress", "completed", "no_answer", "busy",
    "failed", "cancelled",
)  # fmt: skip


def _in(column: str, values: Sequence[str]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _base(t: str) -> list[sa.Column]:  # type: ignore[type-arg]
    return [
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(), sa.ForeignKey("tenants.id", ondelete="RESTRICT", name=f"fk_{t}_tenant_id_tenants"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    ]


def _fk(t: str, column: str, target: str, name: str, ondelete: str = "RESTRICT") -> sa.ForeignKeyConstraint:  # noqa: ARG001 - table kept for symmetry
    return sa.ForeignKeyConstraint(["tenant_id", column], [f"{target}.tenant_id", f"{target}.id"], name=name, ondelete=ondelete)


def upgrade() -> None:
    t = "telephony_settings"
    op.create_table(
        t,
        *_base(t),
        sa.Column("call_window_start", sa.Time(), server_default=sa.text("'09:00'"), nullable=False),
        sa.Column("call_window_end", sa.Time(), server_default=sa.text("'21:00'"), nullable=False),
        sa.Column("daily_cost_cap_minor", sa.BigInteger(), server_default=sa.text("200000"), nullable=False),
        sa.Column("cost_alert_pct", sa.SmallInteger(), server_default=sa.text("80"), nullable=False),
        sa.Column("max_concurrent_per_user", sa.SmallInteger(), server_default=sa.text("1"), nullable=False),
        sa.Column("max_call_minutes", sa.SmallInteger(), server_default=sa.text("30"), nullable=False),
        sa.Column("recording_notice_text", sa.String(300), server_default=sa.text("'This call is recorded for recruitment screening by {tenant}.'"), nullable=False),
        sa.Column("stt_enabled", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("stt_language_hints", postgresql.ARRAY(sa.String(10)), server_default=sa.text("'{en-IN,hi-IN}'::varchar(10)[]"), nullable=False),
        sa.Column("auto_transcribe_uploads", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("updated_by", sa.Uuid(), nullable=True),
        sa.UniqueConstraint("tenant_id", "id", name=f"uq_{t}_tenant_id_id"),
        sa.UniqueConstraint("tenant_id", name="uq_telephony_settings_tenant_id"),
        _fk(t, "updated_by", "users", "fk_telephony_settings_updated_by_users"),
        sa.CheckConstraint("cost_alert_pct BETWEEN 50 AND 95", name=op.f("ck_telephony_settings_cost_alert_pct")),
        sa.CheckConstraint("max_concurrent_per_user BETWEEN 1 AND 3", name=op.f("ck_telephony_settings_max_concurrent")),
        sa.CheckConstraint("max_call_minutes BETWEEN 5 AND 60", name=op.f("ck_telephony_settings_max_call_minutes")),
        sa.CheckConstraint("daily_cost_cap_minor >= 0", name=op.f("ck_telephony_settings_daily_cost_cap")),
        sa.CheckConstraint("call_window_end > call_window_start", name=op.f("ck_telephony_settings_call_window")),
    )

    t = "telephony_numbers"
    op.create_table(
        t,
        *_base(t),
        sa.Column("installation_id", sa.Uuid(), nullable=False),
        sa.Column("legal_entity_id", sa.Uuid(), nullable=True),
        sa.Column("provider_key", sa.String(20), nullable=False),
        sa.Column("number_e164", sa.String(16), nullable=False),
        sa.Column("label", sa.String(80), nullable=True),
        sa.Column("regulatory_ref", sa.String(120), nullable=True),
        sa.Column("rate_per_minute_minor", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("status", sa.String(10), server_default=sa.text("'active'"), nullable=False),
        sa.UniqueConstraint("tenant_id", "id", name=f"uq_{t}_tenant_id_id"),
        _fk(t, "installation_id", "app_installations", "fk_telephony_numbers_installation_id_inst"),
        _fk(t, "legal_entity_id", "legal_entities", "fk_telephony_numbers_legal_entity_id_le"),
        sa.CheckConstraint(_in("provider_key", ("exotel", "twilio", "plivo", "fake")), name=op.f("ck_telephony_numbers_provider_key")),
        sa.CheckConstraint(_in("status", ("active", "inactive")), name=op.f("ck_telephony_numbers_status")),
        sa.CheckConstraint("rate_per_minute_minor >= 0", name=op.f("ck_telephony_numbers_rate")),
    )
    op.create_index(
        "uq_telephony_numbers_active", t, ["tenant_id", "legal_entity_id"], unique=True,
        postgresql_where=sa.text("status = 'active'"), postgresql_nulls_not_distinct=True,
    )  # fmt: skip

    t = "user_call_phones"
    op.create_table(
        t,
        *_base(t),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("phone_cipher", sa.LargeBinary(), nullable=False),
        sa.Column("phone_last4", sa.CHAR(4), nullable=False),
        sa.Column("phone_hash", sa.CHAR(64), nullable=False),
        sa.Column("status", sa.String(10), server_default=sa.text("'pending'"), nullable=False),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("verification_method", sa.String(20), nullable=True),
        sa.Column("attested_by", sa.Uuid(), nullable=True),
        sa.Column("otp_hash", sa.CHAR(64), nullable=True),
        sa.Column("otp_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("otp_attempts", sa.SmallInteger(), server_default=sa.text("0"), nullable=False),
        sa.UniqueConstraint("tenant_id", "id", name=f"uq_{t}_tenant_id_id"),
        _fk(t, "user_id", "users", "fk_user_call_phones_user_id_users", "CASCADE"),
        _fk(t, "attested_by", "users", "fk_user_call_phones_attested_by_users"),
        sa.CheckConstraint(_in("status", ("pending", "verified", "revoked")), name=op.f("ck_user_call_phones_status")),
        sa.CheckConstraint(
            "verification_method IS NULL OR " + _in("verification_method", ("otp_sms", "admin_attested")),
            name=op.f("ck_user_call_phones_method"),
        ),
    )
    op.create_index("uq_user_call_phones_live", t, ["tenant_id", "user_id"], unique=True, postgresql_where=sa.text("status <> 'revoked'"))

    t = "voice_calls"
    op.create_table(
        t,
        *_base(t),
        sa.Column("human_id", sa.String(20), nullable=False),
        sa.Column("voice_session_id", sa.Uuid(), nullable=False),
        sa.Column("application_id", sa.Uuid(), nullable=True),
        sa.Column("candidate_id", sa.Uuid(), nullable=True),
        sa.Column("legal_entity_id", sa.Uuid(), nullable=True),
        sa.Column("placed_by", sa.Uuid(), nullable=False),
        sa.Column("installation_id", sa.Uuid(), nullable=True),
        sa.Column("config_id", sa.Uuid(), nullable=True),
        sa.Column("number_id", sa.Uuid(), nullable=True),
        sa.Column("provider_key", sa.String(20), nullable=False),
        sa.Column("external_ref", sa.String(200), nullable=True),
        sa.Column("status", sa.String(20), server_default=sa.text("'requested'"), nullable=False),
        sa.Column("refusal_code", sa.String(40), nullable=True),
        sa.Column("failure_code", sa.String(40), nullable=True),
        sa.Column("recording_requested", sa.Boolean(), nullable=False),
        sa.Column("recording_unavailable_reason", sa.String(40), nullable=True),
        sa.Column("dual_channel", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("requested_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("answered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duration_s", sa.Integer(), nullable=True),
        sa.Column("billable_minutes", sa.Integer(), nullable=True),
        sa.Column("rate_per_minute_minor", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("reserved_minor", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("cost_estimate_minor", sa.BigInteger(), nullable=True),
        sa.Column("provider_price_minor", sa.BigInteger(), nullable=True),
        sa.Column("price_currency", sa.CHAR(3), nullable=True),
        sa.Column("provider_recording_ref", sa.String(300), nullable=True),
        sa.Column("recording_fetch_status", sa.String(10), server_default=sa.text("'none'"), nullable=False),
        sa.Column("provider_delete_status", sa.String(12), server_default=sa.text("'not_needed'"), nullable=False),
        sa.Column("provider_delete_attempts", sa.SmallInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("provider_delete_first_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("provider_deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("activity_id", sa.Uuid(), nullable=True),
        sa.Column("callback_token_hash", sa.CHAR(64), nullable=False),
        sa.Column("token_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("tenant_id", "id", name=f"uq_{t}_tenant_id_id"),
        sa.UniqueConstraint("tenant_id", "human_id", name="uq_voice_calls_human_id"),
        _fk(t, "voice_session_id", "voice_sessions", "fk_voice_calls_voice_session_id_vs"),
        _fk(t, "placed_by", "users", "fk_voice_calls_placed_by_users"),
        _fk(t, "number_id", "telephony_numbers", "fk_voice_calls_number_id_numbers"),
        sa.CheckConstraint(_in("status", CALL_STATUSES), name=op.f("ck_voice_calls_status")),
        sa.CheckConstraint(
            "recording_unavailable_reason IS NULL OR "
            + _in("recording_unavailable_reason", ("policy_not_permitted", "consent_missing", "notice_unavailable", "provider_unsupported")),
            name=op.f("ck_voice_calls_recording_reason"),
        ),
        sa.CheckConstraint(_in("recording_fetch_status", ("none", "pending", "stored", "failed", "skipped")), name=op.f("ck_voice_calls_fetch_status")),
        sa.CheckConstraint(
            _in("provider_delete_status", ("not_needed", "pending", "deleted", "failed", "unsupported")), name=op.f("ck_voice_calls_delete_status")
        ),
    )
    op.create_index("ix_voice_calls_placed_by_status", t, ["tenant_id", "placed_by", "status"])
    op.create_index("ix_voice_calls_voice_session_id", t, ["tenant_id", "voice_session_id"])
    op.create_index("ix_voice_calls_candidate_id", t, ["candidate_id"])
    op.create_index("ix_voice_calls_token_hash", t, ["callback_token_hash"])
    op.create_index(
        "ix_voice_calls_provider_delete", t, ["provider_delete_status"],
        postgresql_where=sa.text("provider_delete_status IN ('pending', 'failed')"),
    )  # fmt: skip
    op.create_index(
        "uq_voice_calls_external_ref", t, ["tenant_id", "provider_key", "external_ref"], unique=True,
        postgresql_where=sa.text("external_ref IS NOT NULL"),
    )  # fmt: skip
    for table in TABLES:
        op.create_index(f"ix_{table}_tenant_id", table, ["tenant_id"])
        enable_rls(table)
        enable_org_rls(table)


def downgrade() -> None:
    for table in reversed(TABLES):
        disable_org_rls(table)
        disable_rls(table)
        op.drop_table(table)
