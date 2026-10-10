"""AI maintenance CLI (Epic 5): ``python -m app.services.ai.cli backfill-embeddings --tenant <id>``.

(Re-)embeds every consented candidate of the tenant (unchanged profiles are skipped by ``content_hash``) and
every Open demand.
"""

import argparse
import asyncio
import uuid

from app.tasks.ai import backfill_async


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.services.ai.cli")
    sub = parser.add_subparsers(dest="command", required=True)
    backfill = sub.add_parser(
        "backfill-embeddings", help="embed every candidate and open demand of one tenant"
    )
    backfill.add_argument("--tenant", required=True, type=uuid.UUID)
    args = parser.parse_args(argv)
    if args.command == "backfill-embeddings":
        import app.services.matching  # noqa: F401 - registers handlers / models

        count = asyncio.run(backfill_async(args.tenant))
        print(f"embedded {count} candidate(s) for tenant {args.tenant}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
