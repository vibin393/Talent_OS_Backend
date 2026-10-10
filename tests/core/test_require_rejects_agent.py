"""Epic 53 (AD-60): HTTP permission dependencies never admit an agent principal, whatever it holds."""

import uuid

import pytest

from app.core.errors import Forbidden
from app.core.permissions import Principal, Scope, require, require_any

PERM = "user:read"


def _agent() -> Principal:
    return Principal(type="agent", id=uuid.uuid4(), tenant_id=uuid.uuid4(), permissions={PERM: Scope.TENANT})


def _user() -> Principal:
    return Principal(type="user", id=uuid.uuid4(), tenant_id=uuid.uuid4(), permissions={PERM: Scope.TENANT})


async def test_require_refuses_agent_principal() -> None:
    with pytest.raises(Forbidden):
        await require(PERM)(_agent())


async def test_require_any_refuses_agent_principal() -> None:
    with pytest.raises(Forbidden):
        await require_any(PERM, "role:read")(_agent())


async def test_same_grants_admit_a_user() -> None:
    user = _user()
    assert await require(PERM)(user) is user
    assert await require_any(PERM, "role:read")(user) is user
