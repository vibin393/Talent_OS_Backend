import re
import uuid

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from loguru import logger
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.api import build_api_router
from app.core import events
from app.core.config import setup_logger
from app.core.context import RequestContext, reset_context, set_context
from app.core.errors import internal_error_response, register_exception_handlers
from app.core.manager import lifespan
from app.core.middleware.rate_limit import AfterResponseMiddleware, RateLimitMiddleware, client_ip
from app.core.middleware.sandbox import SandboxHeaderMiddleware
from app.core.settings import get_settings
from app.router.base import router as base_router

REQUEST_ID_HEADER = "x-request-id"
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


class RequestContextMiddleware:
    """Creates the per-request ``RequestContext`` (request id, ip, user agent) and echoes X-Request-ID."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        incoming = headers.get(REQUEST_ID_HEADER, "")
        request_id = incoming if _REQUEST_ID_RE.fullmatch(incoming) else uuid.uuid4().hex
        # Epic 12: X-Forwarded-For is honoured only from TRUSTED_PROXIES.
        ctx = RequestContext(
            request_id=request_id, ip=client_ip(scope, headers), user_agent=headers.get("user-agent")
        )

        response_started = False

        async def send_with_request_id(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
                message.setdefault("headers", [])
                message["headers"].append((REQUEST_ID_HEADER.encode(), request_id.encode("latin-1")))
            await send(message)

        token = set_context(ctx)
        deferred_token = events.begin_request_scope()
        try:
            with logger.contextualize(request_id=request_id):
                try:
                    await self.app(scope, receive, send_with_request_id)
                except Exception as exc:
                    # Handle here (not in Starlette's outermost ServerErrorMiddleware) so the 500 keeps
                    # this request's id and flows back out through CORS.
                    logger.opt(exception=exc).error("Unhandled error (request_id={})", request_id)
                    if response_started:
                        raise
                    await internal_error_response()(scope, receive, send_with_request_id)
        finally:
            # Epic 13 safety net: eager tasks deferred by after-commit handlers and not yet flushed.
            events.end_request_scope(deferred_token)
            reset_context(token)


def _install_epic12() -> None:
    """Epic 12 startup wiring (idempotent): the privacy consent checker replaces the gateway's allow-all
    default, privacy event handlers, and the combined principal resolver (API keys, support sessions)."""
    from app.dependencies import auth as auth_dependencies
    from app.services import privacy

    privacy.install()
    auth_dependencies.install()


def _install_epic13() -> None:
    """Phase 2 foundation: import every ``app/services/**/hooks.py`` once (registries, AD-31)."""
    from app.core.hooks import autodiscover

    autodiscover("app.services")


def create_app() -> FastAPI:
    settings = get_settings()
    setup_logger(settings.debug)

    # Starlette's debug mode would replace the 500 error envelope with a traceback page, so it stays off;
    # DEBUG only raises log verbosity. Unhandled errors are logged with their traceback.
    is_prod = settings.app_env == "production"
    app = FastAPI(
        title="Talent OS API",
        lifespan=lifespan,
        docs_url=None if is_prod else "/api/docs",
        openapi_url=None if is_prod else "/openapi.json",
    )

    register_exception_handlers(app)
    _install_epic12()
    _install_epic13()

    # Last added = outermost: CORS wraps the request-context middleware, which wraps rate limiting
    # (Epic 12, NFR-16; before any handler / password hashing) and the after-response hooks.
    # Epic 74 (spec 0.6): conformance-run fault injection on /api/public/v1 (no-op unless a sandbox run is active).
    from app.core.middleware.sandbox_faults import SandboxFaultMiddleware

    app.add_middleware(SandboxFaultMiddleware)
    app.add_middleware(SandboxHeaderMiddleware)  # Epic 66 (FR-339): innermost, sees the authenticated tenant
    app.add_middleware(AfterResponseMiddleware)
    app.add_middleware(RateLimitMiddleware)
    app.add_middleware(RequestContextMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=[
            "X-Request-ID",
            "X-Export-Rows",
            "X-Export-Truncated",
            "X-Export-Id",
            "X-TOS-Masked-Columns",
            "Content-Disposition",
            "Retry-After",
            "X-RateLimit-Limit",
            "X-RateLimit-Remaining",
            "X-RateLimit-Reset",
            "X-TalentOS-Sandbox",
        ],
    )

    app.include_router(base_router)  # legacy /api/health, /api/pulse (frontend status page)
    app.include_router(build_api_router())
    # Epic 66 (AD-77): the Public API v1 surface (separate from /api/v1; served as /public/v1 by the frontend rewrite).
    from app.router.public_v1 import build_public_api_router

    app.include_router(build_public_api_router())
    # Epic 67: OAuth protocol endpoints (/api/oauth/*, /.well-known/oauth-authorization-server); not under /api/v1.
    from app.router import oauth as oauth_router

    app.include_router(oauth_router.router)
    # Epic 73: the MCP endpoint (/api/mcp) and RFC 9728 metadata (/.well-known/oauth-protected-resource[/mcp]).
    from app.router import mcp as mcp_router

    app.include_router(mcp_router.router)
    return app


app = create_app()
