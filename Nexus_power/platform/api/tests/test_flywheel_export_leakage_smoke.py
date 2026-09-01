"""Smoke test for the flywheel EXPORT scaffold + LEAKAGE GUARD + priors TIE-BREAK
BOOST (T5.2 consumption-side floor).

Proves the three safety invariants that let this ship BEFORE federated data exists,
default-off and never-green-wash:

  1. LEAKAGE GUARD (flywheel.leakage_guard): a de-identified payload that echoes a
     raw value/selector/name fails LOUD (LeakageError); a genuinely de-identified
     payload (enums/buckets/hashes only) passes. Short fragments don't false-positive.

  2. EXPORT k-ANON + DP-FLAG + ZERO-RAW (flywheel.export): aggregate_labels DROPS any
     group below the k-anonymity floor; add_dp_noise is flagged ``_dp_applied=False``
     (can never be mistaken for actually-private until the real mechanism lands); the
     aggregate is counts-only and carries the raw-rows-never-leave invariant. Pure;
     no DB / no SDK needed (lightweight stub rows).

  3. PRIORS TIE-BREAK BOOST (flywheel.leakage_guard.boost_confidence): with priors
     OFF (default) it is the IDENTITY (zero-prior invariant); it can NEVER touch an
     authoritative/refuse verdict (real_regression / needs_review); and even with a
     prior present it can only RAISE confidence by a bounded delta, never lower it,
     never change the verdict.

Run:
    python Nexus_power/platform/api/tests/test_flywheel_export_leakage_smoke.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# ── Path setup (SDK is only needed for ledger ORM, which this test avoids) ────
_API_ROOT = Path(__file__).resolve().parents[1]
_SDK_ROOT = _API_ROOT.parents[1] / "sdk" / "nexus-sdk"
sys.path.insert(0, str(_API_ROOT))
sys.path.insert(0, str(_SDK_ROOT))

from app.services.flywheel import export, leakage_guard  # noqa: E402

PASS = 0
FAIL = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  PASS  {label}")
    else:
        FAIL += 1
        print(f"  FAIL  {label}  {detail}")


def _clear_priors_env() -> None:
    for k in ("NEXUS_FLYWHEEL_PRIORS", "NEXUS_FLYWHEEL_PRIORS_PATH",
              "NEXUS_FLYWHEEL_PRIORS_KEY"):
        os.environ.pop(k, None)


# A tiny stand-in for a FlywheelLabelRow: aggregate_labels only reads the
# _GROUP_COLS attributes + verified_green / later_contradicted, so a plain object
# with those attributes is enough to exercise the pure aggregation (no DB/SDK).
class _StubRow:
    def __init__(self, **kw):
        # default every group col so getattr never raises
        for c in export._GROUP_COLS:
            setattr(self, c, kw.get(c))
        self.verified_green = kw.get("verified_green")
        self.later_contradicted = kw.get("later_contradicted")


def _row(pattern: str, *, verdict: str, verified_green=None) -> _StubRow:
    return _StubRow(error_pattern_class=pattern, engine_verdict_enum=verdict,
                    verb_enum="fill", observed_kind_enum="textbox",
                    verified_green=verified_green)


# ── 1. LEAKAGE GUARD ──────────────────────────────────────────────────────────

def test_leakage_guard_catches_raw_echo() -> None:
    print("leakage guard — a raw echo fails LOUD:")
    # The sensitive raw inputs a caller is about to featurize.
    raw = ["John Q. Smith", "$30,000", "#login-username-field", "policy_number_3811"]
    # A BUGGY de-id payload that accidentally echoes a raw selector verbatim.
    leaky = {
        "verb_enum": "fill",
        "value_shape_sig": "numeric",
        "label_token_hash": "ab12cd34",
        # bug: a raw selector leaked into a feature column
        "diff_shape_enum": "#login-username-field",
    }
    leaks = leakage_guard.find_leaks(raw, leaky)
    check("find_leaks detects the raw selector echo", len(leaks) == 1
          and leaks[0]["column"] == "diff_shape_enum", detail=str(leaks))
    raised = False
    try:
        leakage_guard.assert_no_leakage(raw, leaky)
    except leakage_guard.LeakageError:
        raised = True
    check("assert_no_leakage raises LeakageError on a leak", raised)


def test_leakage_guard_passes_clean_payload() -> None:
    print("leakage guard — a genuinely de-identified payload passes:")
    raw = ["John Q. Smith", "$30,000", "#login-username-field", "policy_number_3811"]
    clean = {
        "verb_enum": "fill",
        "emitted_method_enum": "fill",
        "observed_kind_enum": "textbox",
        "value_shape_sig": "numeric",       # shape of $30,000, not the value
        "options_count_bucket": "0",
        "label_token_hash": "deadbeefcafef00d",  # HMAC, not the name
        "error_pattern_class": "locator_missing",
        "engine_verdict_enum": "selector_drift",
    }
    check("find_leaks clean -> []", leakage_guard.find_leaks(raw, clean) == [])
    ok = True
    try:
        leakage_guard.assert_no_leakage(raw, clean)
    except leakage_guard.LeakageError:
        ok = False
    check("assert_no_leakage passes a clean payload", ok)


def test_leakage_guard_ignores_short_fragments() -> None:
    print("leakage guard — short tokens don't false-positive on enum fragments:")
    # 'id' (2 chars) is a substring of many enums; below min_len it must be ignored.
    raw = ["id", "a", "to"]
    payload = {"verb_enum": "fill", "observed_kind_enum": "textbox"}  # contains 'id','to'
    check("short raw fragments ignored (no false leak)",
          leakage_guard.find_leaks(raw, payload) == [])


# ── 2. EXPORT k-ANON + DP-FLAG + ZERO-RAW ───────────────────────────────────────

def test_export_k_anon_drops_small_groups() -> None:
    print("export — k-anonymity floor DROPS small groups, keeps large:")
    rows = []
    # 'locator_missing' group: 5 rows (>= k=3 -> kept), 3 verified-green
    for i in range(5):
        rows.append(_row("locator_missing", verdict="selector_drift",
                         verified_green=(i < 3)))
    # 'timeout' group: only 2 rows (< k=3 -> dropped)
    for _ in range(2):
        rows.append(_row("timeout", verdict="flake", verified_green=True))

    aggs = export.aggregate_labels(rows, k=3)
    by_pattern = {a["error_pattern_class"]: a for a in aggs}
    check("large group kept", "locator_missing" in by_pattern, detail=str(by_pattern))
    check("small group dropped (k-anon)", "timeout" not in by_pattern,
          detail=str(by_pattern))
    if "locator_missing" in by_pattern:
        g = by_pattern["locator_missing"]
        check("group is counts-only (count=5)", g["count"] == 5, detail=str(g))
        check("verified_green_count within group (3)",
              g["verified_green_count"] == 3, detail=str(g))
        # the raw-rows-never-leave invariant: payload has no raw text column
        check("no raw column in the aggregate group",
              all(k not in g for k in ("raw", "value", "selector", "name", "text")),
              detail=str(list(g.keys())))


def test_export_dp_is_flagged_not_private() -> None:
    print("export — DP noise stub is HONESTLY flagged (never green-washed as private):")
    rows = [_row("locator_missing", verdict="selector_drift") for _ in range(4)]
    noised = export.add_dp_noise(export.aggregate_labels(rows, k=3), epsilon=0.5)
    check("at least one group survived", len(noised) >= 1, detail=str(noised))
    if noised:
        check("_dp_applied is False (stub, not actually private)",
              noised[0]["_dp_applied"] is False, detail=str(noised[0]))
        check("_epsilon carried through", noised[0]["_epsilon"] == 0.5,
              detail=str(noised[0]))


def test_export_default_off() -> None:
    print("export — master gate DEFAULT OFF:")
    os.environ.pop("NEXUS_FLYWHEEL_EXPORT", None)
    check("export_enabled() False by default", export.export_enabled() is False)


# ── 3. PRIORS TIE-BREAK BOOST (boost-only rail) ─────────────────────────────────

def test_boost_identity_when_priors_off() -> None:
    print("boost — zero-prior == identity (priors OFF, default):")
    _clear_priors_env()
    for verdict in ("selector_drift", "visual_change", "flake",
                    "real_regression", "needs_review", "passed"):
        conf, note = leakage_guard.boost_confidence(
            verdict, 0.62, decision_point="control_kind_fix", feature_key="k")
        check(f"boost identity for '{verdict}' when off",
              conf == 0.62 and note is None, detail=f"({conf}, {note})")


def test_boost_never_touches_authoritative() -> None:
    print("boost — even ENABLED, an authoritative/refuse verdict is NEVER boosted:")
    os.environ["NEXUS_FLYWHEEL_PRIORS"] = "1"  # enable the gate (no priors file -> no prior)
    try:
        for verdict in ("real_regression", "needs_review"):
            conf, note = leakage_guard.boost_confidence(
                verdict, 0.9, decision_point="control_kind_fix", feature_key="k")
            check(f"'{verdict}' never boosted", conf == 0.9 and note is None,
                  detail=f"({conf}, {note})")
        # A non-authoritative verdict with NO prior present is also identity.
        conf, note = leakage_guard.boost_confidence(
            "selector_drift", 0.62, decision_point="control_kind_fix", feature_key="k")
        check("no prior present -> identity for dismissable verdict",
              conf == 0.62 and note is None, detail=f"({conf}, {note})")
    finally:
        _clear_priors_env()


def test_boost_is_bounded_and_raise_only() -> None:
    print("boost — with a prior present, raise-only + bounded delta (monkeypatched prior):")
    os.environ["NEXUS_FLYWHEEL_PRIORS"] = "1"
    orig_prior_for = leakage_guard.prior_for
    try:
        # Inject a strong, well-supported prior directly (bypasses signed-file load;
        # we are testing the BOUND/RAIL, not the loader — that's covered elsewhere).
        leakage_guard.prior_for = lambda dp, fk: {"support": 500, "p_correct": 1.0}
        base = 0.62
        boosted, note = leakage_guard.boost_confidence(
            "selector_drift", base, decision_point="control_kind_fix", feature_key="k")
        check("boost RAISES confidence", boosted > base, detail=f"{base}->{boosted}")
        check("boost bounded by _MAX_BOOST_DELTA",
              boosted <= base + leakage_guard._MAX_BOOST_DELTA + 1e-9,
              detail=f"{base}->{boosted} (max delta {leakage_guard._MAX_BOOST_DELTA})")
        check("boost never exceeds the ceiling",
              boosted <= leakage_guard._BOOST_CEILING + 1e-9, detail=str(boosted))
        check("boost emits an honest tie-break note", note is not None and "tie-break" in note,
              detail=str(note))

        # A thin prior (support below the bar) must be IGNORED.
        leakage_guard.prior_for = lambda dp, fk: {"support": 3, "p_correct": 1.0}
        conf2, note2 = leakage_guard.boost_confidence(
            "selector_drift", base, decision_point="control_kind_fix", feature_key="k")
        check("thin prior ignored (identity)", conf2 == base and note2 is None,
              detail=f"({conf2}, {note2})")

        # Boost must never LOWER an already-high confidence.
        leakage_guard.prior_for = lambda dp, fk: {"support": 500, "p_correct": 1.0}
        hi = 0.94
        conf3, _ = leakage_guard.boost_confidence(
            "selector_drift", hi, decision_point="control_kind_fix", feature_key="k")
        check("boost never lowers confidence", conf3 >= hi, detail=f"{hi}->{conf3}")
    finally:
        leakage_guard.prior_for = orig_prior_for
        _clear_priors_env()


if __name__ == "__main__":
    test_leakage_guard_catches_raw_echo()
    test_leakage_guard_passes_clean_payload()
    test_leakage_guard_ignores_short_fragments()
    test_export_k_anon_drops_small_groups()
    test_export_dp_is_flagged_not_private()
    test_export_default_off()
    test_boost_identity_when_priors_off()
    test_boost_never_touches_authoritative()
    test_boost_is_bounded_and_raise_only()
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)
