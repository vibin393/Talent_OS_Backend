# ruff: noqa: E501
"""Epic 76 (stories 76.1 / 76.2, spec A1 / A7; approved additive P4/P2 widening, D-22): ``voice_settings.default_provider``
+= ``exotel``, ``twilio``, ``plivo``; ``channel_opt_outs.channel`` += ``voice`` (Do-Not-Call). Downgrade narrows the
lists back and refuses (CHECK violation) while rows use a new value -- no data is deleted. Value lists are frozen here.

Revision ID: 20261010_510_03_voice_alters
Revises: 20261010_510_02_stt_jobs
Create Date: 2026-10-10
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20261010_510_03_voice_alters"
down_revision: str | tuple[str, ...] | None = "20261010_510_02_stt_jobs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

P4_PROVIDERS = ("manual", "fake")
P5_PROVIDERS = (*P4_PROVIDERS, "exotel", "twilio", "plivo")
P2_CHANNELS = ("sms", "whatsapp")
P5_CHANNELS = (*P2_CHANNELS, "voice")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _replace_check(table: str, name: str, condition: str) -> None:
    op.drop_constraint(op.f(name), table, type_="check")
    op.create_check_constraint(op.f(name), table, condition)


def upgrade() -> None:
    _replace_check("voice_settings", "ck_voice_settings_default_provider", _in("default_provider", P5_PROVIDERS))
    _replace_check("channel_opt_outs", "ck_channel_opt_outs_channel", _in("channel", P5_CHANNELS))


def downgrade() -> None:
    _replace_check("voice_settings", "ck_voice_settings_default_provider", _in("default_provider", P4_PROVIDERS))
    _replace_check("channel_opt_outs", "ck_channel_opt_outs_channel", _in("channel", P2_CHANNELS))
