# ruff: noqa: E501
"""XML job feed "board" (``job_feed``, Epic 70 spec A2 / A6): no outbound call. ``publish`` / ``close`` only toggle
inclusion -- the Board Posting row is the flag; the public route renders the feed. Deterministic, no network, so it
serves as the manifest's ``fake`` adapter too (a feed has no live mode to egress from)."""

from collections.abc import Sequence
from datetime import datetime

from app.core.integrations.fakes import FakeAdapterBase
from app.services.connectors.job_board.port import (
    BoardApplicantDTO,
    BoardRefDTO,
    FieldSchemaDTO,
    JobPostingDTO,
)

__all__ = ["JobFeedAdapter"]


class JobFeedAdapter(FakeAdapterBase):
    category = "job_board"
    provider_key = "job_feed"

    async def publish(self, posting: JobPostingDTO) -> BoardRefDTO:
        self._check("publish", posting.posting_id)
        ref = f"FEED-{posting.posting_id}"
        return BoardRefDTO(ref, None, "live", None, posting.expires_at, None, None, None)

    async def update(self, board_ref: str, posting: JobPostingDTO) -> BoardRefDTO:
        self._check("update", board_ref)
        return BoardRefDTO(board_ref, None, "live", None, posting.expires_at, None, None, None)

    async def close(self, board_ref: str) -> BoardRefDTO:
        self._check("close", board_ref)
        return BoardRefDTO(board_ref, None, "closed", None, None, None, None, None)

    async def status(self, board_ref: str) -> BoardRefDTO:
        self._check("status", board_ref)
        return BoardRefDTO(board_ref, None, "live", None, None, None, None, None)

    async def fetch_applicants(
        self,
        since: datetime | None,  # noqa: ARG002
        cursor: str | None,  # noqa: ARG002
    ) -> tuple[Sequence[BoardApplicantDTO], str | None]:
        return (), None

    async def field_schema(self) -> FieldSchemaDTO:
        self._check("field_schema")
        return FieldSchemaDTO(fields=())
