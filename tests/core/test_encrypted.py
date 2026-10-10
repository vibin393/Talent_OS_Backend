"""AD-35: sealed identity values never serialise, blind indexes are per tenant, unmask is permissioned and audited
(Epic 13)."""

import uuid

import pytest
from pydantic import BaseModel
from sqlalchemy import select

from app.core.audit import AuditLog
from app.core.context import RequestContext
from app.core.database import tenant_session
from app.core.encrypted import (
    EncryptedStr,
    Sealed,
    blind_index,
    open_sealed,
    seal,
    set_identity_field,
    unmask,
)
from app.core.errors import Forbidden
from app.core.permissions import CATALOG, Principal, Scope
from app.models.legal_entities import LegalEntityBankAccount
from tests.factories import create_tenant_row


def test_sealed_never_reveals_its_token() -> None:
    sealed, last4, bidx = seal(uuid.uuid4(), "1234 5678 9012")
    assert str(sealed) == repr(sealed) == "Sealed(****)"
    assert last4 == "9012"
    assert len(bidx) == 32

    class Out(BaseModel):
        value: Sealed

    with pytest.raises(Exception, match="never serialised"):
        Out(value=sealed).model_dump()
    with pytest.raises(Exception, match="never serialised"):
        Out(value=sealed).model_dump_json()


def test_blind_index_deterministic_per_tenant() -> None:
    a, b = uuid.uuid4(), uuid.uuid4()
    assert blind_index(a, "ABCDE1234F") == blind_index(a, "abcde 1234-f")
    assert blind_index(a, "ABCDE1234F") != blind_index(b, "ABCDE1234F")


def test_round_trip_and_tenant_keys_differ() -> None:
    tenant = uuid.uuid4()
    sealed, _, _ = seal(tenant, "secret-number")
    assert open_sealed(tenant, sealed) == "secret-number"
    with pytest.raises(Exception):  # noqa: B017 - InvalidToken from another tenant's key
        open_sealed(uuid.uuid4(), sealed)


def test_column_refuses_plain_strings() -> None:
    column_type = EncryptedStr()
    with pytest.raises(TypeError):
        column_type.process_bind_param("plaintext", None)  # type: ignore[arg-type]
    sealed, _, _ = seal(uuid.uuid4(), "x123")
    assert column_type.process_bind_param(sealed, None) == sealed.token  # type: ignore[arg-type]
    assert isinstance(column_type.process_result_value(sealed.token, None), Sealed)  # type: ignore[arg-type]


async def test_unmask_requires_permission_and_audits() -> None:
    from app.services import legal_entities_service

    tenant_id = await create_tenant_row()
    async with tenant_session(tenant_id) as db:
        from app.models.identity import Tenant

        tenant = await db.get(Tenant, tenant_id)
        entity = await legal_entities_service.provision_default_entity(db, tenant)
        account = LegalEntityBankAccount(
            id=uuid.uuid4(), tenant_id=tenant_id, legal_entity_id=entity.id, label="Main", bank_name="Bank"
        )
        set_identity_field(account, "account_number", "000123456789", tenant_id)
        db.add(account)
        await db.flush()
        assert account.account_number_last4 == "6789"

        nobody = RequestContext(tenant_id=tenant_id, principal=Principal(type="user", id=uuid.uuid4(), tenant_id=tenant_id))
        with pytest.raises(Forbidden):
            await unmask(nobody, db, account, "account_number")

        viewer = RequestContext(
            tenant_id=tenant_id,
            principal=Principal(
                type="user", id=uuid.uuid4(), tenant_id=tenant_id, permissions=dict.fromkeys(CATALOG, Scope.TENANT)
            ),
        )
        assert await unmask(viewer, db, account, "account_number") == "000123456789"
        await db.flush()
        rows = (
            await db.execute(select(AuditLog).where(AuditLog.action == "identity_unmasked", AuditLog.tenant_id == tenant_id))
        ).scalars().all()
        assert len(rows) == 1
        assert rows[0].data == {"field": "account_number"}
        # The stored token never appears in the CRUD audit row of the account.
        created = (
            await db.execute(
                select(AuditLog).where(
                    AuditLog.tenant_id == tenant_id, AuditLog.action == "legal_entity_bank_account.created"
                )
            )
        ).scalars().one()
        assert account.account_number.token not in str(created.changes)


# ---------------------------------------------------------------- Epic 36: purposes and EncryptedJSON
def test_identity_tenant_fernet_is_unchanged() -> None:
    """Existing identity tokens stay readable: same master key, same HKDF ``info=identity:<tenant>``."""
    import base64

    from cryptography.fernet import Fernet
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    from app.core.encrypted import tenant_fernet
    from app.core.security import key_material

    tid = uuid.uuid4()
    master = base64.urlsafe_b64decode(key_material("identity").encode("ascii"))
    hkdf = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=f"identity:{tid}".encode())
    raw = hkdf.derive(master)
    legacy = Fernet(base64.urlsafe_b64encode(raw))
    sealed, _, _ = seal(tid, "ABCDE1234F")
    assert legacy.decrypt(sealed.token.encode()).decode() == "ABCDE1234F"
    assert tenant_fernet(tid, "identity").decrypt(legacy.encrypt(b"x")) == b"x"


def test_demographic_purpose_uses_its_own_key() -> None:
    from cryptography.fernet import InvalidToken

    from app.core.encrypted import open_json, seal_json, tenant_fernet

    tid = uuid.uuid4()
    sealed = seal_json(tid, "demographic", {"gender": "female", "age_band": "25-34"})
    assert str(sealed) == "Sealed(****)"
    assert open_json(tid, "demographic", sealed) == {"age_band": "25-34", "gender": "female"}
    with pytest.raises(InvalidToken):
        open_json(tid, "identity", sealed)
    with pytest.raises(InvalidToken):
        open_json(uuid.uuid4(), "demographic", sealed)
    with pytest.raises(ValueError, match="purpose"):
        tenant_fernet(tid, "audit")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        open_json(tid, "demographic", sealed.token)  # type: ignore[arg-type]


def test_encrypted_json_binds_and_loads_sealed_only() -> None:
    from app.core.encrypted import EncryptedJSON, seal_json

    col = EncryptedJSON()
    sealed = seal_json(uuid.uuid4(), "demographic", {"disability": "prefer_not_to_say"})
    assert col.process_bind_param(sealed, None) == sealed.token  # type: ignore[arg-type]
    assert col.process_bind_param(None, None) is None  # type: ignore[arg-type]
    for plain in ('{"gender": "male"}', {"gender": "male"}):
        with pytest.raises(TypeError):
            col.process_bind_param(plain, None)  # type: ignore[arg-type]
    loaded = col.process_result_value(sealed.token, None)  # type: ignore[arg-type]
    assert isinstance(loaded, Sealed) and loaded == sealed
    assert col.process_result_value(None, None) is None  # type: ignore[arg-type]


def test_demographic_key_purpose_and_setting() -> None:
    from app.core.security import _KEY_SETTINGS, key_material
    from app.core.settings import get_settings

    assert _KEY_SETTINGS["demographic"] == "demographic_encryption_key"
    assert hasattr(get_settings(), "demographic_encryption_key")
    assert key_material("demographic") != key_material("identity")
