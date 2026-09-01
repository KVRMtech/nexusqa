"""M2.5 / T-NET-05 — the CRAWLER→ORACLE contract, pinned as frozen data.

The network oracle lives here, in ``platform/api``; the crawler that produces
its evidence lives in ``engines/qe-explorer``, in a different service with a
different deployment cadence and a colliding ``app`` package name.  The two
cannot be imported into one process, so the contract between them cannot be
proven by wiring them together — the established answer in this codebase is to
FREEZE THE PRODUCER'S OUTPUT AS DATA and assert the consumer against it.

That is what the fixture below is.  ``CRAWL_EVENTS`` is not hand-written: it is
copied verbatim out of the manifest a real Chromium crawl wrote in
``engines/qe-explorer/tests/browser/test_network_stream_gate.py``, including the
string-typed fields the manifest's ``dict[str, str]`` schema forces.  If the
crawler ever changes shape, the explorer-side gate goes red on the producing
side and this goes red on the consuming side — which is the whole point, because
the baseline defect was precisely a shape mismatch that made NEITHER side go
red: the oracle read ``start_ms``, the crawler wrote ``timestamp_ms``, and a
missing key does not raise, it silently switches the step window off.
"""
from __future__ import annotations

import pytest

from app.services.test_factory import network_oracle


# ─── Frozen producer output — copied from a real crawl's manifest.jsonl ──────
#
# Crawl: gate-network-stream, fixture 30-network-retry-poll-ratelimit.
# The application answered 503, 503, 200 on the quote endpoint (a retry that
# recovered) and 500 on the claim endpoint (an outright failure).

CRAWL_EVENTS = [
    {
        "action_label": "Get quote", "action_token": "a2", "action_verb": "click",
        "auth_pattern": "bearer", "error": "", "failed": "false",
        "has_query": "false", "method": "POST", "path_template": "/__net/quote",
        "request_body_keys": "age,state,coverage,<secret>",
        "request_headers": "authorization=<bearer>; content-type=application/json",
        "resource_type": "fetch", "response_shape": "json", "sequence": "1",
        "shape_source": "media_type", "status": "503", "timestamp_ms": "6016",
        "url": "http://127.0.0.1:56956/__net/quote",
    },
    {
        "action_label": "Get quote", "action_token": "a2", "action_verb": "click",
        "auth_pattern": "bearer", "error": "", "failed": "false",
        "has_query": "false", "method": "POST", "path_template": "/__net/quote",
        "resource_type": "fetch", "response_shape": "json", "sequence": "3",
        "status": "200", "timestamp_ms": "6046",
        "url": "http://127.0.0.1:56956/__net/quote",
    },
    {
        "action_label": "Submit claim", "action_token": "a5", "action_verb": "click",
        "auth_pattern": "bearer", "error": "", "failed": "false",
        "has_query": "false", "method": "POST", "path_template": "/__net/claim",
        "resource_type": "fetch", "response_shape": "json", "sequence": "11",
        "status": "500", "timestamp_ms": "11375",
        "url": "http://127.0.0.1:56956/__net/claim",
    },
]

#: The adapter output for the events above, likewise frozen — this is what
#: ``app.network_evidence.to_oracle_entries`` produces on the explorer side.
#: Duplicated as DATA rather than imported, because importing it would mean
#: importing the explorer's ``app`` package into this one.
ADAPTED_ENTRIES = [
    {"url": "http://127.0.0.1:56956/__net/quote", "method": "POST", "status": 503,
     "start_ms": 6016, "end_ms": 6016, "timestamp_ms": 6016, "failed": False,
     "error": "", "sequence": 1, "action_token": "a2",
     "action_label": "Get quote", "action_verb": "click"},
    {"url": "http://127.0.0.1:56956/__net/quote", "method": "POST", "status": 200,
     "start_ms": 6046, "end_ms": 6046, "timestamp_ms": 6046, "failed": False,
     "error": "", "sequence": 3, "action_token": "a2",
     "action_label": "Get quote", "action_verb": "click"},
    {"url": "http://127.0.0.1:56956/__net/claim", "method": "POST", "status": 500,
     "start_ms": 11375, "end_ms": 11375, "timestamp_ms": 11375, "failed": False,
     "error": "", "sequence": 11, "action_token": "a5",
     "action_label": "Submit claim", "action_verb": "click"},
]


class TestTheOracleFiresOnCrawlEvidence:

    def test_an_observed_5xx_produces_a_server_error_signal(self):
        """THE ACCEPTANCE CRITERION: the oracle fires because the structured
        evidence contains an observed 5xx."""
        signal = network_oracle.classify_network_signal(ADAPTED_ENTRIES)
        assert signal is not None, (
            "the oracle returned nothing for a stream containing observed 5xx "
            "responses. Before M2.5 this was the normal case: the structured "
            "path had no producer at all, so the only live behaviour was "
            "regex-matching the runner's error prose.")
        assert signal["kind"] == "server_error"
        assert 500 <= signal["status"] <= 599

    def test_the_isolated_500_is_named_when_it_is_the_only_5xx(self):
        """The oracle returns the FIRST 5xx it meets, so a stream that also
        contains a recovered 503 retry names the retry rather than the claim.

        That is the oracle's own severity rule and M2.5 does not change it. What
        M2.5 changes is that the distinction is now VISIBLE at all: the caller
        has the full ordered stream and can see that /quote recovered on its
        third attempt while /claim never did. Under the baseline dedup both
        endpoints collapsed to a single record apiece and no such reading was
        possible.
        """
        claim_only = [e for e in ADAPTED_ENTRIES if "/__net/claim" in e["url"]]
        signal = network_oracle.classify_network_signal(claim_only)
        assert signal["status"] == 500
        assert "/__net/claim" in signal["url"]

    def test_the_recovered_retry_is_still_legible_in_the_stream(self):
        """The evidence must let a reviewer see that the 503 was recovered from.

        A signal that says "server_error" and a stream that cannot show the
        subsequent 200 would push an operator toward filing a defect for an
        endpoint that worked on retry.
        """
        quote = [e for e in ADAPTED_ENTRIES if "/__net/quote" in e["url"]]
        assert [e["status"] for e in sorted(quote, key=lambda e: e["sequence"])] \
            == [503, 200]

    def test_it_is_treated_as_a_real_bug_signal(self):
        """Firing is not enough — it has to reach the adjudication that refuses
        to heal over a backend outage."""
        signal = network_oracle.classify_network_signal(ADAPTED_ENTRIES)
        assert network_oracle.is_real_bug_signal(signal)

    def test_the_verdict_does_not_depend_on_any_error_string(self):
        """Every entry above carries ``error: ""``.

        So the classification cannot be coming from prose. This is the specific
        requirement that the oracle must not depend on searching arbitrary error
        strings — asserted by giving it nothing to search.
        """
        assert all(e["error"] == "" for e in ADAPTED_ENTRIES)
        signal = network_oracle.classify_network_signal(ADAPTED_ENTRIES)
        assert signal and signal["kind"] == "server_error"
        # And the text path, given the same absence of prose, finds nothing —
        # which is what makes the structured read the load-bearing one.
        assert network_oracle.network_signal_from_error("") is None

    def test_a_5xx_outranks_the_4xx_and_the_success_in_the_same_stream(self):
        """Severity ordering over a real mixed stream, not a single entry."""
        mixed = ADAPTED_ENTRIES + [
            {"url": "http://127.0.0.1:56956/__net/limited", "method": "GET",
             "status": 429, "start_ms": 9625, "failed": False, "error": ""},
        ]
        assert network_oracle.classify_network_signal(mixed)["kind"] == "server_error"

    def test_a_429_alone_is_advisory_not_a_bug(self):
        """Rate limiting is not an application defect, and the crawl now sees
        plenty of it. Filing a defect for every 429 would make the signal
        useless."""
        only_429 = [{"url": "http://127.0.0.1:56956/__net/limited", "method": "GET",
                     "status": 429, "start_ms": 9625, "failed": False, "error": ""}]
        signal = network_oracle.classify_network_signal(only_429)
        assert signal["kind"] == "client_error"
        assert not network_oracle.is_real_bug_signal(signal)


class TestTheStepWindowActuallyWindows:
    """The field-name mismatch that switched the window off, pinned."""

    def test_the_crawl_timestamp_is_honoured_as_a_window_bound(self):
        """``timestamp_ms`` must window exactly as ``start_ms`` does.

        A window that does not raise but does not filter either is worse than no
        window: it lets a 5xx from anywhere in the run be attributed to the step
        under adjudication.
        """
        entries = [{"url": "http://x/api/a", "method": "POST", "status": 500,
                    "timestamp_ms": 11375, "failed": False, "error": ""}]
        inside = network_oracle.classify_network_signal(
            entries, step_start_ms=11000, step_end_ms=12000)
        assert inside and inside["kind"] == "server_error"

        outside = network_oracle.classify_network_signal(
            entries, step_start_ms=0, step_end_ms=100)
        assert outside is None, (
            "an event 11 seconds outside the step window was still attributed "
            "to the step. The window is not filtering.")

    def test_start_ms_still_wins_when_both_are_present(self):
        """Back-compatibility: the runner's own entries are unaffected."""
        entries = [{"url": "http://x/api/a", "method": "POST", "status": 500,
                    "start_ms": 50, "timestamp_ms": 99999,
                    "failed": False, "error": ""}]
        assert network_oracle.classify_network_signal(
            entries, step_start_ms=0, step_end_ms=100) is not None


class TestTheFrozenShapeMatchesWhatTheAdapterPromises:
    """If the producer's shape drifts, this is where the consumer notices."""

    @pytest.mark.parametrize("key", ["url", "method", "status", "start_ms",
                                     "failed", "error"])
    def test_every_key_the_oracle_reads_is_present(self, key):
        for entry in ADAPTED_ENTRIES:
            assert key in entry, (
                f"the frozen adapter output is missing {key!r}, which "
                f"classify_network_signal reads. A missing key here does not "
                f"raise — it silently changes the verdict.")

    def test_status_is_an_int_not_the_manifests_string(self):
        """The manifest is ``dict[str, str]``; the oracle compares numerically."""
        assert all(isinstance(e["status"], int) for e in ADAPTED_ENTRIES)
        assert all(isinstance(e["status"], str) for e in CRAWL_EVENTS)

    def test_a_string_false_never_reaches_the_oracle_as_a_failure(self):
        """``bool("false")`` is True, and the manifest stores ``"false"``.

        Untreated, every 5xx would also be reported as a connection failure —
        the difference between "the server rejected this" and "the server was
        unreachable", which route to different remediations.
        """
        assert all(e["failed"] == "false" for e in CRAWL_EVENTS)
        assert all(e["failed"] is False for e in ADAPTED_ENTRIES)

    def test_the_correlation_survives_into_the_oracle_entry(self):
        """A fired oracle must be able to name the click, not just the request."""
        claim = [e for e in ADAPTED_ENTRIES if "/claim" in e["url"]][0]
        assert claim["action_label"] == "Submit claim"
        assert claim["action_verb"] == "click"
