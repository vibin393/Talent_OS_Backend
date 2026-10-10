"""Bot challenge verification (Epic 9, FR-48): Cloudflare Turnstile or hCaptcha behind one port.

* ``BotChallengeVerifier.verify(token, remote_ip) -> bool``.
* ``NoopVerifier`` always passes (dev / test; ``BOT_CHALLENGE_PROVIDER=none``), refused in production by
  ``check_configuration`` (called at startup, like the outbox-mailer check).
* ``TurnstileVerifier`` / ``HCaptchaVerifier`` post to the provider's ``siteverify`` with a 5 s timeout.
* ``verify_or_fail`` fails closed: a missing token, a negative answer or a provider error raise 422
  ``challenge_failed``.
"""

from typing import Protocol

import httpx
from loguru import logger

from app.core.errors import ValidationFailed
from app.core.settings import get_settings

TIMEOUT_SECONDS = 5.0
TURNSTILE_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"
HCAPTCHA_URL = "https://hcaptcha.com/siteverify"


class BotChallengeVerifier(Protocol):
    async def verify(self, token: str | None, remote_ip: str) -> bool: ...


class NoopVerifier:
    async def verify(self, token: str | None, remote_ip: str) -> bool:  # noqa: ARG002
        return True


class _SiteVerify:
    url = ""

    def __init__(self, secret: str) -> None:
        self.secret = secret

    async def verify(self, token: str | None, remote_ip: str) -> bool:
        if not token or not self.secret:
            return False
        data = {"secret": self.secret, "response": token}
        if remote_ip:
            data["remoteip"] = remote_ip
        async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
            response = await client.post(self.url, data=data)
            response.raise_for_status()
            return bool(response.json().get("success"))


class TurnstileVerifier(_SiteVerify):
    url = TURNSTILE_URL


class HCaptchaVerifier(_SiteVerify):
    url = HCAPTCHA_URL


class ChallengeConfigError(RuntimeError):
    pass


_override: BotChallengeVerifier | None = None


def set_verifier(verifier: BotChallengeVerifier | None) -> None:
    """Install a test verifier (None restores the settings-based factory)."""
    global _override  # process-wide test hook
    _override = verifier


def check_configuration() -> None:
    s = get_settings()
    if s.app_env == "production" and s.bot_challenge_provider == "none":
        msg = "BOT_CHALLENGE_PROVIDER=none is dev/test only and must not be used in production"
        raise ChallengeConfigError(msg)


def get_verifier() -> BotChallengeVerifier:
    if _override is not None:
        return _override
    check_configuration()
    s = get_settings()
    if s.bot_challenge_provider == "turnstile":
        return TurnstileVerifier(s.bot_challenge_secret)
    if s.bot_challenge_provider == "hcaptcha":
        return HCaptchaVerifier(s.bot_challenge_secret)
    return NoopVerifier()


def challenge_failed() -> ValidationFailed:
    return ValidationFailed("The bot challenge failed; please try again", code="challenge_failed")


async def verify_or_fail(token: str | None, remote_ip: str | None) -> None:
    try:
        ok = await get_verifier().verify(token, remote_ip or "")
    except ChallengeConfigError:
        raise
    except Exception as exc:  # provider down / timeout -> fail closed
        logger.warning("Bot challenge provider error: {}", type(exc).__name__)
        ok = False
    if not ok:
        raise challenge_failed()


def public_config() -> dict[str, str]:
    s = get_settings()
    return {"provider": s.bot_challenge_provider, "site_key": s.bot_challenge_site_key}
