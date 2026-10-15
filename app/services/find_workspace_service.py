"""Public "find my workspace" flow (Phase 2, Story 1.2).

A signed-out user enters their email and is emailed the workspace link(s) for any tenant(s) their email
belongs to. The flow is **enumeration-safe**: the caller always gets the same answer and the email is sent as
a background task, so neither the API response nor its timing reveals whether the email or any tenant exists.

Implementation note (scale): this scans every live tenant's own database (DB-per-tenant / silo, AD-29) with a
short existence query. That is fine for local / moderate scale. A production implementation would instead keep
an ``email -> tenant`` index in the control plane (hashed email) so no per-tenant fan-out is needed.
"""

from fastapi import BackgroundTasks
from loguru import logger
from sqlalchemy import literal, select

from app.core.database import tenant_session
from app.core.mailer import EmailMessage, send_email
from app.core.settings import get_settings
from app.core.tenant_registry import list_tenants
from app.models.identity import User


def _workspace_url(slug: str) -> str:
    """Build the workspace URL for a tenant slug: ``https://<slug>.<app_root_domain>`` (``http`` for local).

    Falls back to ``<frontend_url>/login`` when no subdomain root is configured (local path-mode dev)."""
    s = get_settings()
    if s.app_root_domain:
        scheme = "https" if s.app_env in ("production", "staging") else "http"
        return f"{scheme}://{slug}.{s.app_root_domain}"
    return f"{s.frontend_url.rstrip('/')}/login?workspace={slug}"


async def _matching_slugs(email: str) -> list[str]:
    """Slugs of live tenants that have an active user with ``email``. Never raises (enumeration-safe)."""
    needle = email.strip().lower()
    slugs: list[str] = []
    try:
        tenants = await list_tenants(only_live=True)
    except Exception:  # control plane unreachable: behave as "no matches", never leak the failure
        logger.exception("find-workspace: could not list tenants")
        return slugs
    for tenant in tenants:
        try:
            # One short existence query per tenant, routed to that tenant's own DB (silo) under its RLS context.
            async with tenant_session(tenant.tenant_id) as db:
                found = (
                    await db.execute(
                        select(literal(1))
                        .select_from(User)
                        .where(User.email == needle, User.is_active.is_(True))
                        .limit(1)
                    )
                ).first()
            if found is not None:
                slugs.append(tenant.slug)
        except Exception:  # a single tenant DB being down must not reveal anything or abort the scan
            logger.exception("find-workspace: lookup failed for tenant {}", tenant.slug)
    return slugs


def _build_email(email: str, slugs: list[str]) -> EmailMessage:
    urls = [_workspace_url(slug) for slug in slugs]
    lines = "\n".join(f"  - {url}" for url in urls)
    text = (
        "Here are your Talent OS workspaces:\n\n"
        f"{lines}\n\n"
        "Open a link to sign in. If you did not request this, you can ignore this email."
    )
    items = "".join(f'<li><a href="{url}">{url}</a></li>' for url in urls)
    html = (
        "<p>Here are your Talent OS workspaces:</p>"
        f"<ul>{items}</ul>"
        "<p>Open a link to sign in. If you did not request this, you can ignore this email.</p>"
    )
    return EmailMessage(
        to=email,
        subject="Your Talent OS workspaces",
        text=text,
        html=html,
        kind="find_workspace",
    )


async def _send(email: str, slugs: list[str]) -> None:
    try:
        await send_email(_build_email(email, slugs))
    except Exception:  # background task: never surface a send failure to the (already-returned) request
        logger.exception("find-workspace: could not send the workspaces email")


async def find_workspaces(email: str, background: BackgroundTasks) -> None:
    """Find the tenants ``email`` belongs to and queue an email with their workspace links.

    Returns nothing in every case. The email is sent only when there is at least one match, but the caller
    cannot tell: the scan time is independent of the result and the send happens after the response.
    """
    slugs = await _matching_slugs(email)
    if slugs:
        background.add_task(_send, email, slugs)
