"""Job board distribution facade (Epic 70). Routers, agent tools and other domains call this module only."""

from app.services.job_distribution.board_sets import (
    create_set,
    delete_set,
    get_settings_out,
    list_mappings,
    list_sets,
    put_mappings,
    put_settings,
    schema_out,
    update_set,
)
from app.services.job_distribution.feed import regenerate as regenerate_feed
from app.services.job_distribution.feed import render as render_feed
from app.services.job_distribution.feed import settings_out as feed_settings
from app.services.job_distribution.feed import update_settings as update_feed_settings
from app.services.job_distribution.postings import (
    abandon,
    close,
    get,
    list_for_demand,
    mark_closed,
    pack,
    preview,
    publish,
    record,
    refresh,
    retry,
    update,
)
from app.services.job_distribution.queries import applicants, board_fields
from app.services.job_distribution.reporting import board_performance

__all__ = [
    "abandon",
    "applicants",
    "board_fields",
    "board_performance",
    "close",
    "create_set",
    "delete_set",
    "feed_settings",
    "get",
    "get_settings_out",
    "list_for_demand",
    "list_mappings",
    "list_sets",
    "mark_closed",
    "pack",
    "preview",
    "publish",
    "put_mappings",
    "put_settings",
    "record",
    "refresh",
    "regenerate_feed",
    "render_feed",
    "retry",
    "schema_out",
    "update",
    "update_feed_settings",
    "update_set",
]
