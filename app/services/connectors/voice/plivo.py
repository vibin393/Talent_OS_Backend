# ruff: file-ignore[line-too-long]
"""Plivo telephony adapter (Epic 76, spec A2; key ``plivo``).

* Bridged call: ``POST /v1/Account/{auth_id}/Call/`` rings the recruiter (``to``) from the caller ID (``from``); the
  answer URL returns Plivo XML (``telephony.twiml``): ``<Speak>`` notice + ``<Record>`` (only when recording) and
  ``<Dial callerId><Number>`` the Candidate. ``hangup_url`` / ``ring_url`` = the connector ingress URL.
* Callbacks: Plivo signature V3 (``X-Plivo-Signature-V3`` + nonce, signed with the auth token).
* Recordings: ``GET https://media.plivo.com/v1/Account/{id}/Recording/{rid}.wav``; ``DELETE /v1/Account/{id}/Recording/{rid}/``.
Destination of personal data: the account's region (US or SG).
"""

from collections.abc import Mapping
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from app.core.integrations.errors import ConnectorPermanentError
from app.services.connectors.voice.bridge import BridgedCallRequest, CallStatusDTO, to_minor
from app.services.connectors.voice.live_base import LiveTelephonyBase, basic_auth, classify
from app.services.connectors.voice.port import RecordingDTO
from app.services.connectors.voice.twilio import parse_form

__all__ = ["API_HOST", "MEDIA_HOST", "PlivoConfig", "PlivoTelephony"]

API_HOST = "api.plivo.com"
MEDIA_HOST = "media.plivo.com"
_HANGUP = {"completed": "completed", "busy": "busy", "no-answer": "no_answer", "timeout": "no_answer", "failed": "failed", "cancel": "failed"}


class PlivoConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    data_region: Literal["US", "SG"] = "US"


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


class PlivoTelephony(LiveTelephonyBase):
    provider_key = "plivo"

    @property
    def _id(self) -> str:
        auth_id = self.ctx.secrets.get("auth_id")
        if not auth_id:
            raise ConnectorPermanentError("credentials_missing", "The Plivo auth ID is missing")
        return auth_id

    def _auth(self) -> dict[str, str]:
        return basic_auth(self.ctx.secrets.get("auth_id"), self.ctx.secrets.get("auth_token"))

    def _url(self, path: str) -> str:
        return f"https://{API_HOST}/v1/Account/{self._id}{path}"

    async def health(self) -> Mapping[str, Any]:
        resp = await self.ctx.http.get(self._url("/"), headers=self._auth())
        classify(resp, "account")
        return {"ok": True}

    async def bridge_call(self, req: BridgedCallRequest) -> Mapping[str, Any]:
        body = {
            "from": req.caller_id_e164.lstrip("+"),
            "to": req.agent_e164.lstrip("+"),
            "answer_url": req.answer_url or "",
            "answer_method": "POST",
            "hangup_url": req.status_callback_url,
            "hangup_method": "POST",
            "ring_url": req.status_callback_url,
            "ring_method": "POST",
            "time_limit": req.max_duration_s,
        }
        resp = await self.ctx.http.post(self._url("/Call/"), headers=self._auth(), json=body)
        classify(resp, "place call")
        ref = str(resp.json().get("request_uuid") or "")
        if not ref:
            return {"external_ref": "", "status": "failed", "failure_code": "provider_no_reference"}
        return {"external_ref": ref, "status": "dialing_recruiter"}

    async def call_status(self, external_ref: str) -> Mapping[str, Any]:
        resp = await self.ctx.http.get(self._url(f"/Call/{external_ref}/"), headers=self._auth())
        classify(resp, "call status")
        body = resp.json()
        state = str(body.get("call_state") or body.get("hangup_cause_name") or "")
        status = {"ANSWER": "answered", "RINGING": "ringing", "EARLY MEDIA": "ringing"}.get(state.upper(), "")
        if body.get("end_time"):
            status = "completed" if _int(body.get("bill_duration")) else "no_answer"
        return CallStatusDTO(
            external_ref=external_ref,
            status=status,
            answered_at=_dt(body.get("answer_time")),
            ended_at=_dt(body.get("end_time")),
            duration_s=_int(body.get("bill_duration") or body.get("call_duration")),
            price_minor=to_minor(body.get("total_amount")),
            currency="USD" if body.get("total_amount") is not None else None,
            recording_ref=None,
            recording_channels=None,
        ).to_mapping()

    async def hangup(self, external_ref: str) -> Mapping[str, Any]:
        resp = await self.ctx.http.request("DELETE", self._url(f"/Request/{external_ref}/"), headers=self._auth())
        if resp.status_code != 404:
            classify(resp, "hang up")
        return {"ok": True}

    async def delete_recording(self, recording_ref: str) -> Mapping[str, Any]:
        resp = await self.ctx.http.request("DELETE", self._url(f"/Recording/{recording_ref}/"), headers=self._auth())
        if resp.status_code == 404:
            return {"status": "deleted"}
        classify(resp, "delete recording")
        return {"status": "deleted"}

    async def fetch_recording(self, recording_ref: str) -> RecordingDTO | None:
        return await self._download(f"https://{MEDIA_HOST}/v1/Account/{self._id}/Recording/{recording_ref}.wav")

    def parse_webhook(self, headers: Mapping[str, str], body: bytes) -> Mapping[str, Any]:  # ruff: ignore[unused-method-argument]
        form = parse_form(body)
        ref = form.get("RequestUUID") or form.get("CallUUID", "")
        if form.get("RecordingID"):
            return {
                "external_ref": ref,
                "kind": "recording_ready",
                "leg": "agent",
                "provider_status": "record",
                "external_event_id": f"rec:{form['RecordingID']}",
                "recording_ref": form["RecordingID"],
                "recording_channels": 2,
                "duration_s": _int(form.get("RecordingDuration")),
            }
        leg_b = form.get("DialBLegStatus", "")
        if leg_b:
            kind = {"answer": "candidate_answered"}.get(leg_b, "")
            return {"external_ref": ref, "kind": kind, "leg": "candidate", "provider_status": leg_b, "external_event_id": f"{ref}:b:{leg_b}"}
        event = form.get("Event", "")
        status = form.get("CallStatus", "")
        out: dict[str, Any] = {"external_ref": ref, "leg": "agent", "provider_status": status or event}
        if event == "Ring" or status == "ringing":
            out["kind"] = "ringing"
        elif event == "StartApp" or status == "in-progress":
            out["kind"] = "answered"
        elif event == "Hangup" or status in _HANGUP:
            out["kind"] = _HANGUP.get(status, "completed")
            out["duration_s"] = _int(form.get("BillDuration") or form.get("Duration"))
            out["price_minor"] = to_minor(form.get("TotalCost"))
        else:
            out["kind"] = ""
        out["external_event_id"] = f"{ref}:{event or status}"
        return out
