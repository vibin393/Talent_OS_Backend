"""Live FX feed settings and runs (Epic 51, FR-255)."""

import uuid
from datetime import date, datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

Precedence = Literal["manual_first", "feed_first"]


class FxFeedSettingsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool
    pairs: Annotated[list[str], Field(max_length=30)]
    precedence: Precedence = "manual_first"
    max_stale_days: Annotated[int, Field(ge=1, le=7)] = 3
    fetch_hour_ist: Annotated[int, Field(ge=0, le=23)] = 10

    @field_validator("pairs")
    @classmethod
    def _pairs(cls, value: list[str]) -> list[str]:
        out: list[str] = []
        for raw in value:
            pair = raw.strip().upper().replace(" ", "")
            parts = pair.split("/")
            if len(parts) != 2 or any(len(p) != 3 or not p.isalpha() for p in parts) or parts[0] == parts[1]:
                msg = f"invalid currency pair {raw!r} (use e.g. USD/INR)"
                raise ValueError(msg)
            if pair not in out:
                out.append(pair)
        return sorted(out)


class FxFeedRunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    on_date: date
    status: Literal["ok", "partial", "failed"]
    provider_key: str
    fetched: int
    inserted: int
    skipped_manual: int
    error: str | None
    started_at: datetime
    finished_at: datetime | None


class FxFeedSettingsOut(BaseModel):
    enabled: bool
    pairs: list[str]
    precedence: Precedence
    max_stale_days: int
    fetch_hour_ist: int
    provider_key: str
    consecutive_failures: int
    last_run: FxFeedRunOut | None = None
