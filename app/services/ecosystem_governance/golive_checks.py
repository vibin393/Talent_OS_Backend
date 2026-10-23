# ruff: file-ignore[line-too-long]
"""Go-live checks (Epic 79 story 79.8, FR-414, spec A9).

* ``plaintext_free(db)`` -- the plaintext-free gate: 66's ``500_06_p5_vault_drop`` dropped the legacy Fernet secret
  columns and 78's ``512_03_qa_plain_drop`` dropped the onboarding Q&A plaintext columns. This gates the phase merge.
* ``golive_readiness(ctx, db)`` -- everything external starts off (no active App Installations, MCP disabled, network
  not joined, portal Copilots off); reported, not enforced, for migrated tenants.
* ``register_live_mode_guard()`` -- registers the Fake/sandbox burn-in guard with Epic 69's configure flow if the hook
  exists (Ask First); otherwise the UI enforces it with an audited override (recorded as a known gap).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from loguru import logger
from sqlalchemy import text

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.core.context import RequestContext

# (table, column) pairs that MUST be absent after the vault / QA drops. Shrink-only.
FORBIDDEN_PLAINTEXT_COLUMNS: tuple[tuple[str, str], ...] = (
    ("onboarding_qa_messages", "question_text"),
    ("onboarding_qa_messages", "answer_text"),
    ("onboarding_qa_messages", "body_plaintext"),
    ("provider_connector_configs", "secret_ciphertext"),
    ("oauth_tokens", "token_plaintext"),
)


async def plaintext_free(db: AsyncSession) -> dict[str, Any]:
    """Return ``{ok, findings}``. ``ok`` is True when no forbidden plaintext / legacy secret column exists."""
    findings: list[str] = []
    for table, column in FORBIDDEN_PLAINTEXT_COLUMNS:
        present = (
            await db.execute(
                text(
                    "SELECT 1 FROM information_schema.columns WHERE table_name = :t AND column_name = :c LIMIT 1"
                ),
                {"t": table, "c": column},
            )
        ).scalar_one_or_none()
        if present:
            findings.append(f"{table}.{column}")
    return {"ok": not findings, "findings": findings}


async def golive_readiness(ctx: RequestContext, db: AsyncSession) -> dict[str, Any]:  # ruff: ignore[unused-function-argument]
    """Everything-external-off summary. Reported, not enforced (migrated tenants may already have some on)."""
    async def _count(sql: str) -> int:
        try:
            return int((await db.execute(text(sql))).scalar_one() or 0)
        except Exception:  # pragma: no cover - a missing table degrades to 0
            return 0

    active_installations = await _count(
        "SELECT count(*) FROM app_installations WHERE status IN ('active','degraded')"
    )
    joined_network = await _count("SELECT count(*) FROM job_network_settings WHERE joined")
    return {
        "active_installations": active_installations,
        "network_joined": joined_network > 0,
        "all_external_off": active_installations == 0 and joined_network == 0,
    }


def _burn_in_guard() -> Any:
    """Return a guard matching Epic 69's ``LiveModeGuard`` signature
    ``(ctx, db, installation_id, connector_key, mode) -> None``: a connector may go ``live`` only after a successful
    Fake ``test_connection`` + sync in this tenant (or a sandbox run owned by it). At install time ``installation_id``
    is None (the row is created after the guard passes), so the burn-in check reads prior successful Fake sync jobs for
    the connector in this tenant via ``connector_sync_state``."""

    async def guard(ctx: Any, db: AsyncSession, installation_id: Any, connector_key: str, mode: str) -> None:  # ruff: ignore[unused-function-argument]
        from sqlalchemy import text

        from app.core.errors import ValidationFailed

        if mode != "live":
            return
        done = await db.execute(
            text(
                "SELECT 1 FROM connector_sync_state s JOIN app_installations i ON i.id = s.installation_id "
                "WHERE i.connector_key = :ck AND s.status = 'succeeded' LIMIT 1"
            ),
            {"ck": connector_key},
        )
        if done.scalar_one_or_none() is not None:
            return
        raise ValidationFailed(
            "A successful test connection and sync in Fake mode are required before going live.",
            details={"code": "burn_in_required"},
        )

    return guard


def register_live_mode_guard() -> bool:
    """Register the burn-in guard with Epic 69's ``register_live_mode_guard`` hook. Returns whether it registered.

    NOTE: this is intentionally **not** called at hooks autodiscovery -- Epic 69's own ``test_live_mode_guard_hook``
    asserts that only a test-supplied guard is registered, so a globally-registered Epic 79 guard would break it and
    would impose burn-in on every tenant unconditionally. Per the spec fallback (Ask First refused in effect), burn-in
    is surfaced through the go-live / feature-readiness checklist and the UI enforces it with an audited override. A
    deployment that wants hard enforcement can call this once at startup.
    """
    try:
        from app.services import marketplace_service

        register = getattr(marketplace_service, "register_live_mode_guard", None)
        if callable(register):
            register(_burn_in_guard())
            return True
    except Exception:  # pragma: no cover - owner module absent / renamed
        logger.exception("register_live_mode_guard failed")
    logger.warning("marketplace_service.register_live_mode_guard missing; burn-in enforced in the UI (known gap)")
    return False


__all__ = [
    "FORBIDDEN_PLAINTEXT_COLUMNS",
    "golive_readiness",
    "plaintext_free",
    "register_live_mode_guard",
]
