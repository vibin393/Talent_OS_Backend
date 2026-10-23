"""Manual ``assessment`` connector (AD-29): no provider. ``list_tests`` is empty, ``invite`` asks a person
(``ManualActionRequired``) and ``fetch_result`` returns ``None`` -- results are entered by a User (Epic 40).
"""

from collections.abc import Mapping, Sequence
from typing import Any

from app.services.connectors.assessment.port import (
    ProviderInviteDTO,
    ProviderInviteResultDTO,
    ProviderResultDTO,
    ProviderTestDTO,
    ProviderWebhookDTO,
)
from app.services.connectors.base import ConnectorConfig, ManualActionRequired
from app.services.connectors.registry import register_adapter


class ManualAssessmentProvider:
    category = "assessment"
    provider_key = "manual"
    key = "manual"

    def __init__(self, config: ConnectorConfig | None = None) -> None:
        self.config = config

    async def handle(self, op: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:  # ruff: ignore[unused-method-argument]
        raise ManualActionRequired(f"assessment: complete {op} manually", {"op": op})

    def verify_webhook(self, headers: Mapping[str, str], body: bytes) -> bool:  # ruff: ignore[unused-method-argument]
        return False

    def parse_webhook(self, headers: Mapping[str, str], body: bytes) -> Mapping[str, Any]:  # ruff: ignore[unused-method-argument]
        return {}

    async def list_tests(self) -> Sequence[ProviderTestDTO]:
        return []

    async def invite(self, req: ProviderInviteDTO) -> ProviderInviteResultDTO:
        raise ManualActionRequired(
            "assessment: send the test to the candidate and enter the result manually",
            {"op": "invite", "invitation_id": str(req.invitation_id)},
        )

    async def fetch_result(self, external_ref: str) -> ProviderResultDTO | None:  # ruff: ignore[unused-method-argument]
        return None

    async def handle_webhook(self, headers: Mapping[str, str], body: bytes) -> ProviderWebhookDTO:  # ruff: ignore[unused-method-argument]
        raise ManualActionRequired(
            "assessment: the manual connector receives no webhooks", {"op": "handle_webhook"}
        )


register_adapter("assessment", "manual", ManualAssessmentProvider)

__all__ = ["ManualAssessmentProvider"]
