# ruff: file-ignore[line-too-long]
"""Analytics answers facade (Epic 64). Used by the ``agent_tools.reporting`` tools, the ``/analytics/answers`` router and
the maintenance task."""

from app.services.analytics_answers.cells import min_cell_size, register_min_cell_size
from app.services.analytics_answers.describe import describe
from app.services.analytics_answers.purge import purge_expired
from app.services.analytics_answers.service import (
    ANSWER_RETENTION_DAYS,
    MODEL_ROWS_MAX,
    compact_catalogue,
    conversation_of_run,
    explain,
    get_answer,
    get_report,
    list_answers,
    list_reports,
    mark_saved,
    preview_answer,
    run_question,
    validate_only,
)

__all__ = [
    "ANSWER_RETENTION_DAYS",
    "MODEL_ROWS_MAX",
    "compact_catalogue",
    "conversation_of_run",
    "describe",
    "explain",
    "get_answer",
    "get_report",
    "list_answers",
    "list_reports",
    "mark_saved",
    "min_cell_size",
    "preview_answer",
    "purge_expired",
    "register_min_cell_size",
    "run_question",
    "validate_only",
]
