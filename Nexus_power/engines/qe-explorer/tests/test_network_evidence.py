"""M2.5 — the network evidence stream: ordering, correlation, redaction, oracle.

These are the pure-logic proofs.  The live proof — a real Chromium crawl of a
fixture that actually retries, polls and rate-limits — is
``tests/browser/test_network_stream_gate.py``; this file pins the rules that
crawl relies on, so a regression is named here before it has to be found there.
"""
from __future__ import annotations

import pytest

from app import endpoint_inventory as inv
from app import network_evidence as ne
from app.state_identity import _network_calls


# ─── T-NET-02 · repetition survives ──────────────────────────────────────────

def _event(seq: int, *, method="POST", url="https://app.test/api/quote",
           status="503", token="a1", label="Get quote", verb="click",
           timestamp=None, **extra):
    row = {
        "sequence": seq, "method": method, "url": url, "status": status,
        "resource_type": "fetch", "action_token": token, "action_label": label,
        "action_verb": verb, "timestamp_ms": timestamp if timestamp is not None else seq * 10,
        "request_headers": {}, "response_headers": {}, "request_body": {},
        "has_query": False,
    }
    row.update(extra)
    return row


class TestRetriesSurvive:
    """Three retries must remain three ordered events (T-NET-02)."""

    def test_three_identical_retries_are_three_records(self):
        raw = [_event(1), _event(2), _event(3, status="200")]
        out = _network_calls(raw)
        assert len(out) == 3, (
            "the normalizer collapsed a retry sequence. The baseline deduped on "
            "method|url|status, which is exactly how three attempts became one "
            "record and a retry stopped being visible at all.")

    def test_a_poll_that_never_varies_is_not_collapsed(self):
        """The hardest case: same method, same URL, SAME status, N times.

        A dedup keyed on anything derived from the request alone destroys this
        one completely — there is no field that differs. Only an ordinal
        assigned at capture keeps it.
        """
        raw = [_event(i, method="GET", url="https://app.test/api/status",
                      status="200") for i in range(1, 5)]
        out = _network_calls(raw)
        assert len(out) == 4
        assert [r["sequence"] for r in out] == ["1", "2", "3", "4"]

    def test_ordering_is_carried_not_inferred(self):
        """Order must survive a transport that does not preserve list order.

        Asserted by shuffling the input: the sequence numbers are the record of
        what happened, so they must still read 1,2,3 after a re-sort — if order
        were only positional, this would come back 3,1,2.
        """
        raw = [_event(3, status="200"), _event(1), _event(2)]
        out = _network_calls(raw)
        by_seq = sorted(out, key=lambda r: int(r["sequence"]))
        assert [r["status"] for r in by_seq] == ["503", "503", "200"], (
            "the retry sequence could not be reconstructed from the evidence "
            "alone, so ordering depends on transport rather than on the record.")

    def test_a_rate_limit_sequence_keeps_every_attempt(self):
        raw = [_event(1, method="GET", url="https://app.test/api/limited", status="429"),
               _event(2, method="GET", url="https://app.test/api/limited", status="429"),
               _event(3, method="GET", url="https://app.test/api/limited", status="200")]
        out = _network_calls(raw)
        assert [r["status"] for r in out] == ["429", "429", "200"]

    def test_truncation_is_reported_never_silent(self):
        """A clipped stream must not read as a complete one."""
        raw = [_event(i) for i in range(1, 260)]
        out = _network_calls(raw)
        assert out[-1].get("event") == "stream_truncated", (
            "the stream hit its cap and stopped silently. A reader cannot tell "
            "that from an application that simply made 100 calls.")

    def test_an_adapter_truncation_marker_is_carried_through(self):
        raw = [_event(1),
               {"event": "buffer_truncated", "dropped": 7, "sequence": 508,
                "reason": "more than 500 network events between drains",
                "timestamp_ms": 900}]
        out = _network_calls(raw)
        assert out[-1]["event"] == "buffer_truncated"
        assert out[-1]["dropped"] == "7"


# ─── T-NET-03 · action correlation ───────────────────────────────────────────

class TestActionCorrelation:
    """"Which click caused this POST?" must be answerable from the record."""

    def test_the_event_names_the_action_that_caused_it(self):
        out = _network_calls([_event(1, token="a4", label="Submit claim", verb="click")])
        assert out[0]["action_token"] == "a4"
        assert out[0]["action_label"] == "Submit claim"
        assert out[0]["action_verb"] == "click"

    def test_two_actions_produce_two_distinct_correlations(self):
        raw = [_event(1, token="a1", label="Get quote"),
               _event(2, token="a2", label="Submit claim", url="https://app.test/api/claim",
                      status="500")]
        out = _network_calls(raw)
        assert out[0]["action_label"] == "Get quote"
        assert out[1]["action_label"] == "Submit claim"
        assert out[0]["action_token"] != out[1]["action_token"]


# ─── T-NET-02 · redaction ────────────────────────────────────────────────────

class TestRedaction:

    def test_a_bearer_token_is_reduced_to_its_scheme(self):
        headers = ne.redact_headers({
            "Authorization": "Bearer eyJhbGciOiJIUzI1NiJ9.super-secret-value",
            "Content-Type": "application/json",
        })
        assert headers["authorization"] == "<bearer>"
        assert "super-secret-value" not in str(headers)
        assert headers["content-type"] == "application/json"

    def test_a_cookie_is_a_presence_not_a_value(self):
        headers = ne.redact_headers({"Cookie": "session=abc123secret; theme=dark"})
        assert headers["cookie"] == "<present>"
        assert "abc123secret" not in str(headers)

    def test_an_unknown_header_is_dropped_by_default(self):
        """Allow-list, not blocklist: a header nobody anticipated must not leak."""
        headers = ne.redact_headers({"X-Customer-SSN": "123-45-6789"})
        assert headers == {}, (
            "an unlisted header survived. The allow-list is the whole guarantee "
            "that a header a future application invents cannot leak by default.")

    def test_a_body_yields_key_names_never_values(self):
        body = ne.describe_body(
            '{"age": 42, "state": "CA", "password": "hunter2", "ssn": "123-45-6789"}',
            "application/json")
        assert "age" in body["keys"] and "state" in body["keys"]
        assert "hunter2" not in str(body)
        assert "123-45-6789" not in str(body)
        assert "<secret>" in body["keys"], "a secret-named key was not masked"

    def test_an_unparseable_body_is_described_honestly_not_guessed(self):
        body = ne.describe_body("\x00\x01binary", "application/octet-stream")
        assert body["keys"] == []
        assert body["bytes"] > 0
        assert body["keys_source"] in ("none", "unparsed")

    def test_form_bodies_are_reduced_the_same_way(self):
        body = ne.describe_body("age=42&password=hunter2", "application/x-www-form-urlencoded")
        assert "age" in body["keys"]
        assert "hunter2" not in str(body)
        assert "<secret>" in body["keys"]


# ─── T-NET-04 · path templating + inventory ──────────────────────────────────

class TestPathTemplate:

    @pytest.mark.parametrize("path,expected", [
        ("/api/policies/8837", "/api/policies/{id}"),
        ("/api/policies/8837/documents", "/api/policies/{id}/documents"),
        ("/api/u/3f2b9c1d4e5a6b7c8d9e0f1a2b3c4d5e", "/api/u/{hex}"),
        ("/api/x/550e8400-e29b-41d4-a716-446655440000", "/api/x/{uuid}"),
        ("/api/reports/2026-08-19", "/api/reports/{date}"),
        ("/api/quote", "/api/quote"),
        ("/underwriting/policy-administration", "/underwriting/policy-administration"),
    ])
    def test_identifiers_template_and_route_words_do_not(self, path, expected):
        assert ne.path_template(path) == expected

    def test_a_redacted_span_is_not_re_templated(self):
        """Losing a redaction marker would hide the redaction from an auditor."""
        assert "[REDACTED:" in ne.path_template("/api/u/[REDACTED:EMAIL]")


class TestEndpointInventory:

    def test_repeated_calls_aggregate_into_one_endpoint_with_counted_statuses(self):
        events = [
            _event(1, status="503", **{"path_template": "/api/quote"}),
            _event(2, status="503", **{"path_template": "/api/quote"}),
            _event(3, status="200", **{"path_template": "/api/quote"}),
        ]
        result = inv.build_inventory(events)
        assert result["endpoint_count"] == 1
        row = result["endpoints"][0]
        assert row["method"] == "POST"
        assert row["path_template"] == "/api/quote"
        assert row["observed_count"] == 3
        assert row["statuses"] == {"503": 2, "200": 1}, (
            "the inventory lost the retry. Reporting only the final 200 would "
            "say this endpoint works, which is the opposite of what was seen.")
        assert row["has_server_error"] is True
        assert row["retried"] is True

    def test_two_records_of_the_same_route_are_one_endpoint(self):
        events = [
            _event(1, method="GET", url="https://app.test/api/policies/8837", status="200"),
            _event(2, method="GET", url="https://app.test/api/policies/9021", status="200"),
        ]
        result = inv.build_inventory(events)
        assert result["endpoint_count"] == 1
        assert result["endpoints"][0]["path_template"] == "/api/policies/{id}"

    def test_a_rate_limited_endpoint_is_flagged(self):
        events = [_event(1, method="GET", url="https://app.test/api/limited", status="429"),
                  _event(2, method="GET", url="https://app.test/api/limited", status="200")]
        row = inv.build_inventory(events)["endpoints"][0]
        assert row["rate_limited"] is True

    def test_the_inventory_names_the_ui_action(self):
        row = inv.build_inventory([_event(1, label="Get quote", verb="click")])["endpoints"][0]
        assert {"verb": "click", "label": "Get quote", "action_token": "a1"} in row["actions"]

    def test_no_raw_url_or_header_value_enters_the_inventory(self):
        """The catalog is a durable, widely-read artifact — the raw stream is not."""
        events = [_event(1, url="https://app.test/api/members/123-45-6789",
                         request_headers={"authorization": "Bearer abc"},
                         request_body={"keys": ["age", "<secret>"], "bytes": 40,
                                       "keys_source": "json"})]
        blob = str(inv.build_inventory(events))
        assert "123-45-6789" not in blob
        assert "Bearer abc" not in blob
        assert "abc" not in blob.replace("action_token", "")

    def test_body_keys_are_read_from_either_evidence_shape(self):
        """The port hands over a dict; a manifest re-read hands over a string.

        Reading only one shape produced an empty `request_keys` for anyone
        rebuilding the inventory from STORED evidence — an inventory that looked
        complete and had quietly lost the API contract.
        """
        structured = _event(1, request_body={"keys": ["age", "state"], "bytes": 20,
                                             "keys_source": "json"})
        flattened = _event(1, request_body_keys="age,state")
        for events in ([structured], [flattened]):
            row = inv.build_inventory(events)["endpoints"][0]
            assert row["request_keys"] == ["age", "state"], (
                f"body keys were lost for this evidence shape: {events[0]}")

    def test_the_strongest_observed_auth_pattern_wins(self):
        events = [_event(1, auth_pattern="none"), _event(2, auth_pattern="bearer")]
        assert inv.build_inventory(events)["endpoints"][0]["auth_pattern"] == "bearer"

    def test_merging_is_associative_with_a_single_build(self):
        """A folded inventory must equal a built-all-at-once one, or the
        crawler's incremental aggregation reports something a reader cannot
        reproduce from the stream."""
        events = [_event(1, status="503"), _event(2, status="503"), _event(3, status="200")]
        whole = inv.build_inventory(events)
        folded = inv.merge_inventories([inv.build_inventory(events[:2]),
                                        inv.build_inventory(events[2:])])
        assert folded["endpoints"][0]["statuses"] == whole["endpoints"][0]["statuses"]
        assert folded["endpoints"][0]["observed_count"] == whole["endpoints"][0]["observed_count"]


# ─── T-NET-05 · the oracle adapter ───────────────────────────────────────────

class TestOracleAdapter:

    def test_the_adapter_emits_every_key_the_oracle_reads(self):
        entries = ne.to_oracle_entries([_event(1, status="500")])
        for key in ne.ORACLE_ENTRY_KEYS:
            assert key in entries[0], (
                f"the adapter omits {key!r}, which the oracle reads. A missing "
                f"key here does not raise — it silently changes what the oracle "
                f"decides, which is exactly how the baseline mismatch survived.")

    def test_status_becomes_an_int_because_the_oracle_compares_it(self):
        entry = ne.to_oracle_entries([_event(1, status="503")])[0]
        assert isinstance(entry["status"], int) and entry["status"] == 503

    def test_the_crawl_timestamp_populates_the_oracle_window_field(self):
        """`start_ms` vs `timestamp_ms` is the mismatch that disabled the window."""
        entry = ne.to_oracle_entries([_event(1, timestamp=1234)])[0]
        assert entry["start_ms"] == 1234
        assert entry["timestamp_ms"] == 1234

    def test_a_failed_request_carries_the_failure_flag(self):
        entry = ne.to_oracle_entries(
            [_event(1, status="0", failed=True, error="net::ERR_CONNECTION_REFUSED")])[0]
        assert entry["failed"] is True
        assert "ERR_CONNECTION_REFUSED" in entry["error"]

    def test_a_string_false_from_the_manifest_is_not_a_failure(self):
        """The manifest is typed dict[str, str], so `False` comes back as
        `"false"` — and `bool("false")` is True.

        Untreated, that marked every successful call AND every 5xx as a
        connection failure the moment the evidence had been through the
        manifest, which is the difference between "the server rejected this"
        and "the server was unreachable".
        """
        entry = ne.to_oracle_entries([_event(1, status="500", failed="false")])[0]
        assert entry["failed"] is False
        assert ne.to_oracle_entries([_event(1, status="0", failed="true")])[0]["failed"] is True

    def test_observed_server_errors_are_read_not_string_matched(self):
        events = [_event(1, status="200"), _event(2, status="500"), _event(3, status="404")]
        errors = ne.observed_server_errors(events)
        assert len(errors) == 1 and errors[0]["status"] == "500"

    def test_a_500ms_timeout_is_not_a_server_error(self):
        """The exact false positive the oracle's text path has to guard against —
        and which a structured read cannot make at all."""
        assert ne.observed_server_errors(
            [_event(1, status="200", error="timed out after 500ms")]) == []

    def test_correlation_rides_into_the_oracle_entry(self):
        """A fired oracle must be able to name the click, not just the request."""
        entry = ne.to_oracle_entries([_event(1, status="500", label="Submit claim")])[0]
        assert entry["action_label"] == "Submit claim"
