from functools import lru_cache
from pathlib import Path
from typing import Literal, Self
from urllib.parse import quote

from pydantic import PositiveInt, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import URL

BACKEND_DIR = Path(__file__).resolve().parents[2]
DEFAULT_APP_DB_PASSWORD = "talent_app_dev"


class Settings(BaseSettings):
    """Single application settings object, loaded from ``Backend/.env`` (env vars win)."""

    debug: bool = False
    app_env: Literal["development", "test", "staging", "production"] = "development"

    # --- Database: owner role (migrations only) ---
    db_host: str = "localhost"
    db_port: int = 5432
    db_name: str = "talent_os_main"
    db_user: str = "postgres"
    db_password: str = ""

    # --- Database: runtime role (NOSUPERUSER NOBYPASSRLS) ---
    db_app_user: str = "talent_app"
    db_app_password: str = DEFAULT_APP_DB_PASSWORD

    db_echo: bool = False
    db_pool_size: int = 10
    db_max_overflow: int = 20
    db_pool_timeout: int = 30

    test_db_name: str = "talent_os_main_test"

    # --- Tenancy (D11 / AD-29): "pooled" = one shared DB + RLS; "silo" = one DB per tenant ---
    # In "silo" mode, tenant_session(tenant_id) routes to that tenant's own database, resolved
    # through the control-plane tenant registry (control_db_name). platform_session() routes to
    # platform_db_name (defaults to db_name). Default stays "pooled" so existing deploys are unchanged.
    tenancy_mode: Literal["pooled", "silo"] = "pooled"
    control_db_name: str = "control"  # control-plane DB holding the tenant registry (tenant_id -> db)
    platform_db_name: str = ""  # DB for platform/global (non-tenant) rows in silo; "" -> db_name
    tenant_db_prefix: str = "tenant_"  # local silo DB naming: <prefix><slug>

    # --- Redis / Celery ---
    redis_host: str = "localhost"
    redis_port: int = 6379
    redis_password: str = ""
    celery_task_always_eager: bool = False

    # --- HTTP ---
    cors_origins: list[str] = [
        "http://localhost:3000",
        "http://127.0.0.1:3000",
        "http://localhost:3001",
        "http://127.0.0.1:3001",
    ]

    # --- Auth / sessions (AD-10) ---
    session_idle_timeout_hours: PositiveInt = 8
    session_absolute_timeout_days: PositiveInt = 7
    invitation_ttl_hours: PositiveInt = 72

    # --- Email ---
    # Base URL of the Next.js app, used to build links in emails (e.g. /accept-invite?token=...).
    frontend_url: str = "http://localhost:3000"
    mail_from: str = "Talent OS <no-reply@talentos.local>"
    # "outbox": log + write each message as JSON under ``outbox_dir`` (dev/test).
    # "smtp": deliver through an SMTP server (e.g. Gmail with an app password, STARTTLS on 587).
    mailer_backend: Literal["outbox", "smtp"] = "outbox"
    outbox_dir: Path = BACKEND_DIR / "data" / "outbox"
    # SMTP (MAILER_BACKEND=smtp). MAILER_SENDER_EMAIL ("Name <addr>") overrides MAIL_FROM when set; with Gmail
    # the address must be the authenticated account (or one of its verified aliases).
    smtp_address: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_use_ssl: bool = False  # True for implicit TLS (port 465); False uses STARTTLS
    smtp_timeout_seconds: PositiveInt = 30
    mailer_sender_email: str = ""
    # Also keep a JSON copy of every SMTP-sent message in ``outbox_dir`` (ignored in production).
    mailer_outbox_copy: bool = True

    @property
    def effective_mail_from(self) -> str:
        return self.mailer_sender_email or self.mail_from

    # --- Encryption keys (Fernet, url-safe base64 of 32 bytes). Empty = a fixed dev key derived per purpose,
    # refused in production. AUDIT_MASTER_KEY wraps the per-subject audit data keys (crypto-shredding);
    # MFA_ENCRYPTION_KEY encrypts TOTP secrets at rest.
    audit_master_key: str = ""
    mfa_encryption_key: str = ""
    # Epic 13 (AD-35 / AD-29): identity & bank data master key (per-tenant keys are HKDF-derived) and the
    # provider-connector credential key. Empty = dev key; in production an empty key is refused when used.
    identity_encryption_key: str = ""
    connector_encryption_key: str = ""
    # Epic 14 (AD-30): identity-provider client secrets and PKCE verifiers (fernet purpose ``sso``).
    sso_encryption_key: str = ""
    # Epic 36 (spec 0.10): demographic self-ID master key (per-tenant HKDF, ``core.encrypted.tenant_fernet``).
    # Empty = dev key; in production an empty key is refused when used (``security.key_material``).
    demographic_encryption_key: str = ""

    # --- External principals (Epic 13, AD-26): Client Users / Vendor Users ---
    external_session_idle_minutes: PositiveInt = 30
    external_session_absolute_hours: PositiveInt = 12

    # --- AI gateway (app/services/ai, AD-6) ---
    anthropic_api_key: str = ""
    ai_model: str = "claude-sonnet-5-5"
    ai_timeout_seconds: PositiveInt = 30
    ai_max_retries: int = 2
    # --- Phase 4 (Epic 53, AD-57/58): agent steps, model tiers, Fake LLM, runtime limits ---
    # ``fake`` serves scripted Fake LLM turns (``AI_FAKE_SCRIPT_DIR/<prompt_id>.yaml``); refused in production
    # (validator below). Outside production a missing key also falls back to the Fake (``ai_fake_when_keyless``).
    ai_provider: Literal["anthropic", "fake"] = "anthropic"
    ai_fake_when_keyless: bool = True
    ai_fake_script_dir: str = "evals/fake_scripts"
    ai_model_fast: str = "claude-haiku-5-5"
    ai_model_standard: str = "claude-sonnet-5-5"
    ai_model_deep: str = "claude-opus-5-5"
    # USD per million tokens (estimates for reporting only; budgets stay token-denominated, FR-63). Cache prices are
    # optional per model (cache read ~0.1x input, cache write ~1.25x input).
    ai_prices: str = (
        '{"claude-haiku-5-5":{"in":0.10,"out":0.50,"cache_read":0.01,"cache_write":0.125},'
        '"claude-sonnet-5-5":{"in":2.0,"out":10.0,"cache_read":0.20,"cache_write":2.5},'
        '"claude-opus-5-5":{"in":4.0,"out":20.0,"cache_read":0.40,"cache_write":5.0}}'
    )
    ai_refusal_fallback: bool = True
    agents_globally_disabled: bool = False
    agent_max_concurrent_runs_per_tenant: PositiveInt = 10
    agent_max_concurrent_runs_per_agent_subject: PositiveInt = 2
    agent_step_timeout_seconds: PositiveInt = 90
    agent_run_wall_clock_seconds: PositiveInt = 900
    agent_proposal_default_ttl_hours: PositiveInt = 72
    agent_burn_in_days: int = 14
    agent_payload_retention_days: PositiveInt = 180
    # Fernet master key of the per-tenant agent payload store; empty = dev key, refused in production when used.
    agent_payload_encryption_key: str = ""
    agent_disclosure_line: str = "This message was prepared by an AI assistant for {owner_name}."
    agent_enablement_notice_version: str = "2026-10-08"
    agent_provider_outage_seconds: PositiveInt = 300
    # Epic 65 (FR-322, AD-69): evaluation harness. Live runs need ``ai_evals_live_enabled`` and a live key; seeds run
    # in the ephemeral ``eval_database_url`` (never DATABASE_URL / talent_os_test); results go to
    # ``eval_results_database_url`` (platform ``ai_eval_*`` tables; empty = the app database).
    # ``eval_gate_enforced`` may be set False only by an explicit CI variable (recorded in the run).
    ai_evals_live_enabled: bool = False
    eval_gate_enforced: bool = True
    eval_database_url: str = ""
    eval_results_database_url: str = ""
    # Epic 54 (Recruitment Copilot): SSE heartbeat (``: ping``) interval and the longest a turn's stream stays open.
    copilot_sse_heartbeat_seconds: PositiveInt = 15
    copilot_turn_timeout_seconds: PositiveInt = 120
    # Epic 55 (Demand Agent intake): platform address domain (``req-<token>@<domain>``) and the HMAC secret of the
    # inbound-email webhook (``X-Intake-Signature``); an empty secret refuses every webhook call (fail closed).
    demand_intake_email_domain: str = "intake.talentos.local"
    demand_intake_webhook_secret: str = ""

    # --- Epic 35 (privacy platform): retention expiry, go-live import, AI document extraction ---
    retention_expiry_batch: PositiveInt = 500
    retention_preview_valid_days: PositiveInt = 7
    golive_max_rows: PositiveInt = 20000
    golive_max_file_mb: PositiveInt = 20
    ai_extraction_max_pages: PositiveInt = 30

    # --- Embeddings & vector index (Epic 5, AD-7) ---
    # ``local``: sentence-transformer on CPU (loaded lazily); ``fake``: deterministic hash-seeded unit vectors
    # (tests / CI, no model download).
    embedding_backend: Literal["local", "fake"] = "local"
    embedding_model: str = "BAAI/bge-small-en-v1.5"
    embedding_dim: PositiveInt = 384
    # ``auto`` picks PgVectorIndex when the ``vector`` extension is installed, else ArrayVectorIndex.
    vector_index: Literal["auto", "array", "pgvector"] = "auto"

    # --- Files (AD-13): storage port + malware scanning ---
    storage_backend: Literal["local", "s3"] = "local"
    file_storage_dir: Path = BACKEND_DIR / "data" / "files"
    s3_bucket: str = ""
    s3_region: str = ""
    # ClamAV clamd (INSTREAM over TCP). Empty host = no scanner: ``skipped_dev`` in development/test,
    # ``error`` (upload refused) elsewhere.
    clamav_host: str = ""
    clamav_port: int = 3310
    max_upload_mb: PositiveInt = 10

    # --- Privacy & platform operations (Epic 12) ---
    # Rate limiting (NFR-16): sliding-window counters in Redis, in-memory fallback (tests / Redis down).
    rate_limit_enabled: bool = True
    rate_limit_backend: Literal["redis", "memory"] = "redis"
    # Proxies whose X-Forwarded-For is trusted for the client IP (exact IPs or CIDRs).
    trusted_proxies: list[str] = []
    # Owner-role DSN used ONLY by the tenant deletion job (empty = owner URL built from DB_* settings).
    database_owner_url: str = ""
    erasure_ledger_prefix: str = "platform/erasure-ledger/"
    support_max_hours: PositiveInt = 24
    offboard_grace_days: PositiveInt = 30
    dsr_due_days: PositiveInt = 30
    bulk_concurrency_per_tenant: PositiveInt = 4
    # Platform operator endpoints of Epic 12 require an MFA-enrolled operator (story 1.4).
    platform_operator_mfa_required: bool = True

    # --- Communication, notifications & workflow (Epic 10) ---
    email_send_rate_per_user_hour: PositiveInt = 200
    workflow_max_chain_depth: PositiveInt = 3
    # --- Recruitment marketing (Epic 39, NFR-34): platform maxima of the per-tenant throttle; tracking link base
    # (empty = frontend_url; the open pixel needs an https base). ---
    marketing_max_rate_email: PositiveInt = 1000
    marketing_max_rate_sms: PositiveInt = 300
    marketing_max_rate_whatsapp: PositiveInt = 300
    marketing_max_daily_email: PositiveInt = 200000
    marketing_max_daily_sms: PositiveInt = 50000
    marketing_max_daily_whatsapp: PositiveInt = 50000
    marketing_link_base_url: str = ""
    # --- SLA, workflow builder v2 & escalations (Epic 32) ---
    sla_eval_interval_seconds: PositiveInt = 60
    escalation_max_repeats: PositiveInt = 5
    workflow_graph_max_steps: PositiveInt = 25
    workflow_max_delay_days: PositiveInt = 90
    workflow_wait_batch: PositiveInt = 500
    # HMAC-SHA256 secret of the generic bounce webhook (``X-TOS-Signature``). Empty = webhook disabled (401).
    email_webhook_secret: str = ""

    # --- Interviews: calendar / mailbox connections (Epic 7, FR-53). Empty = provider not configured. ---
    google_oauth_client_id: str = ""
    google_oauth_client_secret: str = ""
    ms_oauth_client_id: str = ""
    ms_oauth_client_secret: str = ""
    ms_oauth_tenant: str = "common"
    # Base URL of this API (the OAuth callback is ``{OAUTH_REDIRECT_BASE}/api/v1/connections/{provider}/callback``).
    oauth_redirect_base: str = "http://localhost:8000"
    # Fernet key encrypting OAuth tokens at rest; required once Google or Microsoft is configured.
    token_encryption_key: str = ""
    # In-memory fake calendar provider (tests / dev): counts as configured, never calls a real API.
    calendar_provider_fake: bool = False

    # --- Career Portal & Candidate Portal (Epic 9) ---
    # Bot challenge on public apply / sign-in POSTs. ``none`` (always passes) is refused in production at
    # startup (``core.bot_challenge.check_configuration``).
    bot_challenge_provider: Literal["none", "turnstile", "hcaptcha"] = "none"
    bot_challenge_site_key: str = ""
    bot_challenge_secret: str = ""
    # Epic 6.2 / AD-1: tenant-subdomain root (``<slug>.<APP_ROOT_DOMAIN>`` resolves the host to a tenant,
    # so sign-in can infer the tenant from the host). Empty = off (tenant_slug must be supplied).
    app_root_domain: str = ""
    # Subdomain mode ``<slug>.<CAREERS_ROOT_DOMAIN>`` (empty = path mode ``/careers/<slug>`` only).
    careers_root_domain: str = ""
    # Epic 33 (AD-38): CNAME target shown to tenants adding a custom Career Portal domain (hosting-provided).
    careers_cname_target: str = ""
    # Epic 75 (Cross-tenant Job Network): public site URL (empty = ``{frontend_url}/jobs``), listing terms version,
    # default per-member listing limit, optional read-replica DSN for the public search, and the HMAC key for report
    # ``ip_hash`` (rate limiting only, rotated monthly; prod required).
    network_site_url: str = ""
    network_terms_version: str = "2026-10"
    network_listing_limit_default: PositiveInt = 500
    network_read_dsn: str = ""
    network_report_ip_key: str = ""
    candidate_link_minutes: PositiveInt = 15
    apply_confirmation_link_hours: PositiveInt = 24
    candidate_session_idle_minutes: PositiveInt = 1440
    candidate_session_absolute_hours: PositiveInt = 168
    apply_session_minutes: PositiveInt = 60
    data_export_link_days: PositiveInt = 7

    # --- Dashboards & export (Epic 11, FR-60 / FR-61) ---
    export_max_rows: PositiveInt = 50_000
    export_sync_max_rows: PositiveInt = 5_000
    export_file_ttl_hours: PositiveInt = 24
    export_rate_per_user_hour: PositiveInt = 10
    export_async_concurrency_per_tenant: PositiveInt = 2
    # 0 disables the per-tile Redis cache.
    dashboard_cache_seconds: int = 60
    dashboard_max_range_days: PositiveInt = 366
    dashboard_tile_timeout_ms: PositiveInt = 2500
    # Epic 34 (Phase 2 dashboards, A1): summary horizon, rebuild debounce and the P2 tile statement timeout.
    reporting_summary_months: PositiveInt = 36
    reporting_summary_debounce_seconds: int = 60
    dashboard_p2_statement_timeout_ms: PositiveInt = 2500

    # --- Phase 5 foundation (Epic 66, spec 0.16): OAuth, Public API, vault, egress, metering, sandbox, ecosystem ---
    # Empty = derived from FRONTEND_URL / OAUTH_REDIRECT_BASE (``p5_*`` helpers below).
    oauth_issuer: str = ""
    oauth_access_ttl_seconds: PositiveInt = 3600
    oauth_refresh_idle_days: PositiveInt = 30
    oauth_refresh_absolute_days: PositiveInt = 90
    oauth_code_ttl_seconds: PositiveInt = 60
    oauth_secret_overlap_hours: PositiveInt = 24
    public_api_base_url: str = ""
    mcp_resource_url: str = ""
    # HMAC key of Public API cursors (prod required; dev deterministic).
    public_cursor_secret: str = ""
    public_webhook_base_url: str = ""
    # JSON ``{version: base64 32-byte key}`` (prod required; dev deterministic ``dev_fernet_key("vault")``).
    vault_master_keys: dict[str, str] = {}
    vault_current_master_version: PositiveInt = 1
    vault_kms_provider: Literal["local"] = "local"
    egress_denied_cidrs: list[str] = []
    egress_proxy_url: str | None = None
    # Overrides of core.rate_tiers (``{"standard": {"per_minute": 900}}``).
    rate_tiers: dict[str, dict[str, int]] = {}
    public_event_retention_days: PositiveInt = 30
    connector_fake_script_dir: str = "tests/fixtures/connectors"
    sandbox_inactive_purge_days: PositiveInt = 90
    sandbox_import_cap: PositiveInt = 1000
    ecosystem_state_cache_seconds: PositiveInt = 15
    # --- Epic 76 (Real Telephony & STT) ---
    telephony_answer_token_ttl_seconds: PositiveInt = 7200
    telephony_recording_max_bytes: PositiveInt = 209_715_200
    stt_eval_dir: str = "evals/stt"
    # --- Epic 67 (Public API & OAuth Apps, spec A1) ---
    oauth_authorize_request_ttl_seconds: PositiveInt = 600
    public_api_max_body_bytes: PositiveInt = 1_048_576
    public_api_resume_max_bytes: PositiveInt = 10_485_760
    public_api_bulk_max_rows: PositiveInt = 10_000
    public_api_bulk_max_file_bytes: PositiveInt = 52_428_800
    public_api_deprecation_report_day: PositiveInt = 1
    private_apps_per_tenant: PositiveInt = 25
    app_redirect_uris_max: PositiveInt = 10
    # --- Epic 68 (Webhooks, spec A1): limits, the AD-79 retry ladder (1 + 8 retries, D-24), health and retention ---
    webhook_max_per_installation: PositiveInt = 25
    webhook_max_per_tenant: PositiveInt = 100
    webhook_retry_delays_seconds: list[int] = [60, 300, 900, 3600, 7200, 14400, 28800, 28800]
    webhook_retry_jitter_pct: int = 10
    webhook_client_error_retries: int = 2
    webhook_retry_after_cap_seconds: PositiveInt = 3600
    webhook_timeout_seconds: PositiveInt = 10
    webhook_connect_timeout_seconds: PositiveInt = 5
    webhook_response_excerpt_bytes: PositiveInt = 1024
    webhook_failure_notify_after_seconds: PositiveInt = 3600
    webhook_auto_disable_after_seconds: PositiveInt = 259200
    webhook_max_in_flight_default: PositiveInt = 5
    webhook_tenant_concurrency: dict[str, int] = {"sandbox": 2, "standard": 16, "enterprise": 48}
    webhook_log_retention_days: PositiveInt = 30
    webhook_verification_timeout_seconds: PositiveInt = 10
    webhook_echo_enabled: bool = True
    # --- Epic 73 (Governed MCP Server, spec A1) ---
    mcp_supported_protocol_versions: list[str] = ["2025-11-25", "2025-06-18"]
    mcp_max_request_bytes: PositiveInt = 262_144
    mcp_notice_version: str = "mcp-notice-v1"
    mcp_hold_auto_release_hours: int = 24
    mcp_gate_cache_seconds: int = 15
    mcp_session_retention_days: PositiveInt = 365
    mcp_launch_redirect_uris_claude: list[str] = ["https://claude.ai/api/mcp/auth_callback"]
    # --- Epic 74 (Developer Portal, spec A1) ---
    developer_session_idle_hours: PositiveInt = 12
    developer_session_absolute_days: PositiveInt = 7
    developer_email_verify_hours: PositiveInt = 24
    developer_unverified_purge_days: PositiveInt = 7
    developer_invitation_days: PositiveInt = 7
    partner_orgs_per_account: PositiveInt = 3
    partner_sandboxes_max: PositiveInt = 3
    tenant_sandboxes_max: PositiveInt = 2
    partner_verification_validity_months: PositiveInt = 12
    conformance_window_minutes: PositiveInt = 30
    conformance_report_max_age_days: PositiveInt = 30
    developer_terms_version: str = "2026-10"
    developer_portal_enabled: bool = True

    model_config = SettingsConfigDict(env_file=BACKEND_DIR / ".env", extra="ignore")

    def p5_oauth_issuer(self) -> str:
        return (self.oauth_issuer or self.frontend_url).rstrip("/")

    def p5_public_api_base_url(self) -> str:
        return (self.public_api_base_url or f"{self.frontend_url.rstrip('/')}/public/v1").rstrip("/")

    def p5_mcp_resource_url(self) -> str:
        return (self.mcp_resource_url or f"{self.frontend_url.rstrip('/')}/mcp").rstrip("/")

    def p5_public_webhook_base_url(self) -> str:
        return (self.public_webhook_base_url or self.oauth_redirect_base).rstrip("/")

    @model_validator(mode="after")
    def _reject_dev_defaults_in_production(self) -> Self:
        if self.app_env == "production" and self.db_app_password in {"", DEFAULT_APP_DB_PASSWORD}:
            msg = "DB_APP_PASSWORD must be set to a non-default value in production"
            raise ValueError(msg)
        if self.app_env == "production":
            # Epic 13: IDENTITY_ENCRYPTION_KEY / CONNECTOR_ENCRYPTION_KEY are refused lazily in production
            # (``security.key_material`` raises when unset), so existing deployments keep starting.
            for name in ("audit_master_key", "mfa_encryption_key"):
                if not getattr(self, name):
                    msg = f"{name.upper()} must be set in production"
                    raise ValueError(msg)
        # Epic 66 (AD-82, spec I/O matrix): the vault master key and the cursor secret are required in production.
        if self.app_env == "production" and (not self.vault_master_keys or not self.public_cursor_secret):
            msg = "VAULT_MASTER_KEYS and PUBLIC_CURSOR_SECRET must be set in production"
            raise ValueError(msg)
        # Epic 53 (AD-57): the scripted Fake LLM never serves production.
        if self.app_env == "production" and self.ai_provider == "fake":
            msg = "AI_PROVIDER=fake is not allowed in production"
            raise ValueError(msg)
        # Epic 7: a configured OAuth provider stores tokens, so the token key must exist (fail fast).
        oauth_configured = (self.google_oauth_client_id and self.google_oauth_client_secret) or (
            self.ms_oauth_client_id and self.ms_oauth_client_secret
        )
        if oauth_configured and not self.token_encryption_key:
            msg = "TOKEN_ENCRYPTION_KEY must be set when a Google or Microsoft OAuth client is configured"
            raise ValueError(msg)
        return self

    def _url(self, user: str, password: str, db_name: str | None) -> str:
        url = URL.create(
            "postgresql+asyncpg",
            username=user,
            password=password or None,
            host=self.db_host,
            port=self.db_port,
            database=db_name or self.db_name,
        )
        return url.render_as_string(hide_password=False)

    def owner_db_url(self, db_name: str | None = None) -> str:
        """URL for the owner role; used by migrations and admin scripts only."""
        return self._url(self.db_user, self.db_password, db_name)

    def app_db_url(self, db_name: str | None = None) -> str:
        """URL for the runtime ``talent_app`` role; used by the API and workers."""
        return self._url(self.db_app_user, self.db_app_password, db_name)

    def tenant_db_name(self, slug: str) -> str:
        """Local silo DB name for a tenant slug, e.g. ``acme`` -> ``tenant_acme``."""
        return f"{self.tenant_db_prefix}{slug}"

    def platform_db(self) -> str:
        """DB holding platform/global (non-tenant) rows in silo mode."""
        return self.platform_db_name or self.db_name

    def redis_url(self, db: int = 0) -> str:
        auth = f":{quote(self.redis_password, safe='')}@" if self.redis_password else ""
        host = f"[{self.redis_host}]" if ":" in self.redis_host else self.redis_host
        return f"redis://{auth}{host}:{self.redis_port}/{db}"


@lru_cache
def get_settings() -> Settings:
    return Settings()
