"""Sensitive identity and bank data at rest (AD-35).

* ``EncryptedStr`` column type: stores a Fernet token; binds only ``Sealed`` values (a plain ``str`` raises
  ``TypeError`` so plaintext can never be written by accident) and loads ``Sealed``.
* ``Sealed`` never reveals its token: ``str`` / ``repr`` print ``Sealed(****)`` and pydantic serialisation
  raises, so a sealed value cannot leak into logs, API responses or events.
* Keys: per-tenant keys are derived with HKDF-SHA256 from the ``IDENTITY_ENCRYPTION_KEY`` master key
  (``info=identity:<tenant>``); the blind index uses ``info=identity-bidx:<tenant>``. The dev default is the
  deterministic ``dev_fernet_key("identity")``; production refuses an empty key (settings validation).
* ``seal`` returns ``(sealed, last4, bidx)``; ``set_identity_field`` sets ``<field>``, ``<field>_last4`` and
  ``<field>_bidx`` on an ORM object; ``unmask`` decrypts for a principal holding ``identity:view`` and audits
  ``identity_unmasked``.
* Epic 36 (spec 0.10): ``tenant_fernet(tenant_id, purpose)`` derives the per-tenant Fernet for ``identity`` or
  ``demographic`` (master ``DEMOGRAPHIC_ENCRYPTION_KEY``, ``info=<purpose>:<tenant>``; identity keys are
  unchanged). ``EncryptedJSON`` stores a sealed JSON document (binds / loads ``Sealed`` only); ``seal_json`` /
  ``open_json`` encrypt and decrypt it. Used only by Epic 49 (self-ID).
"""

import base64
import functools
import hashlib
import hmac
import json
import re
import uuid
from typing import TYPE_CHECKING, Any, Literal

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from pydantic_core import core_schema
from sqlalchemy import Text
from sqlalchemy.types import TypeDecorator

from app.core.errors import Forbidden
from app.core.security import key_material

if TYPE_CHECKING:
    from sqlalchemy.engine import Dialect
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.core.context import RequestContext

IDENTITY_VIEW = "identity:view"
_NORMALISE = re.compile(r"[\s\-./]")


class Sealed:
    """A Fernet token of an identity value. Never printed, never serialised."""

    __slots__ = ("token",)

    def __init__(self, token: str) -> None:
        if not isinstance(token, str) or not token:
            msg = "Sealed needs a non-empty token"
            raise TypeError(msg)
        self.token = token

    def __repr__(self) -> str:
        return "Sealed(****)"

    __str__ = __repr__

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Sealed) and hmac.compare_digest(self.token, other.token)

    def __hash__(self) -> int:
        return hash(self.token)

    @classmethod
    def __get_pydantic_core_schema__(cls, source: Any, handler: Any) -> core_schema.CoreSchema:
        def _refuse(value: Any) -> Any:
            msg = "sealed identity values are never serialised"
            raise TypeError(msg)

        return core_schema.is_instance_schema(
            cls, serialization=core_schema.plain_serializer_function_ser_schema(_refuse)
        )


class EncryptedStr(TypeDecorator[Sealed]):
    """``TEXT`` column holding a Fernet token; binds/loads ``Sealed`` only."""

    impl = Text
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: "Dialect") -> str | None:  # ruff: ignore[unused-method-argument]
        if value is None:
            return None
        if not isinstance(value, Sealed):
            msg = "EncryptedStr columns accept Sealed values only (use core.encrypted.seal)"
            raise TypeError(msg)
        return value.token

    def process_result_value(self, value: Any, dialect: "Dialect") -> Sealed | None:  # ruff: ignore[unused-method-argument]
        return None if value is None else Sealed(str(value))


# Epic 66 (spec 0.3): ``vault`` is the vault tenant data key (``vault_service.seal_field``), not a Fernet purpose.
TenantKeyPurpose = Literal["identity", "demographic", "agent_payload", "vault"]
_TENANT_PURPOSES: frozenset[str] = frozenset({"identity", "demographic", "agent_payload"})


class EncryptedJSON(TypeDecorator[Sealed]):
    """``TEXT`` column holding a Fernet token of a JSON document; binds/loads ``Sealed`` only
    (``seal_json`` / ``open_json``)."""

    impl = Text
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: "Dialect") -> str | None:  # ruff: ignore[unused-method-argument]
        if value is None:
            return None
        if not isinstance(value, Sealed):
            msg = "EncryptedJSON columns accept Sealed values only (use core.encrypted.seal_json)"
            raise TypeError(msg)
        return value.token

    def process_result_value(self, value: Any, dialect: "Dialect") -> Sealed | None:  # ruff: ignore[unused-method-argument]
        return None if value is None else Sealed(str(value))


def _master(purpose: TenantKeyPurpose = "identity") -> bytes:
    return base64.urlsafe_b64decode(key_material(purpose).encode("ascii"))


@functools.cache
def _derive(master: bytes, info: str) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=info.encode("utf-8")).derive(master)


def tenant_fernet(tenant_id: uuid.UUID, purpose: TenantKeyPurpose) -> Fernet:
    """Per-tenant Fernet: HKDF-SHA256 over the ``purpose`` master key with ``info=<purpose>:<tenant_id>``."""
    if purpose not in _TENANT_PURPOSES:
        msg = f"unknown tenant key purpose {purpose!r}"
        raise ValueError(msg)
    key = _derive(_master(purpose), f"{purpose}:{uuid.UUID(str(tenant_id))}")
    return Fernet(base64.urlsafe_b64encode(key))


def _tenant_fernet(tenant_id: uuid.UUID) -> Fernet:
    return tenant_fernet(tenant_id, "identity")


def seal_json(tenant_id: uuid.UUID, purpose: TenantKeyPurpose, value: Any) -> Sealed:
    """Encrypt a JSON-serialisable ``value`` with the tenant's ``purpose`` key (for ``EncryptedJSON``)."""
    raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return Sealed(tenant_fernet(tenant_id, purpose).encrypt(raw).decode("ascii"))


def open_json(tenant_id: uuid.UUID, purpose: TenantKeyPurpose, sealed: Sealed) -> Any:
    """Decrypt a sealed JSON document (service paths only; aggregate-only readers for ``demographic``)."""
    if not isinstance(sealed, Sealed):
        msg = "open_json needs a Sealed value"
        raise TypeError(msg)
    return json.loads(tenant_fernet(tenant_id, purpose).decrypt(sealed.token.encode("ascii")).decode("utf-8"))


def normalise(plaintext: str) -> str:
    """Canonical form used for the blind index and ``last4`` (spaces / separators removed, upper case)."""
    return _NORMALISE.sub("", plaintext or "").upper()


def blind_index(tenant_id: uuid.UUID, plaintext: str) -> str:
    """Deterministic per-tenant HMAC-SHA256 of the normalised value (first 32 hex chars)."""
    key = _derive(_master(), f"identity-bidx:{uuid.UUID(str(tenant_id))}")
    return hmac.new(key, normalise(plaintext).encode("utf-8"), hashlib.sha256).hexdigest()[:32]


def seal(tenant_id: uuid.UUID, plaintext: str) -> tuple[Sealed, str, str]:
    """Encrypt ``plaintext`` with the tenant key; returns ``(sealed, last4, bidx)``."""
    if not isinstance(plaintext, str) or not plaintext.strip():
        msg = "seal needs a non-empty string"
        raise ValueError(msg)
    token = _tenant_fernet(tenant_id).encrypt(plaintext.strip().encode("utf-8")).decode("ascii")
    return Sealed(token), normalise(plaintext)[-4:], blind_index(tenant_id, plaintext)


def open_sealed(tenant_id: uuid.UUID, sealed: Sealed) -> str:
    """Decrypt (service paths only; callers enforce ``identity:view`` or a documented system read)."""
    return _tenant_fernet(tenant_id).decrypt(sealed.token.encode("ascii")).decode("utf-8")


def set_identity_field(obj: Any, field: str, plaintext: str | None, tenant_id: uuid.UUID) -> None:
    """Set ``field`` (sealed), ``field_last4`` and ``field_bidx`` (when the model has it) on ``obj``."""
    if plaintext is None or not str(plaintext).strip():
        setattr(obj, field, None)
        setattr(obj, f"{field}_last4", None)
        if hasattr(type(obj), f"{field}_bidx"):
            setattr(obj, f"{field}_bidx", None)
        return
    sealed, last4, bidx = seal(tenant_id, plaintext)
    setattr(obj, field, sealed)
    setattr(obj, f"{field}_last4", last4)
    if hasattr(type(obj), f"{field}_bidx"):
        setattr(obj, f"{field}_bidx", bidx)


async def unmask(ctx: "RequestContext", db: "AsyncSession", obj: Any, field: str) -> str:
    """Decrypt ``obj.<field>`` for a principal holding ``identity:view``; audited ``identity_unmasked``."""
    from app.core import audit

    principal = ctx.principal
    has = getattr(principal, "has", None)
    if not callable(has) or not has(IDENTITY_VIEW):
        raise Forbidden(
            "You do not have permission to view this value", details={"permission": IDENTITY_VIEW}
        )
    sealed = getattr(obj, field)
    if sealed is None:
        msg = f"{field} is empty"
        raise ValueError(msg)
    value = open_sealed(obj.tenant_id, sealed)
    audit.record(
        "identity_unmasked",
        audit.entity_type_of(type(obj)),
        getattr(obj, "id", None),
        session=db,
        field=field,
    )
    return value
