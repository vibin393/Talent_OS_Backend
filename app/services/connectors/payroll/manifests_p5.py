# ruff: file-ignore[line-too-long]
"""Connector Manifests of the payroll twins of the HRMS connectors (Epic 71, spec A3/A6): ``darwinbox_payroll`` and
``keka_payroll`` (separate installs; the Tenant may use one without the other). Registered by
``app.services.hrms_sync.hooks``.

The P2 payroll Fake / Manual adapters take a legacy ``ConnectorConfig``; ``FakePayrollPush`` / ``ManualPayrollPush``
adapt them to the manifest factory contract (``factory(AdapterContext)``) without changing their behaviour.
"""

from collections.abc import Mapping
from typing import Any, ClassVar

from app.core.integrations import ConnectorManifest, DataFlow, SecretField, SyncJobSpec
from app.core.integrations.context import AdapterContext
from app.core.integrations.errors import ConnectorAuthError, ConnectorRateLimited
from app.services.connectors.hrms.darwinbox import EGRESS_HOSTS as DARWINBOX_HOSTS
from app.services.connectors.hrms.keka import EGRESS_HOSTS as KEKA_HOSTS
from app.services.connectors.hrms.keka import KekaConfig
from app.services.connectors.payroll.darwinbox import DarwinboxPayroll, DarwinboxPayrollConfig
from app.services.connectors.payroll.fake import FakePayrollProvider
from app.services.connectors.payroll.keka import KekaPayroll
from app.services.connectors.payroll.manual import ManualPayrollProvider

__all__ = ["DARWINBOX_PAYROLL", "KEKA_PAYROLL", "PAYROLL_P5_MANIFESTS", "FakePayrollPush", "ManualPayrollPush"]


class FakePayrollPush(FakePayrollProvider):
    """The P2 payroll Fake behind a manifest context (auth failure when ``api_key == "invalid"``)."""

    provider_key = "fake"
    script: ClassVar[dict[str, Any]] = {}  # test hook: {"rate_limit_ops": [...]}

    def __init__(self, ctx: AdapterContext | None = None) -> None:
        super().__init__(None)
        self.ctx = ctx

    def __repr__(self) -> str:
        return "FakePayrollPush()"

    def _guard(self, op: str) -> None:
        if self.ctx is not None and self.ctx.secrets.get("api_key") == "invalid":
            msg = "The provider rejected the credentials"
            raise ConnectorAuthError(msg)
        if op in set(type(self).script.get("rate_limit_ops") or ()):
            raise ConnectorRateLimited(30)

    async def handle(self, op: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        self._guard(op)
        return await super().handle(op, payload)

    async def push_export(self, export_id: Any, file: bytes, media_type: str) -> Mapping[str, Any]:
        self._guard("push_export")
        return await super().push_export(export_id, file, media_type)

    def verify_webhook(self, headers: Mapping[str, str], body: bytes) -> bool:  # ruff: ignore[unused-method-argument]
        return False


class ManualPayrollPush(ManualPayrollProvider):
    def __init__(self, ctx: AdapterContext | None = None) -> None:
        super().__init__(None)
        self.ctx = ctx


_FLOW = DataFlow(
    "out", "payroll_export", ("worker_id", "hours", "earnings", "deductions", "pan_last4", "bank_last4"),
    "Send the approved payroll inputs to your payroll system", "IN", True,
)  # fmt: skip


def _job() -> tuple[SyncJobSpec, ...]:
    return (
        SyncJobSpec("payroll_push", "payroll.push", "push", "event", ("payroll_export.finalized",), entity_type="payroll_export", per_legal_entity=True),
    )


DARWINBOX_PAYROLL = ConnectorManifest(
    key="darwinbox_payroll",
    version=1,
    owner_epic="71",
    name="Darwinbox Payroll",
    summary="Send approved payroll exports to Darwinbox Payroll.",
    category="payroll",
    capabilities=frozenset({"payroll.push"}),
    auth="basic",
    config_model=DarwinboxPayrollConfig,
    secret_fields=(
        SecretField("api_key", "API key", "api_key"),
        SecretField("username", "API username", "token"),
        SecretField("password", "API password", "password"),
    ),
    data_flows=(_FLOW,),
    egress_hosts=DARWINBOX_HOSTS,
    tenant_host_field="subdomain",
    sync_jobs=_job(),
    adapters={"fake": FakePayrollPush, "live": DarwinboxPayroll, "manual": ManualPayrollPush},
    legal_entity_scoped=True,
)

KEKA_PAYROLL = ConnectorManifest(
    key="keka_payroll",
    version=1,
    owner_epic="71",
    name="Keka Payroll",
    summary="Send approved payroll exports to Keka Payroll as pay inputs.",
    category="payroll",
    capabilities=frozenset({"payroll.push"}),
    auth="oauth2_client",
    config_model=KekaConfig,
    secret_fields=(
        SecretField("client_id", "Client ID", "token"),
        SecretField("client_secret", "Client secret", "password"),
        SecretField("api_key", "API key", "api_key"),
    ),
    data_flows=(_FLOW,),
    egress_hosts=KEKA_HOSTS,
    tenant_host_field="company_slug",
    sync_jobs=_job(),
    adapters={"fake": FakePayrollPush, "live": KekaPayroll, "manual": ManualPayrollPush},
    legal_entity_scoped=True,
)

PAYROLL_P5_MANIFESTS = (DARWINBOX_PAYROLL, KEKA_PAYROLL)
