# ruff: noqa: E501
"""Connector Manifests of the real HRMS connectors (Epic 71, spec A3): Darwinbox and Keka. Registered by
``app.services.hrms_sync.hooks``; listed in the marketplace by Epic 69.

Both: category ``hrms``, Legal-Entity scoped (one active HRMS per entity), ``erasure=False`` (statutory employment
records, FR-410), adapters ``{"live", "fake": FakeHrms, "manual": ManualHrms}``. Sync jobs: ``push_hire`` (internal
``placement.joined``), ``push_change`` (internal ``placement.changed``), ``push_exit`` (public ``placement.ended``),
``pull_status`` (daily) and ``manual_push`` (manual).
"""

from app.core.integrations import ConnectorManifest, DataFlow, SecretField, SyncJobSpec
from app.services.connectors.hrms.darwinbox import EGRESS_HOSTS as DARWINBOX_HOSTS
from app.services.connectors.hrms.darwinbox import DarwinboxConfig, DarwinboxHrms
from app.services.connectors.hrms.fake import FakeHrms
from app.services.connectors.hrms.keka import EGRESS_HOSTS as KEKA_HOSTS
from app.services.connectors.hrms.keka import KekaConfig, KekaHrms
from app.services.connectors.hrms.manual import ManualHrms

__all__ = ["DARWINBOX", "HRMS_MANIFESTS", "KEKA", "OUT_FIELDS"]

OUT_FIELDS = (
    "name", "work_email", "phone", "date_of_joining", "designation", "department", "location", "employment_type",
    "manager", "cost_centre", "bank_account_reference", "pan_reference", "aadhaar_reference",
)  # fmt: skip
_CAPS = frozenset({"hrms.push", "hrms.pull_status", "hrms.fields"})
_FLOWS = (
    DataFlow("out", "worker", OUT_FIELDS, "Create and update the employee record in your HR system", "IN", True),
    DataFlow("in", "employee_status", ("employee_code", "status", "exit_date"), "Keep the employee code and status in Talent OS", "IN", True),
)


def _jobs() -> tuple[SyncJobSpec, ...]:
    return (
        SyncJobSpec("push_hire", "hrms.push", "push", "event", ("placement.joined",), entity_type="worker", per_legal_entity=True),
        SyncJobSpec("push_change", "hrms.push", "push", "event", ("placement.changed",), entity_type="worker", per_legal_entity=True),
        SyncJobSpec("push_exit", "hrms.push", "push", "event", ("placement.ended",), entity_type="worker", per_legal_entity=True),
        SyncJobSpec("pull_status", "hrms.pull_status", "pull", "schedule", every_seconds=86400, entity_type="worker", per_legal_entity=True),
        SyncJobSpec("manual_push", "hrms.push", "push", "manual", entity_type="worker", per_legal_entity=True, max_batch=50),
    )


DARWINBOX = ConnectorManifest(
    key="darwinbox",
    version=1,
    owner_epic="71",
    name="Darwinbox",
    summary="Push new hires, changes and exits to Darwinbox and bring back employee codes and status.",
    category="hrms",
    capabilities=_CAPS,
    auth="basic",
    config_model=DarwinboxConfig,
    secret_fields=(
        SecretField("api_key", "API key", "api_key"),
        SecretField("username", "API username", "token"),
        SecretField("password", "API password", "password"),
    ),
    data_flows=_FLOWS,
    egress_hosts=DARWINBOX_HOSTS,
    tenant_host_field="subdomain",
    sync_jobs=_jobs(),
    adapters={"fake": FakeHrms, "live": DarwinboxHrms, "manual": ManualHrms},
    legal_entity_scoped=True,
    erasure=False,
    rate_per_minute=60,
    docs_url="https://darwinbox.com/",
)

KEKA = ConnectorManifest(
    key="keka",
    version=1,
    owner_epic="71",
    name="Keka HR",
    summary="Push new hires, changes and exits to Keka and bring back employee numbers and status.",
    category="hrms",
    capabilities=_CAPS,
    auth="oauth2_client",
    config_model=KekaConfig,
    secret_fields=(
        SecretField("client_id", "Client ID", "token"),
        SecretField("client_secret", "Client secret", "password"),
        SecretField("api_key", "API key", "api_key"),
    ),
    data_flows=_FLOWS,
    egress_hosts=KEKA_HOSTS,
    tenant_host_field="company_slug",
    sync_jobs=_jobs(),
    adapters={"fake": FakeHrms, "live": KekaHrms, "manual": ManualHrms},
    legal_entity_scoped=True,
    erasure=False,
    rate_per_minute=50,
    docs_url="https://developers.keka.com/",
)

HRMS_MANIFESTS = (DARWINBOX, KEKA)
