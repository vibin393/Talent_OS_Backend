"""Derived Demand Progress (pure)."""

import pytest

from app.services.demands.progress import compute


@pytest.mark.parametrize(
    ("positions", "filled", "apps", "subs", "interviews", "offers", "expected"),
    [
        (1, 0, 0, 0, 0, 0, None),
        (1, 0, 1, 0, 0, 0, "recruiting"),
        (1, 0, 1, 1, 0, 0, "submitted"),
        (1, 0, 1, 1, 1, 0, "interviewing"),
        (1, 0, 0, 0, 0, 1, "offer"),
        (3, 1, 5, 4, 3, 2, "partially_filled"),
        (2, 2, 5, 4, 3, 2, "offer"),  # fully filled: not "partially"
        (2, 0, 0, 1, 0, 0, "submitted"),
    ],
)
def test_progress_table(
    positions: int, filled: int, apps: int, subs: int, interviews: int, offers: int, expected: str | None
) -> None:
    assert (
        compute(
            positions=positions,
            positions_filled=filled,
            applications=apps,
            submissions=subs,
            interviews_scheduled=interviews,
            offers_released=offers,
        )
        == expected
    )
