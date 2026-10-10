"""Epic 17 A6: the duplicate rules are pure over loaded facts (FR-96)."""

import uuid
from datetime import UTC, datetime, timedelta

from app.services.vendors.review import Facts, OtherSubmissionFact, OwnershipFact, duplicate_findings

NOW = datetime(2026, 10, 5, 10, 0, tzinfo=UTC)
V, OTHER, CLIENT, DEMAND, CAND = (uuid.uuid4() for _ in range(5))


def _facts(**kw: object) -> Facts:
    base: dict[str, object] = {"vendor_id": V, "client_id": CLIENT, "demand_id": DEMAND, "at": NOW}
    base.update(kw)
    return Facts(**base)  # type: ignore[arg-type]


def test_no_facts_no_findings() -> None:
    assert duplicate_findings(_facts()) == []


def test_owned_by_other_vendor_for_same_client() -> None:
    own = OwnershipFact(CAND, OTHER, CLIENT, NOW + timedelta(days=30))
    found = duplicate_findings(_facts(matched_candidate_ids=frozenset({CAND}), ownerships=(own,)))
    assert [f.category for f in found] == ["owned_by_other_vendor"]


def test_ownership_ignored_when_expired_own_vendor_or_other_client() -> None:
    for own in (
        OwnershipFact(CAND, OTHER, CLIENT, NOW - timedelta(seconds=1)),
        OwnershipFact(CAND, V, CLIENT, NOW + timedelta(days=30)),
        OwnershipFact(CAND, OTHER, uuid.uuid4(), NOW + timedelta(days=30)),
    ):
        assert duplicate_findings(_facts(matched_candidate_ids=frozenset({CAND}), ownerships=(own,))) == []


def test_existing_candidate_needs_recent_activity() -> None:
    assert duplicate_findings(_facts(matched_candidate_ids=frozenset({CAND}))) == []
    found = duplicate_findings(
        _facts(matched_candidate_ids=frozenset({CAND}), recently_active_candidate_ids=frozenset({CAND}))
    )
    assert [f.category for f in found] == ["existing_candidate"]


def test_other_vendor_earlier_submission_same_demand() -> None:
    earlier = OtherSubmissionFact(uuid.uuid4(), OTHER, DEMAND, "submitted", NOW - timedelta(hours=1))
    later = OtherSubmissionFact(uuid.uuid4(), OTHER, DEMAND, "submitted", NOW + timedelta(hours=1))
    withdrawn = OtherSubmissionFact(uuid.uuid4(), OTHER, DEMAND, "withdrawn", NOW - timedelta(hours=1))
    other_demand = OtherSubmissionFact(
        uuid.uuid4(), OTHER, uuid.uuid4(), "submitted", NOW - timedelta(hours=1)
    )
    found = duplicate_findings(_facts(other_submissions=(earlier, later, withdrawn, other_demand)))
    assert [(f.category, f.submission_id) for f in found] == [
        ("submitted_by_other_vendor", earlier.submission_id)
    ]
