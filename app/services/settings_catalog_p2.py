"""Phase 2 settings hub catalogue (Epic 35 story 35.8, A9 / FR-186).

One registry module for every Phase 2 settings page, pointing at the owners' real endpoints. ``available``
is computed from route existence when the catalogue is read, so an entry lights up once its owner's router
lands. Owners may re-register their own key from ``hooks.py`` (same key replaces the entry).
"""

from app.services.settings_service import register_settings_entry

FINANCE, WORKFORCE, INTEGRATIONS = "finance", "workforce", "integrations"
ORG, SERVICES, SECURITY = "organization", "services", "security_privacy"

# (category, key, label, description, endpoint, permission, owner_epic)
P2_ENTRIES: tuple[tuple[str, str, str, str, str, str, str], ...] = (
    (FINANCE, "legal_entities", "Legal entities", "Your invoicing companies, addresses and bank accounts.",
     "/api/v1/legal-entities", "legal_entity:write", "Epic 14"),
    (FINANCE, "gst_registrations", "GST registrations", "GSTINs per legal entity and state, and LUTs.",
     "/api/v1/legal-entities/{legal_entity_id}/gst-registrations", "legal_entity:write", "Epic 14"),
    (FINANCE, "invoice_series", "Invoice series", "Numbering series for invoices and notes per legal entity.",
     "/api/v1/legal-entities/{legal_entity_id}/document-series", "document_series:write", "Epic 14"),
    (FINANCE, "sac_codes", "SAC codes and GST rates", "Service accounting codes and their tax rates.",
     "/api/v1/billing/sac-codes", "billing:configure", "Epic 29"),
    (FINANCE, "payment_terms", "Payment terms", "Payment terms offered to clients.",
     "/api/v1/payment-terms", "billing:write", "Epic 16"),
    (WORKFORCE, "leave_types", "Leave types", "Leave types, accrual and carry-forward rules.",
     "/api/v1/time/leave-types", "leave:manage_types", "Epic 26"),
    (WORKFORCE, "holiday_calendars", "Holiday calendars", "Holiday calendars per location or client.",
     "/api/v1/time/holiday-calendars", "holiday:manage", "Epic 26"),
    (WORKFORCE, "expense_policy", "Expense policy", "Expense categories, limits and receipt rules.",
     "/api/v1/expenses/policies", "expense:policy", "Epic 27"),
    (WORKFORCE, "timesheet_defaults", "Timesheet defaults", "Periods, reminders and approval defaults.",
     "/api/v1/time/settings", "timesheet:configure", "Epic 25"),
    (WORKFORCE, "onboarding_templates", "Onboarding templates", "Onboarding task templates.",
     "/api/v1/onboarding/templates", "onboarding:configure", "Epic 23"),
    (WORKFORCE, "document_templates", "Document templates", "Templates for generated and e-signed documents.",
     "/api/v1/documents/templates", "document_template:manage", "Epic 22"),
    (WORKFORCE, "bgv_packages", "BGV packages", "Background verification packages and checks.",
     "/api/v1/bgv/packages", "bgv:configure", "Epic 20"),
    (WORKFORCE, "compliance_requirements", "Compliance requirements", "Credentials required per role.",
     "/api/v1/compliance/requirements", "compliance:configure", "Epic 21"),
    (INTEGRATIONS, "provider_connectors", "Provider connectors", "BGV, e-sign and other providers.",
     "/api/v1/connectors/configs", "connector:configure", "Epic 22"),
    (INTEGRATIONS, "client_portal", "Client portal", "Client portal users and access per client.",
     "/api/v1/clients/{client_id}/portal-users", "client_portal:manage", "Epic 14"),
    (INTEGRATIONS, "vendor_portal", "Vendor portal", "Vendor portal users and access per vendor.",
     "/api/v1/vendors/{vendor_id}/portal-users", "vendor_portal:manage", "Epic 14"),
    (SECURITY, "sso", "Single sign-on", "OIDC / SAML identity providers and break-glass admins.",
     "/api/v1/sso/identity-providers", "sso:manage", "Epic 14"),
    # Epic 35's own entries.
    (SECURITY, "retention_classes", "Retention classes", "Statutory retention periods and expiry actions.",
     "/api/v1/privacy/retention/classes", "retention:manage", "Epic 35"),
    (SERVICES, "sla_escalations", "SLA and escalations", "Escalation policies for Phase 2 SLA clocks.",
     "/api/v1/sla/escalation-policies", "sla:manage", "Epic 32"),
    (ORG, "golive_import", "Go-live import", "Import workers, placements and opening balances.",
     "/api/v1/golive/kinds", "golive:import", "Epic 35"),
)  # fmt: skip


def register() -> None:
    for category, key, label, description, endpoint, permission, owner in P2_ENTRIES:
        register_settings_entry(category, key, label, description, endpoint, permission, owner_epic=owner)


register()
