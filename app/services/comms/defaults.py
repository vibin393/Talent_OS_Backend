"""Built-in system email templates (FR-54), one per template key, English.

Pure data: imported by the seeding migration (20261001_101) and by ``comms_service.ensure_default_templates``.
Every token must be allowed for its key (``merge_fields.ALLOWED_GROUPS_BY_KEY``); a unit test checks it.
"""

DEFAULT_TEMPLATES: dict[str, dict[str, str]] = {
    "submission": {
        "name": "Candidate submission",
        "description": "Sent to client contacts with a candidate profile.",
        "subject": "Candidate for {{ demand.title }} ({{ demand.human_id }})",
        "body_html": (
            "<p>Dear {{ contact.first_name }},</p>"
            "<p>Please find the profile of {{ candidate.full_name }} for the {{ demand.title }} role.</p>"
            "<p>Best regards,<br>{{ sender.name }}<br>{{ tenant.name }}</p>"
        ),
    },
    "interview_invite": {
        "name": "Interview invite",
        "description": "Invites a candidate to an interview.",
        "subject": "Interview for {{ demand.title }} at {{ tenant.name }}",
        "body_html": (
            "<p>Hi {{ candidate.first_name }},</p>"
            "<p>We would like to invite you to an interview for the {{ demand.title }} role on "
            "{{ interview.start_local }} ({{ interview.timezone }}), {{ interview.mode }}.</p>"
            '<p><a href="{{ interview.join_link }}">Join the interview</a></p>'
            "<p>Best regards,<br>{{ sender.name }}</p>"
        ),
    },
    "interview_reminder": {
        "name": "Interview reminder",
        "description": "Reminds a candidate of an upcoming interview.",
        "subject": "Reminder: interview for {{ demand.title }}",
        "body_html": (
            "<p>Hi {{ candidate.first_name }},</p>"
            "<p>This is a reminder of your interview on {{ interview.start_local }} "
            "({{ interview.timezone }}).</p>"
            '<p><a href="{{ interview.join_link }}">Join the interview</a></p>'
            "<p>{{ sender.name }}</p>"
        ),
    },
    "rejection": {
        "name": "Application update",
        "description": "Informs a candidate that the application will not proceed.",
        "subject": "Your application for {{ demand.title }}",
        "body_html": (
            "<p>Hi {{ candidate.first_name }},</p>"
            "<p>Thank you for your interest in the {{ demand.title }} role. After careful consideration we "
            "will not be moving forward with your application at this time.</p>"
            "<p>We wish you all the best,<br>{{ tenant.name }}</p>"
        ),
    },
    "offer": {
        "name": "Offer",
        "description": "Shares an offer with a candidate.",
        "subject": "Your offer from {{ tenant.name }}",
        "body_html": (
            "<p>Hi {{ candidate.first_name }},</p>"
            "<p>We are delighted to offer you the {{ demand.title }} role.</p>"
            '<p><a href="{{ offer.link }}">View your offer</a></p>'
            "<p>{{ sender.name }}</p>"
        ),
    },
    "application_confirmation": {
        "name": "Application confirmation",
        "description": "Confirms that an application was received.",
        "subject": "We received your application for {{ demand.title }}",
        "body_html": (
            "<p>Hi {{ candidate.first_name }},</p>"
            "<p>Thank you for applying for the {{ demand.title }} role at {{ tenant.name }}. "
            "We will be in touch soon.</p>"
        ),
    },
    "ai_screening_invite": {
        "name": "AI screening invite",
        "description": "Invites a candidate to answer screening questions.",
        "subject": "A few questions about the {{ demand.title }} role",
        "body_html": (
            "<p>Hi {{ candidate.first_name }},</p>"
            "<p>Please answer a few short questions about the {{ demand.title }} role.</p>"
            '<p><a href="{{ link.url }}">Answer the questions</a></p>'
            "<p>{{ sender.name }}<br>{{ tenant.name }}</p>"
        ),
    },
    "client_feedback_request": {
        "name": "Client feedback request",
        "description": "Asks a client contact for feedback on a submitted candidate.",
        "subject": "Feedback on {{ candidate.full_name }} for {{ demand.title }}",
        "body_html": (
            "<p>Dear {{ contact.first_name }},</p>"
            "<p>Could you share your feedback on {{ candidate.full_name }} for the {{ demand.title }} role?</p>"  # noqa: E501
            '<p><a href="{{ link.url }}">Give feedback</a></p>'
            "<p>{{ sender.name }}</p>"
        ),
    },
    "custom": {
        "name": "General message",
        "description": "A blank message with a signature.",
        "subject": "Message from {{ tenant.name }}",
        "body_html": "<p>Hi,</p><p></p><p>Best regards,<br>{{ sender.name }}<br>{{ tenant.name }}</p>",
    },
    # Epic 58 (coordinator C-11): the voice screening consent request (call and, separately, recording).
    "voice_consent_request": {
        "name": "Voice screening consent request",
        "description": "Asks a candidate to agree to a short screening call and, separately, to its recording.",
        "subject": "A short screening call about the {{ demand.title }} role",
        "body_html": (
            "<p>Hi {{ candidate.first_name }},</p>"
            "<p>{{ sender.name }} from {{ tenant.name }} would like a short screening call with you about the "
            "{{ demand.title }} role. Please tell us where you are, whether you agree to the call and, separately, "
            "whether you agree to it being recorded.</p>"
            '<p><a href="{{ link.url }}">Choose your options</a></p>'
            "<p>{{ sender.name }}<br>{{ tenant.name }}</p>"
        ),
    },
}
