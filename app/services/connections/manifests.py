# ruff: file-ignore[line-too-long]
"""Calendar Connector Manifests (Epic 72, spec A2; registered by ``app.services.calendar_sync.hooks``).

* ``google_calendar`` / ``microsoft_calendar`` -- listing-only (``auth="user_connection"``: personal connections are
  made on Settings -> Connections, P1). Capability ``calendar.sync``.
* ``google_calendar_org`` / ``microsoft_calendar_org`` -- the organisation free/busy source (capability
  ``calendar.org_free_busy``): one op ``free_busy`` ``{emails, start, end}`` -> ``{busy: {email: [[start, end], ...]}}``.
  Only status / start / end are read; subjects and event details are dropped inside the adapter.

Imports only ``core.integrations`` / ``core.net_safety`` DTOs (no models, no DB, no domain services).
"""

import base64
import json
import time
from collections.abc import Mapping
from datetime import datetime
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict

from app.core.integrations import ConnectorManifest, DataFlow, SecretField
from app.core.integrations.errors import ConnectorAuthError, ConnectorTransientError
from app.core.integrations.fakes import FakeAdapterBase

__all__ = ["CALENDAR_MANIFESTS", "FakeCalendarListing", "FakeOrgFreeBusy", "OrgFreeBusyGoogle", "OrgFreeBusyMicrosoft"]

GOOGLE_HOSTS = ("www.googleapis.com", "oauth2.googleapis.com")
MICROSOFT_HOSTS = ("graph.microsoft.com", "login.microsoftonline.com")


class GoogleOrgConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    admin_email: str = ""
    domain: str = ""


class MicrosoftOrgConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    directory_tenant_id: str = ""
    client_id: str = ""


class FakeCalendarListing(FakeAdapterBase):
    """Fake of the listing-only personal calendar manifests (the real work is the P1 ``FakeProvider``)."""

    category = "calendar"
    provider_key = "google_calendar"


class FakeOrgFreeBusy(FakeAdapterBase):
    """Fake organisation free/busy: ``busy`` (class-level, tests) maps email -> [(start, end)]."""

    category = "calendar"
    provider_key = "google_calendar_org"
    busy: ClassVar[dict[str, list[tuple[datetime, datetime]]]] = {}  # shared test fixture state

    async def handle(self, op: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        if op != "free_busy":
            return await super().handle(op, payload)
        self._check(op, {"emails": len(payload.get("emails") or [])})
        start, end = _dt(payload["start"]), _dt(payload["end"])
        out: dict[str, list[list[str]]] = {}
        for email in payload.get("emails") or []:
            blocks = [b for b in type(self).busy.get(str(email).lower(), []) if b[1] > start and b[0] < end]
            out[str(email).lower()] = [[b[0].isoformat(), b[1].isoformat()] for b in blocks]
        return {"busy": out}


def _dt(value: Any) -> datetime:
    return value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _z(value: Any) -> str:
    return _dt(value).isoformat().replace("+00:00", "Z")


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


class OrgFreeBusyGoogle(FakeAdapterBase):
    """Live Google Workspace free/busy via a service account with domain-wide delegation (scope
    ``calendar.freebusy`` only). Reads ``calendars.<email>.busy`` start / end only."""

    category = "calendar"
    provider_key = "google_calendar_org"

    async def _token(self) -> str:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding

        raw = self.ctx.secrets.get("service_account_json")
        if not raw:
            raise ConnectorAuthError("The service account key is missing")
        info = json.loads(raw)
        now = int(time.time())
        claims = {
            "iss": info["client_email"], "sub": getattr(self.ctx.config, "admin_email", ""),
            "scope": "https://www.googleapis.com/auth/calendar.freebusy", "aud": "https://oauth2.googleapis.com/token",
            "iat": now, "exp": now + 3600,
        }  # fmt: skip
        head = _b64url(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
        body = _b64url(json.dumps(claims).encode())
        key = serialization.load_pem_private_key(info["private_key"].encode(), password=None)
        sig = key.sign(f"{head}.{body}".encode(), padding.PKCS1v15(), hashes.SHA256())  # type: ignore[arg-type, call-arg, union-attr]
        resp = await self.ctx.http.post(
            "https://oauth2.googleapis.com/token",
            data={"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": f"{head}.{body}.{_b64url(sig)}"},
        )
        if resp.status_code in (400, 401, 403):
            raise ConnectorAuthError("Google refused the service account")
        return str(resp.json().get("access_token") or "")

    async def handle(self, op: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        if op != "free_busy":
            return {"ok": True}
        token = await self._token()
        emails = [str(e).lower() for e in payload.get("emails") or []][:50]
        resp = await self.ctx.http.post(
            "https://www.googleapis.com/calendar/v3/freeBusy",
            json={"timeMin": _z(payload["start"]), "timeMax": _z(payload["end"]), "items": [{"id": e} for e in emails]},
            headers={"Authorization": f"Bearer {token}"},
        )
        if resp.status_code >= 400:
            raise ConnectorTransientError(f"google freeBusy: {resp.status_code}")
        cals = resp.json().get("calendars") or {}
        return {"busy": {e: [[b.get("start"), b.get("end")] for b in (cals.get(e) or {}).get("busy") or []] for e in emails}}


class OrgFreeBusyMicrosoft(FakeAdapterBase):
    """Live Microsoft 365 free/busy via client credentials (application permission ``Calendars.ReadBasic.All``);
    ``getSchedule`` ``scheduleItems`` status / start / end only (subjects dropped here)."""

    category = "calendar"
    provider_key = "microsoft_calendar_org"

    async def handle(self, op: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        if op != "free_busy":
            return {"ok": True}
        cfg = self.ctx.config
        resp = await self.ctx.http.post(
            f"https://login.microsoftonline.com/{getattr(cfg, 'directory_tenant_id', '')}/oauth2/v2.0/token",
            data={
                "grant_type": "client_credentials", "client_id": getattr(cfg, "client_id", ""),
                "client_secret": self.ctx.secrets.get("client_secret") or "", "scope": "https://graph.microsoft.com/.default",
            },
        )
        if resp.status_code in (400, 401, 403):
            raise ConnectorAuthError("Microsoft refused the client credentials")
        token = str(resp.json().get("access_token") or "")
        emails = [str(e).lower() for e in payload.get("emails") or []][:20]
        if not emails:
            return {"busy": {}}
        sched = await self.ctx.http.post(
            f"https://graph.microsoft.com/v1.0/users/{emails[0]}/calendar/getSchedule",
            json={
                "schedules": emails, "availabilityViewInterval": 15,
                "startTime": {"dateTime": _z(payload["start"])[:-1], "timeZone": "UTC"},
                "endTime": {"dateTime": _z(payload["end"])[:-1], "timeZone": "UTC"},
            },
            headers={"Authorization": f"Bearer {token}", "Prefer": 'outlook.timezone="UTC"'},
        )
        if sched.status_code >= 400:
            raise ConnectorTransientError(f"microsoft getSchedule: {sched.status_code}")
        out: dict[str, list[list[str]]] = {}
        for item in sched.json().get("value") or []:
            email = str(item.get("scheduleId") or "").lower()
            out[email] = [
                [str((s.get("start") or {}).get("dateTime")) + "Z", str((s.get("end") or {}).get("dateTime")) + "Z"]
                for s in item.get("scheduleItems") or []
                if s.get("status") in ("busy", "tentative", "oof", "workingElsewhere")
            ]
        return {"busy": out}


def _flows_personal() -> tuple[DataFlow, ...]:
    return (
        DataFlow("out", "interview_event", ("title", "time", "attendees", "meeting link"), "Send interview invitations from your calendar", "US", True),
        DataFlow("in", "event_changes", ("time", "response", "deleted"), "Keep interviews in step with calendar changes", "US", True),
    )


def _flows_org() -> tuple[DataFlow, ...]:
    return (DataFlow("in", "free_busy", ("busy times",), "Show availability for interview scheduling", "US", True),)


CALENDAR_MANIFESTS: tuple[ConnectorManifest, ...] = (
    ConnectorManifest(
        key="google_calendar", version=1, owner_epic="72", name="Google Calendar",
        summary="Sends interview invitations from your Google Calendar and brings changes back to Talent OS.",
        category="calendar", capabilities=frozenset({"calendar.sync"}), auth="user_connection",
        data_flows=_flows_personal(), adapters={"fake": FakeCalendarListing},
    ),
    ConnectorManifest(
        key="microsoft_calendar", version=1, owner_epic="72", name="Microsoft Outlook Calendar",
        summary="Sends interview invitations from your Outlook calendar and brings changes back to Talent OS.",
        category="calendar", capabilities=frozenset({"calendar.sync"}), auth="user_connection",
        data_flows=_flows_personal(), adapters={"fake": FakeCalendarListing},
    ),
    ConnectorManifest(
        key="google_calendar_org", version=1, owner_epic="72", name="Google Workspace free/busy",
        summary="Shows free/busy for people who haven't connected their own calendar. Never reads event details.",
        category="calendar", capabilities=frozenset({"calendar.org_free_busy"}), auth="oauth2_client",
        config_model=GoogleOrgConfig, secret_fields=(SecretField("service_account_json", "Service account key (JSON)", "private_key"),),
        data_flows=_flows_org(), egress_hosts=GOOGLE_HOSTS, adapters={"fake": FakeOrgFreeBusy, "live": OrgFreeBusyGoogle},
    ),
    ConnectorManifest(
        key="microsoft_calendar_org", version=1, owner_epic="72", name="Microsoft 365 free/busy",
        summary="Shows free/busy for people who haven't connected their own calendar. Never reads event details.",
        category="calendar", capabilities=frozenset({"calendar.org_free_busy"}), auth="oauth2_client",
        config_model=MicrosoftOrgConfig, secret_fields=(SecretField("client_secret", "Client secret", "password"),),
        data_flows=_flows_org(), egress_hosts=MICROSOFT_HOSTS, adapters={"fake": FakeOrgFreeBusy, "live": OrgFreeBusyMicrosoft},
    ),
)  # fmt: skip
