# ruff: noqa: E501
"""Per-tenant spoke -> hub integration credentials (unified platform).

Backs ``GET /api/v1/internal/tenant-ats-key`` (``app.router.internal``). The Portal / Croar / Feedback IQ
spokes call it backend-to-backend to fetch THEIR OWN tenant's ATS Public API key, so each tenant's data lands
only in that tenant's ATS silo (no more one global key -> one silo cross-tenant leak).

Find-or-mint, idempotent:

* The plaintext key is minted once (``api_keys_service.create_key`` in that tenant's own session), then stored
  Fernet-encrypted (``security`` ``connector`` purpose) in ``tenant_integration_credentials`` (platform table,
  keyed by central ``tenant_id``). Public API keys are hash-only, so the ciphertext is the only way to hand back
  the SAME key on later calls.
* On a later call the stored key is decrypted and returned; if it was revoked / expired / its lookup is gone, a
  fresh key is minted and the row is replaced.
* The 200 response also carries, resolved inside the tenant's own silo, ``owner_id`` (a real active user to own
  API-created records -- the tenant admin first, else the earliest active user; null when the tenant has no users)
  and ``client_id`` (a find-or-create default integration Client ``PARTNER_CLIENT_NAME`` that spoke-created demands
  are filed under). This is why spoke creates succeed in ANY tenant instead of only the one the global ids belonged
  to.

Scopes granted unlock exactly the public-API scopes the spokes need for the integration:
``candidates:read|write``, ``demands:read|write`` and ``applications:read|write`` (the proven set the global
outbox key used). ``pii.contact:read`` is a SENSITIVE scope (FR-348): it maps to NO internal permission and is
never grantable to an API key, so contact fields stay masked for key principals -- there is nothing to grant.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import text

from app.core.context import RequestContext
from app.core.database import platform_session, tenant_session
from app.core.errors import NotFound
from app.core.permissions import TENANT_ADMIN, Principal, Scope
from app.core.security import InvalidToken, decrypt_str, encrypt_str
from app.core.settings import get_settings
from app.models.api_keys import ApiKey, ApiKeyLookup
from app.models.integration_credentials import TenantIntegrationCredential
from app.schemas.api_keys import ApiKeyCreate
from app.services import api_keys_service

ATS_SERVICE = "ats"
# Default integration Client (find-or-create per tenant) that spoke-created demands are filed under.
PARTNER_CLIENT_NAME = "Partner Integrations"
# Fernet purpose reused for connector / integration credentials (as the per-tenant mail settings do).
CRED_PURPOSE = "connector"
# Minter / created_by marker for keys no human user created (all-zero UUID; ``api_keys.created_by`` has no FK).
SERVICE_ACTOR_ID = uuid.UUID(int=0)
# The compose-network address of the ATS (Talent OS) Public API the spokes push to.
ATS_PUBLIC_API_BASE_URL = "http://talentos-backend:8000/api/public/v1"

# Internal permissions whose union unlocks the public-API scopes the spokes need (see ``public_scopes_for``):
#   candidate:*           -> candidates:read + candidates:write
#   demand:*              -> demands:read + demands:write
#   application:*         -> applications:read + applications:write
ATS_INTEGRATION_PERMISSIONS: tuple[str, ...] = (
    "candidate:read",
    "candidate:create",
    "candidate:update",
    "demand:read",
    "demand:create",
    "demand:update",
    "application:read",
    "application:create",
    "application:update",
    "application:move",
)


def _not_found() -> NotFound:
    return NotFound("Unknown tenant", code="tenant_not_found")


async def _resolve_tenant(tenant_id: uuid.UUID) -> None:
    """404 unless ``tenant_id`` is a real tenant. In silo mode it must be registered in the control-plane
    tenant registry (so its own DB is routable); in both modes it must exist in the platform ``tenants`` table."""
    s = get_settings()
    if s.tenancy_mode == "silo":
        from app.core import tenant_registry

        try:
            await tenant_registry.resolve_db_name(tenant_id)
        except LookupError as exc:
            raise _not_found() from exc
    async with platform_session() as pdb:
        status = (
            await pdb.execute(
                text("SELECT status FROM tenants WHERE id = CAST(:t AS uuid)"), {"t": str(tenant_id)}
            )
        ).scalar()
    if status is None:
        raise _not_found()


async def _platform_lookup(key: str) -> ApiKeyLookup | None:
    async with platform_session() as pdb:
        return await pdb.get(ApiKeyLookup, api_keys_service.hash_key(key))


async def _key_still_valid(tenant_id: uuid.UUID, api_key_id: uuid.UUID, key: str) -> bool:
    """Return True when the stored key still authenticates: its platform lookup resolves to this tenant / key
    and the tenant-side ``api_keys`` row is neither revoked nor expired."""
    lookup = await _platform_lookup(key)
    if lookup is None or lookup.key_id != api_key_id or lookup.tenant_id != tenant_id:
        return False
    async with tenant_session(tenant_id) as db:
        row = await db.get(ApiKey, api_key_id)
    if row is None or row.revoked_at is not None:
        return False
    return not (row.expires_at is not None and row.expires_at <= datetime.now(UTC))


async def _mint(tenant_id: uuid.UUID) -> tuple[uuid.UUID, str]:
    """Mint a fresh ATS key in the tenant's own silo and make sure its platform lookup exists."""
    principal = Principal(
        type="user",
        id=SERVICE_ACTOR_ID,
        tenant_id=tenant_id,
        permissions=dict.fromkeys(ATS_INTEGRATION_PERMISSIONS, Scope.TENANT),
    )
    ctx = RequestContext(tenant_id=tenant_id, principal=principal)
    data = ApiKeyCreate(
        name="Unified platform integration (per-tenant ATS)",
        scopes=list(ATS_INTEGRATION_PERMISSIONS),
        rate_limit_per_min=6000,
    )
    async with tenant_session(tenant_id) as db:
        created = await api_keys_service.create_key(ctx, db, data)
    key = created.key
    # ``create_key`` writes the lookup through the tenant session; in silo that lands in the tenant DB only,
    # while ``authenticate`` reads it from ``platform_session`` (the platform DB). Mirror it there (idempotent).
    digest = api_keys_service.hash_key(key)
    async with platform_session() as pdb:
        if await pdb.get(ApiKeyLookup, digest) is None:
            pdb.add(ApiKeyLookup(key_hash=digest, tenant_id=tenant_id, key_id=created.id))
    return created.id, key


# Earliest active user, admin-role users first. RLS scopes every table to the session's tenant.
_OWNER_SQL = text(
    """
    SELECT u.id::text
    FROM users u
    WHERE u.is_active = true
    ORDER BY EXISTS (
        SELECT 1 FROM user_roles ur
        JOIN roles r ON r.tenant_id = ur.tenant_id AND r.id = ur.role_id
        WHERE ur.tenant_id = u.tenant_id AND ur.user_id = u.id AND r.key = :admin_key
    ) DESC, u.created_at ASC, u.id ASC
    LIMIT 1
    """
)
_CLIENT_SELECT_SQL = text("SELECT id::text FROM clients WHERE name = :name LIMIT 1")
_CLIENT_INSERT_SQL = text(
    """
    INSERT INTO clients (id, tenant_id, name, owner_id, status, locations, created_at, updated_at)
    VALUES (gen_random_uuid(), CAST(:tid AS uuid), :name, CAST(:owner AS uuid), 'active', '[]'::jsonb, now(), now())
    ON CONFLICT (tenant_id, name) DO NOTHING
    RETURNING id::text
    """
)


async def _resolve_owner_and_client(tenant_id: uuid.UUID) -> tuple[str | None, str | None]:
    """Resolve, inside the tenant's own silo, the owner user and the default integration Client the spokes file
    records under. ``owner_id`` is the earliest active user (the tenant admin first, else any active user); None
    when the tenant has no users (spokes skip). ``client_id`` is a find-or-create of ``PARTNER_CLIENT_NAME`` owned
    by that user (idempotent via the ``(tenant_id, name)`` unique constraint); None when there is no owner."""
    async with tenant_session(tenant_id) as db:
        owner = (await db.execute(_OWNER_SQL, {"admin_key": TENANT_ADMIN})).scalar()
        if owner is None:
            return None, None
        existing = (await db.execute(_CLIENT_SELECT_SQL, {"name": PARTNER_CLIENT_NAME})).scalar()
        if existing is not None:
            return owner, existing
        created = (
            await db.execute(
                _CLIENT_INSERT_SQL, {"tid": str(tenant_id), "name": PARTNER_CLIENT_NAME, "owner": owner}
            )
        ).scalar()
        if created is not None:
            return owner, created
        # A concurrent call inserted it first (ON CONFLICT DO NOTHING) -- re-read the existing row.
        existing = (await db.execute(_CLIENT_SELECT_SQL, {"name": PARTNER_CLIENT_NAME})).scalar()
        return owner, existing


async def get_or_mint_ats_key(tenant_id: uuid.UUID) -> dict[str, str | None]:
    """Return ``{"api_key", "base_url", "owner_id", "client_id"}`` for the tenant's own ATS integration.

    The key is minted + stored on the first call and the SAME key is returned afterwards (re-minting only if the
    stored key was revoked / invalid). ``owner_id`` / ``client_id`` are resolved in the tenant's silo so spoke
    creates (which need a real in-tenant user and client) succeed in the right tenant."""
    await _resolve_tenant(tenant_id)

    async with platform_session() as pdb:
        cred = await pdb.get(TenantIntegrationCredential, (tenant_id, ATS_SERVICE))
    stored_key: str | None = None
    if cred is not None:
        try:
            stored_key = decrypt_str(CRED_PURPOSE, cred.api_key_encrypted)
        except InvalidToken:
            stored_key = None

    if (
        cred is not None
        and stored_key is not None
        and await _key_still_valid(tenant_id, cred.api_key_id, stored_key)
    ):
        key = stored_key
    else:
        api_key_id, key = await _mint(tenant_id)
        encrypted = encrypt_str(CRED_PURPOSE, key)
        async with platform_session() as pdb:
            row = await pdb.get(TenantIntegrationCredential, (tenant_id, ATS_SERVICE))
            if row is None:
                pdb.add(
                    TenantIntegrationCredential(
                        tenant_id=tenant_id,
                        service=ATS_SERVICE,
                        api_key_encrypted=encrypted,
                        api_key_id=api_key_id,
                    )
                )
            else:
                row.api_key_encrypted = encrypted
                row.api_key_id = api_key_id
                row.updated_at = datetime.now(UTC)

    owner_id, client_id = await _resolve_owner_and_client(tenant_id)
    return {
        "api_key": key,
        "base_url": ATS_PUBLIC_API_BASE_URL,
        "owner_id": owner_id,
        "client_id": client_id,
    }
