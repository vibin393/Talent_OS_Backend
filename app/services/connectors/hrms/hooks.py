"""Registers the ``fake_hrms`` manifest (Epic 66; Fake + Manual adapters). Epic 71 registers the real HRMS connectors."""

from app.core.integrations import ConnectorManifest, DataFlow, SecretField, SyncJobSpec, register_connector
from app.core.integrations.fakes import FAKE_WEBHOOK
from app.services.connectors.hrms.fake import FakeHrms
from app.services.connectors.hrms.manual import ManualHrms

FAKE_HRMS = ConnectorManifest(
    key="fake_hrms",
    version=1,
    owner_epic="66",
    name="Fake HRMS",
    summary="A deterministic HRMS for tests, demos and Sandbox Tenants (no network).",
    category="hrms",
    capabilities=frozenset({"hrms.push", "hrms.pull_status", "hrms.fields"}),
    auth="api_key",
    secret_fields=(
        SecretField("api_key", "API key", "api_key"),
        SecretField("webhook_secret", "Webhook secret", "webhook_secret", required=False),
    ),
    data_flows=(
        DataFlow(
            "out",
            "worker",
            ("name", "email", "date_of_joining", "department"),
            "employee onboarding in the HRMS",
            "IN",
            True,
        ),
        DataFlow("in", "employee_status", ("employee_code", "status", "exit_date"), "exit tracking", "IN", True),
    ),
    sync_jobs=(
        SyncJobSpec(
            key="pull_status",
            capability="hrms.pull_status",
            direction="pull",
            trigger="schedule",
            every_seconds=3600,
            entity_type="worker",
            per_legal_entity=True,
        ),
        SyncJobSpec(
            key="push_hire",
            capability="hrms.push",
            direction="push",
            trigger="manual",
            entity_type="worker",
            per_legal_entity=True,
        ),
    ),
    webhook=FAKE_WEBHOOK,
    adapters={"fake": FakeHrms, "manual": ManualHrms},
    legal_entity_scoped=True,
)

register_connector(FAKE_HRMS)
