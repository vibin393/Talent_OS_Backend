# ruff: file-ignore[line-too-long]
"""Twilio telephony adapter (Epic 76, spec A2; key ``twilio``).

* Bridged call: ``POST /2010-04-01/Accounts/{sid}/Calls.json`` rings the recruiter (``To``) from the caller ID
  (``From``); the answer URL returns TwiML (``telephony.twiml``) that speaks the notice (only when recording) and
  ``<Dial record="record-from-answer-dual">`` the Candidate. ``StatusCallback`` = the connector ingress URL.
* Callbacks: HMAC-SHA1 over the URL + sorted form params (``X-Twilio-Signature``, signed with the auth token).
* Recordings: ``GET .../Recordings/{sid}.wav``; ``DELETE .../Recordings/{sid}.json``.
Destination of personal data: US (cross-border; stated in the install notice).
"""

from collections.abc import Mapping
from datetime import datetime
from email.utils import parsedate_to_datetime
from typing import Any, Literal
from urllib.parse import parse_qsl

from pydantic import BaseModel, ConfigDict

from app.core.integrations.errors import ConnectorPermanentError
from app.services.connectors.voice.bridge import BridgedCallRequest, CallStatusDTO, to_minor
from app.services.connectors.voice.live_base import LiveTelephonyBase, basic_auth, classify
from app.services.connectors.voice.port import RecordingDTO

__all__ = ["API_HOST", "TwilioConfig", "TwilioTelephony", "parse_form"]

API_HOST = "api.twilio.com"
_PARENT = {
    "ringing": "ringing",
    "in-progress": "answered",
    "answered": "answered",
    "completed": "completed",
    "busy": "busy",
    "no-answer": "no_answer",
    "failed": "failed",
    "canceled": "failed",
}
_CHILD = {"in-progress": "candidate_answered", "answered": "candidate_answered", "busy": "busy", "no-answer": "no_answer", "failed": "failed"}


class TwilioConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    region: Literal["us1"] = "us1"


def parse_form(body: bytes) -> dict[str, str]:
    try:
        return dict(parse_qsl(body.decode("utf-8"), keep_blank_values=True))
    except UnicodeDecodeError:
        return {}


def _dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None


def _int(value: Any) -> int | None:
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


class TwilioTelephony(LiveTelephonyBase):
    provider_key = "twilio"

    @property
    def _sid(self) -> str:
        sid = self.ctx.secrets.get("account_sid")
        if not sid:
            raise ConnectorPermanentError("credentials_missing", "The Twilio account SID is missing")
        return sid

    def _auth(self) -> dict[str, str]:
        return basic_auth(self.ctx.secrets.get("account_sid"), self.ctx.secrets.get("auth_token"))

    def _url(self, path: str) -> str:
        return f"https://{API_HOST}/2010-04-01/Accounts/{self._sid}{path}"

    async def health(self) -> Mapping[str, Any]:
        resp = await self.ctx.http.get(self._url(".json"), headers=self._auth())
        classify(resp, "account")
        return {"ok": True}

    async def bridge_call(self, req: BridgedCallRequest) -> Mapping[str, Any]:
        data: dict[str, Any] = {
            "To": req.agent_e164,
            "From": req.caller_id_e164,
            "Url": req.answer_url or "",
            "Method": "POST",
            "StatusCallback": req.status_callback_url,
            "StatusCallbackMethod": "POST",
            "StatusCallbackEvent": ["initiated", "ringing", "answered", "completed"],
            "TimeLimit": str(req.max_duration_s),
        }
        resp = await self.ctx.http.post(self._url("/Calls.json"), headers=self._auth(), data=data)
        classify(resp, "place call")
        sid = str(resp.json().get("sid") or "")
        if not sid:
            return {"external_ref": "", "status": "failed", "failure_code": "provider_no_reference"}
        return {"external_ref": sid, "status": "dialing_recruiter"}

    async def call_status(self, external_ref: str) -> Mapping[str, Any]:
        resp = await self.ctx.http.get(self._url(f"/Calls/{external_ref}.json"), headers=self._auth())
        classify(resp, "call status")
        body = resp.json()
        status = _PARENT.get(str(body.get("status") or ""), str(body.get("status") or ""))
        return CallStatusDTO(
            external_ref=external_ref,
            status=status,
            answered_at=_dt(body.get("start_time")),
            ended_at=_dt(body.get("end_time")),
            duration_s=_int(body.get("duration")),
            price_minor=to_minor(body.get("price")),
            currency=(body.get("price_unit") or None),
            recording_ref=None,
            recording_channels=None,
        ).to_mapping()

    async def hangup(self, external_ref: str) -> Mapping[str, Any]:
        resp = await self.ctx.http.post(
            self._url(f"/Calls/{external_ref}.json"), headers=self._auth(), data={"Status": "completed"}
        )
        classify(resp, "hang up")
        return {"ok": True}

    async def delete_recording(self, recording_ref: str) -> Mapping[str, Any]:
        resp = await self.ctx.http.request("DELETE", self._url(f"/Recordings/{recording_ref}.json"), headers=self._auth())
        if resp.status_code == 404:
            return {"status": "deleted"}  # already gone
        classify(resp, "delete recording")
        return {"status": "deleted"}

    async def fetch_recording(self, recording_ref: str) -> RecordingDTO | None:
        return await self._download(self._url(f"/Recordings/{recording_ref}.wav"))

    def parse_webhook(self, headers: Mapping[str, str], body: bytes) -> Mapping[str, Any]:  # ruff: ignore[unused-method-argument]
        form = parse_form(body)
        call_sid = form.get("CallSid", "")
        if form.get("RecordingSid"):
            if form.get("RecordingStatus", "completed") != "completed":
                return {"external_ref": call_sid, "kind": "", "external_event_id": f"rec:{form['RecordingSid']}:{form.get('RecordingStatus')}"}
            return {
                "external_ref": call_sid,
                "kind": "recording_ready",
                "leg": "agent",
                "provider_status": "recording-completed",
                "external_event_id": f"rec:{form['RecordingSid']}",
                "recording_ref": form["RecordingSid"],
                "recording_channels": _int(form.get("RecordingChannels")),
                "duration_s": _int(form.get("RecordingDuration")),
            }
        status = form.get("CallStatus", "")
        parent = form.get("ParentCallSid")
        seq = form.get("SequenceNumber", "")
        if parent:
            return {
                "external_ref": parent,
                "kind": _CHILD.get(status, ""),
                "leg": "candidate",
                "provider_status": status,
                "external_event_id": f"{call_sid}:{status}:{seq}",
            }
        out: dict[str, Any] = {
            "external_ref": call_sid,
            "kind": _PARENT.get(status, ""),
            "leg": "agent",
            "provider_status": status,
            "external_event_id": f"{call_sid}:{status}:{seq}",
        }
        if status == "completed":
            out["duration_s"] = _int(form.get("CallDuration"))
        return out
