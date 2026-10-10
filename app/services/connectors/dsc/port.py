"""``dsc`` (digital signature certificate) signing port (AD-29, Epic 36 contract 0.8; owned by Epic 51).

``manual`` asks a person to upload the externally signed PDF (verification is Epic 51's pure verifier);
``fake`` serves tests. Every adapter also implements the generic ``ConnectorPort``. Signatures are frozen.
"""

import dataclasses
import uuid
from typing import Protocol, runtime_checkable

__all__ = ["DscSignRequestDTO", "DscSignResultDTO", "DscSigner"]


@dataclasses.dataclass(frozen=True, slots=True)
class DscSignRequestDTO:
    tenant_id: uuid.UUID
    file_id: uuid.UUID
    pdf: bytes
    sha256: str
    signer_ref: str
    reason: str
    location: str


@dataclasses.dataclass(frozen=True, slots=True)
class DscSignResultDTO:
    status: str  # signed | pending_manual
    signed_pdf: bytes | None
    external_ref: str | None


@runtime_checkable
class DscSigner(Protocol):
    key: str

    async def sign_pdf(self, req: DscSignRequestDTO) -> DscSignResultDTO: ...
