# ruff: file-ignore[line-too-long]
"""Manual ``bgv`` connector: the Phase 2 workflow (AD-29). No vendor API is called.

``initiate_check`` raises ``ManualActionRequired``: the BGV domain then runs the manual path itself
(``services/bgv/provider_links.py``): a single-purpose Secure Link (``bgv_provider_check``) emailed to the
provider contact, whose submission is stored as a *proposal* until a Compliance User records the result. When the
provider has no email the Compliance User downloads the minimal check packet and records the result directly.
``get_status`` / ``fetch_report`` have no remote state (the domain reads its own rows); there are no webhooks.
"""

from collections.abc import Mapping, Sequence
from typing import Any

from app.services.connectors.base import ConnectorConfig, ManualActionRequired
from app.services.connectors.bgv.port import BgvCheckJob, BgvReportDTO, BgvStatusDTO, BgvWebhookDTO
from app.services.connectors.registry import register_adapter


class ManualBgvProvider:
    category = "bgv"
    provider_key = "manual"
    key = "manual"

    def __init__(self, config: ConnectorConfig | None = None) -> None:
        self.config = config

    async def handle(self, op: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        raise ManualActionRequired(
            f"bgv: complete {op} manually", {"op": op, "check_id": payload.get("check_id")}
        )

    def verify_webhook(self, headers: Mapping[str, str], body: bytes) -> bool:  # ruff: ignore[unused-method-argument]
        return False

    def parse_webhook(self, headers: Mapping[str, str], body: bytes) -> Mapping[str, Any]:  # ruff: ignore[unused-method-argument]
        return {}

    async def initiate_check(self, job: BgvCheckJob) -> str:
        raise ManualActionRequired(
            "bgv: send the check to the provider through a secure link",
            {"op": "initiate_check", "check_id": str(job.check_id)},
        )

    async def get_status(self, external_ref: str) -> BgvStatusDTO:  # NOSONAR
        return BgvStatusDTO(external_ref, "in_progress")

    async def fetch_report(self, external_ref: str) -> BgvReportDTO | None:  # ruff: ignore[unused-method-argument]  # NOSONAR
        return None

    async def handle_webhook(self, headers: Mapping[str, str], body: bytes) -> BgvWebhookDTO:  # ruff: ignore[unused-method-argument]
        raise ManualActionRequired("bgv: the manual connector has no webhooks", {"op": "handle_webhook"})


register_adapter("bgv", "manual", ManualBgvProvider)

__all__ = ["ManualBgvProvider", "Sequence"]
