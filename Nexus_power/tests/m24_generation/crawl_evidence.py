"""M2.4 / T-GEN-06 — the crawl evidence the generation pipeline reads.

WHAT IS FIXTURE HERE AND WHAT IS PRODUCTION, stated plainly so the proof's claim
cannot be over-read.

FIXTURE: the raw network events and the journey graph rows — i.e. what a crawl of
the quote application WOULD have recorded.  Driving the live crawler is M1.x/M2.5
territory and is proven by their own suites; re-running it here would make this
proof a test of the crawler rather than of generation.

PRODUCTION, and therefore genuinely exercised by this proof:

  * ``endpoint_inventory.build_inventory`` — the M2.5 aggregation that turns raw
    events into the application's API surface, INCLUDING the ``actions`` list
    that names the UI action each endpoint was observed firing.  The proof does
    not hand-write that join; it reads the one M2.5 produces.
  * ``endpoint_map.inventory_by_action`` / ``normalize_endpoint`` — the M2.4
    consumer that inverts it and canonicalises every endpoint.
  * ``journey_criticality`` — the banding and the ranking.
  * ``journey_spec.build_journey_case`` — the compile payload.
  * ``journey_compiler.compile_journey`` — the spec, the lint and the audit.

THE THREE JOURNEYS, and why the ranking needs all three.  A Top-N over one
journey demonstrates nothing about ordering, and a ranking that only ever sees
one band cannot show that the band is what orders it.  So the fixture carries a
P0 (a payment funnel: money route AND money control), a P1 (the quote funnel,
banded by multi-page submit), and a journey no signal matches at all — which must
FAIL UP to P1 rather than silently sink to P3.  Only the quote funnel corresponds
to the live application; the other two exist to be ranked, and one of them is
deliberately incompletable so the pipeline's refusal path is exercised too.

Every value-free rule the pipeline claims is respected here: the graph rows carry
labels, paths and shapes, never a committed answer.
"""
from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from . import fixture_app as fixture
from .service_import import load

# Fingerprints are opaque state ids in the real graph; here they only have to be
# distinct and stable, because every join in the pipeline is by identity.
FP_START = "fp-quote-start"
FP_RESULT = "fp-quote-result"
FP_PAY_1 = "fp-pay-cart"
FP_PAY_2 = "fp-pay-confirm"
FP_ABOUT = "fp-about"

JOURNEY_QUOTE = "journey-quote-funnel"
JOURNEY_PAYMENT = "journey-payment"
JOURNEY_BROWSE = "journey-browse"
JOURNEY_ABANDONED = "journey-abandoned"

#: The advance the walk clicked to cross the quote funnel.  Normalised exactly as
#: the fold normalises an edge trigger, because that string is the JOIN KEY
#: between the journey graph and the M2.5 endpoint inventory.
QUOTE_TRIGGER = "get quote"
QUOTE_TRIGGER_RENDERED = "Get Quote"


def network_events(origin: str) -> list[dict[str, Any]]:
    """The raw M2.5 event stream a crawl of the quote application would capture.

    Two events, and the difference between them is the whole point of the
    milestone: the page-load read carries NO action attribution (nothing clicked
    it), while the commit carries the UI action that was in flight when the
    request went out.  The first therefore has to be attributed structurally and
    the second is READ — one journey exercises both attribution rules.
    """
    return [
        {
            "url": f"{origin}{fixture.CONFIG_PATH}",
            "method": "GET",
            "status": 200,
            "sequence": 1,
            "timestamp_ms": 120,
            "resource_type": "fetch",
            "response_shape": "object",
            "shape_source": "media_type",
            "auth_pattern": "none",
            "request_headers": {},
            "request_body": {},
            # No action: this is the page's own boot traffic.
            "action_token": "",
            "action_label": "",
            "action_verb": "",
        },
        {
            "url": f"{origin}{fixture.QUOTE_PATH}",
            "method": "POST",
            "status": 200,
            "sequence": 2,
            "timestamp_ms": 640,
            "resource_type": "fetch",
            "response_shape": "object",
            "shape_source": "media_type",
            "auth_pattern": "none",
            "request_headers": {"content-type": "application/json"},
            # Key NAMES only — the API contract, never the user's answers.
            "request_body": {"bytes": 34, "keys": ["age", "product"],
                             "keys_source": "json"},
            # T-NET-03 — the stamp this whole milestone joins on.
            "action_token": "a2",
            "action_label": QUOTE_TRIGGER_RENDERED,
            "action_verb": "click",
        },
    ]


def endpoint_inventory(origin: str) -> dict[str, Any]:
    """The M2.5 endpoint inventory, built by the PRODUCTION aggregator.

    Loaded through :mod:`service_import` because three services in this
    repository ship a package called ``app``; the returned inventory is a plain
    dict, so nothing live is held across the service switch that follows.
    """
    return load("explorer", "app.endpoint_inventory").build_inventory(
        network_events(origin))


def _raw_calls(origin: str, *paths_and_methods: tuple[str, str]) -> list[dict[str, str]]:
    """Per-state captured calls in the shape a ``page_state`` record carries."""
    return [
        {"method": method, "url": f"{origin}{path}", "status": "200",
         "resource_type": "fetch", "response_mime": "application/json"}
        for method, path in paths_and_methods
    ]


def nodes(origin: str) -> dict[str, dict[str, Any]]:
    """The journey graph's nodes, keyed by fingerprint.

    ``observed_endpoints`` holds the calls observed while each state was open —
    the STATE-level map, which is what the structural attribution rule differences.
    """
    return {
        FP_START: {
            "fingerprint": FP_START,
            "url": f"{origin}/index.html",
            "title": "Quote Start",
            "is_decision": True,
            "is_boundary": False,
            "controls_inventory": [
                {"name": QUOTE_TRIGGER_RENDERED, "type": "button",
                 "signature": "btn-get-quote"},
            ],
            "displayed_outcomes": [],
            "observed_endpoints": _raw_calls(origin, ("GET", fixture.CONFIG_PATH)),
        },
        FP_RESULT: {
            "fingerprint": FP_RESULT,
            "url": f"{origin}/result.html",
            "title": "Quote Result",
            "is_decision": False,
            # The commit boundary: crossing it is what makes this a funnel and
            # not a browse, and it is what the criticality registry reads as a
            # multi-page submission.
            "is_boundary": True,
            "controls_inventory": [],
            "displayed_outcomes": [
                {"label": "Monthly Premium", "selector": "#premium",
                 "value_type": "currency"},
            ],
            # Drained when the crawl RECORDED this state: the POST the click
            # caused lands here, which is why the structural rule differences
            # against the previous state rather than claiming everything.
            "observed_endpoints": _raw_calls(
                origin, ("GET", fixture.CONFIG_PATH), ("POST", fixture.QUOTE_PATH)),
        },
        FP_PAY_1: {
            "fingerprint": FP_PAY_1,
            "url": "https://pay.example.test/checkout/cart",
            "title": "Cart",
            "is_decision": True, "is_boundary": False,
            "controls_inventory": [
                {"name": "Pay Now", "type": "button", "signature": "btn-pay"},
                {"name": "Card Number", "type": "text", "signature": "f-card"},
            ],
            "displayed_outcomes": [], "observed_endpoints": [],
        },
        FP_PAY_2: {
            "fingerprint": FP_PAY_2,
            "url": "https://pay.example.test/checkout/confirm",
            "title": "Confirm Payment",
            "is_decision": False, "is_boundary": True,
            "controls_inventory": [], "displayed_outcomes": [],
            "observed_endpoints": [],
        },
        FP_ABOUT: {
            "fingerprint": FP_ABOUT,
            "url": "https://www.example.test/about/team",
            "title": "About the team",
            "is_decision": False, "is_boundary": False,
            "controls_inventory": [
                {"name": "Read more", "type": "link", "signature": "lnk-more"},
            ],
            "displayed_outcomes": [], "observed_endpoints": [],
        },
    }


def edges() -> list[dict[str, Any]]:
    """The transitions the walks crossed.

    ``advance_tier`` 1 means a deterministic advance decision — the crawl KNOWS
    what it clicked and that the click caused the transition, which is exactly
    the claim a ``toHaveURL`` assertion makes and the auditor checks.
    """
    return [
        {"from_fp": FP_START, "to_fp": FP_RESULT,
         "trigger_label_norm": QUOTE_TRIGGER, "advance_tier": 1},
        {"from_fp": FP_PAY_1, "to_fp": FP_PAY_2,
         "trigger_label_norm": "pay now", "advance_tier": 1},
        {"from_fp": FP_ABOUT, "to_fp": FP_ABOUT,
         "trigger_label_norm": "read more", "advance_tier": 1},
    ]


def traversals() -> dict[str, dict[str, Any]]:
    """One COMPLETED traversal per compilable journey, keyed by journey id."""
    return {
        JOURNEY_QUOTE: {
            "traversal_id": "t-quote-1",
            "path_fps": [FP_START, FP_RESULT],
            "completed": True,
            "terminal": "submit_boundary",
            "outcome_values": [
                {"label": "Monthly Premium",
                 "value": f"${fixture.BASELINE_PREMIUM}",
                 "value_type": "currency"},
            ],
        },
        JOURNEY_PAYMENT: {
            "traversal_id": "t-pay-1",
            "path_fps": [FP_PAY_1, FP_PAY_2],
            "completed": True,
            "terminal": "submit_boundary",
            "outcome_values": [],
        },
        JOURNEY_BROWSE: {
            "traversal_id": "t-browse-1",
            "path_fps": [FP_ABOUT, FP_ABOUT],
            "completed": True,
            "terminal": "exhausted",
            "outcome_values": [],
        },
        # No traversal at all: the crawl found the entry and never finished a
        # walk.  It must be RANKED (it is a real journey) and REFUSED for
        # compilation with a named reason (it has no path to re-prove).
        JOURNEY_ABANDONED: None,
    }


def journeys(origin: str) -> list[dict[str, Any]]:
    """The ``JourneyRow`` projections the pipeline bands, ranks and compiles."""
    return [
        {
            "journey_id": JOURNEY_QUOTE,
            "entry_fingerprint": FP_START,
            "entry_url": f"{origin}/index.html",
            "entry_title": "Quote Start",
            "business_name": "Term Life Quote",
            "name_description": "Quote a term life policy for a 35 year old",
            "deepest_steps": 2,
            # APPROVED on a completed walk — the only combination that makes the
            # outcome criterion a HARD oracle (T-GEN-04).
            "baseline_status": "approved",
        },
        {
            "journey_id": JOURNEY_PAYMENT,
            "entry_fingerprint": FP_PAY_1,
            "entry_url": "https://pay.example.test/checkout/cart",
            "entry_title": "Cart",
            "business_name": "Checkout and Pay",
            "name_description": "Pay for the cart",
            "deepest_steps": 2,
            "baseline_status": "captured",
        },
        {
            "journey_id": JOURNEY_BROWSE,
            "entry_fingerprint": FP_ABOUT,
            "entry_url": "https://www.example.test/about/team",
            "entry_title": "About the team",
            "business_name": "About",
            "name_description": "Read the about page",
            "deepest_steps": 1,
            "baseline_status": "captured",
        },
        {
            "journey_id": JOURNEY_ABANDONED,
            "entry_fingerprint": "fp-abandoned",
            "entry_url": "https://www.example.test/apply/start",
            "entry_title": "Start an application",
            "business_name": "Application",
            "name_description": "",
            "deepest_steps": 0,
            "baseline_status": "captured",
        },
    ]


def rollup_for(journey: dict[str, Any], traversal: Any) -> dict[str, Any]:
    """The rollup fields the ranking reads, in the shape the router produces."""
    return {
        "journey_id": journey["journey_id"],
        "business_name": journey["business_name"],
        "deepest_steps": journey["deepest_steps"],
        "paths_completed": 1 if traversal else 0,
    }


def origin_of(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


__all__ = [
    "FP_START", "FP_RESULT", "FP_PAY_1", "FP_PAY_2", "FP_ABOUT",
    "JOURNEY_QUOTE", "JOURNEY_PAYMENT", "JOURNEY_BROWSE", "JOURNEY_ABANDONED",
    "QUOTE_TRIGGER", "QUOTE_TRIGGER_RENDERED",
    "network_events", "endpoint_inventory", "nodes", "edges", "traversals",
    "journeys", "rollup_for", "origin_of",
]
