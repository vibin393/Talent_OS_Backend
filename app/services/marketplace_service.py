# ruff: noqa: E501
"""Marketplace facade (Epic 66 stub with a working minimal body, spec 0.6 "Install contract"; Epic 69 fills the bodies
with the same signatures and adds the catalogue, policy, processor, health, partner submission and review functions
(spec §C)). Epic 74 calls the partner functions with a ``DeveloperContext``; Epic 79 registers its live-mode guard."""

from app.services.marketplace import review  # platform reviewer actions (router/platform_marketplace.py)
from app.services.marketplace.catalog import get_listing, list_listings
from app.services.marketplace.checks import run_manifest_checks
from app.services.marketplace.conformance_report import verify as verify_conformance_report
from app.services.marketplace.health import history as health_history
from app.services.marketplace.installs import (
    LiveModeGuard,
    configure,
    get,
    install,
    jobs,
    list_installations,
    manifest_digest,
    preapprove_users,
    propagate_erasure,
    register_live_mode_guard,
    retry_job,
    run_sync,
    secret_meta,
    set_status,
    test,
    uninstall,
    unregister_live_mode_guard,
    upgrade,
)
from app.services.marketplace.policy import policy_view
from app.services.marketplace.processors import data_flows, list_processors
from app.services.marketplace.reconsent import upgrade_diff
from app.services.marketplace.sdk import manifest_schema as sdk_manifest_schema
from app.services.marketplace.sdk import ports as sdk_ports
from app.services.marketplace.submissions import (
    create_listing_draft,
    partner_versions,
    register_partner_notifier,
    save_version_draft,
    submit_version,
    unregister_partner_notifier,
    withdraw_version,
)

__all__ = [
    "LiveModeGuard",
    "configure",
    "create_listing_draft",
    "data_flows",
    "get",
    "get_listing",
    "health_history",
    "install",
    "jobs",
    "list_installations",
    "list_listings",
    "list_processors",
    "manifest_digest",
    "partner_versions",
    "policy_view",
    "preapprove_users",
    "propagate_erasure",
    "register_live_mode_guard",
    "register_partner_notifier",
    "retry_job",
    "review",
    "run_manifest_checks",
    "run_sync",
    "save_version_draft",
    "sdk_manifest_schema",
    "sdk_ports",
    "secret_meta",
    "set_status",
    "submit_version",
    "test",
    "uninstall",
    "unregister_live_mode_guard",
    "unregister_partner_notifier",
    "upgrade",
    "upgrade_diff",
    "verify_conformance_report",
    "withdraw_version",
]
