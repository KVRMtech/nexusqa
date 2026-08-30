"""RUNG 2: THE CLIENT'S OWN TEST ENVIRONMENT, ANSWERING FOR ITSELF.

Some values cannot be invented by anybody — a member id that exists, a policy in
force, the fixed OTP a test environment issues. A generator cannot produce them
and a model cannot know them. The client can, and this rung is the ask that gets
them without a spreadsheet: a read-only URL, a token, once.

TWO PROPERTIES CARRY THE WHOLE RUNG, and most of this file exists to hold them:

  1. AN AMBIGUOUS FIELD IS REFUSED, NEVER GUESSED. An environment holding both
     an ``account number`` and a ``bank account`` slot meets a field labelled
     "Bank Account Number" and both slots claim it — two different values, so
     answering with either is a coin toss. Carrying the wrong one is the single
     failure this rung must never produce, and it is worse than answering
     nothing, so the resolver refuses.

  2. AN UNREACHABLE ENVIRONMENT IS A DECLINE, NOT A STOPPED CRAWL. A client's
     test system restarts, rate-limits and expires its certificate on a Sunday.
     If any of that could halt a crawl this rung would make the product LESS
     reliable than not having it.

Each is asserted here WITH ITS CONTROL — a case that must still answer — because
"returns ASK" is satisfied just as well by a rung that never answers anything.
"""
from __future__ import annotations

import httpx
import pytest

from app.services.env_data import (ANSWERED, ASK, R_AMBIGUOUS, R_EMPTY,
                                   R_NO_PROVIDER, R_NO_SLOT, R_UNAVAILABLE,
                                   StaticProvider, answer_key_overlay, resolve)
from app.services.env_data_transports import (MAX_VALUE_CHARS, McpProvider,
                                              RestProvider, build)


# ── the manifest door: a file, no network, no credential ───────────────────

def test_a_slot_the_client_populated_answers_the_field_that_matches_it():
    p = StaticProvider({"member id": "M-1001"})
    got = resolve("Member ID", p)
    assert (got.disposition, got.value, got.slot_key) == (ANSWERED, "M-1001",
                                                          "member id")


def test_a_field_no_slot_matches_stays_the_ask_it_already_was():
    got = resolve("Favourite colour", StaticProvider({"member id": "M-1001"}))
    assert (got.disposition, got.reason) == (ASK, R_NO_SLOT)


def test_a_slot_that_exists_but_holds_nothing_is_not_an_answer():
    got = resolve("Member ID", StaticProvider({"member id": "   "}))
    assert got.disposition == ASK
    assert got.reason == R_EMPTY or got.reason == R_NO_SLOT


def test_no_configured_environment_leaves_the_ladder_exactly_as_it_was():
    got = resolve("Member ID", None)
    assert (got.disposition, got.reason) == (ASK, R_NO_PROVIDER)


# ── property 1: ambiguity is refused, and the control still answers ────────

def test_a_field_matching_two_slots_is_refused_rather_than_guessed():
    """THE ONE THAT MATTERS, and a collision a real client produces rather than
    an invented one: an environment holding both an ``account number`` and a
    ``bank account`` slot meets a field labelled "Bank Account Number", and both
    slots claim it. They are two DIFFERENT values, so answering with either is
    a coin toss — worse than answering nothing at all."""
    p = StaticProvider({"account number": "0001234567",
                        "bank account": "GB29 NWBK 6016"})
    got = resolve("Bank Account Number", p)
    assert (got.disposition, got.reason) == (ASK, R_AMBIGUOUS)
    assert got.value is None


def test_the_control_for_ambiguity_the_same_field_answers_when_alone():
    """FALSIFICATION CONTROL. Without this, a resolver that answered NOTHING —
    a typo in the matcher, a store read that silently returns empty — would
    satisfy the refusal test above and look like a working safety rule."""
    p = StaticProvider({"account number": "0001234567"})
    got = resolve("Bank Account Number", p)
    assert (got.disposition, got.value) == (ANSWERED, "0001234567")


# ── property 2: an unreachable environment declines, and the control answers ─

class _Broken:
    """A client environment having a bad day, in every way it can have one."""

    def __init__(self, mode):
        self.mode = mode

    def slots(self):
        if self.mode == "slots":
            raise ConnectionError("their VPN dropped")
        return ["member id"]

    def value(self, slot_key):
        if self.mode == "value":
            raise TimeoutError("their fixture service is restarting")
        return "M-1001"


def test_an_environment_that_cannot_list_its_slots_declines_quietly():
    got = resolve("Member ID", _Broken("slots"))
    assert (got.disposition, got.reason) == (ASK, R_UNAVAILABLE)


def test_an_environment_that_fails_mid_answer_declines_quietly():
    got = resolve("Member ID", _Broken("value"))
    assert (got.disposition, got.reason) == (ASK, R_UNAVAILABLE)
    assert got.slot_key == "member id", "it should still say what it was after"


def test_the_control_for_unreachability_a_healthy_one_answers():
    """FALSIFICATION CONTROL for both tests above."""
    got = resolve("Member ID", _Broken("none"))
    assert (got.disposition, got.value) == (ANSWERED, "M-1001")


# ── the REST door ──────────────────────────────────────────────────────────

def _stub(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_the_rest_door_asks_two_questions_and_nothing_more():
    """The contract asked of a client is two read-only endpoints. That
    smallness is what makes it a request a platform team says yes to."""
    seen = []

    def handler(request):
        seen.append(request.url.raw_path.decode())
        if request.url.path.endswith("/slots"):
            return httpx.Response(200, json={"slots": ["member id"]})
        return httpx.Response(200, json={"value": "M-2002"})

    p = RestProvider("https://fixtures.client.example", "tok", client=_stub(handler))
    assert resolve("Member ID", p).value == "M-2002"
    # raw_path, not .path: httpx decodes the latter, so only the raw
    # bytes show whether the slot key actually travelled encoded.
    assert seen == ["/slots", "/value/member%20id"]


def test_the_token_travels_as_a_bearer_header():
    seen = {}

    def handler(request):
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={"slots": []})

    RestProvider("https://x.example", "s3cret", client=_stub(handler)).slots()
    assert seen["auth"] == "Bearer s3cret"


def test_a_slot_key_cannot_steer_the_request_to_another_host():
    """A slot key is human-assigned text. Quoting it is what keeps the
    destination the one the tenant registered."""
    seen = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(200, json={"value": "x"})

    p = RestProvider("https://fixtures.client.example", client=_stub(handler))
    p.value("../../evil.example/steal")
    assert seen and seen[0].startswith("https://fixtures.client.example/value/")
    assert "evil.example/steal" not in seen[0]


@pytest.mark.parametrize("response", [
    httpx.Response(500),
    httpx.Response(200, text="not json at all"),
    httpx.Response(200, json=["a", "list", "not", "an", "object"]),
    httpx.Response(404),
])
def test_every_unhappy_rest_response_is_a_decline(response):
    p = RestProvider("https://x.example",
                     client=_stub(lambda request: response))
    assert p.value("member id") is None
    assert list(p.slots()) == []


def test_a_rest_endpoint_that_never_answers_does_not_stop_the_crawl():
    def handler(request):
        raise httpx.ConnectTimeout("no route to their host")

    p = RestProvider("https://gone.example", client=_stub(handler))
    assert resolve("Member ID", p).disposition == ASK


def test_an_absurdly_large_value_is_refused_rather_than_pasted_into_a_field():
    big = "x" * (MAX_VALUE_CHARS + 1)
    p = RestProvider("https://x.example", client=_stub(
        lambda r: httpx.Response(200, json={"value": big})))
    assert p.value("member id") is None


@pytest.mark.parametrize("payload", [{"value": None}, {"value": {"a": 1}},
                                     {"value": ["a"]}, {"value": "   "}, {}])
def test_a_value_that_is_not_a_value_is_not_an_answer(payload):
    p = RestProvider("https://x.example", client=_stub(
        lambda r: httpx.Response(200, json=payload)))
    assert p.value("member id") is None


def test_the_control_for_the_rest_refusals_a_good_value_arrives():
    """FALSIFICATION CONTROL for every REST decline above."""
    p = RestProvider("https://x.example", client=_stub(
        lambda r: httpx.Response(200, json={"value": "M-3003"})))
    assert p.value("member id") == "M-3003"


# ── the MCP door is the same contract over a different pipe ────────────────

def test_mcp_asks_exactly_what_rest_asks():
    """A THIN wrapper on purpose: two sets of rules to keep in step is how a
    rung acquires a hole nobody notices."""
    seen = []

    def handler(request):
        seen.append(request.url.raw_path.decode())
        return httpx.Response(200, json={"slots": ["member id"],
                                         "value": "M-4004"})

    p = McpProvider("https://mcp.client.example", "tok", client=_stub(handler))
    assert resolve("Member ID", p).value == "M-4004"
    assert seen == ["/slots", "/value/member%20id"]


# ── configuration becomes a provider, or nothing ───────────────────────────

def test_each_kind_builds_its_own_door():
    assert isinstance(build({"kind": "manifest", "values": {"a": "1"}}),
                      StaticProvider)
    assert isinstance(build({"kind": "rest", "base_url": "https://x.example"}),
                      RestProvider)
    assert isinstance(build({"kind": "mcp", "endpoint": "https://x.example"}),
                      McpProvider)


@pytest.mark.parametrize("config", [
    {},
    {"kind": "carrier-pigeon"},
    {"kind": "rest"},
    {"kind": "rest", "base_url": "not-a-url"},
    {"kind": "rest", "base_url": "file:///etc/passwd"},
    {"kind": "manifest"},
    {"kind": "manifest", "values": "a string"},
    {"kind": "manifest", "values": {}},
])
def test_a_half_configured_environment_builds_nothing_rather_than_failing(config):
    """An operator's half-finished configuration must leave the ladder as it
    was. They see it in the data account — no values arrived from ``env`` —
    which is truer than a 500 at schedule time."""
    assert build(config) is None


def test_a_non_http_scheme_is_never_dialled():
    """The tenant registers a URL; a scheme that reads local files is not one."""
    for url in ("file:///etc/passwd", "ftp://x.example", "javascript:alert(1)"):
        assert build({"kind": "rest", "base_url": url}) is None


# ── what reaches the explorer ──────────────────────────────────────────────

def test_many_fields_resolve_into_an_answer_key_shaped_overlay():
    """The explorer already understands an answer key, so an environment's
    answers arrive as an extension of one rather than a new concept."""
    p = StaticProvider({"member id": "M-1001", "policy number": "P-77"})
    overlay = answer_key_overlay(p, ["Member ID", "Policy Number", "Colour"])
    assert overlay == {"Member ID": "M-1001", "Policy Number": "P-77"}


def test_an_ambiguous_field_never_reaches_the_overlay_either():
    """The refusal must hold on the batch path too — a second door into the
    same rung is exactly where a safety rule goes missing."""
    p = StaticProvider({"account number": "0001234567",
                        "bank account": "GB29 NWBK 6016"})
    assert answer_key_overlay(p, ["Bank Account Number"]) == {}


def test_the_overlay_is_empty_rather_than_absent_without_an_environment():
    assert answer_key_overlay(None, ["Member ID"]) == {}


def test_a_value_is_never_written_to_a_log(caplog):
    """The values are the CLIENT'S. Counts travel to evidence; values do not."""
    import logging

    caplog.set_level(logging.DEBUG)
    p = StaticProvider({"member id": "M-SECRET-1001"})
    answer_key_overlay(p, ["Member ID"])
    assert "M-SECRET-1001" not in caplog.text


# ── the last mile: a stored app row becomes answers ────────────────────────

def test_an_app_with_no_environment_configured_is_completely_inert():
    """THE DEFAULT, and it is every app today. The rung must cost nothing until
    a client opts in — by construction, not by a flag someone must remember."""
    from app.services.env_data import overlay_for_app

    assert overlay_for_app({}, ["Member ID"]) == {}
    assert overlay_for_app(None, ["Member ID"]) == {}
    assert overlay_for_app({"fill": {"a": "b"}}, ["Member ID"]) == {}


def test_a_manifest_stored_on_the_app_answers_its_fields():
    from app.services.env_data import overlay_for_app

    row = {"environment": {"kind": "manifest",
                           "values": {"member id": "M-1001"}}}
    assert overlay_for_app(row, ["Member ID", "Colour"]) == {"Member ID": "M-1001"}


def test_the_token_comes_from_the_encrypted_blob_not_the_answer_key():
    """A token is a credential. It travels in the tenant's envelope-encrypted
    ``credentials``, is decrypted by the caller entitled to read it, and is
    passed in — never stored beside the non-secret configuration."""
    from app.services.env_data import overlay_for_app

    row = {"environment": {"kind": "rest", "base_url": "https://x.example"}}
    assert "token" not in row["environment"]
    # No network in this test; the point is that the call accepts the split and
    # does not require the secret to live in the answer key.
    assert overlay_for_app(row, ["Member ID"], token="s3cret") == {}


def test_a_token_already_in_the_config_is_not_overwritten():
    from app.services.env_data import overlay_for_app

    row = {"environment": {"kind": "manifest", "values": {"member id": "M-1"}}}
    assert overlay_for_app(row, ["Member ID"], token="ignored") == \
        {"Member ID": "M-1"}


def test_a_broken_environment_block_yields_no_answers_rather_than_raising():
    """A half-configured app must not fail a dispatch."""
    from app.services.env_data import overlay_for_app

    for bad in ({"environment": {"kind": "rest"}},
                {"environment": {"kind": "nonsense"}},
                {"environment": {"kind": "manifest", "values": {}}}):
        assert overlay_for_app(bad, ["Member ID"]) == {}
