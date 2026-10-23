# ruff: file-ignore[line-too-long]
"""``GET /public/v1/changelog`` (Epic 67, spec A5): the machine-readable v1 changelog. Public, no auth, cached 1 h.
Not a ``public:*`` resource route (it carries no data and needs no scope), so it is kept out of the pinned contract."""

from pathlib import Path
from typing import Any

import yaml
from fastapi import APIRouter
from fastapi.responses import JSONResponse

CHANGELOG = Path(__file__).resolve().parents[2] / "core" / "public_api_changelog.yaml"

router = APIRouter(tags=["public-changelog"], include_in_schema=False)


def load_changelog() -> list[dict[str, Any]]:
    data = yaml.safe_load(CHANGELOG.read_text(encoding="utf-8")) or []
    return [dict(e) for e in data]


@router.get("/changelog")
async def changelog() -> JSONResponse:
    return JSONResponse(
        {"api_version": "v1", "entries": load_changelog()},
        headers={"Cache-Control": "public, max-age=3600"},
    )
