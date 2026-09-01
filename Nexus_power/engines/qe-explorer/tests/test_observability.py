"""M0.6 — the observability contract for the crawl lifecycle.

These tests exist to hold three properties that are easy to build and easy to
lose:

  1. ``/metrics`` is scrapeable, valid, and carries the crawl-lifecycle families.
  2. EVERY terminal path — completed, cancelled, timed out, failed — emits the
     terminal signal, and a crawl that never consulted its oracle is
     DISTINGUISHABLE from a healthy one.
  3. No unbounded value ever reaches a Prometheus label.

The third is the one that would take production down rather than merely blind
it, so it is asserted structurally (scan the whole exposition for a crawl id)
rather than by reviewing call sites.
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from prometheus_client.parser import text_string_to_metric_families

from app import crawl_context, metrics


@pytest.fixture(autouse=True)
def _fresh_registry():
    """Rebuild the metric registry per test.

    Counters are process-lifetime in production (a reset would read to
    Prometheus as a restart), so the ONLY legitimate reset is this one.
    """
    metrics.reset_for_tests()
    yield
    metrics.reset_for_tests()


def _families() -> dict:
    payload, _ = metrics.render_latest()
    return {f.name: f for f in
            text_string_to_metric_families(payload.decode("utf-8"))}


def _value(family_name: str, sample_name: str = "", **labels) -> float:
    """Sum the samples of ``family_name`` matching ``labels``."""
    fam = _families().get(family_name)
    if fam is None:
        return 0.0
    target = sample_name or (family_name + "_total")
    total = 0.0
    for sample in fam.samples:
        if sample.name != target:
            continue
        if all(sample.labels.get(k) == v for k, v in labels.items()):
            total += sample.value
    return total


# ══ T-OB-01 — the endpoint ════════════════════════════════════════════════


def test_metrics_endpoint_serves_valid_prometheus_exposition():
    from app.main import app

    with TestClient(app) as client:
        response = client.get("/metrics")

    assert response.status_code == 200
    body = response.text
    # Parsing IS the validity assertion: the parser rejects malformed exposition.
    families = {f.name for f in text_string_to_metric_families(body)}
    assert families, "exposition parsed to zero families"
    assert "nexus_crawls_started" in families


def test_metrics_endpoint_needs_no_token():
    """A Prometheus scraper holds no X-QEC-Token; /metrics must not demand one.

    The same public posture /health already has. If this regresses, every scrape
    401s, `up` pins to 0, and the death alert fires on a healthy explorer.
    """
    from app.main import app

    with TestClient(app) as client:
        assert client.get("/metrics").status_code == 200
        # Contrast: the crawl API still refuses an untokened caller.
        assert client.post("/api/v1/explore", json={}).status_code in (401, 422)


def test_expected_metric_families_are_all_exposed():
    """≥10 useful crawl metrics is the acceptance bar; we expose 21 families."""
    # Touch every family so counters with labels materialise at least one sample.
    _exercise_one_of_everything()
    present = set(_families())
    missing = {n for n in metrics.EXPECTED_METRIC_NAMES if n not in present}
    assert not missing, f"missing metric families: {sorted(missing)}"
    assert len(metrics.EXPECTED_METRIC_NAMES) >= 10


def test_metrics_survive_when_prometheus_is_absent(monkeypatch):
    """Import-guarded: with metrics disabled every helper is a hard no-op."""
    monkeypatch.setattr(metrics, "_M", {}, raising=False)
    monkeypatch.setattr(metrics, "_ENABLED", False, raising=False)
    metrics.record_crawl_started(crawl_mode="e2e", traversal="full")
    metrics.record_crawl_terminal(stop_reason="completed")
    payload, content_type = metrics.render_latest()
    assert b"disabled" in payload or payload.startswith(b"#")
    assert "text/plain" in content_type


def _exercise_one_of_everything() -> None:
    metrics.set_build_info(version="qe-explorer/1.0+t", refuse_pack_version="1")
    metrics.record_dispatch(outcome="accepted")
    metrics.record_dispatch(outcome="busy_409")
    metrics.record_crawl_started(crawl_mode="e2e", traversal="full")
    metrics.record_oracle_call(oracle="advance", outcome="picked",
                               duration_seconds=0.4)
    metrics.record_oracle_call(oracle="advance", outcome="unavailable",
                               duration_seconds=8.0, failure_reason="timeout")
    metrics.record_llm_usage(provider="anthropic", model="claude", outcome="success",
                             prompt_tokens=100, completion_tokens=20,
                             estimated_cost_usd=0.001)
    metrics.record_crawl_terminal(
        stop_reason="completed", duration_seconds=12.0, max_depth=4, states=9,
        guard_blocks=2, oracle_configured=True, oracle_calls=2)
    metrics.record_crawl_terminal(stop_reason="cancelled", duration_seconds=1.0)
    metrics.record_crawl_terminal(stop_reason="budget_max_wall_ms",
                                  duration_seconds=1800.0)
    metrics.record_crawl_terminal(stop_reason="error", failed=True,
                                  failure_kind="browser_launch")


# ══ T-OB-02 — the crawl lifecycle ═════════════════════════════════════════


def test_crawl_start_increments():
    metrics.record_crawl_started(crawl_mode="e2e", traversal="full")
    assert _value("nexus_crawls_started", crawl_mode="e2e", traversal="full") == 1
    assert _value("nexus_crawl_in_flight", "nexus_crawl_in_flight") == 1


def test_crawl_completion_increments_and_clears_in_flight():
    metrics.record_crawl_started(crawl_mode="explore", traversal="probe")
    metrics.record_crawl_terminal(stop_reason="completed", duration_seconds=5.0,
                                  max_depth=3, states=7)
    assert _value("nexus_crawls_completed", terminal_reason="completed") == 1
    assert _value("nexus_crawl_in_flight", "nexus_crawl_in_flight") == 0


def test_terminal_reason_is_recorded_from_the_native_stop_reason():
    metrics.record_crawl_terminal(stop_reason="budget_max_states")
    metrics.record_crawl_terminal(stop_reason="auth_required_no_credentials")
    assert _value("nexus_crawls_completed", terminal_reason="budget_max_states") == 1
    assert _value("nexus_crawls_completed", terminal_reason="auth_required") == 1


def test_wall_budget_is_reported_as_a_timeout():
    """The wall-clock budget IS the crawl timeout — the operator asks for it by
    that name, so it must not require tribal knowledge of the budget vocabulary."""
    metrics.record_crawl_terminal(stop_reason="budget_max_wall_ms",
                                  duration_seconds=1800.0)
    assert _value("nexus_crawls_timed_out", "nexus_crawls_timed_out_total") == 1
    assert _value("nexus_crawls_completed", terminal_reason="timeout") == 1


def test_cancellation_is_recorded():
    metrics.record_crawl_terminal(stop_reason="cancelled", duration_seconds=2.0)
    assert _value("nexus_crawls_cancelled", "nexus_crawls_cancelled_total") == 1


def test_depth_is_recorded_as_a_distribution_and_a_high_water_mark():
    metrics.record_crawl_terminal(stop_reason="completed", max_depth=3)
    metrics.record_crawl_terminal(stop_reason="completed", max_depth=7)
    metrics.record_crawl_terminal(stop_reason="completed", max_depth=2)
    assert _value("nexus_crawl_max_depth", "nexus_crawl_max_depth_count",
                  terminal_reason="completed") == 3
    assert _value("nexus_crawl_max_depth", "nexus_crawl_max_depth_sum",
                  terminal_reason="completed") == 12
    # The gauge is a high-water mark, so the shallow third crawl must not lower it.
    assert _value("nexus_crawl_deepest_depth_observed",
                  "nexus_crawl_deepest_depth_observed") == 7


def test_duration_is_recorded_as_a_histogram():
    metrics.record_crawl_terminal(stop_reason="completed", duration_seconds=42.0)
    assert _value("nexus_crawl_duration_seconds",
                  "nexus_crawl_duration_seconds_sum",
                  terminal_reason="completed") == 42.0


def test_oracle_calls_latency_and_failures_are_recorded():
    metrics.record_oracle_call(oracle="advance", outcome="picked",
                               duration_seconds=0.5)
    metrics.record_oracle_call(oracle="advance", outcome="unavailable",
                               duration_seconds=1.5, failure_reason="transport")
    assert _value("nexus_crawl_oracle_calls", oracle="advance",
                  outcome="picked") == 1
    assert _value("nexus_crawl_oracle_failures", oracle="advance",
                  reason="transport") == 1
    assert _value("nexus_crawl_oracle_latency_seconds",
                  "nexus_crawl_oracle_latency_seconds_count",
                  oracle="advance") == 2


def test_dispatch_409_is_recorded_on_both_the_funnel_and_the_alert_target():
    metrics.record_dispatch(outcome="accepted")
    metrics.record_dispatch(outcome="busy_409")
    metrics.record_dispatch(outcome="busy_409")
    assert _value("nexus_crawl_dispatch_attempts", outcome="busy_409") == 2
    assert _value("nexus_crawl_dispatch_409", "nexus_crawl_dispatch_409_total") == 2


def test_dispatch_409_reaches_metrics_through_the_real_endpoint():
    """End-to-end through the HTTP layer: the single-flight refusal is counted.

    Asserting on the helper alone would not catch the 409 being raised from a
    path that never reaches the instrumentation.
    """
    from app.config import settings
    from app.main import JobManager, app

    with TestClient(app) as client:
        jobs: JobManager = app.state.jobs
        # M0.5 T-SEC-03: the slot is owned by a TENANT, so claiming it names one.
        jobs.accept("already-running-crawl", "tenant-holding-the-slot")
        try:
            response = client.post(
                "/api/v1/explore",
                headers={"X-QEC-Token": settings.explorer_token},
                json={"crawl_id": "second-crawl", "tenant_id": "t1",
                      "target_url": "https://example.test/"},
            )
        finally:
            jobs.finish("already-running-crawl", None)

    assert response.status_code == 409
    assert _value("nexus_crawl_dispatch_409", "nexus_crawl_dispatch_409_total") == 1


# ══ Error paths — instrumentation must not be success-only ════════════════


@pytest.mark.parametrize("stop_reason,expect_failed", [
    ("completed", False),
    ("cancelled", False),
    ("budget_max_wall_ms", False),
    ("error", True),
    ("auth_failed", False),
])
def test_every_terminal_path_emits_a_terminal_signal(stop_reason, expect_failed):
    metrics.record_crawl_started(crawl_mode="e2e", traversal="full")
    metrics.record_crawl_terminal(stop_reason=stop_reason, duration_seconds=1.0,
                                  failed=expect_failed,
                                  failure_kind="crawl_exception" if expect_failed else "")
    reason = metrics.terminal_reason_for(stop_reason)
    assert _value("nexus_crawls_completed", terminal_reason=reason) == 1
    # In-flight must return to zero on EVERY path, or a crashed crawl leaks the
    # gauge upward forever and the dashboard reads permanently busy.
    assert _value("nexus_crawl_in_flight", "nexus_crawl_in_flight") == 0


def test_failed_crawl_records_a_bounded_failure_class():
    metrics.record_crawl_terminal(stop_reason="error", failed=True,
                                  failure_kind="browser_launch")
    assert _value("nexus_crawls_failed", failure_kind="browser_launch") == 1


def test_a_failed_crawl_still_reports_depth_duration_and_oracle_state():
    """Failure must not suppress the rest of the terminal record.

    Suppressing them on the error path is exactly how error-path blindness gets
    built in: the crawls you most need to understand become the ones with no data.
    """
    metrics.record_crawl_terminal(
        stop_reason="error", duration_seconds=9.0, max_depth=2, states=3,
        oracle_configured=True, oracle_calls=0, failed=True,
        failure_kind="crawl_exception")
    assert _value("nexus_crawl_duration_seconds",
                  "nexus_crawl_duration_seconds_sum", terminal_reason="error") == 9.0
    assert _value("nexus_crawl_max_depth", "nexus_crawl_max_depth_count",
                  terminal_reason="error") == 1
    assert _value("nexus_crawls_no_oracle", terminal_reason="error",
                  expected="no") == 1


# ══ THE NO-ORACLE SIGNAL ══════════════════════════════════════════════════


def test_zero_oracle_crawl_with_an_oracle_wired_is_flagged_unexpected():
    """The silent failure: an oracle WAS wired and never answered."""
    metrics.record_crawl_started(crawl_mode="e2e", traversal="full")
    metrics.record_crawl_terminal(stop_reason="completed", duration_seconds=30.0,
                                  max_depth=5, oracle_configured=True,
                                  oracle_calls=0)
    assert _value("nexus_crawls_no_oracle", terminal_reason="completed",
                  expected="no") == 1
    assert _value("nexus_crawls_completed", terminal_reason="completed",
                  oracle_state="configured_unused") == 1


def test_zero_oracle_crawl_with_no_oracle_wired_is_expected():
    """The legitimate case: a probe-posture crawl never wires an oracle.

    It must NOT contaminate the unexpected series, or the alert drowns and the
    real signal becomes unreadable.
    """
    metrics.record_crawl_terminal(stop_reason="completed", oracle_configured=False,
                                  oracle_calls=0)
    assert _value("nexus_crawls_no_oracle", terminal_reason="completed",
                  expected="yes") == 1
    assert _value("nexus_crawls_no_oracle", terminal_reason="completed",
                  expected="no") == 0
    assert _value("nexus_crawls_completed", terminal_reason="completed",
                  oracle_state="unconfigured") == 1


def test_crawl_that_used_the_oracle_is_not_flagged_at_all():
    metrics.record_crawl_terminal(stop_reason="completed", oracle_configured=True,
                                  oracle_calls=3)
    assert _value("nexus_crawls_no_oracle", terminal_reason="completed") == 0
    assert _value("nexus_crawls_completed", terminal_reason="completed",
                  oracle_state="used") == 1


def test_alert_expression_shape_selects_only_unexpected_zero_oracle_crawls():
    """The dashboard/alert selector must isolate the failure from the norm.

    Mirrors VerdictCrawlsCompletingWithoutOracle's
    ``nexus_crawls_no_oracle_total{expected="no"}`` selector.
    """
    for _ in range(4):
        metrics.record_crawl_terminal(stop_reason="completed",
                                      oracle_configured=False, oracle_calls=0)
    metrics.record_crawl_terminal(stop_reason="completed", oracle_configured=True,
                                  oracle_calls=0)
    assert _value("nexus_crawls_no_oracle", expected="no") == 1
    assert _value("nexus_crawls_no_oracle", expected="yes") == 4


@pytest.mark.parametrize("configured,calls,expected_state", [
    (True, 0, metrics.ORACLE_CONFIGURED_UNUSED),
    (False, 0, metrics.ORACLE_UNCONFIGURED),
    (True, 1, metrics.ORACLE_USED),
    (False, 1, metrics.ORACLE_USED),
])
def test_oracle_state_classification(configured, calls, expected_state):
    assert metrics.oracle_state_for(
        oracle_configured=configured, oracle_calls=calls) == expected_state


# ══ CARDINALITY — the property that protects production ═══════════════════


def test_crawl_id_never_appears_as_a_prometheus_label():
    """The single most important cardinality assertion.

    Recorded with a distinctive crawl id, then the ENTIRE exposition is scanned:
    a structural check, not a review of call sites, so a future contributor who
    adds a crawl_id label anywhere fails this test.
    """
    marker = "crawl-id-7f3a9c-do-not-label"
    crawl_context.bind_crawl(crawl_id=marker, tenant_id="tenant-abc-secret")
    metrics.record_crawl_started(crawl_mode="e2e", traversal="full")
    metrics.record_oracle_call(oracle="advance", outcome="picked",
                               duration_seconds=0.2)
    metrics.record_llm_usage(provider="anthropic", model="claude",
                             prompt_tokens=5, completion_tokens=1)
    metrics.record_crawl_terminal(stop_reason="completed", duration_seconds=1.0,
                                  max_depth=2, oracle_configured=True,
                                  oracle_calls=1)

    payload, _ = metrics.render_latest()
    body = payload.decode("utf-8")
    assert marker not in body
    assert "tenant-abc-secret" not in body

    for family in text_string_to_metric_families(body):
        for sample in family.samples:
            forbidden = {"crawl_id", "tenant_id", "application_id", "app_id",
                         "url", "target_url", "user", "user_id", "request_id",
                         "correlation_id", "prompt", "error", "exception",
                         "message", "test_name"}
            overlap = forbidden & set(sample.labels)
            assert not overlap, (
                f"{family.name} exposes forbidden label(s) {overlap}")


def test_every_label_key_in_the_exposition_is_on_the_approved_list():
    """Whitelist the label KEYS themselves, so a new dimension is a decision."""
    _exercise_one_of_everything()
    approved = {
        # bounded enums
        "terminal_reason", "oracle_state", "expected", "failure_kind",
        "crawl_mode", "traversal", "oracle", "outcome", "reason", "kind",
        # cardinality-capped, config-derived
        "provider", "model", "version", "refuse_pack_version",
        # histogram bucket boundary — bounded by the bucket list in code
        "le",
        # prometheus_client's own process/platform collectors
        "implementation", "major", "minor", "patchlevel", "generation",
    }
    payload, _ = metrics.render_latest()
    seen = set()
    for family in text_string_to_metric_families(payload.decode("utf-8")):
        for sample in family.samples:
            seen |= set(sample.labels)
    assert seen <= approved, f"unapproved label keys: {sorted(seen - approved)}"


def test_an_exception_message_can_never_become_a_terminal_reason():
    """Bounded vocabulary, enforced — not merely documented."""
    metrics.record_crawl_terminal(
        stop_reason="TimeoutError: page.goto exceeded 30000ms at https://x/y?tok=s3cret")
    assert _value("nexus_crawls_completed", terminal_reason="other") == 1
    payload, _ = metrics.render_latest()
    assert b"s3cret" not in payload


def test_unbounded_model_names_fold_into_other():
    """A provider reporting a per-request model id costs ONE extra series."""
    for i in range(metrics.MAX_LABEL_VALUES_PER_KEY + 25):
        metrics.record_llm_usage(provider="openai", model=f"gpt-req-{i}",
                                 prompt_tokens=1, completion_tokens=1)
    models = set()
    for family in text_string_to_metric_families(
            metrics.render_latest()[0].decode("utf-8")):
        for sample in family.samples:
            if "model" in sample.labels:
                models.add(sample.labels["model"])
    assert "other" in models
    assert len(models) <= metrics.MAX_LABEL_VALUES_PER_KEY + 2


def test_label_values_are_sanitised_of_exposition_syntax():
    metrics.record_llm_usage(provider='evil"} nexus_fake{x="1', model="m",
                             prompt_tokens=1)
    payload, _ = metrics.render_latest()
    # Parsing still succeeds ⇒ nothing was injected into the exposition.
    families = {f.name for f in
                text_string_to_metric_families(payload.decode("utf-8"))}
    assert "nexus_fake" not in families


def test_recording_never_raises_into_the_caller():
    """Metrics are observability, never correctness."""
    metrics.record_crawl_terminal(stop_reason=None, duration_seconds="not-a-number",
                                  max_depth="deep", states=None)
    metrics.record_oracle_call(oracle=None, outcome=None, duration_seconds=None)
    metrics.record_llm_usage(prompt_tokens="x", completion_tokens=None)


# ══ T-OB-03 / per-crawl token accounting ══════════════════════════════════


def test_prompt_and_completion_tokens_are_captured_and_totalled():
    usage = crawl_context.CrawlTokenUsage(crawl_id="c1")
    usage.record(provider="anthropic", model="claude", prompt_tokens=120,
                 completion_tokens=30)
    assert usage.prompt_tokens == 120
    assert usage.completion_tokens == 30
    assert usage.total_tokens == 150
    assert usage.calls == 1


def test_multiple_llm_calls_aggregate():
    usage = crawl_context.CrawlTokenUsage(crawl_id="c1")
    for _ in range(4):
        usage.record(provider="anthropic", model="claude", prompt_tokens=10,
                     completion_tokens=5)
    assert usage.calls == 4
    assert usage.total_tokens == 60


def test_every_retry_attempt_is_accounted_for():
    """A retried request consumed prompt tokens twice; both must be counted."""
    usage = crawl_context.CrawlTokenUsage(crawl_id="c1")
    usage.record(provider="openai", model="gpt", prompt_tokens=100,
                 completion_tokens=0)   # attempt 1 — failed after billing
    usage.record(provider="openai", model="gpt", prompt_tokens=100,
                 completion_tokens=25)  # attempt 2 — succeeded
    assert usage.calls == 2
    assert usage.prompt_tokens == 200
    assert usage.total_tokens == 225


def test_usage_from_an_errored_call_is_retained():
    usage = crawl_context.CrawlTokenUsage(crawl_id="c1")
    usage.record(provider="anthropic", model="claude", prompt_tokens=500,
                 completion_tokens=0)
    assert usage.total_tokens == 500
    assert usage.calls_missing_usage == 0


def test_a_call_with_no_reported_usage_is_a_visible_gap_not_a_free_call():
    usage = crawl_context.CrawlTokenUsage(crawl_id="c1")
    usage.record(provider="ollama", model="llama")
    assert usage.calls == 1
    assert usage.calls_missing_usage == 1
    assert usage.total_tokens == 0


def test_streaming_usage_aggregates_whether_recorded_per_chunk_or_once():
    once = crawl_context.CrawlTokenUsage(crawl_id="s1")
    once.record(provider="openai", model="gpt", prompt_tokens=80,
                completion_tokens=40)

    chunked = crawl_context.CrawlTokenUsage(crawl_id="s2")
    for delta in (10, 10, 10, 10):
        chunked.record(provider="openai", model="gpt", completion_tokens=delta)
    chunked.record(provider="openai", model="gpt", prompt_tokens=80)

    assert once.total_tokens == chunked.total_tokens == 120


def test_cache_tokens_are_tracked_but_never_folded_into_the_total():
    usage = crawl_context.CrawlTokenUsage(crawl_id="c1")
    usage.record(provider="anthropic", model="claude", prompt_tokens=10,
                 completion_tokens=5, cache_read_tokens=900,
                 cache_creation_tokens=100)
    assert usage.total_tokens == 15, "cache tokens double-count the same prompt"
    assert usage.cache_read_tokens == 900


def test_tokens_and_dollars_stay_separate_concepts():
    usage = crawl_context.CrawlTokenUsage(crawl_id="c1")
    usage.record(provider="anthropic", model="claude", prompt_tokens=1000,
                 completion_tokens=1000, estimated_cost_usd=0.03)
    data = usage.as_dict()
    assert data["total_tokens"] == 2000
    assert data["estimated_cost_usd"] == 0.03
    assert data["total_tokens"] != data["estimated_cost_usd"]


def test_provider_model_breakdown_is_kept():
    usage = crawl_context.CrawlTokenUsage(crawl_id="c1")
    usage.record(provider="anthropic", model="claude", prompt_tokens=10,
                 completion_tokens=5)
    usage.record(provider="openai", model="gpt", prompt_tokens=20,
                 completion_tokens=1)
    breakdown = {(e["provider"], e["model"]): e
                 for e in usage.as_dict()["by_provider_model"]}
    assert breakdown[("anthropic", "claude")]["total_tokens"] == 15
    assert breakdown[("openai", "gpt")]["total_tokens"] == 21


@pytest.mark.parametrize("body,prompt,completion", [
    # The platform LLM router's normalised shape.
    ({"prompt_tokens": 11, "completion_tokens": 3}, 11, 3),
    # OpenAI, nested.
    ({"usage": {"prompt_tokens": 11, "completion_tokens": 3}}, 11, 3),
    # Anthropic spelling.
    ({"usage": {"input_tokens": 11, "output_tokens": 3}}, 11, 3),
    # Ollama spelling.
    ({"prompt_eval_count": 11, "eval_count": 3}, 11, 3),
    # Nothing reported ⇒ None, never a guess.
    ({"text": "hello"}, None, None),
    (None, None, None),
])
def test_provider_specific_usage_shapes_normalise(body, prompt, completion):
    """Provider formats are normalised, never silently discarded."""
    usage = crawl_context.usage_from_response(body)
    assert usage["prompt_tokens"] == prompt
    assert usage["completion_tokens"] == completion


def test_llm_token_metrics_are_aggregate_only():
    metrics.record_llm_usage(provider="anthropic", model="claude",
                             prompt_tokens=100, completion_tokens=25,
                             cache_read_tokens=7)
    assert _value("nexus_crawl_llm_tokens", provider="anthropic", model="claude",
                  kind="prompt") == 100
    assert _value("nexus_crawl_llm_tokens", provider="anthropic", model="claude",
                  kind="completion") == 25
    assert _value("nexus_crawl_llm_tokens", provider="anthropic", model="claude",
                  kind="cache_read") == 7
    assert _value("nexus_crawl_llm_calls", provider="anthropic", model="claude",
                  outcome="success") == 1


def test_estimated_cost_is_a_separate_series_from_tokens():
    metrics.record_llm_usage(provider="anthropic", model="claude",
                             prompt_tokens=1000, estimated_cost_usd=0.015)
    assert _value("nexus_crawl_llm_estimated_cost_usd", provider="anthropic",
                  model="claude") == 0.015
    assert _value("nexus_crawl_llm_tokens", provider="anthropic", model="claude",
                  kind="prompt") == 1000


# ══ Correlation + structured logging ══════════════════════════════════════


def test_crawl_id_propagates_onto_outbound_oracle_headers():
    """Correlation is PROPAGATED across the process hop, not reconstructed."""
    crawl_context.bind_crawl(crawl_id="crawl-42", tenant_id="t1")
    headers = crawl_context.crawl_headers({"Content-Type": "application/json"})
    assert headers[crawl_context.HEADER_CRAWL_ID] == "crawl-42"
    assert headers[crawl_context.HEADER_REQUEST_ID]
    assert headers["Content-Type"] == "application/json"


def test_a_hostile_crawl_id_is_rejected_rather_than_echoed():
    crawl_context.bind_crawl(crawl_id="bad id\r\nX-Injected: 1", tenant_id="t")
    assert crawl_context.current_crawl_id() == ""
    assert crawl_context.HEADER_CRAWL_ID not in crawl_context.crawl_headers()


def test_lifecycle_events_carry_correlation_and_are_machine_readable(caplog):
    crawl_context.bind_crawl(crawl_id="crawl-99", tenant_id="tenant-7")
    with caplog.at_level("INFO", logger="qe-explorer.telemetry"):
        crawl_context.emit(crawl_context.EV_CRAWL_TERMINAL,
                           terminal_reason="completed", oracle_calls=3,
                           max_depth=5)
    line = next(r.getMessage() for r in caplog.records
                if "qec.crawl.event" in r.getMessage())
    payload = json.loads(line.split("qec.crawl.event ", 1)[1])
    assert payload["event"] == "crawl_terminal"
    assert payload["crawl_id"] == "crawl-99"
    assert payload["correlation_id"]
    assert payload["oracle_calls"] == 3
    assert payload["max_depth"] == 5


def test_lifecycle_events_never_carry_prompts_or_secrets(caplog):
    crawl_context.bind_crawl(crawl_id="crawl-99", tenant_id="t")
    with caplog.at_level("INFO", logger="qe-explorer.telemetry"):
        crawl_context.emit(
            crawl_context.EV_LLM_COMPLETED,
            prompt="THE SECRET PROMPT", api_key="sk-live-123",
            authorization="Bearer xyz", screenshot="data:image/png;base64,AAA",
            password="hunter2", cookie="session=abc",
            prompt_tokens=42, completion_tokens=7)
    line = next(r.getMessage() for r in caplog.records
                if "qec.crawl.event" in r.getMessage())
    for leak in ("THE SECRET PROMPT", "sk-live-123", "Bearer xyz", "hunter2",
                 "session=abc", "base64"):
        assert leak not in line
    # ...while the COUNTS this milestone exists to capture do survive.
    payload = json.loads(line.split("qec.crawl.event ", 1)[1])
    assert payload["prompt_tokens"] == 42
    assert payload["completion_tokens"] == 7


def test_unknown_event_names_are_bounded():
    crawl_context.bind_crawl(crawl_id="c", tenant_id="t")
    crawl_context.emit("something_a_caller_invented", value=1)  # must not raise
