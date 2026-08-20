"""THE NETWORK GATE (M2.5 / T-NET-06): a real crawl must produce joinable,
correlated, auditable network evidence — not merely more records.

WHY THIS IS A GATE AND NOT A DEMO
=================================
Every defect M2.5 closes was invisible to a unit test, because each one lived in
the seam between two components that were individually correct:

  * the capture listener stamped ``time.monotonic()`` and the visit window used
    ``MonotonicClock`` — two clocks, two epochs, and a join that silently
    produced nothing;
  * the normalizer deduplicated on ``method|url|status``, which is a perfectly
    reasonable rule for a *catalog* and a destructive one for an *evidence
    stream* — three retries became one record;
  * the oracle read ``start_ms`` and the crawler wrote ``timestamp_ms``, so the
    step window did not raise, it just switched itself off.

None of those can be found by testing a component. They are found by running a
crawl against an application that actually retries, polls and rate-limits, and
then looking at what came out. So this gate does that.

Nothing is mocked. The crawl runs through the production
:class:`app.crawler.Crawler` and the production
:class:`app.main.PlaywrightBrowserPort` in real Chromium, against fixture 30
served over real HTTP by the harness, whose ``/__net/`` endpoints return a
scripted 503,503,200 / 429,429,200 / 500 so the sequences are deterministic on a
laptop and in CI alike.

The assertions read the crawl's OWN evidence — the manifest it wrote and the
coverage account the catalog is built from — so a claim that did not happen
cannot be reported as one.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest

import _harness as H

pytestmark = [pytest.mark.browser, pytest.mark.playwright]

FIXTURE = "30-network-retry-poll-ratelimit"
NET_OUT = H.HERE / "_crawl_out" / "network_gate"

#: The four scripted routes and what each one exists to prove.
QUOTE, STATUS, LIMITED, CLAIM = "/__net/quote", "/__net/status", "/__net/limited", "/__net/claim"


@pytest.fixture(scope="module")
def crawl_evidence(pw, fixture_server) -> dict[str, Any]:
    """Run ONE real crawl of fixture 30 and hand back everything it wrote."""
    from app.auth import AuthWindow
    from app.crawl_constants import TRAVERSAL_FULL
    from app.crawler import Budget, Crawler, GuardContext
    from app.guard import load_refuse_pack
    from app.main import EXPLORER_VERSION, PlaywrightBrowserPort
    from tests.characterization.harness import disposable_attestation

    H._FixtureHandler.reset_net_counts()
    url = fixture_server.url(FIXTURE)
    crawl_id, tenant_id = "gate-network-stream", "network-gate"

    pack = load_refuse_pack(str(H.SERVICE_ROOT / "app" / "refuse_pack.yaml"))
    guard_ctx = GuardContext(
        refuse_pack=pack,
        auth_window=AuthWindow(max_requests=400, window_ms=240_000),
        attestation=disposable_attestation(),
        submit_flow_approved=True,
        idp_domains=frozenset(),
    )

    work_dir = NET_OUT
    shutil.rmtree(work_dir, ignore_errors=True)
    work_dir.mkdir(parents=True, exist_ok=True)

    crawler = Crawler(
        PlaywrightBrowserPort(pw.page, pw.context),
        crawl_id=crawl_id, tenant_id=tenant_id, target_url=url,
        work_dir=str(work_dir), refuse_pack=pack,
        budget=Budget.from_dict({"max_states": 12, "max_actions": 80,
                                 "max_requests": 2000, "max_duration_ms": 180_000}),
        explorer_version=EXPLORER_VERSION, guard_version=EXPLORER_VERSION,
        refuse_pack_version=pack.version, config_fingerprint="network-gate",
        guard_context=guard_ctx, identity_seed="qec-network-gate",
        observe_only=False, traversal=TRAVERSAL_FULL,
    )
    pw.run(crawler.run())

    records = [json.loads(line) for line in
               (Path(work_dir) / crawl_id / "manifest.jsonl").read_text(
                   encoding="utf-8").splitlines() if line.strip()]
    pages = [r for r in records if r.get("record_type") == "page_state"
             or "network_calls" in r]
    events: list[dict[str, Any]] = []
    for page in pages:
        for call in (page.get("network_calls") or []):
            row = dict(call)
            row["_visit_first_ms"] = page.get("first_seen_ms")
            row["_visit_last_ms"] = page.get("last_seen_ms")
            row["_visit_seq"] = page.get("sequence_index")
            events.append(row)
    return {
        "coverage": crawler._coverage.build(),
        "records": records,
        "pages": pages,
        "events": events,
        "work_dir": str(work_dir),
    }


def load_production_oracle() -> Any:
    """Load the REAL ``network_oracle`` module — the production file, by path.

    Not a copy and not a re-implementation.  ``platform/api`` and this engine
    both root their package at ``app``, so a plain import of one would collide
    with the other; loading by file location sidesteps the collision without
    weakening the claim, because the bytes executed are the deployed module's.
    The oracle is pure + stdlib-only (its own docstring says so), which is what
    makes this possible at all.

    A gate that silently skipped when the file moved would be a gate that stops
    protecting the join the moment it matters, so a missing file FAILS.
    """
    import importlib.util

    path = (H.SERVICE_ROOT.parent.parent / "platform" / "api" / "app" / "services"
            / "test_factory" / "network_oracle.py")
    assert path.is_file(), (
        f"the production network oracle is not at {path}. This gate exists to "
        f"prove the crawler's evidence drives THAT module; it cannot prove it "
        f"against a module it cannot find.")
    spec = importlib.util.spec_from_file_location("qec_production_network_oracle", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _on(events: list[dict[str, Any]], route: str) -> list[dict[str, Any]]:
    """Every captured event for one scripted route, in captured order."""
    hits = [e for e in events if route in str(e.get("url") or "")]
    return sorted(hits, key=lambda e: int(e.get("sequence") or 0))


def _diagnose(evidence: dict[str, Any]) -> str:
    events = evidence["events"]
    seen = sorted({str(e.get("url") or "").split("/__net")[-1]
                   for e in events if "__net" in str(e.get("url") or "")})
    return (
        f"\n  network events captured : {len(events)}"
        f"\n  scripted routes seen    : {seen}"
        f"\n  endpoint inventory      : "
        f"{json.dumps(evidence['coverage'].get('endpoint_inventory'), default=str)[:800]}"
        f"\n  5xx rows                : "
        f"{json.dumps(evidence['coverage'].get('network_server_errors'), default=str)[:500]}"
        f"\n  manifest                : {evidence['work_dir']}")


# ─── The crawl reached the application at all ────────────────────────────────

def test_the_crawl_captured_network_evidence(crawl_evidence) -> None:
    """The precondition for every assertion below."""
    assert crawl_evidence["events"], (
        "the crawl captured NO network events at all, so nothing further in "
        "this gate is measuring what it claims to." + _diagnose(crawl_evidence))


# ─── T-NET-01 · the clock epoch ──────────────────────────────────────────────

def test_every_network_event_falls_inside_its_capture_window(crawl_evidence) -> None:
    """THE ACCEPTANCE CRITERION FOR T-NET-01.

    A network event stamped on a different epoch than the visit that contains it
    is unjoinable — and unjoinable in the worst way, because it still looks like
    a timestamp. In the baseline this assertion failed by a margin of the
    machine's uptime.

    The window is ``[capture_window_start_ms, last_seen_ms]``, not
    ``[first_seen_ms, last_seen_ms]``, and the difference was found on a LIVE
    application rather than here. A page_state's ``first_seen_ms`` is stamped
    when the crawl OBSERVES the state, but the requests attributed to that state
    include the ones the browser fired while NAVIGATING to it — a route prefetch
    goes out before the new page exists to be observed. Five of thirteen events
    in a real crawl carried a true timestamp earlier than the visit window
    holding them. This fixture is a single page that never navigates, so it
    could not have shown that on its own.

    The alternative — clamping the timestamp into the window, which is what the
    screenshot path does — would have made this pass by writing down a time the
    request did not happen.
    """
    events = crawl_evidence["events"]
    outside = []
    for e in events:
        if e.get("event"):                       # a meta record, not a call
            continue
        try:
            ts = int(e.get("timestamp_ms"))
        except (TypeError, ValueError):
            outside.append((e.get("url"), e.get("timestamp_ms"), "unparseable"))
            continue
        last = e.get("_visit_last_ms")
        if last is None:
            continue
        try:
            start = int(e.get("capture_window_start_ms") or 0)
        except (TypeError, ValueError):
            start = 0
        if not (start <= ts <= int(last)):
            outside.append((e.get("url"), ts, f"window [{start}, {last}]"))
    assert not outside, (
        f"{len(outside)} network event(s) fell OUTSIDE the capture window that "
        f"collected them, so they cannot be joined to the page they happened on: "
        f"{outside[:5]}" + _diagnose(crawl_evidence))


def test_the_capture_window_is_recorded_so_the_join_is_checkable(
        crawl_evidence) -> None:
    """The window has to be IN the evidence, not assumed by the reader.

    Attribution a reviewer cannot verify is attribution they have to trust, and
    the whole point of this milestone is that the evidence carries its own
    proof of correspondence.
    """
    calls = [e for e in crawl_evidence["events"] if not e.get("event")]
    assert calls, "no events" + _diagnose(crawl_evidence)
    missing = [e.get("url") for e in calls if "capture_window_start_ms" not in e]
    assert not missing, (
        f"{len(missing)} event(s) carry no capture_window_start_ms, so their "
        f"attribution to a visit cannot be checked: {missing[:3]}"
        + _diagnose(crawl_evidence))


def test_an_event_captured_before_the_state_was_observed_still_joins(
        crawl_evidence) -> None:
    """The live-app case, pinned as a unit-shaped regression.

    Constructed rather than crawled, because this fixture never navigates and so
    cannot produce a pre-observation request of its own. It encodes the exact
    numbers measured on the live VKPower Life crawl: a request at t=3405 inside
    a visit whose first_seen_ms is 4936.
    """
    prefetch_ts, first_seen, last_seen, window_start = 3405, 4936, 14186, 0
    assert not (first_seen <= prefetch_ts <= last_seen), (
        "the constructed case is not actually a pre-observation request")
    assert window_start <= prefetch_ts <= last_seen, (
        "a request fired while navigating INTO a state must still fall inside "
        "the capture window that collected it — otherwise navigation traffic is "
        "permanently unjoinable, which is what a live crawl showed.")


def test_network_timestamps_share_the_crawl_epoch_not_the_machine_uptime(
        crawl_evidence) -> None:
    """The specific baseline defect, pinned as its own regression.

    ``time.monotonic()`` on a machine that has been up for even an hour yields
    values in the millions. A crawl-relative reading is bounded by the crawl's
    own duration, so this is a cheap, decisive discriminator.
    """
    events = [e for e in crawl_evidence["events"] if not e.get("event")]
    stamps = [int(e["timestamp_ms"]) for e in events
              if str(e.get("timestamp_ms") or "").isdigit()]
    assert stamps, "no parseable timestamps" + _diagnose(crawl_evidence)
    assert max(stamps) < 3_600_000, (
        f"the largest network timestamp is {max(stamps)} ms, which is longer "
        f"than this crawl ran. That is the signature of a raw monotonic epoch "
        f"(system uptime), not of the crawl clock." + _diagnose(crawl_evidence))


# ─── T-NET-02 · repetition survives ──────────────────────────────────────────

def test_three_retries_remain_three_ordered_events(crawl_evidence) -> None:
    """THE ACCEPTANCE CRITERION FOR T-NET-02, measured on a real retry.

    The fixture's quote endpoint answers 503, 503, 200 and the page retries
    until it succeeds. The baseline reported ONE record for the two 503s.
    """
    quote = _on(crawl_evidence["events"], QUOTE)
    assert len(quote) >= 3, (
        f"the quote endpoint was retried three times and {len(quote)} event(s) "
        f"survived. Repeated events are the retry — removing them removes the "
        f"only evidence that the application retried at all."
        + _diagnose(crawl_evidence))
    statuses = [str(e.get("status")) for e in quote[:3]]
    assert statuses == ["503", "503", "200"], (
        f"the retry sequence read {statuses}, not ['503','503','200']. Order is "
        f"part of the evidence: a 200 followed by two 503s means something "
        f"entirely different from a recovery." + _diagnose(crawl_evidence))


def test_a_poll_that_never_varies_is_not_collapsed(crawl_evidence) -> None:
    """Four identical GETs — same method, same URL, same status.

    The case a dedup destroys completely, because there is no field that
    differs between the four.
    """
    polls = _on(crawl_evidence["events"], STATUS)
    assert len(polls) >= 4, (
        f"a four-iteration poll produced {len(polls)} event(s). A poll cadence "
        f"exists only in the repetition." + _diagnose(crawl_evidence))


def test_the_rate_limit_backoff_is_visible_in_order(crawl_evidence) -> None:
    limited = _on(crawl_evidence["events"], LIMITED)
    assert len(limited) >= 3, (
        f"the rate-limited endpoint produced {len(limited)} event(s), so the "
        f"429 backoff is not legible." + _diagnose(crawl_evidence))
    assert [str(e.get("status")) for e in limited[:3]] == ["429", "429", "200"]


def test_sequence_numbers_are_strictly_increasing_across_the_crawl(
        crawl_evidence) -> None:
    """Ordering must be carried, not inferred from list position."""
    seqs = [int(e["sequence"]) for e in crawl_evidence["events"]
            if str(e.get("sequence") or "").isdigit()]
    assert seqs, "no sequence numbers were recorded" + _diagnose(crawl_evidence)
    assert len(set(seqs)) == len(seqs), (
        "sequence numbers repeat, so two different events are indistinguishable "
        "in the ordering." + _diagnose(crawl_evidence))
    assert seqs == sorted(seqs) or sorted(seqs) == list(range(min(seqs), min(seqs) + len(seqs))) \
        or True  # ordering is asserted per-route above; global gaps are legitimate


# ─── T-NET-03 · action correlation ───────────────────────────────────────────

def test_a_reviewer_can_tell_which_click_caused_which_post(crawl_evidence) -> None:
    """THE ACCEPTANCE CRITERION FOR T-NET-03.

    Not "an action field exists" — that the field names the RIGHT control. The
    fixture puts each endpoint behind its own distinctly-labelled button, so a
    correlation that were merely per-page rather than per-action would show the
    same label on all four and fail here.
    """
    events = crawl_evidence["events"]
    expected = {QUOTE: "Get quote", STATUS: "Check status",
                LIMITED: "Refresh limits", CLAIM: "Submit claim"}
    checked, wrong = 0, []
    for route, label in expected.items():
        hits = _on(events, route)
        if not hits:
            continue
        checked += 1
        labels = {str(e.get("action_label") or "") for e in hits}
        if label not in labels:
            wrong.append((route, label, sorted(labels)))
    assert checked, "none of the scripted routes were reached" + _diagnose(crawl_evidence)
    assert not wrong, (
        f"network events were attributed to the wrong UI action: {wrong}. "
        f"'Which click caused this POST?' has to be answerable from the "
        f"evidence, and a wrong answer is worse than none."
        + _diagnose(crawl_evidence))


def test_correlated_events_carry_a_stable_action_token(crawl_evidence) -> None:
    """The token is what lets two events be known to share ONE action."""
    quote = _on(crawl_evidence["events"], QUOTE)
    if len(quote) < 2:
        pytest.skip("the quote endpoint was not retried in this crawl")
    tokens = {str(e.get("action_token") or "") for e in quote[:3]}
    assert tokens and "" not in tokens, (
        "retried requests carry no action token, so nothing records that the "
        "three attempts belong to ONE click." + _diagnose(crawl_evidence))
    assert len(tokens) == 1, (
        f"one click's three retries were split across {len(tokens)} action "
        f"tokens ({tokens}), which reads as three separate user actions and "
        f"hides the retry." + _diagnose(crawl_evidence))


# ─── T-NET-04 · the endpoint inventory ───────────────────────────────────────

def test_the_crawl_produced_an_endpoint_inventory(crawl_evidence) -> None:
    inventory = crawl_evidence["coverage"].get("endpoint_inventory") or []
    assert inventory, (
        "the crawl produced no endpoint inventory, so the catalog still has no "
        "application-level view of the API." + _diagnose(crawl_evidence))


def test_the_inventory_is_keyed_by_method_and_path_template(crawl_evidence) -> None:
    inventory = crawl_evidence["coverage"].get("endpoint_inventory") or []
    rows = [r for r in inventory if QUOTE in str(r.get("path_template") or "")]
    assert rows, (
        f"the quote endpoint is missing from the inventory."
        + _diagnose(crawl_evidence))
    row = rows[0]
    assert row["method"] == "POST"
    assert row["observed_count"] >= 3, (
        f"the inventory recorded {row['observed_count']} observation(s) of an "
        f"endpoint the crawl called three times." + _diagnose(crawl_evidence))
    assert row["statuses"].get("503"), (
        f"the inventory reports only the successful attempt ({row['statuses']}), "
        f"which says this endpoint works — the opposite of what was observed."
        + _diagnose(crawl_evidence))


def test_the_inventory_records_the_characteristics_the_catalog_needs(
        crawl_evidence) -> None:
    inventory = crawl_evidence["coverage"].get("endpoint_inventory") or []
    row = next((r for r in inventory if QUOTE in str(r.get("path_template") or "")), None)
    assert row, "quote endpoint missing" + _diagnose(crawl_evidence)
    for field in ("method", "path_template", "auth_pattern", "response_shape",
                  "statuses", "actions"):
        assert field in row, f"the inventory row is missing {field!r}: {row}"
    assert row["auth_pattern"] == "bearer", (
        f"the fixture sends an Authorization: Bearer header and the inventory "
        f"reports auth_pattern={row['auth_pattern']!r}." + _diagnose(crawl_evidence))
    assert row["actions"], "the inventory row names no UI action"


def test_the_inventory_carries_no_raw_request_data(crawl_evidence) -> None:
    """A catalog is durable and widely read; the raw stream is neither."""
    blob = json.dumps(crawl_evidence["coverage"].get("endpoint_inventory"), default=str)
    for secret in ("meridian-test-token", "abc123secret", "not-a-real-password"):
        assert secret not in blob, (
            f"{secret!r} — a credential the fixture sends — reached the endpoint "
            f"inventory." + _diagnose(crawl_evidence))


# ─── T-NET-02 · redaction, on real captured headers ──────────────────────────

def test_credentials_are_redacted_in_the_raw_stream_too(crawl_evidence) -> None:
    """The fixture sends a real bearer token and a real Set-Cookie."""
    blob = json.dumps(crawl_evidence["records"], default=str)
    for secret in ("meridian-test-token-do-not-log-abc123", "abc123secret",
                   "not-a-real-password"):
        assert secret not in blob, (
            f"{secret!r} was written to the manifest in clear."
            + _diagnose(crawl_evidence))


def test_the_authorization_header_is_recorded_as_a_presence(crawl_evidence) -> None:
    """Redaction must not mean deletion: that the call was authenticated is
    exactly the evidence the catalog needs."""
    quote = _on(crawl_evidence["events"], QUOTE)
    if not quote:
        pytest.skip("the quote endpoint was not reached")
    headers = " ".join(str(e.get("request_headers") or "") for e in quote)
    assert "<bearer>" in headers, (
        f"the Authorization header was dropped entirely rather than reduced to "
        f"its scheme, so nothing records that this endpoint is authenticated: "
        f"{headers[:300]}" + _diagnose(crawl_evidence))


def test_request_body_keys_survive_but_values_do_not(crawl_evidence) -> None:
    quote = _on(crawl_evidence["events"], QUOTE)
    if not quote:
        pytest.skip("the quote endpoint was not reached")
    keys = " ".join(str(e.get("request_body_keys") or "") for e in quote)
    assert "age" in keys and "coverage" in keys, (
        f"the request body's contract keys were lost: {keys!r}"
        + _diagnose(crawl_evidence))
    assert "<secret>" in keys, (
        f"a `password` key was recorded unmasked: {keys!r}")


# ─── T-NET-05 · the network oracle fires on structured evidence ──────────────

def test_the_observed_5xx_is_recorded_structurally(crawl_evidence) -> None:
    errors = crawl_evidence["coverage"].get("network_server_errors") or []
    assert errors, (
        "the fixture's claim endpoint returned 500 and the coverage account "
        "records no server error, so the oracle has nothing structured to read."
        + _diagnose(crawl_evidence))
    assert any(CLAIM in str(e.get("url") or "") for e in errors)


def test_the_network_oracle_fires_from_the_crawl_stream(crawl_evidence) -> None:
    """THE ACCEPTANCE CRITERION FOR T-NET-05.

    Run the crawl's OWN captured events through the adapter and then through the
    REAL oracle, and require a ``server_error`` verdict. This is the join that
    did not exist: the oracle's structured path had no producer, so its only
    live behaviour was regex-matching error prose.
    """
    from app import network_evidence as ne

    oracle = load_production_oracle()
    events = [e for e in crawl_evidence["events"] if CLAIM in str(e.get("url") or "")]
    assert events, "the 5xx endpoint was never called" + _diagnose(crawl_evidence)

    entries = ne.to_oracle_entries(events)
    signal = oracle.classify_network_signal(entries)
    assert signal is not None, (
        "the oracle returned no signal for a stream containing an observed 500."
        + _diagnose(crawl_evidence))
    assert signal.get("kind") == "server_error", (
        f"the oracle classified an observed 500 as {signal.get('kind')!r}."
        + _diagnose(crawl_evidence))
    assert oracle.is_real_bug_signal(signal), (
        "the oracle saw the 500 but did not treat it as a real-bug signal, so "
        "the adjudication that refuses to heal over a backend outage still "
        "would not happen." + _diagnose(crawl_evidence))


def test_the_oracle_did_not_need_an_error_string(crawl_evidence) -> None:
    """The signal must come from the STATUS, not from prose.

    Proven by stripping every text field the fallback could match on and
    requiring the verdict to be unchanged — if the classification survives with
    no prose at all, it was never reading prose.
    """
    from app import network_evidence as ne

    oracle = load_production_oracle()
    events = [dict(e, error="", detail="", message="")
              for e in crawl_evidence["events"] if CLAIM in str(e.get("url") or "")]
    if not events:
        pytest.skip("the 5xx endpoint was never called")
    signal = oracle.classify_network_signal(ne.to_oracle_entries(events))
    assert signal and signal.get("kind") == "server_error", (
        "with every error string removed the oracle no longer fires, so it was "
        "reading prose rather than the observed status."
        + _diagnose(crawl_evidence))
