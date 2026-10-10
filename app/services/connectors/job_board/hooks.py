"""Registers the ``fake_job_board`` manifest (Epic 66; Fake + Manual adapters). Epic 70 registers the real boards."""

from app.core.integrations import ConnectorManifest, DataFlow, SecretField, SyncJobSpec, register_connector
from app.core.integrations.fakes import FAKE_WEBHOOK
from app.services.connectors.job_board.fake import FakeJobBoard
from app.services.connectors.job_board.manual import ManualJobBoard

FAKE_JOB_BOARD = ConnectorManifest(
    key="fake_job_board",
    version=1,
    owner_epic="66",
    name="Fake job board",
    summary="A deterministic job board for tests, demos and Sandbox Tenants (no network).",
    category="job_board",
    capabilities=frozenset({"job_board.publish", "job_board.applicants", "job_board.status", "job_board.fields"}),
    auth="api_key",
    secret_fields=(
        SecretField("api_key", "API key", "api_key"),
        SecretField("webhook_secret", "Webhook secret", "webhook_secret", required=False),
    ),
    data_flows=(
        DataFlow("out", "job_posting", ("title", "description", "location", "skills"), "job advertising", "IN", False),
        DataFlow(
            "in",
            "board_application",
            ("full_name", "email", "phone", "resume"),
            "candidate applications",
            "IN",
            True,
        ),
    ),
    sync_jobs=(
        SyncJobSpec(
            key="applicants",
            capability="job_board.applicants",
            direction="pull",
            trigger="schedule",
            every_seconds=900,
            entity_type="board_application",
        ),
        SyncJobSpec(
            key="publish",
            capability="job_board.publish",
            direction="push",
            trigger="manual",
            entity_type="job_posting",
        ),
    ),
    webhook=FAKE_WEBHOOK,
    adapters={"fake": FakeJobBoard, "manual": ManualJobBoard},
    legal_entity_scoped=True,
    multi_install=True,
)

register_connector(FAKE_JOB_BOARD)
