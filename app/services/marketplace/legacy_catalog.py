# ruff: noqa: E501
"""Legacy P2-P4 connector listings (Epic 69, spec A3; data only). Each entry opens the existing screen (``manage_path``);
its install state is derived from ``provider_connector_configs`` (an active row in the category -> "Configured"). Kept
until Epic 79 adopts the legacy configs into ``app_installations`` (D-19, Known gap 1)."""

import dataclasses

# Connector manifest category -> marketplace listing category (spec A1).
CATEGORY_MAP: dict[str, str] = {
    "job_board": "job_boards",
    "hrms": "hrms",
    "accounting": "accounting",
    "payroll": "payroll",
    "calendar": "calendar",
    "voice": "telephony",
    "stt": "speech_to_text",
    "assessment": "assessment",
    "bgv": "bgv",
    "esign": "esignature",
    "einvoice": "einvoice",
    "sms": "messaging",
    "whatsapp": "messaging",
    "dsc": "finance_compliance",
    "tds_statement": "finance_compliance",
    "fx": "finance_compliance",
}

CATEGORY_LABELS: dict[str, str] = {
    "job_boards": "Job boards",
    "hrms": "HRMS",
    "accounting": "Accounting",
    "payroll": "Payroll",
    "calendar": "Calendar",
    "telephony": "Telephony",
    "speech_to_text": "Speech-to-text",
    "assessment": "Assessments",
    "bgv": "Background verification",
    "esignature": "eSignature",
    "einvoice": "E-invoicing",
    "messaging": "Messaging",
    "finance_compliance": "Finance compliance",
    "bi": "BI",
    "other": "Other",
}


def listing_category(connector_category: str) -> str:
    return CATEGORY_MAP.get(connector_category, "other")


@dataclasses.dataclass(frozen=True, slots=True)
class LegacyListing:
    key: str  # == provider_connector_configs.category
    name: str
    tagline: str
    manage_path: str

    @property
    def slug(self) -> str:
        return f"legacy-{self.key.replace('_', '-')}"

    @property
    def category(self) -> str:
        return listing_category(self.key)


LEGACY_LISTINGS: tuple[LegacyListing, ...] = (
    LegacyListing(
        "esign",
        "eSignature (built-in)",
        "Sign offers and agreements with the built-in eSign or a provider.",
        "/admin/settings",
    ),
    LegacyListing(
        "bgv",
        "Background verification",
        "Run background checks manually or through a provider.",
        "/admin/bgv",
    ),
    LegacyListing(
        "einvoice",
        "GST e-invoicing",
        "Generate IRNs for invoices through the e-invoice portal.",
        "/settings/billing",
    ),
    LegacyListing(
        "payroll", "Payroll export", "Push approved timesheets to your payroll provider.", "/admin/payroll"
    ),
    LegacyListing(
        "accounting",
        "Accounting export",
        "Export invoices and receipts for your ledger.",
        "/receivables/exports",
    ),
    LegacyListing(
        "sms", "SMS (MSG91)", "Send SMS notifications through MSG91.", "/admin/messaging?tab=connectors"
    ),
    LegacyListing(
        "whatsapp",
        "WhatsApp",
        "Send WhatsApp messages to candidates and workers.",
        "/admin/messaging?tab=connectors",
    ),
    LegacyListing(
        "assessment", "Assessment providers", "Invite candidates to provider assessments.", "/admin/settings"
    ),
    LegacyListing(
        "dsc", "Digital signature certificates", "Sign statutory documents with a DSC.", "/admin/settings"
    ),
    LegacyListing("tds_statement", "TDS statements", "Prepare TDS statements for filing.", "/admin/settings"),
    LegacyListing(
        "fx", "Exchange-rate feed", "Daily foreign exchange rates for invoices.", "/settings/fx-feed"
    ),
    LegacyListing(
        "voice", "Voice calls (manual)", "Log and record voice calls with candidates.", "/admin/settings"
    ),
)


def legacy(key: str) -> LegacyListing | None:
    return next((x for x in LEGACY_LISTINGS if x.key == key), None)


def by_slug(slug: str) -> LegacyListing | None:
    return next((x for x in LEGACY_LISTINGS if x.slug == slug), None)


__all__ = [
    "CATEGORY_LABELS",
    "CATEGORY_MAP",
    "LEGACY_LISTINGS",
    "LegacyListing",
    "by_slug",
    "legacy",
    "listing_category",
]
