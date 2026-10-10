# ruff: noqa: E501
"""Moderation guards (Epic 75, story 75.4). The DB-backed action tests run under the full harness; these cover the
human-only (★) and reason-length guards without a database."""

import uuid

import pytest

from app.core.context import RequestContext
from app.core.errors import Forbidden, ValidationFailed
from app.core.permissions import Principal
from app.services.job_network import moderation


def _automated_ctx(kind: str) -> RequestContext:
    return RequestContext(
        principal=Principal(type="platform", id=uuid.uuid4()),  # type: ignore[arg-type]
        actor_kind=kind,
    )


@pytest.mark.parametrize("kind", ["agent", "app", "api_key", "ai", "workflow", "system"])
def test_automated_actors_cannot_moderate(kind: str) -> None:
    with pytest.raises(Forbidden) as exc:
        moderation._operator_id(_automated_ctx(kind))
    assert exc.value.code == "human_only_action"


def test_non_platform_principal_cannot_moderate() -> None:
    ctx = RequestContext(principal=Principal(type="user", id=uuid.uuid4()), actor_kind="user")  # type: ignore[arg-type]
    with pytest.raises(Forbidden) as exc:
        moderation._operator_id(ctx)
    assert exc.value.code == "human_only_action"


def test_reason_must_be_at_least_ten_chars() -> None:
    with pytest.raises(ValidationFailed) as exc:
        moderation._reason("too short")
    assert exc.value.code == "reason_required"
    assert moderation._reason("A clear and sufficient reason") == "A clear and sufficient reason"
