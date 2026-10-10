"""FR-184 governance (Epic 35 A7): every Phase 2 notification type in the spec table is registered.

Owner epics register their types from ``app/services/**/hooks.py`` (AD-31) via
``notifications_service.register_notification_type``; the registry is populated here by running the same
``app.core.hooks.autodiscover`` the API app and Celery workers run. Owners namespace some keys
(``vendors.submission_received``), so each required type lists the registered keys that satisfy it.

Types whose owner epic has not merged sit in ``PENDING_NOTIFICATION_TYPES`` and are reported as skipped; a
pending entry that is in fact registered fails ("remove from pending") so the list only ever shrinks.
"""

import dataclasses

import pytest

from app.core.hooks import autodiscover
from app.services import notifications_service as ns

autodiscover("app.services")

# spec type key -> (owner epic, registered keys that satisfy it)
REQUIRED_NOTIFICATION_TYPES: dict[str, tuple[str, tuple[str, ...]]] = {
    "vendor_submission_received": (
        "Epic 4 (vendors)",
        ("vendor_submission_received", "vendors.submission_received"),
    ),
    "approval_requested": ("P1 approvals (extended)", ("approval_requested",)),
    "approval_decided": ("P1 approvals (extended)", ("approval_decided",)),
    "bgv_discrepancy_raised": (
        "Epic 7 (bgv)",
        ("bgv_discrepancy_raised", "bgv.discrepancy_raised", "bgv_discrepancy"),
    ),
    "credential_expiring": ("Epic 8 (compliance)", ("credential_expiring", "compliance.credential_expiring")),
    "onboarding_task_returned": (
        "Epic 10 (onboarding)",
        ("onboarding_task_returned", "onboarding.task_returned"),
    ),
    "start_gate_unmet": ("Epic 6 (placements)", ("start_gate_unmet", "placements.start_gate_unmet")),
    "invoice_overdue": ("Epic 17 (receivables)", ("invoice_overdue", "receivables.invoice_overdue")),
    "promise_to_pay_broken": (
        "Epic 17 (receivables)",
        ("promise_to_pay_broken", "receivables.promise_to_pay_broken"),
    ),
    "payment_recorded": ("Epic 17 (receivables)", ("payment_recorded", "receivables.payment_recorded")),
}

# spec type key -> owner epic. Epic 17 receivables registers no notification types yet.
PENDING_NOTIFICATION_TYPES: dict[str, str] = {
    "invoice_overdue": "Epic 17 (receivables): no hooks.py registration yet",
    "promise_to_pay_broken": "Epic 17 (receivables): no hooks.py registration yet",
    "payment_recorded": "Epic 17 (receivables): no hooks.py registration yet",
}


def _registered(key: str) -> str | None:
    return next((k for k in REQUIRED_NOTIFICATION_TYPES[key][1] if k in ns.NOTIFICATION_TYPES), None)


def test_pending_list_only_names_required_types() -> None:
    assert set(PENDING_NOTIFICATION_TYPES) <= set(REQUIRED_NOTIFICATION_TYPES)


@pytest.mark.parametrize("key", sorted(REQUIRED_NOTIFICATION_TYPES))
def test_required_notification_type_is_registered(key: str) -> None:
    found = _registered(key)
    if key in PENDING_NOTIFICATION_TYPES:
        assert found is None, f"{key!r} is registered as {found!r}: remove it from PENDING_NOTIFICATION_TYPES"
        pytest.skip(f"pending: {key} ({PENDING_NOTIFICATION_TYPES[key]})")
    owner = REQUIRED_NOTIFICATION_TYPES[key][0]
    assert found is not None, f"notification type {key!r} (owner {owner}) is not registered"


# Epic 52: Candidate Portal pages that Phase 3 notifications may deep-link to.
# Epic 57: + /portal/screening (Candidate Portal screening chats).
CANDIDATE_PORTAL_PREFIXES = (
    "/portal/assessments", "/portal/interviews", "/portal/privacy", "/portal/profile", "/portal/screening",
)  # fmt: skip


def test_portal_paths_are_portal_deep_links() -> None:
    """A7: External-Principal email carries only portal deep links (/client, /vendor, /portal/work);
    registration of an external type without a portal path is refused by ``notifications_service`` itself.
    Epic 52: Phase 3 candidate-facing types (assessments, campus queue) deep-link into the Candidate Portal
    (``/portal/<page>``, P1 Epic 9); a bare ``/portal`` is not a deep link."""
    fields = {f.name for f in dataclasses.fields(ns.NotificationType)}
    if "portal_path" not in fields:
        pytest.skip("pending: NotificationType has no portal_path yet (Epic 35 A7 external delivery)")
    bad = {
        key: path
        for key, definition in ns.NOTIFICATION_TYPES.items()
        if (path := getattr(definition, "portal_path", None))
        and not str(path).startswith(("/client/", "/vendor/", "/portal/work/", *CANDIDATE_PORTAL_PREFIXES))
    }
    assert not bad, f"portal paths outside the portals: {bad}"
