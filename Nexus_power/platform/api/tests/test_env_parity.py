"""Multi-env Phase 3 — cross-env parity (pure, honest-by-construction).

The safety property: a DIVERGENCE is reported ONLY from PROVEN-grounded observed
values (or differing verdict labels); anything unverified is `incomparable`, never
a divergence and never a silent "match".
"""
from __future__ import annotations

from app.services.env_parity import compute_parity, observed_value_from_error


def test_observed_value_from_nxnum_failure():
    err = "value oracle: unexpected value — expected 75.0 ±0.01 but received 72.0"
    assert observed_value_from_error(err) == "72.0"


def test_observed_value_from_containstext_failure():
    err = 'expect(locator).toContainText\n  Expected string: "UAT"\n  Received string: "Environment: PROD"'
    assert observed_value_from_error(err) == "Environment: PROD"


def test_non_value_error_has_no_observed_value():
    assert observed_value_from_error("locator.click: Timeout 30000ms exceeded") == ""
    assert observed_value_from_error("") == ""
    # a number NOT tied to 'unexpected value' is not a grounded observed value
    assert observed_value_from_error("retry 3 received 200 OK") == ""


def test_parity_diverged_from_grounded_values():
    out = compute_parity([
        {"test_id": "t1", "environment": "uat", "verdict": "passed", "observed": "75.0"},
        {"test_id": "t1", "environment": "prod", "verdict": "real_regression", "observed": "72.0"},
    ])
    t = out["tests"][0]
    assert t["status"] == "diverged" and out["diverged"] == 1
    assert t["grounded_values"] == {"uat": "75.0", "prod": "72.0"}


def test_parity_match_when_grounded_values_equal():
    out = compute_parity([
        {"test_id": "t1", "environment": "dev", "verdict": "real_regression", "observed": "72.00"},
        {"test_id": "t1", "environment": "prod", "verdict": "real_regression", "observed": "72.0"},
    ])
    assert out["tests"][0]["status"] == "match" and out["diverged"] == 0


def test_parity_verdict_diverged_without_grounded_values():
    out = compute_parity([
        {"test_id": "t1", "environment": "uat", "verdict": "passed", "observed": ""},
        {"test_id": "t1", "environment": "prod", "verdict": "real_regression", "observed": ""},
    ])
    assert out["tests"][0]["status"] == "verdict_diverged" and out["diverged"] == 1


def test_parity_incomparable_when_unverified_and_labels_agree():
    # both passed, no grounded value → we CANNOT assert parity → incomparable,
    # NEVER a silent "match" (the green-wash trap).
    out = compute_parity([
        {"test_id": "t1", "environment": "uat", "verdict": "passed", "observed": ""},
        {"test_id": "t1", "environment": "prod", "verdict": "passed", "observed": ""},
    ])
    assert out["tests"][0]["status"] == "incomparable" and out["diverged"] == 0
    assert out["compared"] == 0 and out["incomparable"] == 1


def test_single_env_is_not_compared():
    out = compute_parity([{"test_id": "t1", "environment": "uat", "verdict": "passed", "observed": "75"}])
    assert out["tests"] == []


def test_tolerant_on_garbage():
    assert compute_parity([]) == {
        "tests": [], "environments": [], "diverged": 0, "compared": 0, "incomparable": 0}
    assert compute_parity([None, "x", {"no_env": 1}])["tests"] == []
