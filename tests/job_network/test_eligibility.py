"""Pure eligibility decision (Epic 75, spec A2)."""

import uuid

from app.services.job_network.listings import EligibilityInput, eligible

CLIENT = uuid.uuid4()


def _inp(**over: object) -> EligibilityInput:
    base: dict[str, object] = {
        "is_live": True, "visibility": "external", "client_id": CLIENT, "exclude_client_ids": (),
        "joined": True, "member_status": "active", "verification_status": "verified", "sandbox": False,
        "list_mode": "all", "include_choice": None, "live_count": 0, "listing_limit": 500,
    }
    base.update(over)
    return EligibilityInput(**base)  # type: ignore[arg-type]


def test_happy_path_all_mode() -> None:
    ok, reasons = eligible(_inp())
    assert ok and reasons == []


def test_internal_visibility_excluded() -> None:
    ok, reasons = eligible(_inp(visibility="internal"))
    assert not ok and "internal_visibility" in reasons


def test_not_live() -> None:
    ok, reasons = eligible(_inp(is_live=False))
    assert not ok and "not_live" in reasons


def test_client_excluded() -> None:
    ok, reasons = eligible(_inp(exclude_client_ids=(CLIENT,)))
    assert not ok and "client_excluded" in reasons


def test_not_joined() -> None:
    ok, reasons = eligible(_inp(joined=False))
    assert not ok and "not_joined" in reasons


def test_member_pending() -> None:
    ok, reasons = eligible(_inp(verification_status="pending"))
    assert not ok and "member_pending" in reasons


def test_member_suspended() -> None:
    ok, reasons = eligible(_inp(member_status="suspended"))
    assert not ok and "member_suspended" in reasons


def test_sandbox() -> None:
    ok, reasons = eligible(_inp(sandbox=True))
    assert not ok and "sandbox" in reasons


def test_choose_mode_requires_explicit_include() -> None:
    ok, reasons = eligible(_inp(list_mode="choose", include_choice=None))
    assert not ok and "opted_out" in reasons
    ok2, _ = eligible(_inp(list_mode="choose", include_choice=True))
    assert ok2


def test_explicit_opt_out_wins_over_all_mode() -> None:
    ok, reasons = eligible(_inp(list_mode="all", include_choice=False))
    assert not ok and "opted_out" in reasons


def test_limit_reached_without_explicit_include() -> None:
    ok, reasons = eligible(_inp(live_count=500, listing_limit=500))
    assert not ok and "limit_reached" in reasons


def test_explicit_include_bypasses_limit_reason() -> None:
    ok, reasons = eligible(_inp(include_choice=True, live_count=500, listing_limit=500, list_mode="choose"))
    assert ok and "limit_reached" not in reasons
