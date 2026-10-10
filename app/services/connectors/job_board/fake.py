"""Fake ``job_board`` adapter (Epic 66, spec 0.9): ``publish`` returns ``FJB-<n>`` live refs (idempotent per posting),
scripted status changes (``script["status"] = {"FJB-1": "expired"}``), ``fetch_applicants`` yields N synthetic
applicants (``script["applicants"]``, default 3; reserved ``example.test`` emails, fictional ``+1 555 555 01xx``
numbers, a generated PDF resume; pages of ``script["page_size"]``, default 2) and a small field taxonomy.
No network."""

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, ClassVar

from app.core.integrations.errors import ConnectorPermanentError
from app.core.integrations.fakes import FakeAdapterBase, tiny_pdf
from app.services.connectors.job_board.port import (
    BoardApplicantDTO,
    BoardFieldDTO,
    BoardRefDTO,
    FieldSchemaDTO,
    JobPostingDTO,
)

__all__ = ["EPOCH", "FakeJobBoard"]

EPOCH = datetime(2026, 1, 1, tzinfo=UTC)
_FIELDS = FieldSchemaDTO(
    fields=(
        BoardFieldDTO(
            "industry",
            "Industry",
            True,
            "select",
            (("it_services", "IT Services"), ("bfsi", "Banking & Financial Services"), ("manufacturing", "Manufacturing")),
        ),
        BoardFieldDTO(
            "functional_area",
            "Functional area",
            True,
            "select",
            (("engineering", "Engineering"), ("sales", "Sales"), ("finance", "Finance")),
        ),
        BoardFieldDTO("min_experience", "Minimum experience (years)", False, "number"),
    )
)


class FakeJobBoard(FakeAdapterBase):
    category = "job_board"
    provider_key = "fake_job_board"
    _seq: ClassVar[int] = 0
    _refs: ClassVar[dict[uuid.UUID, str]] = {}
    _boards: ClassVar[dict[str, dict[str, Any]]] = {}

    @classmethod
    def reset(cls) -> None:
        super().reset()
        cls._seq = 0
        cls._refs.clear()
        cls._boards.clear()

    def _dto(self, ref: str) -> BoardRefDTO:
        board = type(self)._boards.get(ref)
        if board is None:
            msg = "Unknown board reference"
            raise ConnectorPermanentError("board_ref_unknown", msg)
        status = (self.scripted().get("status") or {}).get(ref, board["status"])
        return BoardRefDTO(
            board_ref=ref,
            url=f"https://jobs.example.test/fake/{ref}",
            status=status,
            message="Rejected by the board (scripted)" if status == "rejected" else None,
            expires_at=board["expires_at"],
            views=board["views"],
            applies=board["applies"],
            credits_left=max(100 - type(self)._seq, 0),
        )

    async def publish(self, posting: JobPostingDTO) -> BoardRefDTO:
        self._check("publish", posting.posting_id)
        cls = type(self)
        ref = cls._refs.get(posting.posting_id)
        if ref is None:
            cls._seq += 1
            ref = f"FJB-{cls._seq}"
            cls._refs[posting.posting_id] = ref
            cls._boards[ref] = {"status": "live", "revision": posting.revision, "views": 0, "applies": 0}
        board = cls._boards[ref]
        board["revision"] = max(board["revision"], posting.revision)
        board["expires_at"] = posting.expires_at
        if board["status"] == "closed":
            board["status"] = "live"
        return self._dto(ref)

    async def update(self, board_ref: str, posting: JobPostingDTO) -> BoardRefDTO:
        self._check("update", board_ref)
        board = type(self)._boards.get(board_ref)
        if board is None:
            msg = "Unknown board reference"
            raise ConnectorPermanentError("board_ref_unknown", msg)
        board["revision"] = max(board["revision"], posting.revision)
        board["expires_at"] = posting.expires_at
        return self._dto(board_ref)

    async def close(self, board_ref: str) -> BoardRefDTO:
        self._check("close", board_ref)
        board = type(self)._boards.get(board_ref)
        if board is None:
            msg = "Unknown board reference"
            raise ConnectorPermanentError("board_ref_unknown", msg)
        board["status"] = "closed"
        return self._dto(board_ref)

    async def status(self, board_ref: str) -> BoardRefDTO:
        self._check("status", board_ref)
        return self._dto(board_ref)

    def _applicants(self) -> list[BoardApplicantDTO]:
        script = self.scripted()
        count = int(script.get("applicants", 3))
        board_ref = next(iter(type(self)._boards), "FJB-1")
        out: list[BoardApplicantDTO] = []
        for i in range(count):
            n = i + 1
            out.append(
                BoardApplicantDTO(
                    board_application_id=f"FJBA-{n}",
                    board_ref=board_ref,
                    applied_at=EPOCH + timedelta(hours=n),
                    full_name=f"Fake Applicant {n}",
                    email=f"applicant{n}@example.test",
                    phone_e164=f"+1555555{100 + (i % 100):04d}",
                    resume=tiny_pdf(f"Resume of Fake Applicant {n}"),
                    resume_filename=f"fake-applicant-{n}.pdf",
                    resume_media_type="application/pdf",
                    answers=(("notice_period", "30 days"),),
                    location="Bengaluru",
                    current_title="Software Engineer",
                    experience_years=Decimal(n),
                )
            )
        return out

    async def fetch_applicants(
        self, since: datetime | None, cursor: str | None
    ) -> tuple[Sequence[BoardApplicantDTO], str | None]:
        self._check("fetch_applicants", cursor)
        items = [a for a in self._applicants() if since is None or a.applied_at > since]
        size = max(int(self.scripted().get("page_size", 2)), 1)
        start = int(cursor) if cursor and cursor.isdigit() else 0
        page = items[start : start + size]
        nxt = str(start + size) if start + size < len(items) else None
        return page, nxt

    async def field_schema(self) -> FieldSchemaDTO:
        self._check("field_schema")
        return _FIELDS
