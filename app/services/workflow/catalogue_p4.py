# ruff: file-ignore[line-too-long]
"""Phase 4 workflow catalogue (Epic 53 contract, spec 0.15): the ``agents.start_run`` action key ("Start agent run",
FR-273). Imported at the end of ``catalogue`` so the key joins ``ACTION_CATALOGUE``. The ``ActionDef`` itself is
registered from ``services/agent_runtime/hooks.py`` with a callable -- ``workflow`` never imports Phase 4."""

from app.services.workflow.catalogue import ACTION_CATALOGUE

__all__ = ["P4_ACTION_CATALOGUE"]

P4_ACTION_CATALOGUE: dict[str, str] = {"agents.start_run": "53"}

ACTION_CATALOGUE.update(P4_ACTION_CATALOGUE)
