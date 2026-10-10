"""Facade of the ``expenses`` domain (Epic 27). Other domains import from here.

* Consumers: ``mark_reimbursed`` (Epic 28 Payroll Export, inside *mark processed*), ``mark_stated`` (Epic 17
  Vendor Payable Statements, inside finalize).
* Reads: ``list_claims``; offboarding verifier ``final_expense_settlement`` (registered from hooks).
* Client Portal (Epic 18): ``client_claim_summaries``, ``client_claim_detail``.
"""

from app.services.expenses.portal import client_claim_detail, client_claim_summaries
from app.services.expenses.service import final_expense_settlement, list_claims, mark_reimbursed, mark_stated

__all__ = [
    "client_claim_detail",
    "client_claim_summaries",
    "final_expense_settlement",
    "list_claims",
    "mark_reimbursed",
    "mark_stated",
]
