# ruff: noqa: E501
"""Rewrite the pinned Public API v1 contract (Epic 67, FR-347): ``Backend/openapi-public-v1.json``.

    python scripts/update_public_openapi.py            # report breaking changes and uncovered additions
    python scripts/update_public_openapi.py --accept   # rewrite the pin (refused on breaking changes or when
                                                       # app/core/public_api_changelog.yaml misses an addition)
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main(argv: list[str]) -> int:
    import yaml

    from app.core import public_api_contract as contract
    from app.main import app

    current = contract.contract_document(app)
    pinned = contract.load_pin()
    breaking = contract.breaking_changes(pinned, current)
    paths, fields = contract.additions(pinned, current)
    entries = yaml.safe_load(contract.CHANGELOG.read_text(encoding="utf-8")) or []
    missing = contract.changelog_covers(entries, paths, fields)
    for problem in breaking:
        print(f"BREAKING: {problem}")
    for item in missing:
        print(f"NOT IN CHANGELOG: {item}")
    if "--accept" not in argv:
        print(f"{len(paths)} new path(s), {len(fields)} new field(s)")
        return 1 if breaking or missing else 0
    if breaking or missing:
        print("Refusing to rewrite the pin.")
        return 1
    contract.write_pin(current)
    print(f"Wrote {contract.PIN}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
