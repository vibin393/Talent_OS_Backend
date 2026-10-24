"""Manual ``job_board`` adapter (Epic 66, spec 0.9, FR-364): every publish / update / close raises
``ManualActionRequired`` with a board-formatted copy-paste pack; applicants arrive through the careers site or a manual
import (the per-tenant XML feed is Epic 70's)."""

import html
import re
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from app.core.integrations.context import AdapterContext
from app.core.integrations.errors import ManualActionRequired
from app.services.connectors.job_board.port import (
    BoardApplicantDTO,
    BoardRefDTO,
    FieldSchemaDTO,
    JobPostingDTO,
)

__all__ = ["ManualJobBoard", "copy_paste_pack"]

_TAGS = re.compile(r"<[^>]+>")


def _text(description_html: str) -> str:
    text = re.sub(r"<\s*(br|/p|/li|/h\d)\s*/?>", "\n", description_html, flags=re.IGNORECASE)
    return html.unescape(_TAGS.sub("", text)).strip()


def copy_paste_pack(posting: JobPostingDTO) -> dict[str, Any]:
    loc = posting.location
    pack: dict[str, Any] = {
        "title": posting.title,
        "description": _text(posting.description_html),
        "location": ", ".join(p for p in (loc.city, loc.region, loc.country) if p) if loc else "",
        "work_mode": posting.work_mode,
        "employment_type": posting.employment_type,
        "experience": (
            f"{posting.experience_min_years or 0}-{posting.experience_max_years or ''} years".rstrip("- ")
            if posting.experience_min_years is not None or posting.experience_max_years is not None
            else ""
        ),
        "skills": ", ".join(posting.skills),
        "openings": posting.openings,
        "apply_url": posting.apply_url,
    }
    if posting.salary is not None:
        s = posting.salary
        pack["salary"] = f"{s.currency} {s.min_amount or ''}-{s.max_amount or ''} per {s.period}"
    if posting.company_name:
        pack["company"] = posting.company_name
    return pack


class ManualJobBoard:
    category = "job_board"
    provider_key = "manual"
    key = "manual"

    def __init__(self, ctx: AdapterContext | None = None) -> None:
        self.ctx = ctx

    async def handle(self, op: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:  # ruff: ignore[unused-method-argument]
        raise ManualActionRequired(f"job_board: complete {op} manually", {"op": op})

    def verify_webhook(self, headers: Mapping[str, str], body: bytes) -> bool:  # ruff: ignore[unused-method-argument]
        return False

    def parse_webhook(self, headers: Mapping[str, str], body: bytes) -> Mapping[str, Any]:  # ruff: ignore[unused-method-argument]
        return {}

    async def publish(self, posting: JobPostingDTO) -> BoardRefDTO:
        raise ManualActionRequired(
            "Post this job on the board by hand, then record the board reference",
            {"op": "publish", "posting_id": str(posting.posting_id), "pack": copy_paste_pack(posting)},
        )

    async def update(self, board_ref: str, posting: JobPostingDTO) -> BoardRefDTO:
        raise ManualActionRequired(
            "Update the job on the board by hand",
            {"op": "update", "board_ref": board_ref, "pack": copy_paste_pack(posting)},
        )

    async def close(self, board_ref: str) -> BoardRefDTO:
        raise ManualActionRequired("Close the job on the board by hand", {"op": "close", "board_ref": board_ref})

    async def status(self, board_ref: str) -> BoardRefDTO:  # NOSONAR
        return BoardRefDTO(
            board_ref=board_ref,
            url=None,
            status="pending",
            message="Managed manually",
            expires_at=None,
            views=None,
            applies=None,
            credits_left=None,
        )

    async def fetch_applicants(  # NOSONAR
        self,
        since: datetime | None,  # ruff: ignore[unused-method-argument]
        cursor: str | None,  # ruff: ignore[unused-method-argument]
    ) -> tuple[Sequence[BoardApplicantDTO], str | None]:
        return (), None

    async def field_schema(self) -> FieldSchemaDTO:  # NOSONAR
        return FieldSchemaDTO(fields=())
