# ruff: file-ignore[line-too-long]
"""Public campus pages (Epic 43, FR-218 / FR-220, AD-53): ``/api/v1/public/campus/*``. No session -- the Secure Link
token is the credential and is never logged; responses are ``no-store``, ``no-referrer``, ``noindex``.

* ``GET  /public/campus/r/{token}`` -- drive registration page (purpose ``campus_registration``): drive,
  Institution, form schema, the active ``campus`` (and ``marketing``) notice, closes-at, prefill for personal invites.
* ``POST /public/campus/r/{token}`` -- multipart: ``payload`` (JSON ``PublicRegisterIn``) plus one file per ``file``
  form field (``file_<key>``). Bot challenge, per-IP and per-drive rate limits, notice ack (stale -> 409
  ``notice_outdated``), per-field 422.
* ``GET  /public/campus/e/{token}`` -- event registration page, or a personal check-in pass (purpose
  ``event_checkin``).
* ``POST /public/campus/e/{token}`` -- event registration (JSON).
"""

import json
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path, Request, Response

from app.core.context import get_context
from app.core.errors import ValidationFailed
from app.schemas.campus_public import (
    PublicDriveView,
    PublicEventRegisterIn,
    PublicEventRegisterOut,
    PublicEventView,
    PublicRegisterIn,
    PublicRegisterOut,
)
from app.services.campus import events, registration

router = APIRouter(prefix="/public/campus", tags=["public"])
PUBLIC_HEADERS = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer", "X-Robots-Tag": "noindex"}
MAX_FILES = 5
MAX_FILE_MB = 5


async def _headers(response: Response, token: Annotated[str, Path(min_length=20, max_length=200)]) -> str:  # NOSONAR
    response.headers.update(PUBLIC_HEADERS)
    return token


Token = Annotated[str, Depends(_headers)]


@router.get("/r/{token}", response_model=PublicDriveView)
async def drive_view(token: Token) -> Any:
    return await registration.public_view(token, ip=get_context().ip)


async def _multipart(request: Request) -> tuple[PublicRegisterIn, dict[str, tuple[bytes, str | None]]]:
    from app.services import files_service

    form = await request.form()
    raw = form.get("payload")
    if not isinstance(raw, str):
        raise ValidationFailed("payload is required", code="payload_required")
    try:
        payload = PublicRegisterIn.model_validate(json.loads(raw))
    except (ValueError, TypeError) as exc:
        errors = getattr(exc, "errors", None)
        fields = {".".join(str(p) for p in e["loc"]): e["msg"] for e in errors()} if callable(errors) else {}
        raise ValidationFailed("Check the highlighted fields", code="invalid_payload", details={"fields": fields}) from exc
    files: dict[str, tuple[bytes, str | None]] = {}
    for name, value in form.multi_items():
        if not name.startswith("file_") or isinstance(value, str):
            continue
        if len(files) >= MAX_FILES:
            raise ValidationFailed("Too many files", code="too_many_files")
        files[name.removeprefix("file_")] = (await files_service.read_upload(value, MAX_FILE_MB), value.filename)
    return payload, files


@router.post("/r/{token}", response_model=PublicRegisterOut)
async def drive_register(token: Token, request: Request) -> Any:
    ctype = request.headers.get("content-type", "")
    if ctype.startswith("application/json"):
        try:
            payload = PublicRegisterIn.model_validate(await request.json())
        except ValueError as exc:
            errors = getattr(exc, "errors", None)
            fields = {".".join(str(p) for p in e["loc"]): e["msg"] for e in errors()} if callable(errors) else {}
            raise ValidationFailed("Check the highlighted fields", code="invalid_payload", details={"fields": fields}) from exc
        files: dict[str, tuple[bytes, str | None]] = {}
    else:
        payload, files = await _multipart(request)
    ctx = get_context()
    return await registration.public_register(token, payload, files, ip=ctx.ip, user_agent=ctx.user_agent)


@router.get("/e/{token}", response_model=PublicEventView)
async def event_view(token: Token) -> Any:
    return await events.public_view(token, ip=get_context().ip)


@router.post("/e/{token}", response_model=PublicEventRegisterOut)
async def event_register(token: Token, body: PublicEventRegisterIn) -> Any:
    ctx = get_context()
    return await events.public_register(token, body, ip=ctx.ip, user_agent=ctx.user_agent)
