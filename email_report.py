"""
Emails a copy of each finished analysis to the TEN Capital team via Resend.

Best-effort: RESEND_API_KEY unset disables the feature entirely (no-op), and
a send failure is logged and swallowed rather than failing the job — the
docx/pdf are already sitting in the job folder and downloadable either way.
"""

from __future__ import annotations

import os
import traceback
from pathlib import Path

import resend
from resend.exceptions import ResendError

REPORT_EMAIL_TO = os.getenv("REPORT_EMAIL_TO", "info@tencapital.group")
REPORT_EMAIL_FROM = os.getenv("REPORT_EMAIL_FROM", "TEN Capital Deck Analyzer <onboarding@resend.dev>")


def send_report_email(company_name: str, source: str, outputs: dict[str, Path]) -> None:
    """Best-effort email of the finished report(s) to REPORT_EMAIL_TO."""
    api_key = os.getenv("RESEND_API_KEY")
    if not api_key:
        return

    resend.api_key = api_key
    attachments = [
        {"filename": path.name, "content": list(path.read_bytes())} for path in outputs.values()
    ]

    params: resend.Emails.SendParams = {
        "from": REPORT_EMAIL_FROM,
        "to": [REPORT_EMAIL_TO],
        "subject": f"TEN Capital Deck Analysis — {company_name}",
        "html": (
            f"<p>A new pitch deck analysis is ready for <strong>{company_name}</strong>.</p>"
            f"<p>Source deck: {source}</p>"
            "<p>The Word and PDF reports are attached.</p>"
        ),
        "attachments": attachments,
    }

    try:
        resend.Emails.send(params)
    except ResendError:
        traceback.print_exc()
