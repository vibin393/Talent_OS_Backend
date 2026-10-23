# ruff: file-ignore[line-too-long]
"""Connector Manifests of the ledger connectors (Epic 71, spec A7): Zoho Books (OAuth, India DC) and the TallyPrime
XML import file (D-5). Registered by ``app.services.ledger_sync.hooks``.

The P2 accounting Fake / Manual adapters take a legacy ``ConnectorConfig``; ``FakeLedger`` / ``ManualLedger`` adapt
them to the manifest factory contract (``factory(AdapterContext)``). The Manual adapter keeps the P2 export path; its
``AccountingSyncPort`` methods ask for a manual action (the P2 CSV / XLSX accounting export).
"""

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from app.core.integrations import ConnectorManifest, DataFlow, OAuthProviderSpec, SecretField, SyncJobSpec
from app.core.integrations.context import AdapterContext
from app.core.integrations.errors import ManualActionRequired
from app.services.connectors.accounting.fake import FakeAccountingExporter
from app.services.connectors.accounting.manual import ManualAccountingExporter
from app.services.connectors.accounting.port import LedgerDocumentDTO, LedgerPaymentDTO, LedgerResultDTO
from app.services.connectors.accounting.tally_xml import TallyConfig, TallyXmlAdapter
from app.services.connectors.accounting.zoho_books import (
    AUTHORIZE_URL,
    EGRESS_HOSTS,
    SCOPES,
    TOKEN_URL,
    ZohoBooks,
    ZohoBooksConfig,
)

__all__ = ["LEDGER_MANIFESTS", "TALLY_XML", "ZOHO_BOOKS", "FakeLedger", "ManualLedger"]

_MANUAL = "Use the accounting export (CSV / XLSX) and import it into your accounting system"


class FakeLedger(FakeAccountingExporter):
    """The P2 accounting Fake behind a manifest context (scripted payments: ``FakeAccountingExporter.payments``)."""

    def __init__(self, ctx: AdapterContext | None = None) -> None:
        super().__init__(None)
        self.ctx = ctx

    def __repr__(self) -> str:
        return "FakeLedger()"

    def verify_webhook(self, headers: Mapping[str, str], body: bytes) -> bool:  # ruff: ignore[unused-method-argument]
        return False


class ManualLedger(ManualAccountingExporter):
    def __init__(self, ctx: AdapterContext | None = None) -> None:
        super().__init__(None)
        self.ctx = ctx

    async def push_documents(self, docs: Sequence[LedgerDocumentDTO]) -> Sequence[LedgerResultDTO]:
        raise ManualActionRequired(_MANUAL, {"op": "push_documents", "documents": len(docs)})

    async def document_status(self, refs: Sequence[str]) -> Sequence[LedgerResultDTO]:  # ruff: ignore[unused-method-argument]
        raise ManualActionRequired(_MANUAL, {"op": "document_status"})

    async def pull_payments(self, since: datetime | None, cursor: str | None) -> tuple[Sequence[LedgerPaymentDTO], str | None]:  # ruff: ignore[unused-method-argument]
        raise ManualActionRequired("Record client payments in Talent OS", {"op": "pull_payments"})

    async def masters_sync(self, customers: Sequence[Mapping[str, Any]], ledgers: Sequence[Mapping[str, Any]]) -> Mapping[str, str]:  # ruff: ignore[unused-method-argument]
        raise ManualActionRequired(_MANUAL, {"op": "masters_sync"})


_DOCS = DataFlow(
    "out", "finance_documents", ("client_name", "gstin", "addresses", "invoice_lines", "payments"),
    "Record your invoices, notes and payments in your accounting system", "IN", False,
)  # fmt: skip

ZOHO_BOOKS = ConnectorManifest(
    key="zoho_books",
    version=1,
    owner_epic="71",
    name="Zoho Books",
    summary="Push issued invoices, notes and payments to Zoho Books (India) and bring payments back as suggestions.",
    category="accounting",
    capabilities=frozenset({"accounting.push_documents", "accounting.pull_payments", "accounting.masters", "accounting.export"}),
    auth="oauth2_code",
    oauth=OAuthProviderSpec(
        authorize_url=AUTHORIZE_URL,
        token_url=TOKEN_URL,
        scopes=SCOPES,
        pkce=False,
        extra_params={"access_type": "offline", "prompt": "consent"},
    ),
    config_model=ZohoBooksConfig,
    secret_fields=(
        SecretField("oauth_refresh", "Refresh token", "oauth_refresh"),
        SecretField("client_id", "Client ID", "token", required=False),
        SecretField("client_secret", "Client secret", "password", required=False),
    ),
    data_flows=(
        _DOCS,
        DataFlow("in", "customer_payments", ("invoice_number", "amount", "paid_on", "tds_amount", "mode", "reference"), "Suggest payments to record in Talent OS", "IN", False),
    ),
    egress_hosts=EGRESS_HOSTS,
    sync_jobs=(
        SyncJobSpec(
            "push_document", "accounting.push_documents", "push", "event",
            ("invoice.issued", "credit_note.issued", "debit_note.issued", "invoice.cancelled", "payment.recorded"),
            entity_type="invoice", per_legal_entity=True, max_batch=25,
        ),
        SyncJobSpec("pull_payments", "accounting.pull_payments", "pull", "schedule", every_seconds=1800, entity_type="payment", per_legal_entity=True),
        SyncJobSpec("document_status", "accounting.push_documents", "pull", "schedule", every_seconds=86400, entity_type="invoice", per_legal_entity=True),
        SyncJobSpec("masters", "accounting.masters", "push", "manual", entity_type="client", per_legal_entity=True),
    ),  # fmt: skip
    adapters={"fake": FakeLedger, "live": ZohoBooks, "manual": ManualLedger},
    legal_entity_scoped=True,
    rate_per_minute=90,
    docs_url="https://www.zoho.com/books/api/v3/",
)

TALLY_XML = ConnectorManifest(
    key="tally_xml",
    version=1,
    owner_epic="71",
    name="TallyPrime (import file)",
    summary="Generate TallyPrime XML import files of your invoices, notes and receipts per legal entity and period.",
    category="accounting",
    capabilities=frozenset({"accounting.push_documents", "accounting.masters", "accounting.export"}),
    auth="file",
    config_model=TallyConfig,
    data_flows=(DataFlow("out", "finance_documents", ("client_name", "gstin", "invoice_lines", "payments"), "Download for import into TallyPrime", "IN", True),),
    sync_jobs=(SyncJobSpec("tally_file", "accounting.export", "push", "manual", entity_type="invoice", per_legal_entity=True),),
    adapters={"fake": TallyXmlAdapter, "manual": ManualLedger},
    legal_entity_scoped=True,
    docs_url="https://help.tallysolutions.com/",
)

LEDGER_MANIFESTS = (ZOHO_BOOKS, TALLY_XML)
