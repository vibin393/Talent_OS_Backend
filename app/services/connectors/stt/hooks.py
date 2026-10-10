"""Registers the ``fake_stt`` manifest (Epic 66; Fake + Manual adapters). Epic 76 registers the real STT providers."""

from app.core.integrations import ConnectorManifest, DataFlow, SecretField, register_connector
from app.services.connectors.stt.fake import FakeSpeechToText
from app.services.connectors.stt.manual import ManualSpeechToText

FAKE_STT = ConnectorManifest(
    key="fake_stt",
    version=1,
    owner_epic="66",
    name="Fake speech-to-text",
    summary="A deterministic transcription provider for tests, demos and Sandbox Tenants (no network).",
    category="stt",
    capabilities=frozenset({"stt.transcribe"}),
    auth="api_key",
    secret_fields=(SecretField("api_key", "API key", "api_key"),),
    data_flows=(
        DataFlow("out", "recording", ("audio",), "transcription", "IN", True),
        DataFlow("in", "transcript", ("segments",), "transcription", "IN", True),
    ),
    adapters={"fake": FakeSpeechToText, "manual": ManualSpeechToText},
)

register_connector(FAKE_STT)
