"""Demand reason codes (story 4.1/4.2). Every list includes ``other``."""

HOLD_REASONS: tuple[str, ...] = (
    "client_paused",
    "budget_pending",
    "requirement_changing",
    "awaiting_client_feedback",
    "other",
)
CANCEL_REASONS: tuple[str, ...] = (
    "client_cancelled",
    "budget_withdrawn",
    "duplicate",
    "position_eliminated",
    "created_in_error",
    "other",
)
CLOSE_REASONS: tuple[str, ...] = (
    "filled",
    "filled_by_client",
    "filled_by_competitor",
    "client_closed",
    "no_suitable_candidates",
    "other",
)

REASON_CODES: dict[str, tuple[str, ...]] = {
    "on_hold": HOLD_REASONS,
    "cancelled": CANCEL_REASONS,
    "closed": CLOSE_REASONS,
}

REQ_SEQUENCE_KEY = "REQ"
MAX_INTAKE_BYTES = 10 * 1024 * 1024
MAX_INTAKE_PAGES = 10
MAX_INTAKE_CHARS = 60_000
