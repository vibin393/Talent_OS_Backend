# ruff: file-ignore[line-too-long]
"""Shared base of the real telephony adapters (Epic 76, spec A2): Exotel, Twilio and Plivo.

Adapters see only ``core.integrations``, ``core.net_safety``, ``core.signing``, the voice DTOs and their own package.
Every HTTP call goes through ``AdapterContext.http`` (the Egress Guard limited to the manifest's ``egress_hosts``);
recording downloads use a sibling client with the same hosts and a 200 MB cap. Phone numbers, tokens and recording
URLs never appear in ``repr``, logs or exception messages.

The frozen ``VoiceProvider`` port is implemented for compatibility only: the P4 scheduling path is not used for real
providers (``schedule_call`` / ``start_call`` raise ``ManualActionRequired`` -- "Use Call").
"""

import base64
import hashlib
import hmac
from collections.abc import Mapping
from typing import Any, ClassVar

from app.core.integrations.context import AdapterContext
from app.core.integrations.errors import (
    ConnectorAuthError,
    ConnectorPermanentError,
    ConnectorRateLimited,
    ConnectorTransientError,
    ManualActionRequired,
)
from app.core.net_safety import SafeHttpClient, SafeResponse
from app.services.connectors.voice.bridge import (
    OP_BRIDGE_CALL,
    OP_CALL_STATUS,
    OP_DELETE_RECORDING,
    OP_HANGUP,
    BridgedCallRequest,
)
from app.services.connectors.voice.port import (
    CallSessionDTO,
    CallSessionRequestDTO,
    RecordingDTO,
    TranscriptDTO,
)

__all__ = ["RECORDING_MAX_BYTES", "USE_CALL", "LiveTelephonyBase", "classify", "relay_signature"]

RECORDING_MAX_BYTES = 200 * 1024 * 1024
USE_CALL = "Use the Call button to place a bridged call"


def classify(resp: SafeResponse, what: str) -> None:
    """Raise the classified connector error for a non-2xx provider response (no body text in messages)."""
    code = resp.status_code
    if 200 <= code < 300:
        return
    if code in (401, 403):
        msg = f"The provider rejected the credentials ({what})"
        raise ConnectorAuthError(msg)
    if code == 429:
        retry = resp.headers.get("retry-after", "30")
        raise ConnectorRateLimited(float(retry) if retry.strip().isdigit() else 30.0)
    if code >= 500:
        msg = f"provider error {code} ({what})"
        raise ConnectorTransientError(msg)
    raise ConnectorPermanentError(f"provider_{code}", f"The provider refused the request ({what}, HTTP {code})")


def relay_signature(secret: str, body: bytes) -> str:
    """HMAC used by the Talent OS Exotel relay (path token verified first; see ``telephony.ingress``)."""
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def basic_auth(user: str | None, password: str | None) -> dict[str, str]:
    if not user or not password:
        msg = "The provider credentials are missing"
        raise ConnectorAuthError(msg)
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


class LiveTelephonyBase:
    category: ClassVar[str] = "voice"
    provider_key: ClassVar[str] = ""
    media_type: ClassVar[str] = "audio/wav"

    def __init__(self, ctx: AdapterContext) -> None:
        self.ctx = ctx
        self.key = type(self).provider_key

    def __repr__(self) -> str:
        return f"{type(self).__name__}(mode={self.ctx.mode})"

    # ------------------------------------------------------------------------------------------ helpers
    def _recording_client(self) -> SafeHttpClient:
        http = self.ctx.http
        return SafeHttpClient(
            allowed_hosts=http.allowed_hosts,
            max_redirects=3,
            max_bytes=RECORDING_MAX_BYTES,
            total_timeout=120.0,
            purpose=http.purpose,
            transport=http._transport,
        )

    def _auth(self) -> dict[str, str]:
        raise NotImplementedError

    # ------------------------------------------------------------------------------------------ ConnectorPort
    async def handle(self, op: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        if op == "health":
            return await self.health()
        if op == OP_BRIDGE_CALL:
            return await self.bridge_call(BridgedCallRequest.from_payload(payload))
        if op == OP_CALL_STATUS:
            return await self.call_status(str(payload["external_ref"]))
        if op == OP_HANGUP:
            return await self.hangup(str(payload["external_ref"]))
        if op == OP_DELETE_RECORDING:
            return await self.delete_recording(str(payload["recording_ref"]))
        raise ConnectorPermanentError("unsupported_op", f"{self.key}: unsupported operation {op}")

    async def health(self) -> Mapping[str, Any]:
        raise NotImplementedError

    async def bridge_call(self, req: BridgedCallRequest) -> Mapping[str, Any]:
        raise NotImplementedError

    async def call_status(self, external_ref: str) -> Mapping[str, Any]:
        raise NotImplementedError

    async def hangup(self, external_ref: str) -> Mapping[str, Any]:
        raise NotImplementedError

    async def delete_recording(self, recording_ref: str) -> Mapping[str, Any]:
        raise NotImplementedError

    def verify_webhook(self, headers: Mapping[str, str], body: bytes) -> bool:  # ruff: ignore[unused-method-argument]
        return False  # verified by the ingress through the manifest WebhookSpec

    def parse_webhook(self, headers: Mapping[str, str], body: bytes) -> Mapping[str, Any]:
        raise NotImplementedError

    # ------------------------------------------------------------------------------------------ voice port
    async def schedule_call(self, req: CallSessionRequestDTO) -> CallSessionDTO:
        raise ManualActionRequired(USE_CALL, {"op": "schedule_call", "voice_session_id": str(req.voice_session_id)})

    async def start_call(self, external_ref: str) -> CallSessionDTO:  # ruff: ignore[unused-method-argument]
        raise ManualActionRequired(USE_CALL, {"op": "start_call"})

    async def end_call(self, external_ref: str) -> CallSessionDTO:
        await self.hangup(external_ref)
        return CallSessionDTO(external_ref=external_ref, status="ended")

    async def fetch_recording(self, recording_ref: str) -> RecordingDTO | None:
        raise NotImplementedError

    async def fetch_transcript(self, transcript_ref: str) -> TranscriptDTO | None:  # ruff: ignore[unused-method-argument]  # NOSONAR
        return None  # transcription is the ``stt`` category's (Epic 76 A5)

    async def _download(self, url: str) -> RecordingDTO:
        # not closed: the transport is shared with ``ctx.http``
        resp = await self._recording_client().get(url, headers=self._auth())
        classify(resp, "fetch recording")
        ctype = (resp.headers.get("content-type") or self.media_type).split(";")[0].strip() or self.media_type
        return RecordingDTO(data=resp.content, media_type=ctype, duration_s=0)
