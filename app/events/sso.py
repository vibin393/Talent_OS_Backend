"""Single sign-on events (Epic 14, FR-77, AD-30): frozen, slotted, ids only; dispatched after commit."""

import dataclasses
import uuid

__all__ = [
    "ClientIdentityProviderChanged",
    "ClientUserJitProvisioned",
    "ClientUserLinkedToIdentity",
    "IdentityProviderChanged",
    "UserJitProvisioned",
    "UserLinkedToIdentity",
]


@dataclasses.dataclass(frozen=True, slots=True)
class IdentityProviderChanged:
    tenant_id: uuid.UUID
    idp_id: uuid.UUID
    change: str  # created | updated | activated | disabled | deleted


@dataclasses.dataclass(frozen=True, slots=True)
class UserLinkedToIdentity:
    tenant_id: uuid.UUID
    user_id: uuid.UUID
    idp_id: uuid.UUID
    method: str  # verified_email | jit


@dataclasses.dataclass(frozen=True, slots=True)
class UserJitProvisioned:
    tenant_id: uuid.UUID
    user_id: uuid.UUID
    idp_id: uuid.UUID


# -------------------------------------------------------- Epic 50 (Client Portal SSO, AD-48): domain-local
@dataclasses.dataclass(frozen=True, slots=True)
class ClientIdentityProviderChanged:
    """A client IdP or the Client's SSO enforcement changed (``IdentityProviderChanged`` stays frozen)."""

    tenant_id: uuid.UUID
    idp_id: uuid.UUID | None  # None for settings / exemption changes
    client_id: uuid.UUID
    # created | updated | activated | disabled | deleted | enforcement_changed | exemption_added | ...
    change: str


@dataclasses.dataclass(frozen=True, slots=True)
class ClientUserLinkedToIdentity:
    tenant_id: uuid.UUID
    client_user_id: uuid.UUID
    client_id: uuid.UUID
    idp_id: uuid.UUID
    method: str  # verified_email | jit


@dataclasses.dataclass(frozen=True, slots=True)
class ClientUserJitProvisioned:
    tenant_id: uuid.UUID
    client_user_id: uuid.UUID
    client_id: uuid.UUID
    idp_id: uuid.UUID
