# ruff: file-ignore[line-too-long]
"""Phase 3 read-model guards (Epic 52 A4, AD-1 read-model exception; see ``common`` / ``p2tables``).

Every Phase 3 table a dashboard tile reads is registered here as a lightweight ``sqlalchemy.table()`` projection in
``common.OPTIONAL_TABLES`` with the columns the tile needs, so ``available()`` hides a tile whose table (or column) is
absent (``unavailable``) instead of failing. Tile SQL itself is built on the owners' ORM models together with the
owners' own scope helpers (one FROM object per table, and identical row scope to the owner's list endpoint, which the
parity tests compare); reporting never writes to any of these tables.
"""

from sqlalchemy.sql.expression import TableClause

from app.services.reporting import common
from app.services.reporting.p2tables import _required, _t

campaign_messages = _t(
    "campaign_messages",
    "id:u campaign_id:u candidate_id:u channel:s status:s suppression_reason:s created_at:t",
)
engagement_events = _t("engagement_events", "id:u campaign_message_id:u kind:s occurred_at:t")
enrollments = _t("enrollments", "id:u status:s started_at:t")
campaign_attributions = _t(
    "campaign_attributions", "application_id:u campaign_id:u attributed_at:t hired_at:t"
)
referrals = _t(
    "referrals",
    "id:u demand_id:u status:s referrer_kind:s referrer_user_id:u referrer_candidate_id:u created_at:t",
)
referral_rewards = _t("referral_rewards", "id:u referral_id:u status:s amount:n currency:s")
campus_drives = _t("campus_drives", "id:u owner_id:u status:s registration_closes_at:t")
registrations = _t("registrations", "id:u drive_id:u institution_id:u status:s offered_at:t")
event_checkins = _t("event_checkins", "id:u event_id:u checked_in_at:t")
queue_entries = _t("queue_entries", "id:u event_id:u status:s created_at:t started_at:t")
mobility_applications = _t("mobility_applications", "id:u demand_id:u status:s submitted_at:t")
internal_transfers = _t("internal_transfers", "id:u confirmed_at:t")
candidate_skills = _t("candidate_skills", "id:u candidate_id:u verification:s")
projects = _t("projects", "id:u human_id:s owner_id:u business_unit_id:u status:s currency:s archived_at:t")
project_budgets = _t("project_budgets", "project_id:u dimension:s amount:n")
project_burn = _t("project_burn", "project_id:u dimension:s actual:n budget:n pct:n as_of:d")
deliverables = _t("deliverables", "id:u project_id:u status:s submitted_at:t")
immigration_cases = _t("immigration_cases", "id:u owner_user_id:u business_unit_id:u status:s stage_key:s")
permits = _t("permits", "id:u case_id:u valid_to:d superseded_by_id:u")
ml_predictions = _t(
    "ml_predictions", "id:u model_id:u kind:s subject_kind:s subject_id:u band:s computed_at:t"
)
ml_models = _t("ml_models", "id:u kind:s status:s trained_at:t n_samples:i n_minority:i")

P3_TABLES: tuple[TableClause, ...] = (
    campaign_messages, engagement_events, enrollments, campaign_attributions, referrals, referral_rewards,
    campus_drives, registrations, event_checkins, queue_entries, mobility_applications, internal_transfers,
    candidate_skills, projects, project_budgets, project_burn, deliverables, immigration_cases, permits,
    ml_predictions, ml_models,
)  # fmt: skip
for _tbl in P3_TABLES:
    common.OPTIONAL_TABLES.setdefault(_tbl.name, _required(_tbl))
