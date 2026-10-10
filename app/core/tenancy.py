"""Tenancy classification of tables (AD-2).

Every table in ``public`` is either:

* a **platform table** listed in ``PLATFORM_TABLES`` -- no RLS, never holds tenant business data
  (``sessions`` carries a nullable ``tenant_id`` only as a lookup hint for the principal's tenant;
  ``mfa_challenges`` likewise; ``skills`` / ``skill_aliases`` are the platform skill catalog,
  SELECT-only for ``talent_app``), or
* a **mixed table** listed in ``MIXED_TABLES`` -- nullable ``tenant_id`` (NULL = platform row) with FORCE
  RLS whose policy shows tenant rows to that tenant and platform rows only without tenant context
  (``audit_logs``, ``mfa_recovery_codes``; see ``migration_helpers.enable_mixed_rls``), or
* an **infrastructure table** listed in ``INFRA_TABLES`` (migration bookkeeping), or
* a **tenant-owned table** -- ``tenant_id NOT NULL`` (``TenantMixin``) with ENABLE + FORCE row level
  security created by ``enable_rls()`` in its migration.

``tests/core/test_rls.py`` enforces this against the migrated database, so a new table that is neither
listed here nor RLS-protected fails the test suite.
"""

PLATFORM_TABLES: frozenset[str] = (
    frozenset({"tenants", "platform_operators", "sessions", "mfa_challenges", "skills", "skill_aliases"})
    | frozenset(
        {
            # Epic 12 (privacy & platform operations). Lookup tables carry only a token/key hash, tenant id
            # and row id (no PII); the others are operator-side records read through platform_service with
            # explicit tenant filters.
            "secure_link_lookups",
            "api_key_lookups",
            "support_grant_lookups",
            "sub_processors",
            "platform_notices",
            "platform_notice_acks",
            "incidents",
            "incident_notifications",
            "tenant_exports",
            "tenant_deletion_certificates",
        }
    )
    | frozenset(
        {
            # Epic 9: apply-session token hash -> (tenant, session) so a public apply request opens the right
            # tenant session (no PII).
            "apply_session_lookups"
        }
    )
    | frozenset(
        {
            # Epic 13 (AD-17): GST state codes and ISO-4217 currencies -- platform reference data, SELECT-only for
            # talent_app.
            "gst_states",
            "currencies",
            # Epic 13 (AD-29): connector webhook config id -> tenant (no PII), written via platform_session like
            # secure_link_lookups.
            "connector_hook_lookups",
        }
    )
    | frozenset(
        {
            # Epic 33 (AD-38): custom Career Portal host -> tenant (hostnames and DNS tokens only, no PII).
            "custom_domains"
        }
    )
)

PLATFORM_TABLES |= frozenset(
    {
        # Epic 14 (AD-30): SSO login state hash -> (tenant, IdP), single use, 10 minutes (no PII).
        "sso_login_states"
    }
)

PLATFORM_TABLES |= frozenset(
    {
        # Epic 22 (FR-119): signature certificate id -> (tenant, request) for the public verify page (no PII).
        "esign_certificate_lookups"
    }
)

PLATFORM_TABLES |= frozenset(
    {
        # Epic 37 (AD-41): platform skill graph edges (seeded, SELECT-only for ``talent_app``, no PII).
        "skill_edges"
    }
)

PLATFORM_TABLES |= frozenset(
    {
        # Epic 46 (AD-52): platform visa type templates (seeded, SELECT-only for ``talent_app``, no PII).
        "visa_type_templates"
    }
)

PLATFORM_TABLES |= frozenset(
    {
        # Epic 53 (FR-271, FR-275): platform AI controls -- global kill switch and provider-outage marker (one row,
        # SELECT / UPDATE for ``talent_app``; no PII).
        "platform_ai_controls"
    }
)

PLATFORM_TABLES |= frozenset(
    {
        # Epic 65 (FR-322, AD-69): platform evaluation runs and results (platform test data, no tenant data;
        # SELECT / INSERT / UPDATE for ``talent_app``).
        "ai_eval_runs",
        "ai_eval_results",
    }
)

PLATFORM_TABLES |= frozenset(
    {
        # Epic 55 (FR-281): platform intake address token -> tenant for the inbound-email webhook (no PII; DELETE /
        # TRUNCATE revoked from ``talent_app``).
        "demand_intake_routes"
    }
)

INFRA_TABLES: frozenset[str] = frozenset({"alembic_version"})

MIXED_TABLES: frozenset[str] = frozenset({"audit_logs", "mfa_recovery_codes"})

# Epic 30 (FR-157): TDS sections -- platform seed rows (``tenant_id`` NULL) plus tenant copies, mixed RLS
# (``enable_mixed_rls`` in 20261005_217_02).
MIXED_TABLES |= frozenset({"tds_sections"})


def is_platform_table(table: str) -> bool:
    return table in PLATFORM_TABLES


def requires_rls(table: str) -> bool:
    """Return True for every table that must be tenant-owned (i.e. RLS-protected)."""
    return table not in PLATFORM_TABLES and table not in INFRA_TABLES and table not in MIXED_TABLES


PLATFORM_TABLES |= frozenset(
    {
        # Epic 66 (AD-75/AD-76/AD-82/AD-86): partner organisations, Apps (runtime status) and their client-secret
        # hashes, OAuth token hash -> tenant lookups, the global ecosystem suspension (one row, SELECT / UPDATE) and
        # platform connector credentials (vault ciphertext). No tenant personal data.
        "partner_orgs",
        "apps",
        "app_secrets",
        "oauth_token_lookups",
        "platform_ecosystem_controls",
        "platform_secrets",
    }
)

PLATFORM_TABLES |= frozenset(
    {
        # Epic 69 (AD-81/AD-83): marketplace listings, listing versions and reviewer actions (no tenant data; a private
        # listing's owner_tenant_id is shown only to that tenant).
        "marketplace_listings",
        "marketplace_listing_versions",
        "marketplace_reviews",
    }
)

PLATFORM_TABLES |= frozenset(
    {
        # Epic 74 (AD-85): developer accounts and their sessions / tokens / recovery codes, partner org members,
        # invitations, verification requests and the org audit log, self-serve sandboxes and conformance runs
        # (developer contact data under the platform privacy notice; never tenant data).
        "developer_accounts",
        "developer_recovery_codes",
        "developer_sessions",
        "developer_email_tokens",
        "partner_org_members",
        "partner_org_invitations",
        "partner_verification_requests",
        "partner_org_audit_log",
        "developer_sandboxes",
        "developer_sandbox_members",
        "conformance_runs",
    }
)

PLATFORM_TABLES |= frozenset(
    {
        # Epic 72 (AD-81): calendar push channel id -> tenant for the public push endpoint (no PII).
        "calendar_channel_lookups"
    }
)

# Epic 75 (509_01): Job Network public index. Careers-public posting data and opaque routing keys only; no Candidate
# data ever crosses tenants (schema guard ``tests/job_network/test_no_personal_data.py``). ``tenant_id`` on these rows
# is routing / isolation only and is never serialised by any public schema. Grants per the migration
# (``network_moderation_actions`` is append-only: SELECT / INSERT only).
PLATFORM_TABLES |= frozenset(
    {
        "network_members",
        "network_listings",
        "network_listing_stats",
        "network_reports",
        "network_moderation_actions",
    }
)


# ---------------------------------------------------------------- Sandbox Tenants (Epic 66, AD-85, FR-339)
_SANDBOX_TTL_SECONDS = 60.0
_sandbox_cache: dict[str, tuple[bool, float]] = {}


def clear_sandbox_cache(tenant_id: object | None = None) -> None:
    if tenant_id is None:
        _sandbox_cache.clear()
    else:
        _sandbox_cache.pop(str(tenant_id), None)


async def is_sandbox(tenant_id: object) -> bool:
    """True when ``tenants.mode = 'sandbox'`` (cached 60 s per process). Enforcement points: connectors resolve only
    Fake / Manual adapters, mailer outbox only, Fake LLM, the ``X-TalentOS-Sandbox`` header, Job Network and platform
    analytics exclusion, the import cap."""
    import time

    from sqlalchemy import text

    from app.core.database import platform_session

    if tenant_id is None:
        return False
    key = str(tenant_id)
    hit = _sandbox_cache.get(key)
    now = time.monotonic()
    if hit is not None and hit[1] > now:
        return hit[0]
    async with platform_session() as db:
        mode = (
            await db.execute(text("SELECT mode FROM tenants WHERE id = CAST(:t AS uuid)"), {"t": key})
        ).scalar()
    value = mode == "sandbox"
    _sandbox_cache[key] = (value, now + _SANDBOX_TTL_SECONDS)
    return value
