"""Epic 7 interview plans (tenant-owned, RLS): interview settings, kits, stage templates (+ default template
seed "Technical -> Client -> HR" for existing tenants), per-demand interview stages.

Branch ``interviews`` (spec A8); new tenants get the default template lazily on first use.

Revision ID: 20261001_70
Revises: 20261001_103
Create Date: 2026-10-01

"""

import json
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_helpers import disable_rls, enable_rls

revision: str = "20261001_70"
down_revision: str | tuple[str, ...] | None = '20261001_103'
branch_labels: str | Sequence[str] | None = ['interviews']
depends_on: str | Sequence[str] | None = '20261001_61'

TABLES: tuple[str, ...] = ('interview_settings', 'interview_kits', 'interview_stage_templates', 'interview_stages')

DEFAULT_TEMPLATE_STAGES = [
    {"name": "Technical", "kind": "technical", "kit_id": None, "default_duration_minutes": 60},
    {"name": "Client", "kind": "client", "kit_id": None, "default_duration_minutes": 60},
    {"name": "HR", "kind": "hr", "kit_id": None, "default_duration_minutes": 30},
]
SEED_TEMPLATE = """
    INSERT INTO interview_stage_templates (id, tenant_id, name, stages, is_default)
    SELECT gen_random_uuid(), t.id, :name, CAST(:stages AS jsonb), true
    FROM tenants t
"""


def upgrade() -> None:
    op.create_table('interview_settings',
    sa.Column('tenant_id', sa.Uuid(), nullable=False),
    sa.Column('reminder_offsets_minutes', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[1440, 60]'::jsonb"), nullable=False),
    sa.Column('scorecard_reminder_after_hours', sa.Integer(), server_default=sa.text('24'), nullable=False),
    sa.Column('external_scorecard_link_days_after', sa.Integer(), server_default=sa.text('7'), nullable=False),
    sa.Column('default_duration_minutes', sa.Integer(), server_default=sa.text('60'), nullable=False),
    sa.Column('rating_scale', sa.Integer(), server_default=sa.text('5'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint('default_duration_minutes BETWEEN 15 AND 480', name=op.f('ck_interview_settings_duration')),
    sa.CheckConstraint('external_scorecard_link_days_after BETWEEN 1 AND 90', name=op.f('ck_interview_settings_link_days')),
    sa.CheckConstraint('rating_scale BETWEEN 3 AND 10', name=op.f('ck_interview_settings_rating_scale')),
    sa.CheckConstraint('scorecard_reminder_after_hours BETWEEN 1 AND 720', name=op.f('ck_interview_settings_scorecard_reminder')),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], name=op.f('fk_interview_settings_tenant_id_tenants'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('tenant_id', name=op.f('pk_interview_settings'))
    )
    op.create_table('interview_kits',
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('competencies', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
    sa.Column('questions', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
    sa.Column('rating_scale', sa.Integer(), server_default=sa.text('5'), nullable=False),
    sa.Column('recommendation_options', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text('\'["strong_yes", "yes", "no", "strong_no"]\'::jsonb'), nullable=False),
    sa.Column('version', sa.Integer(), server_default=sa.text('1'), nullable=False),
    sa.Column('archived_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('tenant_id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint('rating_scale BETWEEN 3 AND 10', name=op.f('ck_interview_kits_rating_scale')),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], name=op.f('fk_interview_kits_tenant_id_tenants'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_interview_kits')),
    sa.UniqueConstraint('tenant_id', 'id', name=op.f('uq_interview_kits_tenant_id_id'))
    )
    op.create_index(op.f('ix_interview_kits_tenant_id'), 'interview_kits', ['tenant_id'], unique=False)
    op.create_table('interview_stage_templates',
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('stages', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
    sa.Column('is_default', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('archived_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('tenant_id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], name=op.f('fk_interview_stage_templates_tenant_id_tenants'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_interview_stage_templates')),
    sa.UniqueConstraint('tenant_id', 'id', name=op.f('uq_interview_stage_templates_tenant_id_id'))
    )
    op.create_index(op.f('ix_interview_stage_templates_tenant_id'), 'interview_stage_templates', ['tenant_id'], unique=False)
    op.create_index('uq_interview_stage_templates_default', 'interview_stage_templates', ['tenant_id'], unique=True, postgresql_where=sa.text('is_default AND archived_at IS NULL'))
    op.create_table('interview_stages',
    sa.Column('demand_id', sa.Uuid(), nullable=False),
    sa.Column('position', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('kind', sa.String(length=20), nullable=False),
    sa.Column('kit_id', sa.Uuid(), nullable=True),
    sa.Column('default_duration_minutes', sa.Integer(), server_default=sa.text('60'), nullable=False),
    sa.Column('is_final', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('archived_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('tenant_id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("kind IN ('technical', 'hr', 'client', 'managerial', 'other')", name=op.f('ck_interview_stages_kind')),
    sa.CheckConstraint('default_duration_minutes BETWEEN 15 AND 480', name=op.f('ck_interview_stages_duration')),
    sa.ForeignKeyConstraint(['tenant_id', 'demand_id'], ['demands.tenant_id', 'demands.id'], name='fk_interview_stages_demand_id_demands', ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['tenant_id', 'kit_id'], ['interview_kits.tenant_id', 'interview_kits.id'], name='fk_interview_stages_kit_id_interview_kits', ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], name=op.f('fk_interview_stages_tenant_id_tenants'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_interview_stages')),
    sa.UniqueConstraint('tenant_id', 'id', name=op.f('uq_interview_stages_tenant_id_id'))
    )
    op.create_index('ix_interview_stages_demand_id', 'interview_stages', ['demand_id'], unique=False)
    op.create_index(op.f('ix_interview_stages_tenant_id'), 'interview_stages', ['tenant_id'], unique=False)
    op.create_index('uq_interview_stages_demand_position', 'interview_stages', ['demand_id', 'position'], unique=True, postgresql_where=sa.text('archived_at IS NULL'))
    op.get_bind().execute(
        sa.text(SEED_TEMPLATE), {"name": "Technical \u2192 Client \u2192 HR", "stages": json.dumps(DEFAULT_TEMPLATE_STAGES)}
    )
    for table in TABLES:
        enable_rls(table)


def downgrade() -> None:
    for table in reversed(TABLES):
        disable_rls(table)
        op.drop_table(table)
