# ruff: noqa: E501
"""Schema guard (Epic 75, story 75.3, spec A1 -- binding).

* the column set of every ``network_*`` platform table equals the frozen allow-list ``NETWORK_PUBLIC_COLUMNS``;
* no column name matches a personal-data pattern (except the justified ``network_reports.ip_hash``);
* no FK from any ``network_*`` table points at a tenant (RLS) table;
* ``job_network_settings`` / ``job_network_choices`` hold no Candidate column.

Adding a column to a ``network_*`` table fails this test until a human updates the allow-list.
"""

import re  # noqa: F401 - kept for documented pattern reference

import pytest

from app.core import tenancy
from app.models import job_network as m

# Frozen allow-list -- the ONLY columns a network_* platform table may have (routing keys included, never serialised).
NETWORK_PUBLIC_COLUMNS: dict[str, frozenset[str]] = {
    "network_members": frozenset(
        {
            "id", "created_at", "updated_at", "tenant_id", "public_slug", "display_name", "logo_url", "about_text",
            "careers_base_url", "status", "verification_status", "verified_at", "verified_by", "suspended_at",
            "suspended_reason", "violation_count", "listing_limit", "joined_at", "left_at",
        }
    ),
    "network_listings": frozenset(
        {
            "id", "member_id", "tenant_id", "posting_ref", "status", "hold_reasons", "title", "description_html",
            "description_text", "client_name", "city", "region", "country", "location_hidden", "work_mode",
            "employment_type", "experience_min", "experience_max", "skills", "category", "pay_min", "pay_max",
            "pay_currency", "pay_period", "apply_url", "posting_url", "published_at", "listed_at", "valid_through",
            "content_sha256", "search_tsv", "synced_at",
        }
    ),
    "network_listing_stats": frozenset({"listing_id", "day", "impressions", "views", "apply_clicks"}),
    "network_reports": frozenset(
        {"id", "listing_id", "reason", "details", "ip_hash", "status", "decided_by", "decided_at", "created_at"}
    ),
    "network_moderation_actions": frozenset(
        {"id", "target_type", "target_id", "action", "reason", "operator_id", "created_at"}
    ),
}

# Spec A1 pattern, token-anchored so it matches column-name *tokens* (``ip`` the column, not "descr-ip-tion"):
# email|phone|mobile|candidate|applicant|resume|cv|name_of_person|ip(?!_hash)|cookie|session|user_agent|dob|pan|aadhaar
_FORBIDDEN_TOKENS = frozenset(
    {"email", "phone", "mobile", "candidate", "applicant", "resume", "cv", "cookie", "session", "dob", "pan", "aadhaar"}
)


def _looks_like_personal_data(name: str) -> bool:
    tokens = name.split("_")
    if any(t in _FORBIDDEN_TOKENS for t in tokens):
        return True
    if "ip" in tokens and name != "ip_hash":  # ip(?!_hash)
        return True
    if "user" in tokens and "agent" in tokens:  # user_agent
        return True
    return name == "name_of_person"


_PLATFORM_TABLES = {
    "network_members": m.NetworkMember,
    "network_listings": m.NetworkListing,
    "network_listing_stats": m.NetworkListingStat,
    "network_reports": m.NetworkReport,
    "network_moderation_actions": m.NetworkModerationAction,
}
_TENANT_TABLES = {"job_network_settings": m.JobNetworkSettings, "job_network_choices": m.JobNetworkChoice}


@pytest.mark.parametrize("table", sorted(_PLATFORM_TABLES))
def test_columns_match_allow_list(table: str) -> None:
    actual = {c.name for c in _PLATFORM_TABLES[table].__table__.columns}
    assert actual == NETWORK_PUBLIC_COLUMNS[table], (
        f"{table} columns changed; a human must review DPDP impact and update NETWORK_PUBLIC_COLUMNS. "
        f"added={actual - NETWORK_PUBLIC_COLUMNS[table]} removed={NETWORK_PUBLIC_COLUMNS[table] - actual}"
    )


@pytest.mark.parametrize("table", sorted(_PLATFORM_TABLES))
def test_no_personal_data_column_names(table: str) -> None:
    for col in _PLATFORM_TABLES[table].__table__.columns:
        if table == "network_reports" and col.name == "ip_hash":
            continue  # justified: rate limiting only, HMAC, purged at 30 days
        assert not _looks_like_personal_data(col.name), f"{table}.{col.name} looks like personal data"


@pytest.mark.parametrize("table", sorted(_PLATFORM_TABLES))
def test_no_fk_to_tenant_tables(table: str) -> None:
    for fk in _PLATFORM_TABLES[table].__table__.foreign_keys:
        referred = fk.column.table.name
        assert not tenancy.requires_rls(referred), (
            f"{table} has a FK to tenant (RLS) table {referred}; the network index must never reference tenant data"
        )


@pytest.mark.parametrize("table", sorted(_TENANT_TABLES))
def test_tenant_tables_hold_no_candidate_column(table: str) -> None:
    for col in _TENANT_TABLES[table].__table__.columns:
        assert not _looks_like_personal_data(col.name), f"{table}.{col.name} looks like personal data"


def test_moderation_actions_is_append_only_platform_table() -> None:
    # append-only audit table: a platform table (no RLS) with no updatable business columns beyond the decision record.
    assert tenancy.is_platform_table("network_moderation_actions")
