"""Search maintenance CLI (Epic 5): ``python -m app.services.search.cli reindex --tenant <id>``.

Rebuilds the tenant's ``candidate_search_docs`` in batches of 1,000 (runs as the runtime role inside the
tenant's RLS session).
"""

import argparse
import asyncio
import uuid

from app.tasks.search import reindex_async


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.services.search.cli")
    sub = parser.add_subparsers(dest="command", required=True)
    reindex = sub.add_parser("reindex", help="rebuild the candidate search projection of one tenant")
    reindex.add_argument("--tenant", required=True, type=uuid.UUID)
    args = parser.parse_args(argv)
    if args.command == "reindex":
        import app.services.search  # ruff: ignore[unused-import] - registers handlers / models  # noqa: F401

        count = asyncio.run(reindex_async(args.tenant))
        print(f"reindexed {count} candidate(s) for tenant {args.tenant}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
