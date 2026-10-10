# ruff: noqa: E501
"""Epic 66 (story 66.1): no configuration, script or fixture points at the database ``talent_os_test`` (another
project's database on the shared server). Only ``tests/conftest.py`` may name it (in prose, as the historic default)."""

import re
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2]
PATTERN = re.compile(r"talent_os_test(?![A-Za-z0-9_])")
ROOTS = ("app", "tests", "scripts", "alembic")
ROOT_FILES = (
    "alembic.ini",
    "pyproject.toml",
    "docker-compose.yml",
    "start.sh",
    "setup.sh",
    "coverage.sh",
    ".env.example",
)
SUFFIXES = {".py", ".ini", ".toml", ".yml", ".yaml", ".sh", ".cfg", ".json", ".env"}
# Guards that name the database only to refuse it (comments / refusal tests), never to connect to it.
ALLOWED = {
    Path("tests/conftest.py"),
    Path("tests/meta/test_no_talent_os_test.py"),
    Path("app/core/settings.py"),  # comment: the eval runner never uses it
    Path("tests/evals/test_cli.py"),  # asserts the eval runner refuses it
    Path("tests/migrations/test_round_trip.py"),  # FORBIDDEN round-trip targets
}


def _files() -> list[Path]:
    out = [BACKEND / f for f in ROOT_FILES if (BACKEND / f).is_file()]
    for root in ROOTS:
        out += [
            p
            for p in (BACKEND / root).rglob("*")
            if p.is_file() and p.suffix in SUFFIXES and "__pycache__" not in p.parts
        ]
    return out


def test_no_config_names_talent_os_test() -> None:
    offenders = sorted(
        str(p.relative_to(BACKEND))
        for p in _files()
        if p.relative_to(BACKEND) not in ALLOWED
        and PATTERN.search(p.read_text(encoding="utf-8", errors="ignore"))
    )
    assert offenders == [], f"never point at talent_os_test: {offenders}"
