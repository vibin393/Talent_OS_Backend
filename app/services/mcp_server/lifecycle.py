# ruff: file-ignore[line-too-long]
"""Which sessions an event ends (Epic 73, spec 0.7; FR-380, AD-86). Pure: event -> ``EndRequest``.

| Event | Sessions ended | ``end_reason`` |
|---|---|---|
| ``KillSwitchChanged(on=True)`` (P4 AI kill switch) | the tenant's | ``ai_kill_switch`` |
| ``EcosystemKillSwitchChanged(on=True)`` (app scope -> that App's) | matching | ``kill_switch`` |
| ``McpEnablementChanged(on=False)`` | the tenant's | ``mcp_disabled`` |
| ``InstallationChanged(change in suspended, uninstalled)`` | that Installation's | ``uninstalled`` |
| ``UserGrantChanged(change=revoked, resource=mcp)`` / ``OAuthTokenFamilyRevoked`` | that user + Installation | ``authorization_revoked`` |

No P1 user-deactivation event exists (Known gap 6): a deactivated user fails ``authenticate_bearer`` at once and the
session is ended lazily (``user_deactivated``) on its next request or by the idle sweep. Independently, the request
gates refuse the next call within the 15 s gate cache, so the 1-minute guarantee holds even when a task is delayed.
"""

import dataclasses
import uuid
from typing import Any

END_ON_INSTALLATION = frozenset({"suspended", "uninstalled"})


@dataclasses.dataclass(frozen=True, slots=True)
class EndRequest:
    tenant_id: uuid.UUID
    reason: str
    installation_id: uuid.UUID | None = None
    user_id: uuid.UUID | None = None
    app_id: uuid.UUID | None = None

    def task_args(self) -> tuple[str, dict[str, str | None], str]:
        flt = {
            "installation_id": str(self.installation_id) if self.installation_id else None,
            "user_id": str(self.user_id) if self.user_id else None,
            "app_id": str(self.app_id) if self.app_id else None,
        }
        return str(self.tenant_id), flt, self.reason


def for_event(event: Any) -> EndRequest | None:
    name = type(event).__name__
    if name == "KillSwitchChanged":
        return EndRequest(event.tenant_id, "ai_kill_switch") if event.on else None
    if name == "EcosystemKillSwitchChanged":
        if not event.on:
            return None
        app_id = event.app_id if event.scope == "platform_app" else None
        return EndRequest(event.tenant_id, "kill_switch", app_id=app_id)
    if name == "McpEnablementChanged":
        return None if event.on else EndRequest(event.tenant_id, "mcp_disabled")
    if name == "InstallationChanged":
        if event.change in END_ON_INSTALLATION:
            return EndRequest(event.tenant_id, "uninstalled", installation_id=event.installation_id)
        return None
    if name == "UserGrantChanged":
        if event.change == "revoked" and event.resource == "mcp":
            return EndRequest(event.tenant_id, "authorization_revoked", installation_id=event.installation_id, user_id=event.user_id)
        return None
    if name == "OAuthTokenFamilyRevoked":
        if event.user_id is None:  # an App-only (client credentials) family never carries MCP sessions
            return None
        return EndRequest(event.tenant_id, "authorization_revoked", installation_id=event.installation_id, user_id=event.user_id)
    return None


__all__ = ["END_ON_INSTALLATION", "EndRequest", "for_event"]
