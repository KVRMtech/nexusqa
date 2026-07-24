"""P0.3 regression — the certification quarantine rule (pure).

Doctrine pinned:
  * a case that failed certification for a PRODUCT/unproven cause is not
    offered to the client as runnable (the first failure is OURS);
  * an APPLICATION-attributed certification failure is NEVER hidden — a
    grounded regression on the baseline is the signal the client pays for
    (hiding it would be green-wash);
  * environment/configuration outages never shame the cases;
  * quarantine and blame are separate decisions.

The rule is one pure function (``quarantine_decision``) used by BOTH the
run-gate (server-authoritative) and the runs-summary UI flag, so the two can
never diverge.

Run from Nexus_power/platform/api:
    python -m pytest tests/test_certification_quarantine.py -q
"""
from __future__ import annotations

import importlib.util
import os
import re
import sys

# quarantine_decision lives in app/services/test_runs.py, which imports SDK DB
# models at module scope — load the FUNCTION source-level to keep this test
# DB-free (same pure-logic philosophy as the other unit suites): execute only
# the def via a tiny namespace exec over the extracted source.
_SRC = open(
    os.path.join(os.path.dirname(__file__), "..", "app", "services", "test_runs.py"),
    encoding="utf-8",
).read()
_m = re.search(r"\ndef quarantine_decision\(.*?\n(?=\nasync def |\ndef |\Z)", _SRC, re.S)
assert _m, "quarantine_decision not found in test_runs.py"
_ns: dict = {
    "CATEGORY_PRODUCT": "product_script_defect",
    "CATEGORY_UNKNOWN": "unknown",
}
exec(compile(_m.group(0), "test_runs.py::quarantine_decision", "exec"), _ns)  # noqa: S102
quarantine_decision = _ns["quarantine_decision"]


def _cert(status: str, category: str | None) -> dict:
    return {
        "status": status,
        "at": "2026-07-24T05:00:00Z",
        "attribution": {"category": category} if category is not None else None,
    }


def test_no_certification_record_is_not_quarantined():
    assert quarantine_decision(None) is False
    assert quarantine_decision({}) is False


def test_passed_certification_is_not_quarantined():
    assert quarantine_decision(_cert("certified", None)) is False


def test_product_fault_certification_failure_quarantines():
    assert quarantine_decision(_cert("failed", "product_script_defect")) is True


def test_unknown_and_unattributed_failures_quarantine():
    """Unproven-runnable = not offered to the client (baseline is attested)."""
    assert quarantine_decision(_cert("failed", "unknown")) is True
    assert quarantine_decision(_cert("failed", None)) is True
    assert quarantine_decision(_cert("failed", "")) is True


def test_application_regression_is_never_hidden():
    """A grounded regression found on the baseline is a REAL client signal —
    quarantining it would green-wash. It stays runnable and visible."""
    assert quarantine_decision(_cert("failed", "application_defect")) is False


def test_environment_and_config_outages_do_not_shame_cases():
    assert quarantine_decision(_cert("failed", "environment")) is False
    assert quarantine_decision(_cert("failed", "configuration")) is False
