"""Candidates service package (Epic 3): records, skills catalog, resumes/parsing, duplicates/merge,
bulk import, tags and saved lists.

Public API used by routers and other services (import from here):
``records``, ``skills`` (``resolve_skill``), ``parsing``, ``duplicates`` (``find_duplicates``,
``link_by_exact_email``), ``merge``, ``imports``, ``tags_lists``.
"""

from app.services.candidates import access, duplicates, imports, merge, parsing, records, skills, tags_lists
from app.services.candidates.duplicates import find_duplicates, link_by_exact_email
from app.services.candidates.records import list_query  # Epic 11: list endpoint + export source
from app.services.candidates.skills import resolve_skill

access.register()

# Epic 5: candidate change events (flush listener) + thin read port for search / ai / matching.
from app.services.candidates import change_events, search_port  # noqa: E402

change_events.register()

__all__ = [
    "duplicates",
    "find_duplicates",
    "imports",
    "link_by_exact_email",
    "list_query",
    "merge",
    "parsing",
    "records",
    "resolve_skill",
    "skills",
    "tags_lists",
]

__all__ += ["change_events", "search_port"]

# Epic 35 (FR-185): go-live Worker import (find by email / phone, else create with ``engagement`` consent).
from app.services.candidates.golive_import import find_or_create_for_import  # noqa: E402

__all__ += ["find_or_create_for_import"]
