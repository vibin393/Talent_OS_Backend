"""Residency invariant check (Phase 1, Epic 6.4 / AD-22, NFR5).

Walks the mapped models and asserts the hard rule: **no control-plane table carries tenant PII**.
A model declares PII via ``AuditedMixin.__pii_fields__``; any such model whose table is classified
control-plane (see app/core/residency.py) is a violation — tenant PII would leave the tenant's cell.

Also prints the control-plane / tenant-pinned split. Exit 0 if the invariant holds, 1 otherwise, so
CI can gate on it.

Usage (from Backend/):  python scripts/residency_check.py
"""

import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import app.main  # noqa: E402, F401 — importing the app registers every model on Base.metadata
from app.core import residency  # noqa: E402
from app.core.models import Base  # noqa: E402


def main() -> int:
    # Map table name -> whether any mapped class on it declares PII fields.
    pii_tables: set[str] = set()
    for mapper in Base.registry.mappers:
        cls = mapper.class_
        pii = tuple(getattr(cls, "__pii_fields__", ()) or ())
        if pii:
            pii_tables.add(cls.__tablename__)

    control, tenant = [], []
    violations: list[str] = []
    for table in Base.metadata.tables:
        where = residency.residency_of(table)
        (control if where == residency.CONTROL_PLANE else tenant).append(table)
        if where == residency.CONTROL_PLANE and table in pii_tables:
            violations.append(table)

    print(f"Residency classification — {len(control)} control-plane, {len(tenant)} tenant-pinned "
          f"(of {len(Base.metadata.tables)} tables); {len(pii_tables)} tables carry PII")
    print(f"  control-plane sample: {sorted(control)[:12]}")
    if violations:
        print("\n  VIOLATIONS (tenant PII in the control plane — must be tenant-pinned):")
        for t in sorted(violations):
            print(f"    - {t}")
    ok = not violations
    print("\nRESULT:", "no tenant PII in the control plane ✓" if ok else "RESIDENCY VIOLATION ✗")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
