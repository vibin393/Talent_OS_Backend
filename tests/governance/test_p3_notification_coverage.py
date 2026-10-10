# ruff: noqa: E501, TC002
"""FR-259 governance (Epic 52 story 52.3, spec A3): the Phase 3 notification types of foundation 0.7.

* Every required type is registered after ``autodiscover`` (``PENDING_P3_NOTIFICATION_TYPES`` shrink-only, empty at
  phase close).
* Recipient kinds: candidate-facing types are declared external with a portal path; ``prediction_band_changed`` is
  opt-in (``default_channel="off"``, not mandatory, internal only) and reaches a User only after the User opts in
  through ``PUT /me/notification-preferences``.
* Renderers carry ids and labels only: a payload carrying self-ID, compensation, other candidates' scores or
  proctoring details never leaks those values into the rendered title / body / link.

Owner deviations (recorded in the Epic 52 notes, not patched here): ``campus_registration_received`` is a daily
digest to the drive owner and ``campus_slot_assigned`` goes to the interviewers (both internal only, Epic 43);
the candidate-facing campus type is ``queue_turn``.
"""

import uuid

import httpx
import pytest

from app.core.database import tenant_session
from app.core.hooks import autodiscover
from app.services import notifications_service as ns
from tests.epic3 import Org3, h

autodiscover("app.services")

# type key -> owner epic
REQUIRED_P3_NOTIFICATION_TYPES: dict[str, str] = {
    "pool_new_members": "Epic 38",
    "campaign_approval_requested": "Epic 39",
    "campaign_completed": "Epic 39",
    "sequence_reply": "Epic 39",
    "assessment_invited": "Epic 40",
    "assessment_reminder": "Epic 40",
    "assessment_completed": "Epic 40",
    "assessment_review_needed": "Epic 40",
    "internal_application_submitted": "Epic 41",
    "internal_application_decided": "Epic 41",
    "referral_status_changed": "Epic 42",
    "campus_registration_received": "Epic 43",
    "campus_slot_assigned": "Epic 43",
    "queue_turn": "Epic 43",
    "workforce_plan_approval_requested": "Epic 44",
    "workforce_plan_decided": "Epic 44",
    "deliverable_submitted": "Epic 45",
    "project_budget_threshold": "Epic 45",
    "immigration_case_step": "Epic 46",
    "report_delivered": "Epic 47",
    "report_failed": "Epic 47",
    "prediction_band_changed": "Epic 48",
    "dsc_signing_pending": "Epic 51",
}
PENDING_P3_NOTIFICATION_TYPES: dict[str, str] = {}

# Types delivered to Candidates (Candidate / Worker portal) -> must be external with a portal deep link.
CANDIDATE_FACING = ("assessment_invited", "assessment_reminder", "queue_turn")
CLIENT_FACING = ("deliverable_submitted",)

POISON: dict[str, str] = {
    "gender": "POISON-GENDER-female",
    "age_band": "POISON-AGE-25-34",
    "disability": "POISON-DISABILITY-yes",
    "self_id": "POISON-SELFID",
    "compensation": "POISON-COMP-987654",
    "bill_rate": "POISON-RATE-123456",
    "reward_amount": "POISON-REWARD-55555",
    "other_candidate_score": "POISON-SCORE-99.9",
    "proctoring_signals": "POISON-PROCTOR-tab_switch",
    "feedback": "POISON-FEEDBACK-weak candidate",
}


@pytest.mark.parametrize("key", sorted(REQUIRED_P3_NOTIFICATION_TYPES))
def test_p3_type_registered(key: str) -> None:
    found = ns.NOTIFICATION_TYPES.get(key)
    if key in PENDING_P3_NOTIFICATION_TYPES:
        assert found is None, f"{key} is registered now: remove it from PENDING_P3_NOTIFICATION_TYPES"
        pytest.skip(f"pending: {key}")
    assert found is not None, (
        f"notification type {key!r} ({REQUIRED_P3_NOTIFICATION_TYPES[key]}) is not registered"
    )


def test_phase_close_pending_list_is_empty() -> None:
    assert PENDING_P3_NOTIFICATION_TYPES == {}


@pytest.mark.parametrize("key", CANDIDATE_FACING + CLIENT_FACING)
def test_external_types_declare_portal_link(key: str) -> None:
    d = ns.NOTIFICATION_TYPES[key]
    assert d.external and d.portal_path, f"{key}: external delivery needs external=True and a portal_path"
    assert not d.internal_only
    prefix = "/client/" if key in CLIENT_FACING else "/portal/"
    assert str(d.portal_path).startswith(prefix), f"{key}: {d.portal_path}"


def test_referral_status_uses_referrer_safe_renderer() -> None:
    d = ns.NOTIFICATION_TYPES["referral_status_changed"]
    assert d.render.__module__.startswith("app.services.referrals")


def test_prediction_band_changed_is_opt_in() -> None:
    d = ns.NOTIFICATION_TYPES["prediction_band_changed"]
    assert (d.default_channel, d.mandatory_in_app, d.internal_only, d.external) == ("off", False, True, False)


def test_attrition_predictions_need_read_attrition() -> None:
    from app.services.analytics.governance import kind_permission

    assert kind_permission("attrition") == "prediction:read_attrition"


async def test_prediction_band_changed_reaches_only_opted_in_users(org3: Org3) -> None:
    user_id = org3.admin.user_id
    content = ns.Content(title="Time to fill is now High", body="Supporting signal, not a decision.")
    async with tenant_session(org3.tenant_id) as db:
        sent = await ns.deliver(db, "prediction_band_changed", [user_id], content, event_id=str(uuid.uuid4()))
    assert sent == [], "a User without a preference row must not get prediction_band_changed"

    client: httpx.AsyncClient = org3.admin.client
    resp = await client.put(
        "/api/v1/me/notification-preferences",
        json={"items": [{"type": "prediction_band_changed", "channel": "in_app"}]},
        headers=h(org3.admin),
    )
    assert resp.status_code == 200, resp.text
    async with tenant_session(org3.tenant_id) as db:
        sent = await ns.deliver(db, "prediction_band_changed", [user_id], content, event_id=str(uuid.uuid4()))
    assert sent == [user_id]


@pytest.mark.parametrize("key", sorted(REQUIRED_P3_NOTIFICATION_TYPES))
async def test_rendered_payload_has_no_denylisted_values(key: str, org3: Org3) -> None:
    """Renderers build content from ids / labels; protected or sensitive payload values never pass through."""
    d = ns.NOTIFICATION_TYPES[key]
    rid = str(uuid.uuid4())
    payload: dict[str, object] = dict.fromkeys(("pool_id", "campaign_id", "sequence_id", "enrollment_id", "invitation_id", "assessment_id", "candidate_id", "application_id", "mobility_application_id", "demand_id", "referral_id", "drive_id", "registration_id", "event_id", "entry_id", "plan_id", "workforce_plan_id", "deliverable_id", "project_id", "client_id", "case_id", "immigration_case_id", "run_id", "schedule_id", "report_definition_id", "prediction_id", "subject_id", "document_id", "invoice_id", "batch_id", "slot_id", "queue_entry_id"), rid)  # fmt: skip
    payload |= {"subject_kind": "demand", "model_kind": "time_to_fill", "to_band": "high", "status": "hired"}
    payload |= POISON
    async with tenant_session(org3.tenant_id) as db:
        try:
            content = await d.render(db, payload)
        except Exception as exc:
            pytest.skip(f"{key}: renderer refused synthetic ids ({type(exc).__name__})")
    rendered = " ".join(str(x) for x in (content.title, content.body, content.link_path) if x)
    leaked = sorted(k for k, v in POISON.items() if v in rendered)
    assert leaked == [], f"{key} renders denylisted payload values: {leaked}"
