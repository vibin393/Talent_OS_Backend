"""``app.services.interviews_service`` -- public facade of the interviews domain (Epic 7, AD-1).

Routers and other domains use only these names:

* configuration: ``get_settings`` / ``update_settings``, kits (``list_kits`` ... ``archive_kit``), stage
  templates (``list_templates`` ... ``update_template``).
* plans: ``get_plan``, ``save_plan``, ``apply_template``.
* interviews: ``schedule_interview``, ``reschedule_interview``, ``cancel_interview``, ``mark_no_show``,
  ``complete_interview``, ``get_interview``, ``list_for_demand``, ``list_for_application``, ``list_mine``,
  ``ics_for``.
* scorecards: ``get_my_scorecard``, ``save_my_scorecard``, ``submit_my_scorecard``, ``list_scorecards``,
  ``public_scorecard_view``, ``public_scorecard_submit``.
* outcomes: ``record_stage_outcome``, ``select_application``, ``interview_summary``.
* Epic 6 board: ``board_badges(ctx, db, application_ids)`` (the pipeline service calls it; no reverse import).
* tasks: ``sync_invites``, ``send_due_reminders``, ``complete_elapsed``, ``scorecard_reminders``.
* FR-65: ``record_stage_outcome`` / ``select_application`` refuse non-human actors (403 ``human_only_action``).
"""

from app.services.interviews import handlers
from app.services.interviews.configuration import (
    archive_kit,
    create_kit,
    create_template,
    get_kit,
    get_settings,
    get_template,
    list_kits,
    list_templates,
    update_kit,
    update_settings,
    update_template,
)
from app.services.interviews.invites import (
    resync_event,
    scorecard_reminders,
    send_due_reminders,
    sync_invites,
)
from app.services.interviews.outcomes import (
    board_badges,
    interview_summary,
    record_stage_outcome,
    select_application,
    selection_eligible,
)
from app.services.interviews.plan import apply_template, get_plan, save_plan
from app.services.interviews.scheduling import (
    cancel_interview,
    complete_elapsed,
    complete_interview,
    get_interview,
    ics_for,
    list_for_application,
    list_for_demand,
    list_mine,
    mark_no_show,
    reschedule_interview,
    schedule_interview,
)
from app.services.interviews.scorecards import get_mine as get_my_scorecard
from app.services.interviews.scorecards import list_scorecards
from app.services.interviews.scorecards import public_submit as public_scorecard_submit
from app.services.interviews.scorecards import public_view as public_scorecard_view
from app.services.interviews.scorecards import save_draft as save_my_scorecard
from app.services.interviews.scorecards import submit_mine as submit_my_scorecard

SCORECARD_PURPOSE = "interview_scorecard"


def register() -> None:
    handlers.register()


register()

__all__ = [
    "SCORECARD_PURPOSE",
    "apply_template",
    "archive_kit",
    "board_badges",
    "cancel_interview",
    "complete_elapsed",
    "complete_interview",
    "create_kit",
    "create_template",
    "get_interview",
    "get_kit",
    "get_my_scorecard",
    "get_plan",
    "get_settings",
    "get_template",
    "ics_for",
    "interview_summary",
    "list_for_application",
    "list_for_demand",
    "list_kits",
    "list_mine",
    "list_scorecards",
    "list_templates",
    "mark_no_show",
    "public_scorecard_submit",
    "public_scorecard_view",
    "record_stage_outcome",
    "register",
    "reschedule_interview",
    "resync_event",
    "save_my_scorecard",
    "save_plan",
    "schedule_interview",
    "scorecard_reminders",
    "select_application",
    "selection_eligible",
    "send_due_reminders",
    "submit_my_scorecard",
    "sync_invites",
    "update_kit",
    "update_settings",
    "update_template",
]
