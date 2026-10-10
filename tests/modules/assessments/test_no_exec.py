"""Epic 40 A7 no-execution guard (CI): candidate code is NEVER executed on our servers or workers.

An AST scan of ``app/services/assessments/**``, ``app/services/connectors/assessment/**`` and the assessment
routers fails on calls to the builtins ``exec`` / ``eval`` / ``compile`` / ``__import__``, on imports of process,
interpreter, container or sandbox modules, and on ``os.system`` / ``os.popen`` / ``os.exec*`` / ``os.spawn*``.
Attribute calls such as ``re.compile`` are allowed. A deliberately added ``subprocess`` import fails the scan (proved
on a synthetic source below).
"""

import ast
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[3]
SCANNED = [
    *sorted((BACKEND / "app" / "services" / "assessments").rglob("*.py")),
    *sorted((BACKEND / "app" / "services" / "connectors" / "assessment").rglob("*.py")),
    BACKEND / "app" / "services" / "assessments_service.py",
    BACKEND / "app" / "services" / "assessments_erasure.py",
    BACKEND / "app" / "router" / "assessments.py",
    BACKEND / "app" / "router" / "portal_assessments.py",
    BACKEND / "app" / "router" / "public_assessment.py",
    BACKEND / "app" / "tasks" / "assessments.py",
]
BANNED_BUILTINS = frozenset({"exec", "eval", "compile", "__import__"})
BANNED_MODULES = frozenset(
    {
        "subprocess", "multiprocessing", "pty", "runpy", "code", "codeop", "ctypes", "importlib", "docker",
        "wasmtime", "wasmer", "pyodide", "judge0", "epicbox", "pexpect",
    }
)  # fmt: skip
BANNED_OS_PREFIXES = ("system", "popen", "exec", "spawn", "fork", "posix_spawn")


def violations(source: str, name: str = "<src>") -> list[str]:
    found: list[str] = []
    tree = ast.parse(source, filename=name)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in BANNED_BUILTINS:
            found.append(f"{name}:{node.lineno} calls {node.func.id}()")
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] in BANNED_MODULES:
                    found.append(f"{name}:{node.lineno} imports {alias.name}")
        if isinstance(node, ast.ImportFrom) and node.module and node.module.split(".")[0] in BANNED_MODULES:
            found.append(f"{name}:{node.lineno} imports from {node.module}")
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "os"
            and node.attr.startswith(BANNED_OS_PREFIXES)
        ):
            found.append(f"{name}:{node.lineno} uses os.{node.attr}")
        if isinstance(node, ast.ImportFrom) and node.module == "os":
            for alias in node.names:
                if alias.name.startswith(BANNED_OS_PREFIXES):
                    found.append(f"{name}:{node.lineno} imports os.{alias.name}")
    return found


def test_scanned_files_exist() -> None:
    missing = [str(p) for p in SCANNED if not p.exists()]
    assert not missing, missing
    assert len(SCANNED) > 15


def test_no_code_execution_anywhere_in_assessments() -> None:
    found: list[str] = []
    for path in SCANNED:
        found += violations(path.read_text(encoding="utf-8"), str(path.relative_to(BACKEND)))
    assert found == [], "candidate code must never be executed: " + "; ".join(found)


@pytest.mark.parametrize(
    "source",
    [
        "import subprocess\n",
        "from subprocess import run\n",
        "eval('1 + 1')\n",
        "exec('x = 1')\n",
        "compile('1', '<s>', 'eval')\n",
        "__import__('os')\n",
        "import os\nos.system('ls')\n",
        "import os\nos.execv('/bin/sh', [])\n",
        "import importlib\n",
        "import docker\n",
        "from multiprocessing import Pool\n",
    ],
)
def test_guard_catches_a_deliberate_violation(source: str) -> None:
    assert violations(source), source


def test_regex_compile_is_allowed() -> None:
    assert violations("import re\nre.compile('a+')\nre.fullmatch('a', 'a')\n") == []
