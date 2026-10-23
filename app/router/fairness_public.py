# ruff: file-ignore[line-too-long]
"""Public bias-audit endpoints (Epic 49): ``/api/v1/public/fairness/{slug}/*`` -- no principal.

* ``GET  self-id-form`` / ``POST self-id`` -- the optional self-ID step on the apply success page (apply token,
  submitted session, single use, rate-limited ``self_id_submit`` 10/h per IP). Nothing about self-ID is required to
  apply.
* ``GET  notice?posting_id=`` -- the NYC LL144 notice for a flagged posting (``applies=false`` otherwise).
* ``POST alternative-request`` -- alternative selection process request from the apply success page.
* ``GET  summary`` -- published bias-audit summaries for the Career Portal page.
"""

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path, Query, Response

import app.models.fairness  # ruff: ignore[unused-import]  - fairness tables on Base.metadata at app start (AD-47)  # noqa: F401
from app.core import rate_limit
from app.core.context import get_context
from app.schemas.fairness import (
    PublicAlternativeRequestIn,
    PublicNoticeOut,
    PublicPublicationOut,
    PublicSelfIdIn,
    ReceivedOut,
    SelfIdFormOut,
)
from app.services.fairness import alternatives, ll144, publications, self_id
from app.services.portal.common import ip_hash

router = APIRouter(prefix="/public/fairness/{slug}", tags=["public"])
Slug = Annotated[str, Path(min_length=1, max_length=63)]
PUBLIC_HEADERS = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}
SUBMIT_PER_HOUR = 10
READS_PER_MINUTE = 120


async def _read_guard(response: Response) -> None:
    response.headers.update(PUBLIC_HEADERS)
    await rate_limit.enforce("fairness_public_read", ip_hash(get_context().ip), READS_PER_MINUTE, 60)


async def _write_guard(response: Response) -> None:
    response.headers.update(PUBLIC_HEADERS)
    await rate_limit.enforce("self_id_submit", ip_hash(get_context().ip), SUBMIT_PER_HOUR, 3600)


ReadGuard = Annotated[None, Depends(_read_guard)]
WriteGuard = Annotated[None, Depends(_write_guard)]


@router.get("/self-id-form", response_model=SelfIdFormOut)
async def self_id_form(slug: Slug, _: ReadGuard) -> Any:
    return await self_id.apply_form(slug)


@router.post("/self-id", response_model=ReceivedOut)
async def submit_self_id(slug: Slug, body: PublicSelfIdIn, _: WriteGuard) -> Any:
    await self_id.upsert_from_apply(slug, body, get_context().ip)
    return ReceivedOut()


@router.get("/notice", response_model=PublicNoticeOut)
async def notice(slug: Slug, _: ReadGuard, posting_id: Annotated[uuid.UUID, Query()]) -> Any:
    return await ll144.public_notice(slug, posting_id)


@router.post("/alternative-request", response_model=ReceivedOut)
async def alternative_request(slug: Slug, body: PublicAlternativeRequestIn, _: WriteGuard) -> Any:
    await alternatives.from_apply(slug, body)
    return ReceivedOut()


@router.get("/summary", response_model=list[PublicPublicationOut])
async def summary(slug: Slug, _: ReadGuard) -> Any:
    return await publications.public_summaries(slug)
