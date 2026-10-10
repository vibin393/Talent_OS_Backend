# ruff: noqa: E501
"""Exotel telephony adapter (Epic 76, spec A2; key ``exotel``; India DC).

* Bridged call: ``POST https://{subdomain}/v1/Accounts/{sid}/Calls/connect.json`` (``From`` = recruiter, ``To`` =
  Candidate, ``CallerId`` = ExoPhone, ``TimeLimit``, ``StatusCallback`` + ``StatusCallbackEvents[0]=terminal``).
  Recording (``Record=true``, ``RecordingChannels=dual``) is requested only when a recording-notice flow is configured
  (``recording_flow_app_id``, the ExoPhone greeting with the notice); otherwise ``record`` is refused upstream with
  ``notice_unavailable``.
* Callbacks: Exotel cannot sign requests. The status callback goes to the Talent OS relay
  ``/api/v1/hooks/voice/{config_id}/exotel/{path_token}`` which checks the secret path token (constant time) and the
  configured Exotel egress CIDRs, then hands the body to the connector ingress with ``X-TalentOS-Relay`` (HMAC of the
  body with the path token) -- ``verify_webhook`` checks that. **Verify-by-fetch**: the callback content is never
  applied; ``telephony.ingress`` fetches ``OP_CALL_STATUS`` and applies only the fetched status.
* Recordings: ``GET`` the recording URL with basic auth. Deletion through the API is not supported:
  ``delete_recording`` returns ``unsupported`` (account-level retention applies; disclosed per Tenant).
"""

from collections.abc import Mapping
from datetime import datetime
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field

from app.core.integrations.errors import ConnectorPermanentError
from app.services.connectors.voice.bridge import BridgedCallRequest, CallStatusDTO, to_minor
from app.services.connectors.voice.live_base import LiveTelephonyBase, basic_auth, classify, relay_signature
from app.services.connectors.voice.port import RecordingDTO
from app.services.connectors.voice.twilio import parse_form

__all__ = ["EGRESS_HOSTS", "RELAY_HEADER", "ExotelConfig", "ExotelTelephony"]

EGRESS_HOSTS = ("api.exotel.com", "api.in.exotel.com", "recordings.exotel.com")
RELAY_HEADER = "X-TalentOS-Relay"
_STATUS = {"completed": "completed", "busy": "busy", "no-answer": "no_answer", "failed": "failed", "canceled": "failed", "in-progress": "answered", "ringing": "ringing"}


class ExotelConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    account_sid: str = Field(default="", max_length=80)
    subdomain: Literal["api.exotel.com", "api.in.exotel.com"] = "api.in.exotel.com"
    recording_flow_app_id: str | None = Field(default=None, max_length=40)
    callback_ip_cidrs: list[str] = Field(default_factory=list)
    account_recording_retention_days: int | None = Field(default=None, ge=0, le=3650)


def _int(value: Any) -> int | None:
    try:
        return int(float(value)) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace(" ", "T"))
    except ValueError:
        return None


class ExotelTelephony(LiveTelephonyBase):
    provider_key = "exotel"
    media_type = "audio/mpeg"

    @property
    def _cfg(self) -> ExotelConfig:
        return self.ctx.config  # type: ignore[return-value]

    def _auth(self) -> dict[str, str]:
        return basic_auth(self.ctx.secrets.get("api_key"), self.ctx.secrets.get("api_token"))

    def _url(self, path: str) -> str:
        sid = self._cfg.account_sid
        if not sid:
            raise ConnectorPermanentError("config_missing", "The Exotel account SID is not configured")
        return f"https://{self._cfg.subdomain}/v1/Accounts/{sid}{path}"

    def can_record(self) -> bool:
        return bool(self._cfg.recording_flow_app_id)

    async def health(self) -> Mapping[str, Any]:
        resp = await self.ctx.http.get(self._url(".json"), headers=self._auth())
        classify(resp, "account")
        return {"ok": True}

    async def bridge_call(self, req: BridgedCallRequest) -> Mapping[str, Any]:
        record = bool(req.record and self.can_record())
        data: dict[str, Any] = {
            "From": req.agent_e164,
            "To": req.candidate_e164,
            "CallerId": req.caller_id_e164,
            "TimeLimit": str(req.max_duration_s),
            "StatusCallback": req.status_callback_url,
            "StatusCallbackEvents[0]": "terminal",
            "Record": "true" if record else "false",
        }
        if record:
            data["RecordingChannels"] = "dual" if req.dual_channel else "single"
        resp = await self.ctx.http.post(self._url("/Calls/connect.json"), headers=self._auth(), data=data)
        classify(resp, "place call")
        call = (resp.json() or {}).get("Call") or {}
        sid = str(call.get("Sid") or "")
        if not sid:
            return {"external_ref": "", "status": "failed", "failure_code": "provider_no_reference"}
        return {"external_ref": sid, "status": "dialing_recruiter"}

    async def call_status(self, external_ref: str) -> Mapping[str, Any]:
        resp = await self.ctx.http.get(self._url(f"/Calls/{external_ref}.json"), headers=self._auth())
        classify(resp, "call status")
        call = (resp.json() or {}).get("Call") or {}
        status = _STATUS.get(str(call.get("Status") or ""), "")
        return CallStatusDTO(
            external_ref=external_ref,
            status=status,
            answered_at=_dt(call.get("StartTime")),
            ended_at=_dt(call.get("EndTime")),
            duration_s=_int(call.get("Duration")),
            price_minor=to_minor(call.get("Price")),
            currency="INR" if call.get("Price") not in (None, "") else None,
            recording_ref=call.get("RecordingUrl") or None,
            recording_channels=2 if call.get("RecordingUrl") else None,
        ).to_mapping()

    async def hangup(self, external_ref: str) -> Mapping[str, Any]:  # noqa: ARG002
        return {"ok": False, "unsupported": True}  # Exotel has no hang-up API for connect calls

    async def delete_recording(self, recording_ref: str) -> Mapping[str, Any]:  # noqa: ARG002
        return {"status": "unsupported", "retention_days": self._cfg.account_recording_retention_days}

    async def fetch_recording(self, recording_ref: str) -> RecordingDTO | None:
        host = urlsplit(recording_ref).hostname or ""
        if host not in EGRESS_HOSTS:
            raise ConnectorPermanentError("recording_host", "The recording host is not allowed")
        return await self._download(recording_ref)

    def verify_webhook(self, headers: Mapping[str, str], body: bytes) -> bool:
        import hmac

        token = self.ctx.secrets.get("webhook_path_token")
        given = next((v for k, v in headers.items() if k.lower() == RELAY_HEADER.lower()), None)
        if not token or not given:
            return False
        return hmac.compare_digest(given.strip(), relay_signature(token, body))

    def parse_webhook(self, headers: Mapping[str, str], body: bytes) -> Mapping[str, Any]:  # noqa: ARG002
        form = parse_form(body)
        sid = form.get("CallSid", "")
        status = form.get("Status") or form.get("CallStatus") or ""
        # Content is a hint only: the ingress verifies by fetching the call status (verify-by-fetch).
        return {
            "external_ref": sid,
            "kind": _STATUS.get(status, ""),
            "leg": "agent",
            "provider_status": status,
            "external_event_id": f"{sid}:{status}",
            "verify_by_fetch": True,
        }
