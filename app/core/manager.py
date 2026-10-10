from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI

from app.core.bot_challenge import check_configuration as check_bot_challenge
from app.core.database import dispose_engines
from app.core.mailer import get_mailer
from app.core.passwords import common_passwords


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[Any]:  # noqa: ARG001
    """
    To handles startup and shutdown events.
    """
    get_mailer()  # fail fast on a mailer misconfiguration (e.g. outbox mailer in production)
    common_passwords()  # fail fast if the bundled common-password list is missing or empty
    check_bot_challenge()  # Epic 9: BOT_CHALLENGE_PROVIDER=none is refused in production
    yield
    await dispose_engines()
