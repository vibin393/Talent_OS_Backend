"""Registrations of the ``sso`` domain (autodiscovered by ``core.hooks``; Epic 14).

* The password gate and admin-loss guard of ``auth_service`` (SSO enforcement, break-glass protection).
* Notification type ``sso.user_jit_provisioned`` (in-app + email to holders of ``sso:manage``) fanned out from
  ``UserJitProvisioned``.
* Epic 50: notification types ``client_sso.config_changed`` / ``client_sso.user_jit_provisioned`` (the
  Client's Account Manager + ``client_sso:manage`` holders), the Client Portal SSO gate of
  ``external_auth.signin`` and the settings-hub entry.
"""

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.events.sso import UserJitProvisioned
from app.services.notifications_service import (
    Content,
    NotificationType,
    register_event_source,
    register_notification_type,
)

JIT_TYPE = "sso.user_jit_provisioned"


async def _sso_admins(db: AsyncSession, payload: dict[str, Any]) -> set[uuid.UUID]:  # ruff: ignore[unused-function-argument]
    from app.services import identity_bridge

    return {row["id"] for row in await identity_bridge.users_with_permission(db, "sso:manage")}


async def _render(db: AsyncSession, payload: dict[str, Any]) -> Content:  # ruff: ignore[unused-function-argument]
    raw = payload.get("user_id")
    user_id = uuid.UUID(str(raw)) if raw else None
    return Content(
        title="A user was created through single sign-on",
        body="A new user signed in through your identity provider and was given the default role.",
        entity_type="user" if user_id else None,
        entity_id=user_id,
        link_path=f"/admin/users/{user_id}" if user_id else "/admin/users",
    )


register_notification_type(
    JIT_TYPE,
    NotificationType(
        label="User created by single sign-on",
        group="Administration",
        default_channel="both",
        mandatory_in_app=False,
        recipients_resolver=_sso_admins,
        render=_render,
    ),
)
register_event_source(UserJitProvisioned, JIT_TYPE)


def _register_identity_gates() -> None:
    from app.services import auth_service
    from app.services.sso.service import assert_break_glass_preserved, password_allowed

    auth_service.register_password_gate("sso", password_allowed)
    auth_service.register_admin_loss_guard("sso_break_glass", assert_break_glass_preserved)


_register_identity_gates()


# ================================================================ Epic 50: Client Portal SSO (AD-48)
CLIENT_CONFIG_TYPE = "client_sso.config_changed"
CLIENT_JIT_TYPE = "client_sso.user_jit_provisioned"


async def _client_sso_recipients(db: AsyncSession, payload: dict[str, Any]) -> set[uuid.UUID]:
    """Return the Client's Account Manager (``clients.owner_id``) and the holders of ``client_sso:manage``."""
    from sqlalchemy import select

    from app.models.clients import Client
    from app.services import identity_bridge

    out = {row["id"] for row in await identity_bridge.users_with_permission(db, "client_sso:manage")}
    raw = payload.get("client_id")
    if raw:
        owner = (
            await db.execute(select(Client.owner_id).where(Client.id == uuid.UUID(str(raw))))
        ).scalar_one_or_none()
        if owner is not None:
            out.add(owner)
    return out


def _client_link(payload: dict[str, Any]) -> tuple[uuid.UUID | None, str]:
    raw = payload.get("client_id")
    client_id = uuid.UUID(str(raw)) if raw else None
    return client_id, (f"/clients/{client_id}?tab=single-sign-on" if client_id else "/clients")


async def _render_config(db: AsyncSession, payload: dict[str, Any]) -> Content:  # ruff: ignore[unused-function-argument]
    client_id, link = _client_link(payload)
    change = str(payload.get("change") or "updated").replace("_", " ")
    return Content(
        title="A client's single sign-on settings changed",
        body=f"Client Portal single sign-on: {change}.",
        entity_type="client" if client_id else None,
        entity_id=client_id,
        link_path=link,
    )


async def _render_client_jit(db: AsyncSession, payload: dict[str, Any]) -> Content:  # ruff: ignore[unused-function-argument]
    client_id, link = _client_link(payload)
    return Content(
        title="A client portal user was created through single sign-on",
        body="A new person signed in with their company account and got the default portal permissions.",
        entity_type="client" if client_id else None,
        entity_id=client_id,
        link_path=link,
    )


def _register_client_sso() -> None:
    from app.events.sso import ClientIdentityProviderChanged, ClientUserJitProvisioned
    from app.services.external_auth import signin
    from app.services.settings_service import register_settings_entry
    from app.services.sso.client_admin import client_login_required

    register_notification_type(
        CLIENT_CONFIG_TYPE,
        NotificationType(
            label="Client single sign-on changed",
            group="Clients",
            default_channel="both",
            mandatory_in_app=False,
            recipients_resolver=_client_sso_recipients,
            render=_render_config,
            internal_only=True,
        ),
    )
    register_notification_type(
        CLIENT_JIT_TYPE,
        NotificationType(
            label="Client portal user created by single sign-on",
            group="Clients",
            default_channel="both",
            mandatory_in_app=False,
            recipients_resolver=_client_sso_recipients,
            render=_render_client_jit,
            internal_only=True,
        ),
    )
    register_event_source(ClientIdentityProviderChanged, CLIENT_CONFIG_TYPE)
    register_event_source(ClientUserJitProvisioned, CLIENT_JIT_TYPE)
    # FR-252: magic-link enforcement lives in external_auth; sso (above it, AD-32) supplies the decision.
    signin.register_client_sso_gate(client_login_required)
    register_settings_entry(
        "security_privacy",
        "client_sso",
        "Client single sign-on",
        "Per-client identity providers, SSO enforcement and break-glass users (Client 360 > Single sign-on).",
        "/api/v1/clients/{client_id}/sso/settings",
        "client_sso:manage",
        owner_epic="Epic 50",
    )


_register_client_sso()
