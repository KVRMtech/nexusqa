"""R7 — External Dependency Failure: the origin gate on the network oracle.

Audit finding: classify_network_signal took the worst 5xx from ANY origin, so a
third-party outage (analytics, CDN, payment sandbox) would be misclassified as
the client's own Application Defect — a doctrine violation. A foreign-origin
failure now surfaces as advisory ``external_dependency`` (never a real-bug
signal) and a same-origin signal always wins.
"""
from app.services.test_factory.network_oracle import (
    classify_network_signal,
    detect,
    is_real_bug_signal,
)

BASE = "vkpowerlife.35-186-147-245.sslip.io"


def _e(url, status=0, error=None):
    d = {"url": url, "method": "GET", "status": status}
    if error:
        d["error"] = error
    return d


def test_foreign_5xx_is_external_dependency_not_app_defect():
    sig = classify_network_signal(
        [_e("https://analytics.example.com/collect", status=503)], base_host=BASE)
    assert sig and sig["kind"] == "external_dependency"
    assert "third-party" in sig["detail"]
    assert is_real_bug_signal(sig) is False, \
        "a third-party outage must NEVER count as the app's own defect"


def test_same_origin_5xx_still_a_real_bug_and_beats_foreign():
    sig = classify_network_signal([
        _e("https://cdn.example.com/app.js", status=500),
        _e(f"https://{BASE}/api/quote", status=500),
    ], base_host=BASE)
    assert sig["kind"] == "server_error"
    assert BASE in sig["url"]
    assert is_real_bug_signal(sig) is True


def test_subdomain_counts_as_same_app():
    sig = classify_network_signal(
        [_e(f"https://api.{BASE}/quote", status=502)], base_host=BASE)
    assert sig["kind"] == "server_error"


def test_no_base_host_keeps_legacy_behaviour():
    sig = classify_network_signal(
        [_e("https://anything.example.com/x", status=500)])
    assert sig["kind"] == "server_error"  # unchanged for legacy callers


def test_foreign_network_failure_is_external_and_same_origin_4xx_wins_nothing_false():
    sig = classify_network_signal([
        _e("https://fonts.example.com/f.woff2", error="net::ERR_FAILED"),
        _e(f"https://{BASE}/api/opts", status=404),
    ], base_host=BASE)
    # Same-origin 4xx is advisory client_error and outranks the foreign advisory.
    assert sig["kind"] == "client_error"
    assert is_real_bug_signal(sig) is False


def test_detect_derives_base_host_from_observed():
    sig = detect(
        {"network": [_e("https://tracker.example.net/px", status=500)]},
        observed={"url": f"https://{BASE}/quote"},
    )
    assert sig and sig["kind"] == "external_dependency"


def test_defect_report_wiring_smoke():
    """R5 — build_defect had ZERO call sites; it is now wired into the confirmed
    REAL_REGRESSION stop. Prove the exact call shape used by the router works
    and yields a filing-ready markdown."""
    from nexus_sdk.models import ProductionTestCase, ProductionTestStep
    from app.services.test_factory.defect_report import build_defect, defect_to_markdown

    tc = ProductionTestCase(
        test_id="t1", name="Quote flow", description="d",
        steps=[
            ProductionTestStep(step_number=1, action="Open https://a/quote",
                               expected="page shows", expected_result="page shows"),
            ProductionTestStep(step_number=2, action="Click 'Calculate my premium'",
                               expected="proceeds to result", expected_result="proceeds to result"),
            ProductionTestStep(step_number=3, action="Verify the application navigated to https://a/quote?submitted=1",
                               expected="URL is the result", expected_result="URL is the result"),
        ],
        priority="P0_critical", type="functional", tags=[],
    )
    d = build_defect(
        tc=tc, failing_step_number=2,
        diag={"cause": "REAL_REGRESSION", "cause_label": "outcome contradicted"},
        network={"kind": "server_error", "detail": "POST /api/quote -> 500"},
        error_message="expect(page).toHaveURL failed", base_url="https://a/quote",
        scenario_id="t1",
    )
    assert d["severity"] == "critical"          # a downstream step is blocked
    assert d["precise_failure"]["step_number"] == 2
    md = defect_to_markdown(d)
    assert md.startswith("# ")
    assert "step 2" in md and "Calculate my premium" in md
