# ruff: noqa: E501
"""Epic 66 (story 66.6, spec 0.11): the Secrets Vault -- envelope round trip, AAD / tenant binding, logged reads,
rotation overlap, master rewrap, field sealing, ``SecretBag`` refusal and the legacy-move verification."""

import copy
import pickle
import uuid

import pytest
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select

from app.core.context import RequestContext
from app.core.database import tenant_session
from app.core.kms import LocalKms, set_kms
from app.models.vault import VaultAccessLog, VaultDataKey, VaultSecret
from app.services import vault_service
from app.services.vault import crypto, legacy_move, store
from app.services.vault.bag import SecretBag
from tests.epic3 import Org3

OWNER = "installation"


def _ctx(tenant_id: uuid.UUID) -> RequestContext:
    return RequestContext(tenant_id=tenant_id)


async def _put(tenant_id: uuid.UUID, owner_id: uuid.UUID, name: str, value: str) -> None:
    async with tenant_session(tenant_id) as db:
        await vault_service.put(_ctx(tenant_id), db, vault_service.SecretRef(OWNER, owner_id, name), value)


async def test_round_trip_meta_and_logged_read(org3: Org3) -> None:
    tenant, owner = org3.tenant_id, uuid.uuid4()
    await _put(tenant, owner, "api_key", "s3cr3t-value-1234")
    async with tenant_session(tenant) as db:
        row = (await db.execute(select(VaultSecret))).scalar_one()
        assert b"s3cr3t" not in row.ciphertext and len(row.fingerprint) == 4
        meta = await vault_service.meta(_ctx(tenant), db, OWNER, owner)
        assert [(m.name, m.has_secret, m.fingerprint) for m in meta] == [("api_key", True, row.fingerprint)]
        value = await vault_service.get(
            _ctx(tenant), db, vault_service.SecretRef(OWNER, owner, "api_key"), purpose="t"
        )
        assert value == "s3cr3t-value-1234"
        logs = list((await db.execute(select(VaultAccessLog))).scalars())
    assert len(logs) == 1 and logs[0].purpose == "t" and logs[0].caller.startswith("tests.vault")


async def test_aad_and_tenant_binding(org3: Org3) -> None:
    tenant, owner = org3.tenant_id, uuid.uuid4()
    await _put(tenant, owner, "token", "value")
    async with tenant_session(tenant) as db:
        row = (await db.execute(select(VaultSecret))).scalar_one()
        dek = await store.dek_for_version(db, tenant, row.dek_version)
    with pytest.raises(crypto.InvalidTag):
        crypto.decrypt(
            dek, row.nonce, row.ciphertext, crypto.secret_aad(tenant, OWNER, owner, "other", row.dek_version)
        )
    with pytest.raises(crypto.InvalidTag):
        crypto.decrypt(
            dek,
            row.nonce,
            row.ciphertext,
            crypto.secret_aad(uuid.uuid4(), OWNER, owner, "token", row.dek_version),
        )
    other = uuid.uuid4()  # RLS: any other tenant context sees nothing
    async with tenant_session(other) as db:
        assert (await db.execute(select(VaultSecret))).first() is None


async def test_rotate_keeps_previous_and_delete_owner(org3: Org3) -> None:
    tenant, owner = org3.tenant_id, uuid.uuid4()
    await _put(tenant, owner, "signing", "old-secret")
    async with tenant_session(tenant) as db:
        ref = vault_service.SecretRef(OWNER, owner, "signing")
        await vault_service.rotate(_ctx(tenant), db, ref, "new-secret", keep_previous_hours=24)
        bag = await vault_service.open_bag(_ctx(tenant), db, OWNER, owner, purpose="t")
        assert bag.get("signing") == "new-secret" and bag.get("signing__previous") == "old-secret"
        assert await vault_service.delete_owner(_ctx(tenant), db, OWNER, owner) == 2
        assert await vault_service.meta(_ctx(tenant), db, OWNER, owner) == []


async def test_master_rewrap_changes_only_the_dek_wrap(org3: Org3) -> None:
    tenant, owner = org3.tenant_id, uuid.uuid4()
    v1, v2 = b"\x01" * 32, b"\x02" * 32
    set_kms(LocalKms({1: v1}, current_version=1))
    store.clear_dek_cache()
    try:
        await _put(tenant, owner, "api_key", "keep-me")
        async with tenant_session(tenant) as db:
            before = (await db.execute(select(VaultSecret))).scalar_one().ciphertext
            wrap_before = (await db.execute(select(VaultDataKey))).scalar_one().wrapped_key
        set_kms(LocalKms({1: v1, 2: v2}, current_version=2))
        store.clear_dek_cache()
        assert await vault_service.rewrap(tenant) == 1
        async with tenant_session(tenant) as db:
            dk = (await db.execute(select(VaultDataKey))).scalar_one()
            assert dk.kek_id == "local:v2" and dk.wrapped_key != wrap_before
            assert (await db.execute(select(VaultSecret))).scalar_one().ciphertext == before
            store.clear_dek_cache()
            got = await vault_service.get(
                _ctx(tenant), db, vault_service.SecretRef(OWNER, owner, "api_key"), purpose="t"
            )
        assert got == "keep-me"
    finally:
        set_kms(None)
        store.clear_dek_cache()


async def test_seal_and_open_field(org3: Org3) -> None:
    tenant = org3.tenant_id
    async with tenant_session(tenant) as db:
        blob = await vault_service.seal_field(db, tenant, "onboarding_qa_messages.text:1", "hello")
        assert b"hello" not in blob
        assert await vault_service.open_field(db, tenant, "onboarding_qa_messages.text:1", blob) == "hello"
        with pytest.raises(crypto.InvalidTag):
            await vault_service.open_field(db, tenant, "onboarding_qa_messages.text:2", blob)


async def test_secret_bag_refuses_serialisation(org3: Org3) -> None:
    tenant, owner = org3.tenant_id, uuid.uuid4()
    await _put(tenant, owner, "api_key", "abc")
    async with tenant_session(tenant) as db:
        bag = await vault_service.open_bag(_ctx(tenant), db, OWNER, owner, purpose="t")
    assert repr(bag) == "SecretBag(****)" and str(bag) == "SecretBag(****)"
    with pytest.raises(TypeError):
        pickle.dumps(bag)
    with pytest.raises(TypeError):
        copy.copy(bag)

    class Holder(BaseModel):
        model_config = ConfigDict(arbitrary_types_allowed=True)
        bag: SecretBag

    with pytest.raises((TypeError, ValueError)):
        Holder(bag=bag).model_dump()


def test_legacy_move_verification_aborts_on_mismatch() -> None:
    tenant, owner = uuid.uuid4(), uuid.uuid4()
    secret = legacy_move.LegacySecret("provider_config", owner, "credentials", b'{"k": "v"}')
    dek = crypto.new_dek()
    rows = legacy_move.encrypt_rows(tenant, [secret], 1, dek)
    stored = [{**r, "dek_version": 1} for r in rows]
    assert legacy_move.verify(tenant, [secret], stored, {1: dek}) == 1
    corrupted = [{**stored[0], "ciphertext": bytes(b ^ 1 for b in stored[0]["ciphertext"])}]
    with pytest.raises(legacy_move.LegacyMoveError):
        legacy_move.verify(tenant, [secret], corrupted, {1: dek})
    other = legacy_move.LegacySecret("provider_config", owner, "credentials", b'{"k": "w"}')
    reencrypted = legacy_move.encrypt_rows(tenant, [other], 1, dek)
    with pytest.raises(legacy_move.LegacyMoveError):
        legacy_move.verify(tenant, [secret], reencrypted, {1: dek})
    with pytest.raises(legacy_move.LegacyMoveError):
        legacy_move.verify(tenant, [secret], [], {1: dek})
