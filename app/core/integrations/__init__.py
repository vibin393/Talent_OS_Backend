"""Connector Manifest and SDK contracts (Epic 66, spec 0.9, AD-81). Imports no service.

Adapters see only this package, ``core.net_safety``, ``core.signing``, their DTOs and their own package.
"""

from app.core.integrations.capabilities import CAPABILITIES, CAPABILITY_METHODS, PERSONAL_DATA_CAPABILITIES
from app.core.integrations.context import (
    AdapterContext,
    AdapterFactory,
    MappingSecretBag,
    SecretBag,
    connector_http,
    make_context,
    resolve_mode,
)
from app.core.integrations.diff import ManifestDiff, manifest_diff
from app.core.integrations.errors import (
    ConnectorAuthError,
    ConnectorError,
    ConnectorPermanentError,
    ConnectorRateLimited,
    ConnectorTransientError,
    ManifestError,
    ManualActionRequired,
)
from app.core.integrations.manifest import (
    AdapterMode,
    AuthType,
    ConnectorManifest,
    DataFlow,
    EmptyConfig,
    OAuthProviderSpec,
    SecretField,
    SyncJobSpec,
    WebhookSpec,
    manifest_sha256,
)
from app.core.integrations.registry import (
    BridgedFactory,
    find_manifest,
    is_sandbox_safe,
    manifest,
    manifests,
    on_register,
    register_connector,
    validate_catalogue,
    validate_manifest,
)

__all__ = [
    "CAPABILITIES",
    "CAPABILITY_METHODS",
    "PERSONAL_DATA_CAPABILITIES",
    "AdapterContext",
    "AdapterFactory",
    "AdapterMode",
    "AuthType",
    "BridgedFactory",
    "ConnectorAuthError",
    "ConnectorError",
    "ConnectorManifest",
    "ConnectorPermanentError",
    "ConnectorRateLimited",
    "ConnectorTransientError",
    "DataFlow",
    "EmptyConfig",
    "ManifestDiff",
    "ManifestError",
    "ManualActionRequired",
    "MappingSecretBag",
    "OAuthProviderSpec",
    "SecretBag",
    "SecretField",
    "SyncJobSpec",
    "WebhookSpec",
    "connector_http",
    "find_manifest",
    "is_sandbox_safe",
    "make_context",
    "manifest",
    "manifest_diff",
    "manifest_sha256",
    "manifests",
    "on_register",
    "register_connector",
    "resolve_mode",
    "validate_catalogue",
    "validate_manifest",
]
