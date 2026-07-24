"""Central registry of platform/api tests that are RED at HEAD due to a VERIFIED
runtime regression — so CI can run the whole suite and stay green while keeping
each failure VISIBLE (xfail, with a reason) instead of hidden (skip/deselect) or
faked (a weakened assertion). This is the never-green-wash way to gate a suite
that has known-open regressions in code we are not authorised to patch.

Every entry below is a git-bisected, reproduced regression introduced by commit
efd0269 ("trust-track production sync from VM"), which overwrote several
platform/api files with an OLDER VM lineage — reverting the Phase-0
never-green-wash auditor upgrade (ambiguous-locator detection + honest gate
reporting) and introducing a FastAPI app-assembly break. Full evidence and the
exact recommended fixes live in
    docs/FINDINGS_PLATFORM_API_REGRESSIONS_2026-07-21.md

These are FROZEN-factory RUNTIME fixes and are held for founder sign-off (the
"which lineage is canonical" decision is the founder's). The markers are
strict=True on purpose: the instant the runtime is fixed the test XPASSES, strict
xfail turns that xpass into a FAILURE, and CI forces this stale entry to be
removed. Nothing here is a stale TEST — those were corrected in place.
"""
import pytest

_FINDINGS = "docs/FINDINGS_PLATFORM_API_REGRESSIONS_2026-07-21.md"

# "test_file.py::test_name" -> why it fails at HEAD (the regression, not the symptom).
# EMPTY: the efd0269 regressions are RESOLVED (founder-signed-off restoration).
#   * The Phase-0 never-green-wash auditor (V_AMBIGUOUS ambiguous-locator
#     dimension + report-consuming gate() with honest independent would_block)
#     was restored from 6bfcbad; the one live gate() caller was reconciled to
#     the report API.
#   * _reconcile now matches the audio transcript against the action's
#     verb/value (audio_intent_match contract), not the field prompt.
# Each formerly-xfail test now passes for real. A NEW genuine regression can be
# re-registered here as a documented strict-xfail (kept visible, never hidden).
_KNOWN_REGRESSIONS: dict[str, str] = {}


def pytest_collection_modifyitems(config, items):
    for item in items:
        node_file = item.nodeid.split("::", 1)[0]
        for key, reason in _KNOWN_REGRESSIONS.items():
            fname, tname = key.split("::", 1)
            if item.name == tname and node_file.endswith(fname):
                item.add_marker(pytest.mark.xfail(
                    reason=f"REGRESSION (efd0269 VM-sync): {reason} See {_FINDINGS}.",
                    strict=True,
                ))
                break
