"""Seed Manifest: the crawler-coverage action-label filter.

Only real data-input fields belong in a "provide a value" manifest; UI action controls
(buttons/toggles the crawler also flags as ungrounded) must be filtered out. LIVE E2E
on a bank app surfaced "Mark TC-DASH-001 as done" and "Enable Two-Factor" as ASK items
until this filter was added.
"""
from app.services.dispositions import is_action_label as _is_action_label


def test_action_controls_are_filtered():
    for a in [
        "Mark TC-DASH-001 as done", "Enable Two-Factor Authentication", "Download CSV",
        "Go to page 2", "Send feedback or report an issue", "Switch to light mode",
        "Apply for Loan", "Reset All My Data", "Previous page", "Add",
    ]:
        assert _is_action_label(a), f"{a!r} should be filtered as an action"


def test_real_data_fields_are_kept():
    for d in ["From Account", "Payee", "Account", "Loan Type", "Amount", "Email", "Username", "Date of Birth"]:
        assert not _is_action_label(d), f"{d!r} is a data field and must be kept"
