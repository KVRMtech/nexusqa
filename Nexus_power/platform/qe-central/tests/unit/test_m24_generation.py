"""M2.4 — endpoint map, journey criticality and the journey compile payload.

Unit-level coverage of the three pure qe-central modules the generation path is
built on.  The end-to-end proof (``tests/m24_generation``) shows they work
together against a real browser; this file pins the edges that an end-to-end run
cannot reach — malformed evidence, precedence between the two attribution rules,
and the honesty rules that decide when an assertion is allowed to exist at all.
"""
from __future__ import annotations

import pytest

from app.services import endpoint_map as EM
from app.services import journey_criticality as JCRIT
from app.services import journey_spec as JS


# ══════════════════════════════════════════════════════════════════════════
#  endpoint_map — T-GEN-03
# ══════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("raw,expected", [
    ("https://a.test/API/Quote/", "/api/quote"),
    ("/api/quote?x=1", "/api/quote"),
    ("https://a.test/", "/"),
    ("/", "/"),
    ("", ""),
])
def test_paths_normalise_so_a_spec_survives_an_environment_swap(raw, expected):
    """Host and query never participate: a spec generated against staging has to
    run against production."""
    assert EM.normalize_path(raw) == expected


@pytest.mark.parametrize("status", ["100", "301", "302", "404", "500", "0", "x", ""])
def test_only_a_settled_success_becomes_an_endpoint(status):
    """A 3xx is a hop and a 4xx/5xx is a DEFECT.  Compiling one in would freeze
    the application's bug into its own regression suite as the behaviour the
    suite demands."""
    assert EM.normalize_endpoint(
        {"method": "GET", "url": "/api/x", "status": status}) is None


def test_a_2xx_becomes_a_canonical_record():
    assert EM.normalize_endpoint(
        {"method": "post", "url": "https://a.test/api/Quote?k=v", "status": "201",
         "response_mime": "application/json"}
    ) == {"method": "POST", "path": "/api/quote", "status": "201",
          "response_mime": "application/json"}


def test_malformed_evidence_is_dropped_never_raised():
    assert EM.endpoints_of(None) == []
    assert EM.endpoints_of({"observed_endpoints": "not-a-list"}) == []
    assert EM.endpoints_of({"observed_endpoints": [None, 7, {}]}) == []
    assert EM.normalize_endpoint("nope") is None


def test_the_structural_rule_differences_the_two_states():
    """``caused(A -> B) = endpoints(B) \\ endpoints(A)``.

    Claiming everything at B would attribute B's own page-load traffic to
    whatever control happened to reach it.
    """
    a = {"observed_endpoints": [
        {"method": "GET", "url": "/api/config", "status": "200"}]}
    b = {"observed_endpoints": [
        {"method": "GET", "url": "/api/config", "status": "200"},
        {"method": "POST", "url": "/api/quote", "status": "200"}]}
    assert [(e["method"], e["path"]) for e in EM.caused_by(a, b)] == [
        ("POST", "/api/quote")]
    # The entry step owns the whole map: nothing precedes it.
    assert len(EM.caused_by(None, a, is_entry=True)) == 1


def test_the_recorded_cause_outranks_the_structural_inference():
    """When M2.5 stamped the action, the attribution is READ, not derived."""
    inventory = {"endpoints": [{
        "method": "POST", "path_template": "/api/quote",
        "statuses": {"503": 2, "200": 1},
        "actions": [{"verb": "click", "label": "Get Quote"}],
    }]}
    by_action = EM.inventory_by_action(inventory)
    assert list(by_action) == ["get quote"]
    # The endpoint RETRIED and eventually succeeded; the success is the
    # behaviour a regression test should demand.
    assert by_action["get quote"][0]["status"] == "200"

    steps = EM.attribute_steps(
        ["a", "b"], {"a": {}, "b": {"observed_endpoints": [
            {"method": "GET", "url": "/api/other", "status": "200"}]}},
        step_labels=["Get Quote"], by_action=by_action)
    assert steps[1]["attribution"] == EM.ATTRIBUTION_RECORDED
    assert [(e["method"], e["path"]) for e in steps[1]["caused"]] == [
        ("POST", "/api/quote")]


def test_an_endpoint_that_only_ever_failed_contributes_nothing():
    """No test may be generated that requires an application's bug."""
    assert EM.inventory_by_action({"endpoints": [{
        "method": "POST", "path_template": "/api/quote",
        "statuses": {"500": 3},
        "actions": [{"label": "Get Quote"}]}]}) == {}


def test_the_fallback_is_used_and_named_when_nothing_was_stamped():
    steps = EM.attribute_steps(
        ["a", "b"],
        {"a": {}, "b": {"observed_endpoints": [
            {"method": "POST", "url": "/api/quote", "status": "200"}]}},
        step_labels=["Get Quote"], by_action={})
    assert steps[1]["attribution"] == EM.ATTRIBUTION_INFERRED
    assert all(e["attribution"] == EM.ATTRIBUTION_INFERRED
               for e in steps[1]["caused"])


def test_a_mutation_outranks_a_read_when_assertions_are_bounded():
    """The claim of an end-to-end journey is that its COMMIT reached the
    backend; if only a few assertions fit, that is the one that must."""
    ranked = EM.rank_for_assertion([
        {"method": "GET", "path": "/a"}, {"method": "GET", "path": "/b"},
        {"method": "GET", "path": "/c"}, {"method": "POST", "path": "/z"},
    ], limit=2)
    assert [e["method"] for e in ranked] == ["POST", "GET"]
    assert ranked[0]["path"] == "/z"


def test_merging_never_erases_or_rewrites_evidence():
    merged = EM.merge_endpoints(
        [{"method": "GET", "path": "/a", "status": "200"}],
        [{"method": "GET", "path": "/a", "status": "204"},
         {"method": "POST", "path": "/b", "status": "201"}])
    # First observation wins for a repeated key; the new key is added.
    assert [(e["method"], e["path"], e["status"]) for e in merged] == [
        ("GET", "/a", "200"), ("POST", "/b", "201")]


def test_endpoint_output_is_sorted_and_therefore_reproducible():
    unsorted = [{"method": "POST", "url": "/z", "status": "200"},
                {"method": "GET", "url": "/a", "status": "200"}]
    first = EM.endpoints_of({"observed_endpoints": unsorted})
    second = EM.endpoints_of({"observed_endpoints": list(reversed(unsorted))})
    assert first == second


# ══════════════════════════════════════════════════════════════════════════
#  journey_criticality — T-GEN-02
# ══════════════════════════════════════════════════════════════════════════

def _node(url, *, fields=(), targets=(), boundary=False, endpoints=()):
    return {
        "url": url, "is_boundary": boundary,
        "controls_inventory": (
            [{"name": f, "type": "text"} for f in fields]
            + [{"name": t, "type": "button"} for t in targets]),
        "observed_endpoints": list(endpoints),
    }


def test_the_projection_splits_questions_from_targets():
    """The registry reads the field and button spaces separately on purpose —
    a money marker must not fire on unrelated field text."""
    subject = JCRIT.subject_from_journey_graph(
        {}, [_node("https://a.test/checkout", fields=["Card Number"],
                   targets=["Pay Now"])])
    assert "card number" in subject["field_label"].lower()
    assert "pay now" in subject["button_label"].lower()
    assert "card number" not in subject["button_label"].lower()


def test_advance_triggers_join_the_button_space():
    """A funnel's commit control is read on the page it LEAVES, so it is often
    absent from any single node's inventory."""
    subject = JCRIT.subject_from_journey_graph(
        {}, [_node("https://a.test/x")], edge_labels=["Authorize Payment"])
    assert "authorize payment" in subject["button_label"].lower()


def test_a_money_route_bands_p0_with_its_evidence():
    banded = JCRIT.evaluate_journey(
        {}, [_node("https://a.test/checkout/cart", targets=["Pay Now"])])
    assert banded["band"] == "P0"
    assert {h["signal_id"] for h in banded["evidence"]} >= {
        "money_route", "money_control"}
    assert banded["subject"]["url_path"]


def test_an_unmatched_journey_fails_up_to_p1():
    banded = JCRIT.evaluate_journey({}, [_node("https://a.test/about")])
    assert banded["band"] == "P1"
    assert banded["evidence"][0]["signal_id"] == "fail_up_default"


def test_a_multi_page_submit_is_banded_structurally():
    banded = JCRIT.evaluate_journey(
        {}, [_node("https://a.test/a"), _node("https://a.test/b", boundary=True)])
    assert banded["band"] == "P1"
    assert any(h["signal_id"] == "multi_page_submit" for h in banded["evidence"])


def test_band_order_is_read_from_the_registrys_own_tuple():
    assert JCRIT.band_order("P0") < JCRIT.band_order("P1") < JCRIT.band_order("P3")
    # An unrecognised band takes the FAIL-UP position, never the least critical.
    assert JCRIT.band_order("nonsense") == JCRIT.band_order("P1")


def _entry(jid, band, **kw):
    e = {"journey_id": jid, "criticality": {"band": band},
         "deepest_steps": 0, "paths_completed": 0,
         "boundary_nodes": 0, "endpoints_observed": 0}
    e.update(kw)
    return e


def test_the_ranking_is_total_and_order_independent():
    entries = [_entry("c", "P1", deepest_steps=1), _entry("a", "P0"),
               _entry("b", "P1", deepest_steps=5)]
    order = [e["journey_id"] for e in JCRIT.rank(entries)]
    assert order == ["a", "b", "c"]
    assert [e["journey_id"] for e in JCRIT.rank(list(reversed(entries)))] == order


def test_identical_journeys_never_swap_places():
    """The tie-break of last resort: two rows equal on every measure are ordered
    by id, so two reads of one database agree."""
    same = [_entry("z", "P1"), _entry("a", "P1"), _entry("m", "P1")]
    assert [e["journey_id"] for e in JCRIT.rank(same)] == ["a", "m", "z"]


def test_rank_is_assigned_over_the_whole_set_before_any_slice():
    entries = [_entry(str(i), "P1", deepest_steps=i) for i in range(5)]
    head = JCRIT.top_n(entries, 2)
    assert [e["rank"] for e in head] == [1, 2]
    assert head[0]["deepest_steps"] == 4


def test_the_default_top_n_is_twenty():
    assert JCRIT.TOP_N_DEFAULT == 20


# ══════════════════════════════════════════════════════════════════════════
#  journey_spec — T-GEN-01 / T-GEN-04
# ══════════════════════════════════════════════════════════════════════════

FP_A, FP_B = "fp-a", "fp-b"


def _journey(**kw):
    j = {"journey_id": "j1", "entry_url": "https://a.test/start",
         "entry_title": "Start", "business_name": "Quote",
         "name_description": "", "baseline_status": "approved"}
    j.update(kw)
    return j


def _graph():
    return {
        FP_A: {"fingerprint": FP_A, "url": "https://a.test/start",
               "title": "Start",
               "controls_inventory": [{"name": "Get Quote", "type": "button"}],
               "displayed_outcomes": [], "observed_endpoints": []},
        FP_B: {"fingerprint": FP_B, "url": "https://a.test/result",
               "title": "Result", "controls_inventory": [],
               "displayed_outcomes": [
                   {"label": "Premium", "selector": "#premium",
                    "value_type": "currency"}],
               "observed_endpoints": [
                   {"method": "POST", "url": "/api/quote", "status": "200"}]},
    }


def _traversal(**kw):
    t = {"traversal_id": "t1", "path_fps": [FP_A, FP_B], "completed": True,
         "terminal": "submit_boundary",
         "outcome_values": [{"label": "Premium", "value": "$42.50",
                             "value_type": "currency"}]}
    t.update(kw)
    return t


def _build(**kw):
    args = {"journey": _journey(), "traversal": _traversal(),
            "nodes_by_fp": _graph(),
            "edges": [{"from_fp": FP_A, "to_fp": FP_B,
                       "trigger_label_norm": "get quote", "advance_tier": 1}],
            "endpoint_inventory": None}
    args.update(kw)
    journey = args.pop("journey")
    return JS.build_journey_case(journey, tenant_id="t1", **args)


def test_a_walked_journey_compiles_from_its_own_evidence():
    payload = _build()
    assert payload["compilable"] is True
    assert payload["provenance"] == "journey_direct"
    assert [s["observed"]["verb"] for s in payload["steps"]] == [
        "navigate", "click"]
    # The ORIGINAL casing is recovered from the node's control inventory: an
    # accessible-name locator needs the name the page renders.
    assert payload["steps"][1]["observed"]["label"] == "Get Quote"
    assert payload["steps"][1]["observed"]["next_url"] == "https://a.test/result"


def test_the_test_id_is_keyed_on_the_journey_not_the_artifact():
    """A re-crawl mints a new artifact and must not mint a new test identity,
    or every history join breaks on re-crawl."""
    assert JS.test_id_for("t1", "j1") == JS.test_id_for("t1", "j1")
    assert JS.test_id_for("t1", "j1") != JS.test_id_for("t2", "j1")


@pytest.mark.parametrize("traversal,fragment", [
    (None, "no completed walk"),
    ({"path_fps": [FP_A], "completed": True}, "never advanced"),
])
def test_a_journey_with_no_path_is_refused_with_a_named_reason(traversal, fragment):
    payload = _build(traversal=traversal)
    assert payload["compilable"] is False
    assert fragment in payload["reason"]


def test_a_transition_with_no_recorded_trigger_refuses_the_whole_journey():
    """No step can be fabricated for a click the crawl cannot name, and
    compiling around the gap would silently reorder every later assertion."""
    payload = _build(edges=[])
    assert payload["compilable"] is False
    assert "no recorded trigger" in payload["reason"]


def test_a_confirmed_baseline_arms_the_hard_outcome_oracle():
    payload = _build()
    assert payload["outcome_oracle"] == "hard"
    assert payload["value_assertions"] == [{
        "field": "Premium", "expected": 42.5, "match": "numeric",
        "tolerance": JS.NUMERIC_TOLERANCE, "source_hint": "#premium",
        "observed_text": "$42.50"}]


@pytest.mark.parametrize("status", ["captured", "drifted", ""])
def test_an_unapproved_baseline_arms_nothing_and_says_so(status):
    """Withheld, not downgraded.  Emitting it soft would be the informational
    log this milestone removes; emitting it hard would let a value nobody
    approved fail somebody's build."""
    payload = _build(journey=_journey(baseline_status=status))
    assert payload["outcome_oracle"] == "soft"
    assert payload["value_assertions"] == []
    assert payload["unconfirmed_outcomes"] == ["Premium"]


def test_an_incomplete_walk_is_never_confirmed_however_it_was_approved():
    """Both halves are required: a human's approval cannot supply an end state
    the walk never reached."""
    assert JS.is_confirmed(_journey(), _traversal(completed=False)) is False


def test_an_outcome_with_no_captured_selector_is_named_not_guessed():
    """A silent drop and a fabricated locator are the two ways this becomes
    green-wash; both are refused."""
    graph = _graph()
    graph[FP_B]["displayed_outcomes"] = []
    payload = _build(nodes_by_fp=graph)
    assert payload["value_assertions"] == []
    assert payload["ungrounded_outcomes"] == ["Premium"]


def test_a_tier_zero_advance_does_not_get_to_claim_causality():
    """``navigation_grounded`` is what lets a step assert a toHaveURL.  A
    pre-evidence edge cannot make that claim."""
    payload = _build(edges=[{"from_fp": FP_A, "to_fp": FP_B,
                             "trigger_label_norm": "get quote",
                             "advance_tier": 0}])
    step = payload["steps"][1]
    assert step["observed"]["navigation_grounded"] is False
    assert step["confidence"] == "review"
    assert step["provenance"] == "inferred"


def test_the_highest_advance_tier_wins_for_a_repeated_transition():
    index = JS.edge_index([
        {"from_fp": FP_A, "to_fp": FP_B, "trigger_label_norm": "old", "advance_tier": 0},
        {"from_fp": FP_A, "to_fp": FP_B, "trigger_label_norm": "new", "advance_tier": 3},
    ])
    assert index[(FP_A, FP_B)]["trigger_label_norm"] == "new"


def test_the_payload_is_deterministic():
    assert _build() == _build()


def test_the_persisted_causal_join_is_read_from_the_edge_rows():
    """The API request path holds graph ROWS, not a crawl blob.

    A fold passes the M2.5 inventory; a request has only ``journey_edges``, where
    the fold stored the same join. Reading only the first would make every
    request-path attribution degrade to inference — a recorded fact reported as
    a guess.
    """
    edges = [{"from_fp": FP_A, "to_fp": FP_B,
              "trigger_label_norm": "get quote", "advance_tier": 1,
              "observed_endpoints": [
                  {"method": "POST", "path": "/api/quote", "status": "200"}]}]
    assert EM.by_action_from_edges(edges)["get quote"][0]["attribution"] == (
        EM.ATTRIBUTION_RECORDED)

    payload = _build(edges=edges)
    step = payload["steps"][1]
    assert step["network_attribution"] == EM.ATTRIBUTION_RECORDED
    assert [(e["method"], e["path"]) for e in step["network_expect"]] == [
        ("POST", "/api/quote")]
    assert payload["endpoints_recorded_cause"] == 1


def test_an_edge_with_nothing_recorded_falls_back_and_says_so():
    payload = _build()          # edges carry no observed_endpoints
    assert payload["steps"][1]["network_attribution"] == EM.ATTRIBUTION_INFERRED
    assert payload["endpoints_recorded_cause"] == 0


def test_the_inventory_wins_over_the_edge_rows_when_both_are_present():
    """A fold has the fresher evidence; the edge row is what it is about to
    write."""
    edges = [{"from_fp": FP_A, "to_fp": FP_B, "trigger_label_norm": "get quote",
              "advance_tier": 1, "observed_endpoints": [
                  {"method": "POST", "path": "/api/stale", "status": "200"}]}]
    payload = _build(edges=edges, endpoint_inventory={"endpoints": [{
        "method": "POST", "path_template": "/api/fresh",
        "statuses": {"200": 1}, "actions": [{"label": "Get Quote"}]}]})
    assert [e["path"] for e in payload["steps"][1]["network_expect"]] == [
        "/api/fresh"]
