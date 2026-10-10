"""§5.3 offer status machine (AD-4, FR-65): the declared table, and every other (from, to, actor) is 409."""

import itertools

import pytest

from app.core.errors import DomainError
from app.services import offers_service
from app.services.offers.status import STATUSES, TRANSITIONS, check_transition

ACTORS = ("user", "candidate", "system", "workflow", "ai", "api_key")

EXPECTED: dict[tuple[str, str], set[str]] = {
    ("draft", "pending_approval"): {"user"},
    ("draft", "approved"): {"user"},
    ("pending_approval", "approved"): {"user"},
    ("pending_approval", "draft"): {"user"},
    ("approved", "released"): {"user"},
    ("released", "viewed"): {"candidate"},
    ("released", "accepted"): {"candidate"},
    ("viewed", "accepted"): {"candidate"},
    ("released", "declined"): {"candidate"},
    ("viewed", "declined"): {"candidate"},
    ("released", "expired"): {"system"},
    ("viewed", "expired"): {"system"},
    ("approved", "draft"): {"user"},
    ("released", "draft"): {"user"},
    ("viewed", "draft"): {"user"},
    **{(s, "withdrawn"): {"user"} for s in ("draft", "pending_approval", "approved", "released", "viewed")},
}


def test_table_matches_spec() -> None:
    assert {k: set(v) for k, v in TRANSITIONS.items()} == EXPECTED


@pytest.mark.parametrize(("pair", "actor"), [(p, a) for p, actors in EXPECTED.items() for a in actors])
def test_allowed_rows(pair: tuple[str, str], actor: str) -> None:
    check_transition(pair[0], pair[1], actor)


SWEEP = [
    (f, t, a)
    for f, t, a in itertools.product(STATUSES, STATUSES, ACTORS)
    if a not in EXPECTED.get((f, t), set())
]


@pytest.mark.parametrize(("from_status", "to_status", "actor"), SWEEP)
def test_everything_else_is_409(from_status: str, to_status: str, actor: str) -> None:
    with pytest.raises(DomainError) as info:
        check_transition(from_status, to_status, actor)
    assert info.value.status_code == 409


def test_automated_actors_never_allowed() -> None:
    for allowed in TRANSITIONS.values():
        assert not allowed & {"ai", "workflow", "api_key"}


@pytest.mark.parametrize("actor", ["ai", "workflow"])
async def test_human_only_probes(actor: str) -> None:
    for probe in offers_service.HUMAN_ONLY.values():
        with pytest.raises(DomainError) as info:
            await probe(actor)
        assert info.value.status_code == 409
