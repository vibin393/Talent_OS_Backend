"""Admin CLI.

    python -m app.cli create-operator --email ops@example.com --name "Ops Person" [--password ...]
    python -m app.cli seed-skills          # re-sync the platform skill catalog (idempotent, owner role)
    python -m app.cli seed-skill-graph     # re-sync the Epic 37 skills graph seed (idempotent, owner role)
    python -m app.cli seed-visa-templates  # re-sync the Epic 46 visa type templates (idempotent, owner role)
    python -m app.cli reapply-erasures --since 2026-09-01   # after a backup restore (FR-68, Epic 12)
    python -m app.cli email-bounce someone@example.com [--tenant-id <uuid>] [--type hard|soft|complaint]
                                            # dev: simulate a bounce (Epic 10)
    python -m app.cli purge-audit --tenant <uuid> [--dry-run]   # owner role: audit rows past retention (22.9)

Without ``--password`` the password is prompted (hidden, confirmed). Fails with exit code 1 (and does not
change anything) if an operator with that email already exists.
"""

import argparse
import asyncio
import getpass
import sys

from app.core.database import dispose_engines
from app.core.errors import DomainError
from app.services import auth_service as service


async def _create_operator(email: str, name: str, password: str) -> int:
    try:
        operator = await service.create_operator(email, name, password)
    except DomainError as exc:
        print(f"error: {exc.message} ({exc.code})", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        await dispose_engines()
    print(f"Created platform operator {operator.email} (id={operator.id})")
    return 0


async def _seed_skills() -> int:
    from app.core.database import owner_engine
    from app.services.candidates.skill_seed import sync_seed

    engine = owner_engine()
    try:
        async with engine.begin() as conn:
            counts = await conn.run_sync(sync_seed)
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        await engine.dispose()
    print(f"Synced {counts['skills']} skills and {counts['aliases']} aliases")
    return 0


async def _seed_skill_graph() -> int:
    from app.core.database import owner_engine
    from app.services.skills.seed import sync_graph_seed

    engine = owner_engine()
    try:
        async with engine.begin() as conn:
            counts = await conn.run_sync(sync_graph_seed)
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        await engine.dispose()
    print(f"Synced {counts['skills']} skills, {counts['aliases']} aliases and {counts['edges']} edges")
    return 0


async def _seed_visa_templates() -> int:
    from app.core.database import owner_engine
    from app.services.immigration.templates import sync_seed

    engine = owner_engine()
    try:
        async with engine.begin() as conn:
            counts = await conn.run_sync(sync_seed)
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        await engine.dispose()
    print(f"Visa templates: {counts['inserted']} inserted, {counts['updated']} updated, {counts['unchanged']} unchanged")
    return 0


async def _reapply_erasures(since: str) -> int:
    """Replay the erasure ledger: re-erase every listed candidate that a backup restore brought back."""
    import uuid
    from datetime import date

    from app.core.context import RequestContext
    from app.core.database import tenant_session
    from app.services.candidates import privacy_port
    from app.services.privacy import erasure

    try:
        entries = await erasure.read_ledger(date.fromisoformat(since))
        replayed = 0
        for entry in entries:
            tenant_id, candidate_id = uuid.UUID(entry["tenant_id"]), uuid.UUID(entry["candidate_id"])
            async with tenant_session(tenant_id) as db:
                facts = (await privacy_port.basics(db, [candidate_id])).get(candidate_id)
                if facts is None or facts["erased_at"] is not None:
                    continue  # gone, or still erased
                run = await erasure.create_run(
                    RequestContext(tenant_id=tenant_id), db, candidate_id, reason="dsr", enqueue=False
                )
                run_id = run.id
            if await erasure.run_erasure(tenant_id, run_id) == "completed":
                replayed += 1
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        await dispose_engines()
    print(f"Re-applied {replayed} erasure(s) from {len(entries)} ledger line(s)")
    return 0


def _prompt_password() -> str:
    password = getpass.getpass("Password: ")
    if getpass.getpass("Confirm password: ") != password:
        print("error: passwords do not match", file=sys.stderr)
        raise SystemExit(1)
    return password


async def _email_bounce(address: str, tenant_id: str | None, bounce_type: str) -> int:
    import uuid

    from app.core.database import tenant_session
    from app.services import comms_service

    reason = "Simulated bounce (CLI)"
    if tenant_id:
        async with tenant_session(uuid.UUID(tenant_id)) as db:
            result = await comms_service.record_bounce(db, address, None, bounce_type, reason)
        matched = len(result.recipient_ids)
    else:
        matched = await comms_service.record_bounce_everywhere(address, bounce_type, reason)
    print(f"Recorded a {bounce_type} bounce for {matched} recipient row(s)")
    return 0


def _run_email_bounce(address: str, tenant_id: str | None, bounce_type: str) -> int:
    from app.tasks.runtime import run

    try:
        return int(run(lambda: _email_bounce(address, tenant_id, bounce_type)))
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


async def _purge_audit(tenant: str, dry_run: bool) -> int:
    """Story 22.9 (FR-180): purge the tenant's audit rows past ``audit_retention_years`` (owner role)."""
    import uuid

    from app.core.database import owner_engine
    from app.services.privacy.audit_purge import purge_audit

    engine = owner_engine()
    try:
        out = await purge_audit(engine, uuid.UUID(tenant), dry_run=dry_run)
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        await engine.dispose()
    verb = "Would delete" if dry_run else "Deleted"
    print(
        f"{verb} {out['deleted']} audit row(s) older than {out['cutoff']:%Y-%m-%d} ({out['years']} y); "
        f"kept {out['protected']} row(s) referenced by a retention class"
    )
    return 0


def _run_seal_qa(tenant: str | None, all_tenants: bool, verify_only: bool) -> int:
    """Epic 78 (FR-404): ``knowledge seal-qa`` -- counts only are printed, never message text."""
    import uuid

    from app.tasks.knowledge_sealing import seal_tenant
    from app.tasks.runtime import for_each_tenant, run

    async def job(tid: uuid.UUID) -> dict[str, object]:
        return await seal_tenant(tid, verify_only=verify_only)

    try:
        if all_tenants:
            out = run(lambda: for_each_tenant(job))
        else:
            tid = uuid.UUID(str(tenant))
            out = {str(tid): run(lambda: job(tid), tenant_id=tid)}
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    failed = False
    for tid, result in dict(out).items():
        print(f"{tid}: {result}")
        failed = failed or (isinstance(result, dict) and bool(result.get("failed") or result.get("undecryptable_rows")))
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cli", description="Talent OS admin commands")
    sub = parser.add_subparsers(dest="command", required=True)
    op = sub.add_parser("create-operator", help="create a platform operator")
    op.add_argument("--email", required=True)
    op.add_argument("--name", required=True)
    op.add_argument("--password", help="omit to be prompted (recommended: keeps it out of shell history)")
    sub.add_parser("seed-skills", help="re-sync the platform skill catalog from skills_seed.yaml")
    sub.add_parser("seed-skill-graph", help="re-sync the platform skills graph from skill_graph_seed.yaml")
    sub.add_parser("seed-visa-templates", help="re-sync the platform visa type templates (Epic 46)")
    re = sub.add_parser("reapply-erasures", help="replay the erasure ledger after a backup restore")
    re.add_argument("--since", required=True, help="YYYY-MM-DD (first day of the restored backup)")
    eb = sub.add_parser("email-bounce", help="dev: record a bounce for an address (Epic 10)")
    eb.add_argument("address")
    eb.add_argument("--tenant-id", help="limit to one tenant (default: every tenant that emailed the address)")
    eb.add_argument("--type", dest="bounce_type", default="hard", choices=("hard", "soft", "complaint"))
    pa = sub.add_parser("purge-audit", help="owner role: delete audit rows past audit retention")
    pa.add_argument("--tenant", required=True, help="tenant id (uuid)")
    pa.add_argument("--dry-run", action="store_true", help="count only")
    kn = sub.add_parser("knowledge", help="knowledge maintenance (Epic 78)")
    kn_sub = kn.add_subparsers(dest="knowledge_command", required=True)
    sq = kn_sub.add_parser("seal-qa", help="seal onboarding Q&A plaintext under the tenant data key, then verify")
    target = sq.add_mutually_exclusive_group(required=True)
    target.add_argument("--tenant", help="tenant id (uuid)")
    target.add_argument("--all", action="store_true", help="every active tenant")
    sq.add_argument("--verify-only", action="store_true", help="count plaintext / undecryptable rows only")
    args = parser.parse_args(argv)

    if args.command == "knowledge":
        return _run_seal_qa(args.tenant, bool(args.all), bool(args.verify_only))

    if args.command == "purge-audit":
        return asyncio.run(_purge_audit(args.tenant, args.dry_run))

    if args.command == "email-bounce":
        return _run_email_bounce(args.address, args.tenant_id, args.bounce_type)

    if args.command == "reapply-erasures":
        return asyncio.run(_reapply_erasures(args.since))

    if args.command == "seed-skills":
        return asyncio.run(_seed_skills())

    if args.command == "seed-visa-templates":
        return asyncio.run(_seed_visa_templates())

    if args.command == "seed-skill-graph":
        return asyncio.run(_seed_skill_graph())

    if args.command == "create-operator":
        password = args.password if args.password is not None else _prompt_password()
        return asyncio.run(_create_operator(args.email, args.name, password))
    return 2  # pragma: no cover - argparse enforces a known command


if __name__ == "__main__":
    raise SystemExit(main())
