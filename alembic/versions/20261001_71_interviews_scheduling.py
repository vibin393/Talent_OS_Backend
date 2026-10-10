"""Epic 7 interview scheduling (tenant-owned, RLS): interviews, participants, invite log, scorecards,
stage outcomes.

Revision ID: 20261001_71
Revises: 20261001_70
Create Date: 2026-10-01

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_helpers import disable_rls, enable_rls

revision: str = "20261001_71"
down_revision: str | tuple[str, ...] | None = '20261001_70'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES: tuple[str, ...] = ('interviews', 'interview_participants', 'interview_invite_log', 'scorecards', 'interview_stage_outcomes')


def upgrade() -> None:
    op.create_table('interviews',
    sa.Column('human_id', sa.String(length=20), nullable=False),
    sa.Column('application_id', sa.Uuid(), nullable=False),
    sa.Column('demand_id', sa.Uuid(), nullable=False),
    sa.Column('candidate_id', sa.Uuid(), nullable=False),
    sa.Column('stage_id', sa.Uuid(), nullable=False),
    sa.Column('stage_name', sa.String(length=200), nullable=False),
    sa.Column('kit_snapshot', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('round', sa.Integer(), server_default=sa.text('1'), nullable=False),
    sa.Column('starts_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('ends_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('timezone', sa.String(length=64), nullable=False),
    sa.Column('mode', sa.String(length=20), nullable=False),
    sa.Column('meeting_url', sa.String(length=2000), nullable=True),
    sa.Column('location', sa.String(length=500), nullable=True),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.Column('status', sa.String(length=20), server_default=sa.text("'scheduled'"), nullable=False),
    sa.Column('sequence', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.Column('ical_uid', sa.String(length=200), nullable=False),
    sa.Column('organizer_user_id', sa.Uuid(), nullable=False),
    sa.Column('provider', sa.String(length=20), server_default=sa.text("'none'"), nullable=False),
    sa.Column('provider_event_id', sa.String(length=500), nullable=True),
    sa.Column('provider_connection_id', sa.Uuid(), nullable=True),
    sa.Column('create_online_meeting', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('send_invites', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('cancelled_reason', sa.Text(), nullable=True),
    sa.Column('reminder_state', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('version', sa.Integer(), server_default=sa.text('1'), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('tenant_id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("mode IN ('video', 'phone', 'in_person')", name=op.f('ck_interviews_mode')),
    sa.CheckConstraint("provider IN ('none', 'google', 'microsoft', 'fake')", name=op.f('ck_interviews_provider')),
    sa.CheckConstraint("status IN ('scheduled', 'completed', 'cancelled', 'no_show')", name=op.f('ck_interviews_status')),
    sa.CheckConstraint('ends_at > starts_at', name=op.f('ck_interviews_times')),
    sa.ForeignKeyConstraint(['tenant_id', 'application_id'], ['applications.tenant_id', 'applications.id'], name='fk_interviews_application_id_applications', ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['tenant_id', 'candidate_id'], ['candidates.tenant_id', 'candidates.id'], name='fk_interviews_candidate_id_candidates', ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['tenant_id', 'demand_id'], ['demands.tenant_id', 'demands.id'], name='fk_interviews_demand_id_demands', ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['tenant_id', 'organizer_user_id'], ['users.tenant_id', 'users.id'], name='fk_interviews_organizer_user_id_users', ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['tenant_id', 'stage_id'], ['interview_stages.tenant_id', 'interview_stages.id'], name='fk_interviews_stage_id_interview_stages', ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], name=op.f('fk_interviews_tenant_id_tenants'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_interviews')),
    sa.UniqueConstraint('ical_uid', name=op.f('uq_interviews_ical_uid')),
    sa.UniqueConstraint('tenant_id', 'human_id', name=op.f('uq_interviews_tenant_id_human_id')),
    sa.UniqueConstraint('tenant_id', 'id', name=op.f('uq_interviews_tenant_id_id'))
    )
    op.create_index('ix_interviews_application_id', 'interviews', ['application_id'], unique=False)
    op.create_index('ix_interviews_candidate_id', 'interviews', ['candidate_id'], unique=False)
    op.create_index('ix_interviews_demand_id_starts_at', 'interviews', ['demand_id', 'starts_at'], unique=False)
    op.create_index('ix_interviews_stage_id', 'interviews', ['stage_id'], unique=False)
    op.create_index('ix_interviews_status_starts_at', 'interviews', ['status', 'starts_at'], unique=False)
    op.create_index(op.f('ix_interviews_tenant_id'), 'interviews', ['tenant_id'], unique=False)
    op.create_table('interview_participants',
    sa.Column('interview_id', sa.Uuid(), nullable=False),
    sa.Column('kind', sa.String(length=30), nullable=False),
    sa.Column('user_id', sa.Uuid(), nullable=True),
    sa.Column('email', postgresql.CITEXT(), nullable=False),
    sa.Column('name', sa.String(length=200), nullable=True),
    sa.Column('timezone', sa.String(length=64), nullable=False),
    sa.Column('response_status', sa.String(length=20), server_default=sa.text("'needs_action'"), nullable=False),
    sa.Column('scorecard_required', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('secure_link_id', sa.Uuid(), nullable=True),
    sa.Column('reminded_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('removed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('tenant_id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("kind IN ('interviewer_internal', 'interviewer_external', 'candidate', 'observer')", name=op.f('ck_interview_participants_kind')),
    sa.CheckConstraint("response_status IN ('needs_action', 'accepted', 'declined')", name=op.f('ck_interview_participants_response_status')),
    sa.ForeignKeyConstraint(['tenant_id', 'interview_id'], ['interviews.tenant_id', 'interviews.id'], name='fk_interview_participants_interview_id_interviews', ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['tenant_id', 'user_id'], ['users.tenant_id', 'users.id'], name='fk_interview_participants_user_id_users', ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], name=op.f('fk_interview_participants_tenant_id_tenants'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_interview_participants')),
    sa.UniqueConstraint('interview_id', 'email', name=op.f('uq_interview_participants_interview_id_email')),
    sa.UniqueConstraint('tenant_id', 'id', name=op.f('uq_interview_participants_tenant_id_id'))
    )
    op.create_index(op.f('ix_interview_participants_tenant_id'), 'interview_participants', ['tenant_id'], unique=False)
    op.create_index('ix_interview_participants_user_id', 'interview_participants', ['user_id'], unique=False)
    op.create_table('interview_invite_log',
    sa.Column('interview_id', sa.Uuid(), nullable=False),
    sa.Column('participant_id', sa.Uuid(), nullable=False),
    sa.Column('sequence', sa.Integer(), nullable=False),
    sa.Column('method', sa.String(length=10), nullable=False),
    sa.Column('channel', sa.String(length=20), nullable=False),
    sa.Column('sent_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('error', sa.String(length=200), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('tenant_id', sa.Uuid(), nullable=False),
    sa.CheckConstraint("channel IN ('ics_email', 'provider')", name=op.f('ck_interview_invite_log_channel')),
    sa.CheckConstraint("method IN ('REQUEST', 'CANCEL')", name=op.f('ck_interview_invite_log_method')),
    sa.ForeignKeyConstraint(['tenant_id', 'interview_id'], ['interviews.tenant_id', 'interviews.id'], name='fk_interview_invite_log_interview_id_interviews', ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['tenant_id', 'participant_id'], ['interview_participants.tenant_id', 'interview_participants.id'], name='fk_interview_invite_log_participant_id_interview_participants', ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], name=op.f('fk_interview_invite_log_tenant_id_tenants'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_interview_invite_log')),
    sa.UniqueConstraint('interview_id', 'sequence', 'participant_id', 'method', name=op.f('uq_interview_invite_log_interview_id_sequence_participant_id_method'))
    )
    op.create_index(op.f('ix_interview_invite_log_tenant_id'), 'interview_invite_log', ['tenant_id'], unique=False)
    op.create_table('scorecards',
    sa.Column('interview_id', sa.Uuid(), nullable=False),
    sa.Column('participant_id', sa.Uuid(), nullable=False),
    sa.Column('author_user_id', sa.Uuid(), nullable=True),
    sa.Column('kit_snapshot', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('ratings', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
    sa.Column('answers', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
    sa.Column('recommendation', sa.String(length=20), nullable=True),
    sa.Column('summary', sa.Text(), nullable=True),
    sa.Column('status', sa.String(length=20), server_default=sa.text("'draft'"), nullable=False),
    sa.Column('submitted_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('source', sa.String(length=20), server_default=sa.text("'in_app'"), nullable=False),
    sa.Column('secure_link_id', sa.Uuid(), nullable=True),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('tenant_id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("recommendation IS NULL OR recommendation IN ('strong_yes', 'yes', 'no', 'strong_no')", name=op.f('ck_scorecards_recommendation')),
    sa.CheckConstraint("source IN ('in_app', 'secure_link')", name=op.f('ck_scorecards_source')),
    sa.CheckConstraint("status IN ('draft', 'submitted')", name=op.f('ck_scorecards_status')),
    sa.ForeignKeyConstraint(['tenant_id', 'interview_id'], ['interviews.tenant_id', 'interviews.id'], name='fk_scorecards_interview_id_interviews', ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['tenant_id', 'participant_id'], ['interview_participants.tenant_id', 'interview_participants.id'], name='fk_scorecards_participant_id_interview_participants', ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], name=op.f('fk_scorecards_tenant_id_tenants'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_scorecards')),
    sa.UniqueConstraint('interview_id', 'participant_id', name=op.f('uq_scorecards_interview_id_participant_id'))
    )
    op.create_index(op.f('ix_scorecards_tenant_id'), 'scorecards', ['tenant_id'], unique=False)
    op.create_table('interview_stage_outcomes',
    sa.Column('application_id', sa.Uuid(), nullable=False),
    sa.Column('stage_id', sa.Uuid(), nullable=False),
    sa.Column('outcome', sa.String(length=10), nullable=False),
    sa.Column('reason_code', sa.String(length=50), nullable=True),
    sa.Column('comment', sa.Text(), nullable=True),
    sa.Column('decided_by', sa.Uuid(), nullable=True),
    sa.Column('decided_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('superseded_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('tenant_id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("outcome IN ('advance', 'reject', 'hold')", name=op.f('ck_interview_stage_outcomes_outcome')),
    sa.ForeignKeyConstraint(['tenant_id', 'application_id'], ['applications.tenant_id', 'applications.id'], name='fk_interview_stage_outcomes_application_id_applications', ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['tenant_id', 'stage_id'], ['interview_stages.tenant_id', 'interview_stages.id'], name='fk_interview_stage_outcomes_stage_id_interview_stages', ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], name=op.f('fk_interview_stage_outcomes_tenant_id_tenants'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_interview_stage_outcomes'))
    )
    op.create_index('ix_interview_stage_outcomes_application_stage', 'interview_stage_outcomes', ['application_id', 'stage_id'], unique=False)
    op.create_index(op.f('ix_interview_stage_outcomes_tenant_id'), 'interview_stage_outcomes', ['tenant_id'], unique=False)

    for table in TABLES:
        enable_rls(table)


def downgrade() -> None:
    for table in reversed(TABLES):
        disable_rls(table)
        op.drop_table(table)
