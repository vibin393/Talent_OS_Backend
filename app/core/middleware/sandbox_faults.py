# ruff: file-ignore[line-too-long]
"""Conformance-run fault injection (Epic 74, story 74.4, spec 0.6). Pure core: the decision is made by a resolver that
Epic 74's ``developer_portal`` registers (``register_fault_resolver``); without one this middleware is a no-op.

For ``/api/public/v1`` requests the resolver returns ``None`` (no-op: every production tenant, every sandbox without a
``running`` run for the calling Installation) or a ``FaultDecision``: an injected response (503 / 429 with
``Retry-After``) and / or an ``observe(status)`` callback recording the outcome for the run's checks. The resolver
checks both guards (``is_sandbox`` and an active run); this module never inspects tenants itself.
"""

import dataclasses
from collections.abc import Awaitable, Callable

from loguru import logger
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.errors import error_response
from app.core.public_api import PUBLIC_PREFIX


@dataclasses.dataclass(frozen=True, slots=True)
class InjectedResponse:
    status: int
    code: str
    message: str
    retry_after: int


@dataclasses.dataclass(frozen=True, slots=True)
class FaultDecision:
    inject: InjectedResponse | None = None
    observe: Callable[[int], Awaitable[None]] | None = None


FaultResolver = Callable[[str, str, dict[str, str]], Awaitable[FaultDecision | None]]
_resolver: dict[str, FaultResolver | None] = {"fn": None}


def register_fault_resolver(fn: FaultResolver | None) -> None:
    _resolver["fn"] = fn


class SandboxFaultMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        fn = _resolver["fn"]
        if scope["type"] != "http" or fn is None or not str(scope.get("path", "")).startswith(PUBLIC_PREFIX):
            await self.app(scope, receive, send)
            return
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        try:
            decision = await fn(scope["method"].upper(), scope["path"], headers)
        except Exception:
            logger.exception("Fault resolver failed; request passes through")
            decision = None
        if decision is None:
            await self.app(scope, receive, send)
            return
        if decision.inject is not None:
            inj = decision.inject
            response = error_response(inj.status, inj.code, inj.message, {"retry_after": inj.retry_after}, headers={"Retry-After": str(inj.retry_after)})
            await send({"type": "http.response.start", "status": inj.status,
                        "headers": [(k.encode("latin-1"), v.encode("latin-1")) for k, v in response.headers.items()]})  # fmt: skip
            await send({"type": "http.response.body", "body": response.body})
            if decision.observe is not None:
                await decision.observe(inj.status)
            return
        status_box: list[int] = []

        async def capture(message: Message) -> None:
            if message["type"] == "http.response.start":
                status_box.append(int(message["status"]))
            await send(message)

        await self.app(scope, receive, capture)
        if decision.observe is not None and status_box:
            try:
                await decision.observe(status_box[0])
            except Exception:
                logger.exception("Conformance observation failed")


__all__ = ["FaultDecision", "FaultResolver", "InjectedResponse", "SandboxFaultMiddleware", "register_fault_resolver"]
