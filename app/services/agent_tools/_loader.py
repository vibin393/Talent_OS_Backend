# ruff: file-ignore[line-too-long]
"""``load_tool_modules(package)`` -- imports every ``tools_*.py`` (and, Epic 73, ``exposure_*.py``) module of an ``agent_tools`` domain package (Epic 53).
Epics add tool files; nobody edits a domain package's ``hooks.py``."""

import importlib
import pkgutil


def load_tool_modules(package: str | None) -> list[str]:
    if not package:
        return []
    root = importlib.import_module(package)
    names = sorted(
        f"{package}.{info.name}"
        for info in pkgutil.iter_modules(list(getattr(root, "__path__", [])))
        if not info.ispkg and info.name.startswith(("tools_", "exposure_"))  # Epic 73: + exposure_*.py
    )
    for name in names:
        importlib.import_module(name)
    return names


__all__ = ["load_tool_modules"]
