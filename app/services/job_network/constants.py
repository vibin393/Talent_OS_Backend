# ruff: file-ignore[line-too-long]
"""Constants of the Cross-tenant Job Network (Epic 75)."""

# Listing terms version a Tenant must accept to join (spec A2). Bump when the terms change.
NETWORK_TERMS_VERSION = "2026-10"

# FTS configuration used for both the stored vector and the query parse (Devanagari tokens index under ``simple``).
SEARCH_CONFIG = "simple"

# Default JSON-LD validity window when the posting / demand has no closing date (days).
DEFAULT_VALID_THROUGH_DAYS = 60

# Public-data fields the projection may copy into ``network_listings`` (subset of the table's non-routing columns).
# ``tests/job_network/test_no_personal_data.py`` freezes the full column allow-list separately; this is the set the
# projection is allowed to populate (``test_projection_allowlist.py`` asserts the projected dict keys are a subset).
PROJECTED_FIELDS: frozenset[str] = frozenset(
    {
        "title", "description_html", "description_text", "client_name", "city", "region", "country",
        "location_hidden", "work_mode", "employment_type", "experience_min", "experience_max", "skills",
        "category", "pay_min", "pay_max", "pay_currency", "pay_period", "apply_url", "posting_url",
        "published_at", "valid_through", "content_sha256",
    }
)  # fmt: skip

# Eligibility reasons surfaced to the Tenant.
ELIGIBILITY_REASONS = (
    "not_live", "internal_visibility", "client_excluded", "not_joined", "member_pending", "member_suspended",
    "sandbox", "limit_reached", "opted_out",
)  # fmt: skip

# Hold reason codes produced by the automated checks (spec A5).
CHECK_CODES = ("fee_request", "external_contact", "duplicate_spam", "prohibited", "biased_terms", "missing_basics")

# 3 open reports within this window -> listing held pending review.
REPORT_HOLD_THRESHOLD = 3
REPORT_HOLD_WINDOW_DAYS = 7

# Upheld-violation auto-suspend (spec A5).
AUTO_SUSPEND_VIOLATIONS = 3
VIOLATION_WINDOW_DAYS = 180

# Retention windows.
REPORT_RETENTION_DAYS = 30
UNLISTED_RETENTION_DAYS = 30
