# ruff: file-ignore[line-too-long]
"""The ``azure_speech`` Connector Manifest (Epic 76, spec A5; D-6). Registered by ``app.services.telephony.hooks``.
Fake = the foundation ``FakeSpeechToText``; Manual = the foundation ``ManualSpeechToText`` (upload a transcript)."""

from app.core.integrations import ConnectorManifest, DataFlow, SecretField
from app.services.connectors.stt.azure_speech import EGRESS_HOSTS, AzureSpeechConfig, AzureSpeechToText
from app.services.connectors.stt.fake import FakeSpeechToText
from app.services.connectors.stt.manual import ManualSpeechToText

__all__ = ["AZURE_SPEECH"]

AZURE_SPEECH = ConnectorManifest(
    key="azure_speech",
    version=1,
    owner_epic="76",
    name="Azure AI Speech (Central India)",
    summary="Transcribes screening-call recordings in English (India) and Hindi; audio is processed in Central India.",
    category="stt",
    capabilities=frozenset({"stt.transcribe"}),
    auth="api_key",
    config_model=AzureSpeechConfig,
    secret_fields=(SecretField("subscription_key", "Speech resource key", "api_key"),),
    data_flows=(
        DataFlow("out", "call.recording", ("audio",), "Transcribe the screening call", "IN", True),
        DataFlow("in", "transcript", ("segments",), "Transcribe the screening call", "IN", True),
    ),
    egress_hosts=EGRESS_HOSTS,
    adapters={"fake": FakeSpeechToText, "live": AzureSpeechToText, "manual": ManualSpeechToText},
    rate_per_minute=60,
    docs_url="https://learn.microsoft.com/azure/ai-services/speech-service/fast-transcription-create",
)
