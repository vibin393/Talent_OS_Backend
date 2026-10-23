# ruff: file-ignore[line-too-long]
"""Connector Manifests of the real telephony providers (Epic 76, spec A2): Exotel, Twilio and Plivo. Registered by
``app.services.telephony.hooks`` (``register_connector``); listed in the marketplace by Epic 69.

Each manifest: category ``voice``, capabilities ``voice.call`` + ``voice.recording``, basic auth with the provider's
credentials (the field that signs callbacks has kind ``webhook_secret``: Twilio / Plivo sign with the auth token,
Exotel uses the relay path token), data flows for the install notice, egress hosts, webhook verification and the
adapters per mode (``fake`` = ``FakeBridgedVoiceProvider``, ``live`` = the real adapter, ``manual`` = the P4 Manual).
"""

from app.core.integrations import ConnectorManifest, DataFlow, SecretField, WebhookSpec
from app.services.connectors.voice.exotel import EGRESS_HOSTS as EXOTEL_HOSTS
from app.services.connectors.voice.exotel import ExotelConfig, ExotelTelephony
from app.services.connectors.voice.fake import FakeBridgedVoiceProvider
from app.services.connectors.voice.manual import ManualVoiceProvider
from app.services.connectors.voice.plivo import API_HOST as PLIVO_API
from app.services.connectors.voice.plivo import MEDIA_HOST as PLIVO_MEDIA
from app.services.connectors.voice.plivo import PlivoConfig, PlivoTelephony
from app.services.connectors.voice.twilio import API_HOST as TWILIO_API
from app.services.connectors.voice.twilio import TwilioConfig, TwilioTelephony

__all__ = ["EXOTEL", "PLIVO", "TELEPHONY_MANIFESTS", "TWILIO"]

_CAPS = frozenset({"voice.call", "voice.recording"})
_PURPOSE_CALL = "Place the screening call"
_PURPOSE_REC = "Retrieve the call recording"


def _flows(country: str) -> tuple[DataFlow, ...]:
    return (
        DataFlow("out", "candidate.phone", ("phone_e164",), _PURPOSE_CALL, country, True),
        DataFlow("out", "user.call_phone", ("phone_e164",), _PURPOSE_CALL, country, True),
        DataFlow("in", "call.recording", ("audio",), _PURPOSE_REC, country, True),
    )


def _adapters(live: type) -> dict:  # type: ignore[type-arg]
    return {"fake": FakeBridgedVoiceProvider, "live": live, "manual": ManualVoiceProvider}


EXOTEL = ConnectorManifest(
    key="exotel",
    version=1,
    owner_epic="76",
    name="Exotel",
    summary="Recruiter-initiated bridged screening calls from your ExoPhone (India data centre).",
    category="voice",
    capabilities=_CAPS,
    auth="basic",
    config_model=ExotelConfig,
    secret_fields=(
        SecretField("api_key", "API key", "api_key"),
        SecretField("api_token", "API token", "password"),
        SecretField("webhook_path_token", "Callback path token", "webhook_secret"),
    ),
    data_flows=_flows("IN"),
    egress_hosts=EXOTEL_HOSTS,
    webhook=WebhookSpec(scheme="custom", signature_header="X-TalentOS-Relay", event_id_path="/external_event_id"),
    adapters=_adapters(ExotelTelephony),
    legal_entity_scoped=True,
    rate_per_minute=30,
    docs_url="https://developer.exotel.com/api/make-a-call-api",
)

TWILIO = ConnectorManifest(
    key="twilio",
    version=1,
    owner_epic="76",
    name="Twilio",
    summary="Recruiter-initiated bridged screening calls through Twilio Programmable Voice (data processed in the US).",
    category="voice",
    capabilities=_CAPS,
    auth="basic",
    config_model=TwilioConfig,
    secret_fields=(
        SecretField("account_sid", "Account SID", "api_key"),
        SecretField("auth_token", "Auth token", "webhook_secret"),
    ),
    data_flows=_flows("US"),
    egress_hosts=(TWILIO_API,),
    webhook=WebhookSpec(scheme="hmac_sha1_url", signature_header="X-Twilio-Signature", event_id_path="/external_event_id"),
    adapters=_adapters(TwilioTelephony),
    legal_entity_scoped=True,
    rate_per_minute=30,
    docs_url="https://www.twilio.com/docs/voice/api/call-resource",
)

PLIVO = ConnectorManifest(
    key="plivo",
    version=1,
    owner_epic="76",
    name="Plivo",
    summary="Recruiter-initiated bridged screening calls through Plivo (data processed in the US or Singapore).",
    category="voice",
    capabilities=_CAPS,
    auth="basic",
    config_model=PlivoConfig,
    secret_fields=(
        SecretField("auth_id", "Auth ID", "api_key"),
        SecretField("auth_token", "Auth token", "webhook_secret"),
    ),
    data_flows=_flows("US"),
    egress_hosts=(PLIVO_API, PLIVO_MEDIA),
    webhook=WebhookSpec(
        scheme="plivo_v3",
        signature_header="X-Plivo-Signature-V3",
        timestamp_header="X-Plivo-Signature-V3-Nonce",
        event_id_path="/external_event_id",
    ),
    adapters=_adapters(PlivoTelephony),
    legal_entity_scoped=True,
    rate_per_minute=30,
    docs_url="https://www.plivo.com/docs/voice/api/call",
)

TELEPHONY_MANIFESTS = (EXOTEL, TWILIO, PLIVO)
