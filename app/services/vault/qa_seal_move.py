# ruff: file-ignore[line-too-long]
"""In-migration sealing of onboarding Q&A plaintext (Epic 78, story 78.4, FR-404, AD-90).

Run by ``20261010_512_03_qa_plain_drop`` (remainder sealing + verify) and by the downgrades of ``512_02`` / ``512_03``
(unseal back into the plaintext columns). FROZEN with those migrations: synchronous, plain SQLAlchemy ``Connection``
with SQL text, and only the pure modules ``app.core.kms`` and ``app.services.vault.crypto`` -- never ORM models or
services. The blob format and AAD are exactly ``vault_service.seal_field``'s: ``version(2) || nonce || ct`` under the
Tenant DEK with AAD = the context ``onboarding_qa_messages.text:{id}`` / ``onboarding_qa_messages.feedback_note:{id}``.

Per tenant (``app.tenant_id`` set by the caller; every statement also filters ``tenant_id``): batches of 500, seal,
decrypt back and compare (constant time) before the plaintext is NULLed. Any mismatch raises ``QaSealError`` and the
migration's transaction rolls back. Values are never logged; only counts are.
"""

import hmac
import uuid

import sqlalchemy as sa

from app.core.kms import Kms
from app.services.vault import crypto

BATCH = 500
CTX_TEXT = "onboarding_qa_messages.text:{id}"
CTX_NOTE = "onboarding_qa_messages.feedback_note:{id}"


class QaSealError(RuntimeError):
    """A sealed Q&A value could not be verified; the migration aborts."""


def ensure_dek(conn: sa.Connection, tenant_id: uuid.UUID, kms: Kms) -> tuple[int, bytes]:
    row = conn.execute(
        sa.text("SELECT version, wrapped_key, kek_id FROM vault_data_keys WHERE tenant_id = :tid AND status = 'active'"),
        {"tid": tenant_id},
    ).first()
    if row is not None:
        return int(row[0]), kms.unwrap(bytes(row[1]), str(row[2]), str(tenant_id))
    dek = crypto.new_dek()
    wrapped, kek_id = kms.wrap(dek, str(tenant_id))
    last = conn.execute(sa.text("SELECT max(version) FROM vault_data_keys WHERE tenant_id = :tid"), {"tid": tenant_id}).scalar()
    version = int(last or 0) + 1
    conn.execute(
        sa.text(
            "INSERT INTO vault_data_keys (id, tenant_id, version, wrapped_key, kek_id, status) "
            "VALUES (:id, :tid, :version, :wrapped, :kek_id, 'active')"
        ),
        {"id": uuid.uuid4(), "tid": tenant_id, "version": version, "wrapped": wrapped, "kek_id": kek_id},
    )
    return version, dek


def deks(conn: sa.Connection, tenant_id: uuid.UUID, kms: Kms) -> dict[int, bytes]:
    rows = conn.execute(
        sa.text("SELECT version, wrapped_key, kek_id FROM vault_data_keys WHERE tenant_id = :tid"), {"tid": tenant_id}
    ).all()
    return {int(v): kms.unwrap(bytes(w), str(k), str(tenant_id)) for v, w, k in rows}


def _open(keys: dict[int, bytes], context: str, blob: bytes) -> bytes:
    version = crypto.blob_version(bytes(blob))
    dek = keys.get(version)
    if dek is None:
        msg = f"tenant data key v{version} is missing"
        raise QaSealError(msg)
    return crypto.open_blob(dek, context, bytes(blob))


def seal_tenant(conn: sa.Connection, tenant_id: uuid.UUID, kms: Kms) -> int:
    """Seal every remaining plaintext ``text`` / ``feedback_note`` of the tenant; returns the rows sealed."""
    version, dek = ensure_dek(conn, tenant_id, kms)
    keys = {version: dek, **deks(conn, tenant_id, kms)}
    sealed = 0
    while True:
        rows = conn.execute(
            sa.text(
                "SELECT id, text, feedback_note FROM onboarding_qa_messages WHERE tenant_id = :tid "
                "AND (text IS NOT NULL OR feedback_note IS NOT NULL) ORDER BY id LIMIT :n"
            ),
            {"tid": tenant_id, "n": BATCH},
        ).all()
        if not rows:
            return sealed
        for message_id, text, note in rows:
            params: dict[str, object] = {"id": message_id, "tid": tenant_id}
            sets: list[str] = []
            for column, value, template in (("text", text, CTX_TEXT), ("feedback_note", note, CTX_NOTE)):
                if value is None:
                    continue
                context = template.format(id=message_id)
                plain = str(value).encode("utf-8")
                blob = crypto.seal_blob(dek, version, context, plain)
                if not hmac.compare_digest(_open(keys, context, blob), plain):
                    msg = f"onboarding_qa_messages {message_id}: {column} did not round-trip"
                    raise QaSealError(msg)
                sets += [f"{column}_sealed = :{column}_blob", f"{column} = NULL"]
                params[f"{column}_blob"] = blob
            conn.execute(sa.text(f"UPDATE onboarding_qa_messages SET {', '.join(sets)} WHERE id = :id AND tenant_id = :tid"), params)  # nosec B608 - table/column identifiers are in-code constants (whitelisted); values are bound params
            sealed += 1


def unseal_tenant(conn: sa.Connection, tenant_id: uuid.UUID, kms: Kms, *, only_missing: bool = True) -> int:
    """Decrypt sealed values back into ``text`` / ``feedback_note`` (downgrades); returns the rows restored."""
    keys = deks(conn, tenant_id, kms)
    where = "text IS NULL" if only_missing else "TRUE"
    rows = conn.execute(
        sa.text(
            f"SELECT id, text_sealed, feedback_note_sealed FROM onboarding_qa_messages WHERE tenant_id = :tid AND text_sealed IS NOT NULL AND {where}"  # nosec B608 - table/column identifiers are in-code constants (whitelisted); values are bound params
        ),
        {"tid": tenant_id},
    ).all()
    for message_id, text_blob, note_blob in rows:
        text = _open(keys, CTX_TEXT.format(id=message_id), text_blob).decode("utf-8")
        note = _open(keys, CTX_NOTE.format(id=message_id), note_blob).decode("utf-8") if note_blob is not None else None
        conn.execute(
            sa.text("UPDATE onboarding_qa_messages SET text = :t, feedback_note = :n WHERE id = :id AND tenant_id = :tid"),
            {"t": text, "n": note, "id": message_id, "tid": tenant_id},
        )
    return len(rows)


def tenants(conn: sa.Connection) -> list[uuid.UUID]:
    return [uuid.UUID(str(r[0])) for r in conn.execute(sa.text("SELECT id FROM tenants ORDER BY id")).all()]


__all__ = ["BATCH", "CTX_NOTE", "CTX_TEXT", "QaSealError", "deks", "ensure_dek", "seal_tenant", "tenants", "unseal_tenant"]
