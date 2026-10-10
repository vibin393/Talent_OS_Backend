"""Facade of the ``workforce`` domain (Epic 44; AD-39 / AD-40). Other domains import from here only.

``plan_summary`` feeds the Epic 52 dashboards; ``import_plan_line`` is the go-live importer (foundation 0.7).
"""

from app.services.workforce.service import get_settings, import_plan_line, plan_summary, update_settings

__all__ = ["get_settings", "import_plan_line", "plan_summary", "update_settings"]
