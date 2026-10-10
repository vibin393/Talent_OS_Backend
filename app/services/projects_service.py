"""Facade of the ``projects`` domain (Epic 36 contract layer, AD-39; filled by Epic 45)."""

from app.services.projects.service import (
    import_project,
    import_project_assignment,
    import_project_role,
    project_context,
    quick_search,
)

__all__ = [
    "import_project",
    "import_project_assignment",
    "import_project_role",
    "project_context",
    "quick_search",
]
