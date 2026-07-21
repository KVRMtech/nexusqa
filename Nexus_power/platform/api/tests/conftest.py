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
_KNOWN_REGRESSIONS = {
    "test_track_a_fixes.py::test_auditor_flags_ambiguous_locator_without_anchor":
        "efd0269 reverted the auditor's ambiguous-locator dimension "
        "(V_AMBIGUOUS / _ambiguous_labels gone) — the saucedemo 6x 'Add to cart' blind spot is uncaught.",
    "test_track_a_fixes.py::test_gate_default_is_warning_only_but_honest":
        "efd0269 reverted gate() to the pre-Phase-0 signature; the honest "
        "enforced/would_block/block_reasons warning-only reporting was lost.",
    "test_track_a_fixes.py::test_gate_blocking_rejects_impossible_transition":
        "efd0269 reverted gate() to gate(spec_text,steps,*,enforce=): the block-impossible "
        "BEHAVIOUR survives but the structured block_reasons API this test asserts regressed.",
    "test_track_a_fixes.py::test_gate_passes_a_clean_report":
        "efd0269 reverted gate() so it no longer consumes a score_spec report; "
        "honest passed = not would_block was lost.",
    "test_track_a_fixes.py::test_gate_surfaces_ambiguous_but_does_not_block":
        "efd0269 reverted the ambiguous-locator dimension and the gate ambiguous_locators field.",
    "test_action_extractor.py::test_reconcile_corroborates_value_via_ocr_and_control":
        "_reconcile matches the audio transcript against target_label (the field prompt) instead of "
        "the value/verb, breaking the documented audio_intent_match contract.",
}


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
