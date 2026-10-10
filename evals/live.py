# ruff: noqa: E501, N818
"""Live evaluation safety and rubric judging (Epic 65 story 65.5, FR-322, NFR-54).

* ``check_target(url)`` -- live seeds run only in the ephemeral ``EVAL_DATABASE_URL``: refused when it equals the app
  database, names ``talent_os_test``, or (in production) does not end in ``_evals``. Checked before any write.
* ``live_key()`` -- ``ANTHROPIC_API_KEY_LIVE`` (never the app key).
* ``judge(rubric, material)`` -- the ``fast`` tier scores one rubric item 0/1 with the fixed prompt
  ``eval_judge.v1.md`` (temperature 0); returns ``(score, model)``. Only ids, labels and tool names are judged.
"""

import json
import os
from pathlib import Path
from typing import Any

from sqlalchemy.engine import make_url

FORBIDDEN_DB = "talent_os_test"
JUDGE_PROMPT = Path(__file__).resolve().parent / "eval_judge.v1.md"
ANTHROPIC_HOST = "api.anthropic.com"


class UnsafeTarget(RuntimeError):
    """The live eval database is not an ephemeral eval database."""


def _db_name(url: str) -> str:
    return str(make_url(url).database or "")


def check_target(url: str | None) -> str:
    """Return the eval database name or raise ``UnsafeTarget`` (before any write)."""
    from app.core.settings import get_settings

    s = get_settings()
    if not url:
        msg = "EVAL_DATABASE_URL is not set"
        raise UnsafeTarget(msg)
    name = _db_name(url)
    if name == FORBIDDEN_DB:
        msg = f"refusing to run live evals against {FORBIDDEN_DB}"
        raise UnsafeTarget(msg)
    app_names = {s.db_name, _db_name(s.app_db_url()), _db_name(s.owner_db_url())}
    if name in app_names or url in (s.app_db_url(), s.owner_db_url(), os.environ.get("DATABASE_URL")):
        msg = "EVAL_DATABASE_URL must not be the application database"
        raise UnsafeTarget(msg)
    if s.app_env == "production" and not name.endswith("_evals"):
        msg = "in production the eval database name must end in _evals"
        raise UnsafeTarget(msg)
    return name


def live_key() -> str:
    return os.environ.get("ANTHROPIC_API_KEY_LIVE", "")


async def judge(rubric: str, material: dict[str, Any]) -> tuple[int, str]:
    """Score one rubric item 0/1 with the fast tier."""
    import anthropic

    from app.core.settings import get_settings

    model = get_settings().ai_model_fast
    client = anthropic.AsyncAnthropic(api_key=live_key(), timeout=60.0, max_retries=2)
    response = await client.messages.create(
        model=model,
        max_tokens=200,
        temperature=0,
        system=JUDGE_PROMPT.read_text(encoding="utf-8"),
        messages=[{"role": "user", "content": json.dumps({"rubric": rubric, "run": material}, default=str)}],
    )
    text = "".join(getattr(block, "text", "") for block in response.content)
    try:
        score = int(json.loads(text.strip()).get("score", 0))
    except (ValueError, AttributeError):
        score = 0
    return (1 if score == 1 else 0), str(getattr(response, "model", model))


__all__ = ["ANTHROPIC_HOST", "FORBIDDEN_DB", "UnsafeTarget", "check_target", "judge", "live_key"]
