# ruff: noqa: E501
"""Controlled tools (Epic 53, AD-59): ``app/services/agent_tools/<domain>/tools_e<NN>.py`` wrap domain facades as
``ToolSpec``s. Each domain package's ``hooks.py`` (a copy of ``_template_hooks.py``) loads every ``tools_*.py``."""
