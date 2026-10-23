"""Calendar / mailbox connections (Epic 7, story 7.4): the ``CalendarProvider`` port, its Google / Microsoft /
fake implementations, token encryption and a small key-value store (OAuth state, free/busy cache).

``get_provider(name)`` returns the implementation for ``google`` / ``microsoft``: the real one when its
client id and secret are set, the in-memory fake when ``CALENDAR_PROVIDER_FAKE`` is on, else None.
The business logic lives in ``app.services.connections_service``.
"""

from app.core.settings import get_settings
from app.services.connections import fake
from app.services.connections.provider import CalendarProvider

PROVIDERS: tuple[str, ...] = ("google", "microsoft")


def real_configured(name: str) -> bool:
    s = get_settings()
    if name == "google":
        return bool(s.google_oauth_client_id and s.google_oauth_client_secret)
    if name == "microsoft":
        return bool(s.ms_oauth_client_id and s.ms_oauth_client_secret)
    return False


def is_configured(name: str) -> bool:
    return name in PROVIDERS and (real_configured(name) or get_settings().calendar_provider_fake)


def get_provider(name: str) -> CalendarProvider | None:
    if name not in PROVIDERS:
        return None
    if real_configured(name):
        if name == "google":
            from app.services.connections.google import GoogleProvider

            return GoogleProvider()  # type: ignore[return-value]
        from app.services.connections.microsoft import MicrosoftProvider

        return MicrosoftProvider()  # type: ignore[return-value]
    if get_settings().calendar_provider_fake:
        return fake.instance()  # type: ignore[return-value]
    return None


__all__ = ["PROVIDERS", "get_provider", "is_configured", "real_configured"]
