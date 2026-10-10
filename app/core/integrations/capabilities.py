"""Connector capability catalogue (Epic 66, spec 0.9, AD-81).

``CAPABILITIES`` is frozen by the spec. ``CAPABILITY_METHODS`` names the category-port methods a capability requires
(``validate_catalogue`` checks the Fake adapter implements them; the Conformance Kit calls them).
``PERSONAL_DATA_CAPABILITIES`` must be covered by a ``DataFlow(personal_data=True)`` in the manifest (FR-335).
``MANIFEST_CATEGORIES`` mirrors ``app.models.connectors.CONNECTOR_CATEGORIES`` + the listing-only ``calendar``
(a test pins the equality; core never imports models).
"""

from types import MappingProxyType

__all__ = [
    "CAPABILITIES",
    "CAPABILITY_METHODS",
    "MANIFEST_CATEGORIES",
    "PERSONAL_DATA_CAPABILITIES",
]

CAPABILITIES = frozenset(
    {
        "job_board.publish", "job_board.applicants", "job_board.status", "job_board.fields",
        "hrms.push", "hrms.pull_status", "hrms.fields",
        "payroll.push",
        "accounting.push_documents", "accounting.pull_payments", "accounting.masters", "accounting.export",
        "calendar.sync", "calendar.org_free_busy",
        "voice.call", "voice.recording", "stt.transcribe",
    }
)  # fmt: skip

CAPABILITY_METHODS = MappingProxyType(
    {
        "job_board.publish": ("publish", "update", "close"),
        "job_board.applicants": ("fetch_applicants",),
        "job_board.status": ("status",),
        "job_board.fields": ("field_schema",),
        "hrms.push": ("push_hire", "push_change", "push_exit"),
        "hrms.pull_status": ("pull_status",),
        "hrms.fields": ("field_schema",),
        "payroll.push": ("push_export",),
        "accounting.push_documents": ("push_documents", "document_status"),
        "accounting.pull_payments": ("pull_payments",),
        "accounting.masters": ("masters_sync",),
        "accounting.export": ("export",),
        "calendar.sync": (),  # Epic 72 owns the calendar DTOs (listing-only manifests here)
        "calendar.org_free_busy": (),
        "voice.call": ("schedule_call", "start_call", "end_call"),
        "voice.recording": ("fetch_recording",),
        "stt.transcribe": ("transcribe", "languages"),
    }
)

PERSONAL_DATA_CAPABILITIES = frozenset(
    {
        "job_board.applicants",
        "hrms.push",
        "hrms.pull_status",
        "payroll.push",
        "calendar.sync",
        "voice.call",
        "voice.recording",
        "stt.transcribe",
    }
)

MANIFEST_CATEGORIES = (
    "esign", "bgv", "einvoice", "payroll", "accounting", "sms", "whatsapp",
    "assessment", "dsc", "tds_statement", "fx",
    "voice",
    "job_board", "hrms", "stt",
    "calendar",
)  # fmt: skip
