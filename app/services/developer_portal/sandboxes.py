# ruff: file-ignore[line-too-long, non-imperative-mood]
"""Self-serve Sandbox Tenants (Epic 74, story 74.3, spec 0.4).

Create (``developer`` / ``owner``; a partner org holds at most ``partner_sandboxes_max`` = 3, a Tenant
``tenant_sandboxes_max`` = 2): a ``developer_sandboxes`` row ``creating`` -> task ``developer.create_sandbox`` ->
``sandbox_service.create_sandbox_tenant`` (66: Fake adapters only, synthetic seed) -> the developer's sandbox user
(Tenant Admin, plus the Integration Administrator role when it exists; viewers get the Viewer role) -> ``ready``.
Reset = delete + recreate with the same size and seed (66 has no in-place reset; Known gap 6): the row keeps its id and
points at the new tenant; optionally the Apps are re-installed with the same scopes as the developer's sandbox user.
Delete runs ``tenant_lifecycle_service.delete_tenant_data`` (vault DEKs go with it = crypto-shred).
"""

import secrets
import uuid
from collections.abc import Callable
from typing import Any

from loguru import logger
from sqlalchemy import func, select, update

from app.core.context import RequestContext
from app.core.database import platform_session, tenant_session
from app.core.errors import ConflictError, NotFound
from app.core.permissions import TENANT_ADMIN, Principal
from app.core.security import hash_password
from app.core.settings import get_settings
from app.core.tenancy import clear_sandbox_cache, is_sandbox
from app.models.developer import DeveloperAccount, DeveloperSandbox, DeveloperSandboxMember, PartnerOrgMember
from app.models.identity import Role, Tenant, User
from app.models.oauth import App, AppInstallation, PartnerOrg
from app.schemas.developer import SandboxCreate, SandboxInstallIn, SandboxInstallOut, SandboxOut
from app.services.developer_portal import audit_log, notifier
from app.services.developer_portal.common import now
from app.services.developer_portal.owner import Owner

INTEGRATION_ADMIN_KEYS = ("integration_admin", "integration_administrator")
VIEWER_KEY = "viewer"
LIVE = ("creating", "ready", "resetting", "failed")
Kicker = Callable[[str, uuid.UUID, dict[str, Any]], bool]
_kicker: dict[str, Kicker | None] = {"fn": None}


def set_kicker(fn: Kicker | None) -> None:
    """Hooks register the Celery kick (``developer.create_sandbox`` / ``reset_sandbox`` / ``delete_sandbox``); it returns
    False when nothing was enqueued (eager mode), and the work then runs inline."""
    _kicker["fn"] = fn


async def _kick(op: str, sandbox_id: uuid.UUID, **kw: Any) -> None:
    fn = _kicker["fn"]
    if fn is not None and fn(op, sandbox_id, kw):  # enqueued (False in eager mode: run inline)
        return
    await {"create": run_create, "reset": run_reset, "delete": run_delete}[op](sandbox_id, **kw)


async def _out(row: DeveloperSandbox) -> SandboxOut:
    slug = None
    if row.tenant_id is not None and row.status not in ("deleted",):
        async with platform_session() as db:
            slug = (await db.execute(select(Tenant.slug).where(Tenant.id == row.tenant_id))).scalar_one_or_none()
    return SandboxOut(
        id=row.id, name=row.name, size=row.size, status=row.status, owner_kind=row.owner_kind, tenant_id=row.tenant_id,  # type: ignore[arg-type]
        tenant_slug=str(slug) if slug else None, failure_code=row.failure_code, created_at=row.created_at, ready_at=row.ready_at,
        last_reset_at=row.last_reset_at,
    )  # fmt: skip


def _owned(stmt: Any, owner: Owner) -> Any:
    if owner.kind == "partner_org":
        return stmt.where(DeveloperSandbox.owner_kind == "partner_org", DeveloperSandbox.partner_org_id == owner.id)
    return stmt.where(DeveloperSandbox.owner_kind == "tenant", DeveloperSandbox.owner_tenant_id == owner.id)


async def get_owned(owner: Owner, sandbox_id: uuid.UUID, *, require_ready: bool = False) -> DeveloperSandbox:
    async with platform_session() as db:
        row = (await db.execute(_owned(select(DeveloperSandbox).where(DeveloperSandbox.id == sandbox_id), owner))).scalar_one_or_none()
    if row is None or row.status == "deleted":
        raise NotFound("Sandbox not found")
    if require_ready:
        if row.tenant_id is None or not await is_sandbox(row.tenant_id):
            raise ConflictError("Only sandbox tenants can be used here", code="sandbox_only")
        if row.status != "ready":
            raise ConflictError("The sandbox is not ready yet", code="sandbox_not_ready")
    return row


async def list_sandboxes(owner: Owner) -> list[SandboxOut]:
    async with platform_session() as db:
        rows = list((await db.execute(_owned(select(DeveloperSandbox).where(DeveloperSandbox.status != "deleted"), owner).order_by(DeveloperSandbox.created_at))).scalars())
    return [await _out(r) for r in rows]


async def create_sandbox(owner: Owner, data: SandboxCreate) -> SandboxOut:
    owner.require("developer")
    s = get_settings()
    limit = s.partner_sandboxes_max if owner.kind == "partner_org" else s.tenant_sandboxes_max
    async with platform_session() as db:
        if owner.kind == "partner_org":
            await db.execute(select(PartnerOrg.id).where(PartnerOrg.id == owner.id).with_for_update())  # serialise the count
        count = (await db.execute(_owned(select(func.count()).select_from(DeveloperSandbox).where(DeveloperSandbox.status.in_(LIVE)), owner))).scalar_one()
        if count >= limit:
            noun = "organisation" if owner.kind == "partner_org" else "workspace"
            raise ConflictError(f"Your {noun} can have up to {limit} sandboxes", code="sandbox_limit")
        row = DeveloperSandbox(
            id=uuid.uuid4(), owner_kind=owner.kind, partner_org_id=owner.partner_org_id, owner_tenant_id=owner.owner_tenant_id,
            created_by_account_id=owner.account_id, created_by_user_id=owner.user_id, name=data.name.strip(), size=data.size,
            seed=secrets.randbelow(2**31 - 1) + 1, status="creating",
        )  # fmt: skip
        db.add(row)
        if owner.kind == "partner_org":
            audit_log.add(db, "sandbox.created", partner_org_id=owner.id, account_id=owner.account_id, target_type="sandbox", target_id=row.id, name=row.name, size=row.size)
        await db.flush()
        await db.refresh(row)
        sandbox_id = row.id
    await _kick("create", sandbox_id)
    async with platform_session() as db:
        fresh = await db.get(DeveloperSandbox, sandbox_id)
        assert fresh is not None
        return await _out(fresh)


# ================================================================ task bodies
async def _owner_label(row: DeveloperSandbox) -> str:
    async with platform_session() as db:
        if row.partner_org_id is not None:
            return str((await db.execute(select(PartnerOrg.name).where(PartnerOrg.id == row.partner_org_id))).scalar_one_or_none() or "Partner")
        return str((await db.execute(select(Tenant.name).where(Tenant.id == row.owner_tenant_id))).scalar_one_or_none() or "Workspace")


async def _new_tenant(row: DeveloperSandbox) -> uuid.UUID:
    from app.services import sandbox_service

    actor = row.created_by_account_id or row.created_by_user_id or row.id
    ctx = RequestContext(principal=Principal(type="platform", id=actor))
    label = await _owner_label(row)
    tenant = await sandbox_service.create_sandbox_tenant(
        ctx, name=f"Sandbox - {label} - {row.name}"[:200], owner_kind=row.owner_kind,  # type: ignore[arg-type]
        owner_id=row.partner_org_id or row.owner_tenant_id, size=row.size, seed=int(row.seed),  # type: ignore[arg-type]
    )  # fmt: skip
    return tenant.id


async def _role_ids(db: Any, keys: tuple[str, ...]) -> list[uuid.UUID]:
    return list((await db.execute(select(Role.id).where(Role.key.in_(keys)))).scalars())


async def ensure_member(sandbox: DeveloperSandbox, *, account_id: uuid.UUID | None, user_id: uuid.UUID | None, email: str, name: str, role: str) -> uuid.UUID:
    """The member's user inside the sandbox tenant (created lazily; sign-in only via the handoff: unusable password)."""
    from app.services import access_service

    assert sandbox.tenant_id is not None
    async with platform_session() as pdb:
        stmt = select(DeveloperSandboxMember).where(DeveloperSandboxMember.sandbox_id == sandbox.id)
        stmt = stmt.where(DeveloperSandboxMember.account_id == account_id) if account_id else stmt.where(DeveloperSandboxMember.owner_user_id == user_id)
        member = (await pdb.execute(stmt)).scalar_one_or_none()
    if member is not None:
        async with tenant_session(sandbox.tenant_id) as tdb:
            if await tdb.get(User, member.sandbox_user_id) is not None:
                return member.sandbox_user_id
    member_role = "viewer" if role == "viewer" else "admin"
    keys = (VIEWER_KEY,) if member_role == "viewer" else (TENANT_ADMIN, *INTEGRATION_ADMIN_KEYS)
    async with tenant_session(sandbox.tenant_id) as tdb:
        user = (await tdb.execute(select(User).where(User.email == email))).scalar_one_or_none()
        if user is None:
            user = User(id=uuid.uuid4(), tenant_id=sandbox.tenant_id, email=email, name=name[:200] or email, password_hash=hash_password(secrets.token_urlsafe(32)))
            tdb.add(user)
            await tdb.flush()
        await access_service.set_user_roles(tdb, sandbox.tenant_id, user.id, await _role_ids(tdb, keys))
        user_id_in_sandbox = user.id
    async with platform_session() as pdb:
        if member is None:
            pdb.add(DeveloperSandboxMember(id=uuid.uuid4(), sandbox_id=sandbox.id, account_id=account_id, owner_user_id=None if account_id else user_id,
                                           sandbox_user_id=user_id_in_sandbox, role=member_role))  # fmt: skip
        else:
            await pdb.execute(update(DeveloperSandboxMember).where(DeveloperSandboxMember.id == member.id).values(sandbox_user_id=user_id_in_sandbox, role=member_role))
    return user_id_in_sandbox


async def _creator(row: DeveloperSandbox) -> tuple[str, str, str] | None:
    """(email, name, role) of the creator."""
    if row.created_by_account_id is not None:
        async with platform_session() as db:
            a = await db.get(DeveloperAccount, row.created_by_account_id)
            role = (await db.execute(select(PartnerOrgMember.role).where(PartnerOrgMember.account_id == row.created_by_account_id,
                    PartnerOrgMember.partner_org_id == row.partner_org_id, PartnerOrgMember.removed_at.is_(None)))).scalar_one_or_none()  # fmt: skip
            return (str(a.email), a.name, role or "developer") if a else None
    if row.created_by_user_id is not None and row.owner_tenant_id is not None:
        async with tenant_session(row.owner_tenant_id) as db:
            u = await db.get(User, row.created_by_user_id)
            return (str(u.email), u.name, "developer") if u else None
    return None


async def _set(sandbox_id: uuid.UUID, **values: Any) -> DeveloperSandbox:
    async with platform_session() as db:
        await db.execute(update(DeveloperSandbox).where(DeveloperSandbox.id == sandbox_id).values(**values))
        row = await db.get(DeveloperSandbox, sandbox_id)
        assert row is not None
        await db.refresh(row)
        return row


async def run_create(sandbox_id: uuid.UUID) -> None:
    async with platform_session() as db:
        row = await db.get(DeveloperSandbox, sandbox_id)
    if row is None or row.status != "creating":
        return
    creator = await _creator(row)
    try:
        tenant_id = await _new_tenant(row)
        row = await _set(sandbox_id, tenant_id=tenant_id)
        if creator is not None:
            await ensure_member(row, account_id=row.created_by_account_id, user_id=row.created_by_user_id, email=creator[0], name=creator[1], role=creator[2])
        await _set(sandbox_id, status="ready", ready_at=now(), failure_code=None)
    except Exception as exc:
        logger.exception("Sandbox {} could not be created", sandbox_id)
        await _set(sandbox_id, status="failed", failure_code=type(exc).__name__[:60])
        if creator is not None:
            await notifier.sandbox_status(creator[0], row.name, ready=False)
        return
    if creator is not None:
        await notifier.sandbox_status(creator[0], row.name, ready=True)


async def _installed_apps(tenant_id: uuid.UUID) -> list[tuple[uuid.UUID, list[str], dict[str, str]]]:
    async with tenant_session(tenant_id) as db:
        rows = (await db.execute(select(AppInstallation).where(AppInstallation.kind == "app", AppInstallation.status != "uninstalled"))).scalars()
        return [(r.app_id, list(r.granted_scopes or []), dict(r.sensitive_purposes or {})) for r in rows if r.app_id is not None]


async def _drop_tenant(tenant_id: uuid.UUID) -> None:
    from app.services import tenant_lifecycle_service

    await tenant_lifecycle_service.delete_tenant_data(tenant_id)
    async with platform_session() as db:
        await db.execute(update(Tenant).where(Tenant.id == tenant_id).values(status="deleted"))
    clear_sandbox_cache(tenant_id)


async def run_reset(sandbox_id: uuid.UUID, reinstall_apps: bool = False) -> None:
    async with platform_session() as db:
        row = await db.get(DeveloperSandbox, sandbox_id)
        members = list((await db.execute(select(DeveloperSandboxMember).where(DeveloperSandboxMember.sandbox_id == sandbox_id))).scalars())
    if row is None or row.status != "resetting":
        return
    old = row.tenant_id
    apps = await _installed_apps(old) if (old and reinstall_apps) else []
    member_info: list[tuple[uuid.UUID | None, uuid.UUID | None, str, str, str]] = []
    if old is not None:
        async with tenant_session(old) as tdb:
            for m in members:
                u = await tdb.get(User, m.sandbox_user_id)
                if u is not None:
                    member_info.append((m.account_id, m.owner_user_id, str(u.email), u.name, "viewer" if m.role == "viewer" else "developer"))
        await _drop_tenant(old)
    async with platform_session() as db:
        await db.execute(DeveloperSandboxMember.__table__.delete().where(DeveloperSandboxMember.sandbox_id == sandbox_id))
    try:
        tenant_id = await _new_tenant(row)
        row = await _set(sandbox_id, tenant_id=tenant_id)
        for account_id, user_id, email, name, role in member_info:
            await ensure_member(row, account_id=account_id, user_id=user_id, email=email, name=name, role=role)
        if apps:
            creator = await _creator(row)
            if creator is not None:
                actor_user = await ensure_member(row, account_id=row.created_by_account_id, user_id=row.created_by_user_id, email=creator[0], name=creator[1], role=creator[2])
                for app_id, scopes, purposes in apps:
                    try:
                        await _install(row, actor_user, app_id, scopes, purposes)
                    except Exception:
                        logger.exception("Re-install of app {} in sandbox {} failed", app_id, sandbox_id)
        await _set(sandbox_id, status="ready", last_reset_at=now(), failure_code=None)
    except Exception as exc:
        logger.exception("Sandbox {} could not be reset", sandbox_id)
        await _set(sandbox_id, status="failed", failure_code=type(exc).__name__[:60])


async def run_delete(sandbox_id: uuid.UUID) -> None:
    async with platform_session() as db:
        row = await db.get(DeveloperSandbox, sandbox_id)
    if row is None or row.status != "deleting":
        return
    if row.tenant_id is not None:
        await _drop_tenant(row.tenant_id)
    await _set(sandbox_id, status="deleted", deleted_at=now())


# ================================================================ reset / delete / install
async def reset_sandbox(owner: Owner, sandbox_id: uuid.UUID, *, reinstall_apps: bool) -> SandboxOut:
    owner.require("developer")
    row = await get_owned(owner, sandbox_id)
    if row.status not in ("ready", "failed"):
        raise ConflictError("The sandbox is busy", code="sandbox_busy")
    await _set(sandbox_id, status="resetting")
    if owner.kind == "partner_org":
        await audit_log.record("sandbox.reset", partner_org_id=owner.id, account_id=owner.account_id, target_type="sandbox", target_id=sandbox_id, reinstall_apps=reinstall_apps)
    await _kick("reset", sandbox_id, reinstall_apps=reinstall_apps)
    return await _out(await get_owned(owner, sandbox_id))


async def delete_sandbox(owner: Owner, sandbox_id: uuid.UUID) -> SandboxOut:
    owner.require("developer")
    row = await get_owned(owner, sandbox_id)
    if row.status in ("creating", "resetting", "deleting"):
        raise ConflictError("The sandbox is busy", code="sandbox_busy")
    await _set(sandbox_id, status="deleting")
    if owner.kind == "partner_org":
        await audit_log.record("sandbox.deleted", partner_org_id=owner.id, account_id=owner.account_id, target_type="sandbox", target_id=sandbox_id)
    await _kick("delete", sandbox_id)
    async with platform_session() as db:
        fresh = await db.get(DeveloperSandbox, sandbox_id)
        assert fresh is not None
        return await _out(fresh)


async def member_user(owner: Owner, row: DeveloperSandbox) -> uuid.UUID:
    return await ensure_member(row, account_id=owner.account_id, user_id=owner.user_id if owner.kind == "tenant" else None,
                               email=owner.email, name=owner.name, role=owner.role)  # fmt: skip


async def sandbox_user_ctx(row: DeveloperSandbox, sandbox_user_id: uuid.UUID) -> RequestContext:
    from app.services import entity_refs

    assert row.tenant_id is not None
    async with tenant_session(row.tenant_id) as db:
        principal = await entity_refs.principal_for_user(db, row.tenant_id, sandbox_user_id)
    if principal is None:
        raise NotFound("Sandbox user not found")
    return RequestContext(tenant_id=row.tenant_id, principal=principal)


async def owner_app(owner: Owner, app_id: uuid.UUID) -> App:
    async with platform_session() as db:
        app = await db.get(App, app_id)
    if app is None:
        raise NotFound("App not found")
    if owner.kind == "partner_org" and not (app.publisher_kind == "partner" and app.partner_org_id == owner.id):
        raise NotFound("App not found")
    if owner.kind == "tenant" and not (app.publisher_kind == "tenant" and app.owner_tenant_id == owner.id):
        raise NotFound("App not found")
    return app


async def _install(row: DeveloperSandbox, sandbox_user_id: uuid.UUID, app_id: uuid.UUID, scopes: list[str], purposes: dict[str, str]) -> SandboxInstallOut:
    from app.schemas.marketplace import InstallIn
    from app.services import marketplace_service

    ctx = await sandbox_user_ctx(row, sandbox_user_id)
    async with platform_session() as db:
        app = await db.get(App, app_id)
        assert app is not None
        sha = app.manifest_sha256
    assert row.tenant_id is not None
    async with tenant_session(row.tenant_id) as tdb:
        out = await marketplace_service.install(ctx, tdb, InstallIn(kind="app", app_id=app_id, scopes=scopes, sensitive_purposes=purposes, accept_manifest_sha256=sha))
    return SandboxInstallOut(installation_id=out.id, status=out.status, granted_scopes=list(getattr(out, "granted_scopes", scopes) or scopes))


async def install_in_sandbox(owner: Owner, sandbox_id: uuid.UUID, data: SandboxInstallIn) -> SandboxInstallOut:
    """Install an App in the owner's sandbox as the developer's sandbox user (a human: the ★ grant is satisfied)."""
    owner.require("developer")
    row = await get_owned(owner, sandbox_id, require_ready=True)
    await owner_app(owner, data.app_id)
    user_id = await member_user(owner, row)
    out = await _install(row, user_id, data.app_id, list(data.scopes), dict(data.sensitive_purposes))
    if owner.kind == "partner_org":
        await audit_log.record("sandbox.app_installed", partner_org_id=owner.id, account_id=owner.account_id, target_type="sandbox", target_id=sandbox_id, app_id=data.app_id, scopes=list(data.scopes))
    return out


async def sandbox_owners(tenant_id: uuid.UUID) -> list[str]:
    """For Epic 79's inactivity purge warnings: the emails of the sandbox's owners (org owners, or the Tenant creator)."""
    async with platform_session() as db:
        row = (await db.execute(select(DeveloperSandbox).where(DeveloperSandbox.tenant_id == tenant_id, DeveloperSandbox.status != "deleted"))).scalar_one_or_none()
    if row is None:
        return []
    if row.partner_org_id is not None:
        return await notifier.org_recipients(row.partner_org_id, ("owner",))
    creator = await _creator(row)
    return [creator[0]] if creator else []


__all__ = [
    "create_sandbox",
    "delete_sandbox",
    "ensure_member",
    "get_owned",
    "install_in_sandbox",
    "list_sandboxes",
    "member_user",
    "owner_app",
    "reset_sandbox",
    "run_create",
    "run_delete",
    "run_reset",
    "sandbox_owners",
    "sandbox_user_ctx",
    "set_kicker",
]
