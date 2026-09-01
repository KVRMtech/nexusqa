"""Outcome-oracle breadth: a FAILED on-page value/text assertion is a real
regression too — not just a failed navigation (toHaveURL) oracle.

Pins the never-green-wash contract for ``outcome_contradicted_from_error``:
  * a value/text mismatch where the control RESOLVED (positive "unexpected value"
    / "Received" evidence) is dispositive real_regression — the app produced a
    different value/text than the recording (a wrong amount, a changed disclosure,
    a no-op fill);
  * a value/text oracle that failed because the control was renamed / not found
    carries no value-comparison evidence and stays heal-able selector drift — it
    is NEVER mis-escalated to a real regression (no over-refusal).
"""
import pytest

from app.services.test_runs import outcome_contradicted_from_error as f


URL_REGRESSION = (
    "Error: Timed out 5000ms waiting for expect(page).toHaveURL(expected)\n"
    'Expected pattern: /\\/confirm/\nReceived string: "https://app/home"\n'
)
VALUE_MISMATCH = (
    "Error: Timed out 5000ms waiting for expect(locator).toHaveValue(expected)\n"
    "Call log:\n  - waiting for locator('#premium')\n"
    '  -   locator resolved to <input value="1200"/>\n  -   unexpected value "1200"'
)
VALUE_NOT_FOUND = (
    "Error: Timed out 5000ms waiting for expect(locator).toHaveValue(expected)\n"
    "Call log:\n  - expect.toHaveValue with timeout 5000ms\n  - waiting for locator('#premium')"
)
TEXT_MISMATCH = (
    "Error: expect(locator).toHaveText(expected)\n"
    'Expected string: "Approved"\nReceived string: "Declined"\n'
    "Call log:\n  - waiting for locator('.status')"
)
CONTAINS_MISMATCH = (
    "expect(locator).toContainText(expected)\nReceived string: \"foo\"\n"
    "Call log:\n  - waiting for locator('.msg')"
)
ACTION_NOT_FOUND = (
    "Error: locator.click: Timeout 5000ms exceeded.\n"
    "Call log:\n  - waiting for locator('#btn')\n  - locator resolved to 0 elements"
)
VISIBLE_NOT_FOUND = (
    "Error: expect(locator).toBeVisible() failed\n"
    "Call log:\n  - waiting for locator('#x')\n  - locator resolved to 0 elements"
)


@pytest.mark.parametrize(
    "name,error,expected",
    [
        ("url_regression", URL_REGRESSION, True),
        ("value_mismatch_resolved", VALUE_MISMATCH, True),
        ("value_oracle_locator_not_found", VALUE_NOT_FOUND, False),
        ("text_mismatch_resolved", TEXT_MISMATCH, True),
        ("contains_mismatch_resolved", CONTAINS_MISMATCH, True),
        ("plain_action_locator_not_found", ACTION_NOT_FOUND, False),
        ("visibility_not_found_stays_healable", VISIBLE_NOT_FOUND, False),
        ("empty_error", "", False),
    ],
)
def test_outcome_contradicted_breadth(name, error, expected):
    assert f(error) == expected, f"{name}: expected {expected}, got {f(error)}"
