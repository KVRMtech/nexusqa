"""Regression tests for the Track-1 Playwright-quality generator fixes.

These pin the auditor's 2/10 failure mode shut: the deterministic generator must
NEVER assert a navigation a click is not shown to cause (the impossible
"Add to cart -> /checkout-step-one" transition), MUST restore fills that live only
in the action stream (verb=type), and MUST keep a genuinely-grounded navigation
(no over-suppression). Acceptance tests AT-0/AT-1/AT-2 from the 10/10 plan.

NO live stack / NO DB / NO SDK: nexus_sdk lives in the separate nexus-base image,
so we stub nexus_sdk.models and load the generator by file path (same approach as
test_induced_drift_benchmark.py). Run from Nexus_power/platform/api:
    python -m pytest tests/test_generator_navigation_grounding.py -q
"""
from __future__ import annotations

import importlib.util
import os
import sys
import types

# Prefer the REAL SDK models when available (CI / container); fall back to a
# minimal stub so the generator's pure logic still loads standalone. Trying the
# real import FIRST means this test never poisons sys.modules['nexus_sdk'] for
# sibling tests that need the real package.
try:
    from nexus_sdk.models import (  # noqa: F401
        Precondition,
        ProductionTestCase,
        ProductionTestStep,
    )
except Exception:
    class _Base:
        def __init__(self, **kw):
            for k, v in kw.items():
                setattr(self, k, v)

    class Precondition(_Base):
        pass

    class ProductionTestStep(_Base):
        def __init__(self, **kw):
            self.observed = {}
            self.provenance = ""
            self.screenshot = ""
            self.data_ref = None
            super().__init__(**kw)

    class ProductionTestCase(_Base):
        pass

    _mod = types.ModuleType("nexus_sdk")
    _models = types.ModuleType("nexus_sdk.models")
    _models.Precondition = Precondition
    _models.ProductionTestStep = ProductionTestStep
    _models.ProductionTestCase = ProductionTestCase
    _mod.models = _models
    sys.modules["nexus_sdk"] = _mod
    sys.modules["nexus_sdk.models"] = _models

_GEN_PATH = os.path.join(
    os.path.dirname(__file__), "..", "app", "services", "test_factory", "generator.py"
)
_spec = importlib.util.spec_from_file_location("nexus_generator_under_test", _GEN_PATH)
gen = importlib.util.module_from_spec(_spec)
sys.modules["nexus_generator_under_test"] = gen  # dataclass processing needs this
_spec.loader.exec_module(gen)

PV, PA = gen.PageVisitInput, gen.PageActionInput


def _visits():
    return [
        PV(page_visit_id="v1", sequence_index=0, location="Inventory",
           url_host="saucedemo.com", url_path="/inventory", url_query="",
           canonical_host="saucedemo.com", source="url_regex", form_snapshot={}),
        PV(page_visit_id="v2", sequence_index=1, location="Checkout: Your Information",
           url_host="saucedemo.com", url_path="/checkout-step-one", url_query="",
           canonical_host="saucedemo.com", source="url_regex", form_snapshot={}),
    ]


def _actions(addcart_navigated: bool):
    """A click that stays on the page (Add to cart) + two typed fields whose values
    live ONLY in the action stream (empty form_snapshot above)."""
    return [
        PA(page_visit_id="v1", subaction_index=0, verb="click",
           target_label="Add to cart (Sauce Labs Backpack)", target_kind="button",
           value=None,
           after_outcome=("navigation" if addcart_navigated else "content_appeared"),
           after_detail=("" if addcart_navigated else "button now reads Remove; cart shows 1 item"),
           navigated=addcart_navigated),
        PA(page_visit_id="v2", subaction_index=0, verb="type", target_label="First Name",
           target_kind="text_field", value="Venkata"),
        PA(page_visit_id="v2", subaction_index=1, verb="type", target_label="Last Name",
           target_kind="text_field", value="karnam"),
    ]


def _steps(addcart_navigated: bool):
    res = gen.generate_demonstrated_test_cases(
        artifact_id="t", page_visits=_visits(), page_actions=_actions(addcart_navigated))
    assert res.test_cases, "expected a demonstrated test case (>=2 page groups)"
    return res.test_cases[0].steps


def _find(steps, needle):
    return [s for s in steps if needle.lower() in (getattr(s, "action", "") or "").lower()]


def test_non_navigating_click_does_not_assert_navigation():
    """AT-0: 'Add to cart' (stays on page) must NOT pin next_url -> no impossible toHaveURL."""
    steps = _steps(addcart_navigated=False)
    addcart = _find(steps, "Add to cart")[0]
    assert "next_url" not in (addcart.observed or {})


def test_uncaptured_boundary_is_demoted_to_inferred():
    """AT-0: the un-grounded inventory->checkout-step-one boundary must be UNPROVEN
    (provenance='inferred'), not a hard nav assertion that can only fail RED."""
    steps = _steps(addcart_navigated=False)
    navverify = _find(steps, "Verify the application navigated")[0]
    assert getattr(navverify, "provenance", "") == "inferred"


def test_typed_fills_restored_from_action_stream():
    """AT-2: typed values (verb=type) with an EMPTY form_snapshot must still become
    grounded fill steps (otherwise the data file / value-token helper go dead)."""
    steps = _steps(addcart_navigated=False)
    firsts = _find(steps, "First Name")
    lasts = _find(steps, "Last Name")
    assert any((s.observed or {}).get("value") == "Venkata" for s in firsts)
    assert any((s.observed or {}).get("value") == "karnam" for s in lasts)


def test_inferred_page_url_is_not_asserted():
    """Phase 1 (honest contract): a vision/title-INFERRED page URL must NOT be
    asserted as fact — its nav-verify is UNPROVEN even when the navigation itself
    was grounded. A PROVEN (url_regex / ground_truth) page still asserts."""
    visits = [
        PV(page_visit_id="v1", sequence_index=0, location="Inventory",
           url_host="saucedemo.com", url_path="/inventory", url_query="",
           canonical_host="saucedemo.com", source="url_regex", form_snapshot={}),
        PV(page_visit_id="v2", sequence_index=1, location="Checkout",
           url_host="saucedemo.com", url_path="/checkout-step-one", url_query="",
           canonical_host="saucedemo.com", source="llm_inferred", form_snapshot={},
           extraction_confidence=0.85),
    ]
    acts = [
        PA(page_visit_id="v1", subaction_index=0, verb="click",
           target_label="Continue", target_kind="button", value=None,
           after_outcome="navigation", navigated=True),
    ]
    res = gen.generate_demonstrated_test_cases(artifact_id="t", page_visits=visits, page_actions=acts)
    steps = res.test_cases[0].steps
    navverify = _find(steps, "Verify the application navigated")[0]
    assert getattr(navverify, "provenance", "") == "inferred"  # vision URL → UNPROVEN


def test_grounded_navigation_is_kept():
    """AT-1: a click the recording PROVES navigated keeps its assertion (no over-suppression)."""
    steps = _steps(addcart_navigated=True)
    addcart = _find(steps, "Add to cart")[0]
    navverify = _find(steps, "Verify the application navigated")[0]
    assert (addcart.observed or {}).get("next_url")
    assert (addcart.observed or {}).get("navigation_grounded") is True
    assert getattr(navverify, "provenance", "") == "demonstrated"


# ─── Fix B: grounded MENU transition + per-action boundary (never green-wash) ────
#
# The live defect: a menu-gated category nav (Categories ▸ Hand Tools) was captured
# with a grounded click-path, but the generator picked the chronologically-LAST
# click (a footer credit link, 'Unsplash') as the transition and then HARD-asserted
# the category navigation because *some* click in the group navigated. These pin:
#   (a) the grounded navigating click (matched by its captured destination) is the
#       transition, its disclosure OPENER is emitted first, and the noise is dropped;
#   (b) the boundary is PROVEN only when the emitted transition itself reached the
#       next page — a different click navigating ELSEWHERE never green-washes it.

def _menu_visits(second_path: str):
    return [
        PV(page_visit_id="v1", sequence_index=0, location="Home",
           url_host="shop.test", url_path="/", url_query="",
           canonical_host="shop.test", source="ground_truth", form_snapshot={}),
        PV(page_visit_id="v2", sequence_index=1, location="Category",
           url_host="shop.test", url_path=second_path, url_query="",
           canonical_host="shop.test", source="ground_truth", form_snapshot={}),
    ]


def _menu_actions():
    """Home page: open the 'Categories' disclosure, click the hidden 'Hand Werkzeuge'
    item (grounded nav to /category/hand-tools), then a trailing noise click that is
    chronologically LAST but navigates nowhere ('Unsplash', a footer credit link)."""
    return [
        PA(page_visit_id="v1", subaction_index=0, verb="click",
           target_label="Categories", target_kind="button", value=None,
           after_outcome="none", after_detail="", navigated=False,
           control_role="button", css_hint="button.nav-link.dropdown-toggle",
           expanded="false"),
        PA(page_visit_id="v1", subaction_index=1, verb="click",
           target_label="Hand Werkzeuge", target_kind="link", value=None,
           after_outcome="navigation",
           after_detail="https://shop.test/category/hand-tools", navigated=True,
           control_role="link", css_hint="a.dropdown-item", expanded=""),
        PA(page_visit_id="v1", subaction_index=2, verb="click",
           target_label="Unsplash", target_kind="link", value=None,
           after_outcome="none", after_detail="", navigated=False,
           control_role="link", css_hint="a", expanded=""),
    ]


def test_grounded_menu_emits_opener_then_item_drops_noise():
    """Fix B(a): the grounded menu nav becomes [Click 'Categories', Click 'Hand
    Werkzeuge'] with a PROVEN next_url; the trailing 'Unsplash' noise is dropped."""
    res = gen.generate_demonstrated_test_cases(
        artifact_id="t", page_visits=_menu_visits("/category/hand-tools"),
        page_actions=_menu_actions())
    steps = res.test_cases[0].steps
    opener = _find(steps, "Click 'Categories'")
    item = _find(steps, "Hand Werkzeuge")
    assert opener and item, "opener + grounded item must both be emitted"
    assert opener[0].step_number < item[0].step_number, "opener must precede the item"
    assert not _find(steps, "Unsplash"), "trailing non-navigating noise must be dropped"
    assert (opener[0].observed or {}).get("next_url") in (None, ""), \
        "the opener is a menu-open, not the navigation"
    assert gen._path_of((item[0].observed or {}).get("next_url", "")) == "/category/hand-tools"
    assert (item[0].observed or {}).get("navigation_grounded") is True


def test_grounded_menu_boundary_is_demonstrated():
    """Fix B: the boundary INTO the category page is PROVEN (hard toHaveURL) because
    the emitted transition (the item click) itself reached it."""
    res = gen.generate_demonstrated_test_cases(
        artifact_id="t", page_visits=_menu_visits("/category/hand-tools"),
        page_actions=_menu_actions())
    steps = res.test_cases[0].steps
    navverify = _find(steps, "Verify the application navigated")[0]
    assert getattr(navverify, "provenance", "") == "demonstrated"


def test_group_navigation_elsewhere_does_not_greenwash_boundary():
    """Fix B(b) — the green-wash guard: the only navigating click reaches
    /category/hand-tools, but the NEXT milestone is /category/power-tools. No click
    grounded THAT boundary, so it must be UNPROVEN (inferred) — never hard-asserted
    off an unrelated click's navigation."""
    res = gen.generate_demonstrated_test_cases(
        artifact_id="t", page_visits=_menu_visits("/category/power-tools"),
        page_actions=_menu_actions())
    steps = res.test_cases[0].steps
    navverify = _find(steps, "Verify the application navigated")[0]
    assert getattr(navverify, "provenance", "") == "inferred", \
        "a click navigating ELSEWHERE must not green-wash this boundary"
    # and the noise click must never carry the (mis-attributed) next_url
    for s in _find(steps, "Unsplash"):
        assert (s.observed or {}).get("next_url") in (None, "")


def test_direct_navlink_transition_has_no_spurious_opener():
    """Fix B(a): a DIRECT nav-link transition (not menu-gated) is grounded WITHOUT
    prepending a disclosure opener that happened to be clicked earlier."""
    visits = _menu_visits("/contact")
    actions = [
        PA(page_visit_id="v1", subaction_index=0, verb="click",
           target_label="Categories", target_kind="button", value=None,
           after_outcome="none", after_detail="", navigated=False,
           control_role="button", css_hint="button.nav-link.dropdown-toggle",
           expanded="false"),
        PA(page_visit_id="v1", subaction_index=1, verb="click",
           target_label="Contact", target_kind="link", value=None,
           after_outcome="navigation", after_detail="https://shop.test/contact",
           navigated=True, control_role="link", css_hint="a.nav-link", expanded=""),
    ]
    res = gen.generate_demonstrated_test_cases(
        artifact_id="t", page_visits=visits, page_actions=actions)
    steps = res.test_cases[0].steps
    contact = _find(steps, "Click 'Contact'")
    assert contact, "the direct nav-link must be the grounded transition"
    assert not _find(steps, "Click 'Categories'"), \
        "no spurious menu-opener for a non-menu-gated nav-link"
    assert gen._path_of((contact[0].observed or {}).get("next_url", "")) == "/contact"
    navverify = _find(steps, "Verify the application navigated")[0]
    assert getattr(navverify, "provenance", "") == "demonstrated"


# ─── Fix B hardening: opener-heuristic robustness (adversarial-review findings) ──

def test_wordpress_menu_item_class_is_not_treated_as_menu_gated():
    """Finding #2: the bare 'menu-item' class (CMS top-nav, always visible) must NOT
    be treated as a collapsed menu item — no spurious opener prepended to a direct
    nav-link, even if an unrelated disclosure was opened just before it."""
    visits = _menu_visits("/deals")
    actions = [
        PA(page_visit_id="v1", subaction_index=0, verb="click", target_label="Cart",
           target_kind="button", value=None, after_outcome="none", after_detail="",
           navigated=False, control_role="button", css_hint="button.cart-toggle",
           expanded="true"),
        PA(page_visit_id="v1", subaction_index=1, verb="click", target_label="Deals",
           target_kind="link", value=None, after_outcome="navigation",
           after_detail="https://shop.test/deals", navigated=True,
           control_role="link", css_hint="li.menu-item a", expanded=""),
    ]
    res = gen.generate_demonstrated_test_cases(
        artifact_id="t", page_visits=visits, page_actions=actions)
    steps = res.test_cases[0].steps
    assert _find(steps, "Click 'Deals'"), "the direct nav-link is the transition"
    assert not _find(steps, "Click 'Cart'"), \
        "an unrelated CMS 'menu-item' link must not trigger a fabricated menu-open"


def test_interleaved_unrelated_opener_keeps_the_real_opener():
    """Finding #1: when an unrelated disclosure ('Search') is opened between the real
    opener ('Categories') and the grounded item, the REAL opener must not be dropped
    (old last-wins picked 'Search' and lost 'Categories' → replay RED)."""
    visits = _menu_visits("/category/hand-tools")
    actions = [
        PA(page_visit_id="v1", subaction_index=0, verb="click", target_label="Categories",
           target_kind="button", value=None, after_outcome="none", after_detail="",
           navigated=False, control_role="button",
           css_hint="button.nav-link.dropdown-toggle", expanded="false"),
        PA(page_visit_id="v1", subaction_index=1, verb="click", target_label="Search",
           target_kind="button", value=None, after_outcome="none", after_detail="",
           navigated=False, control_role="button", css_hint="button.search-toggle",
           expanded="false"),
        PA(page_visit_id="v1", subaction_index=2, verb="click", target_label="Hand Werkzeuge",
           target_kind="link", value=None, after_outcome="navigation",
           after_detail="https://shop.test/category/hand-tools", navigated=True,
           control_role="link", css_hint="a.dropdown-item", expanded=""),
    ]
    res = gen.generate_demonstrated_test_cases(
        artifact_id="t", page_visits=visits, page_actions=actions)
    steps = res.test_cases[0].steps
    cats = _find(steps, "Click 'Categories'")
    item = _find(steps, "Hand Werkzeuge")
    assert cats and item, "the REAL opener 'Categories' must survive, not be dropped"
    assert cats[0].step_number < item[0].step_number


def test_nested_menu_prepends_full_opener_chain():
    """Finding #3: a nested/mega-menu item gated by TWO toggles must reproduce BOTH,
    outermost first (single last-wins opener would leave the item hidden → RED)."""
    visits = _menu_visits("/category/drills")
    actions = [
        PA(page_visit_id="v1", subaction_index=0, verb="click", target_label="Categories",
           target_kind="button", value=None, after_outcome="none", after_detail="",
           navigated=False, control_role="button", css_hint="button.dropdown-toggle",
           expanded="false"),
        PA(page_visit_id="v1", subaction_index=1, verb="click", target_label="Power Tools",
           target_kind="button", value=None, after_outcome="none", after_detail="",
           navigated=False, control_role="menuitem",
           css_hint="button.dropdown-item.has-submenu", expanded="false"),
        PA(page_visit_id="v1", subaction_index=2, verb="click", target_label="Drills",
           target_kind="link", value=None, after_outcome="navigation",
           after_detail="https://shop.test/category/drills", navigated=True,
           control_role="menuitem", css_hint="a.dropdown-item", expanded=""),
    ]
    res = gen.generate_demonstrated_test_cases(
        artifact_id="t", page_visits=visits, page_actions=actions)
    steps = res.test_cases[0].steps
    cats = _find(steps, "Click 'Categories'")
    power = _find(steps, "Click 'Power Tools'")
    drills = _find(steps, "Click 'Drills'")
    assert cats and power and drills, "both openers + the item must be emitted"
    assert cats[0].step_number < power[0].step_number < drills[0].step_number, \
        "opener chain must be outermost → innermost → item"


def test_last_click_navigating_elsewhere_does_not_pin_next_url():
    """Green-wash guard (fallback path): the trailing click navigated to /promo, but
    the next milestone is /category/hand-tools and NO click reached it. The click must
    NOT get next_url pinned onto it, and the boundary must be UNPROVEN — never a hard
    toHaveURL for a page this click never reached."""
    visits = _menu_visits("/category/hand-tools")
    actions = [
        PA(page_visit_id="v1", subaction_index=0, verb="click", target_label="Promo",
           target_kind="link", value=None, after_outcome="navigation",
           after_detail="https://shop.test/promo", navigated=True,
           control_role="link", css_hint="a", expanded=""),
    ]
    res = gen.generate_demonstrated_test_cases(
        artifact_id="t", page_visits=visits, page_actions=actions)
    steps = res.test_cases[0].steps
    promo = _find(steps, "Click 'Promo'")[0]
    assert (promo.observed or {}).get("next_url") in (None, ""), \
        "a click that navigated ELSEWHERE must not be credited with reaching next_url"
    navverify = _find(steps, "Verify the application navigated")[0]
    assert getattr(navverify, "provenance", "") == "inferred"


# ─── Grounded-journey generator (short coherent per-click-path flows) ────────────
#
# The demonstrated E2E flattens BFS order and can only use a grounded click that
# sits at a forward transition boundary. generate_grounded_journeys builds one flow
# per grounded navigation DIRECTLY from the click — so a menu nav the crawler
# grounded on a REVISIT (home seq 7, terminal in the E2E) still becomes a runnable
# category flow: open home → open the menu → click the item → verify (PROVEN).

def _journey_visits():
    # home visited twice; the grounded menu nav lands on the REVISIT (seq 2), which
    # the flattened E2E cannot turn into a home→category transition.
    return [
        PV(page_visit_id="h1", sequence_index=0, location="Home",
           url_host="shop.test", url_path="/", url_query="",
           canonical_host="shop.test", source="ground_truth", form_snapshot={}),
        PV(page_visit_id="cat", sequence_index=1, location="Hand Tools",
           url_host="shop.test", url_path="/category/hand-tools", url_query="",
           canonical_host="shop.test", source="ground_truth", form_snapshot={}),
        PV(page_visit_id="h2", sequence_index=2, location="Home",
           url_host="shop.test", url_path="/", url_query="",
           canonical_host="shop.test", source="ground_truth", form_snapshot={}),
    ]


def _journey_actions():
    return [
        # on the home REVISIT (h2): open Categories, click the hidden Hand Werkzeuge
        PA(page_visit_id="h2", subaction_index=0, verb="click", target_label="Categories",
           target_kind="button", value=None, after_outcome="none", after_detail="",
           navigated=False, control_role="button",
           css_hint="button.nav-link.dropdown-toggle", expanded="false"),
        PA(page_visit_id="h2", subaction_index=1, verb="click", target_label="Hand Werkzeuge",
           target_kind="link", value=None, after_outcome="navigation",
           after_detail="https://shop.test/category/hand-tools", navigated=True,
           control_role="link", css_hint="a.dropdown-item", expanded=""),
        # an external footer link that navigated OFF-app — must NOT become a journey
        PA(page_visit_id="h1", subaction_index=0, verb="click", target_label="Unsplash",
           target_kind="link", value=None, after_outcome="navigation",
           after_detail="https://unsplash.com/photos/x", navigated=True,
           control_role="link", css_hint="a", expanded=""),
    ]


def test_grounded_journey_built_from_revisit_menu_click():
    """The menu nav grounded on the home REVISIT becomes a coherent PROVEN journey:
    Open home → Click 'Categories' → Click 'Hand Werkzeuge' → Verify hand-tools."""
    journeys = gen.generate_grounded_journeys(
        artifact_id="t", page_visits=_journey_visits(), page_actions=_journey_actions())
    hand = [c for c in journeys
            if "hand-tools" in (c.steps[-1].action or "").lower()]
    assert hand, "a journey to /category/hand-tools must be emitted"
    steps = hand[0].steps
    acts = [s.action for s in steps]
    assert acts[0].startswith("Open https://shop.test/"), acts
    assert "Click 'Categories'" in acts[1], acts
    assert "Click 'Hand Werkzeuge'" in acts[2], acts
    nav = steps[2]
    assert gen._path_of((nav.observed or {}).get("next_url", "")) == "/category/hand-tools"
    assert (nav.observed or {}).get("navigation_grounded") is True
    verify = steps[3]
    assert getattr(verify, "provenance", "") == "demonstrated"


def test_grounded_journey_skips_external_navigation():
    """A click that navigated OFF the app (unsplash.com) is not an in-app journey."""
    journeys = gen.generate_grounded_journeys(
        artifact_id="t", page_visits=_journey_visits(), page_actions=_journey_actions())
    assert not any("unsplash" in (c.description or "").lower() for c in journeys)
    assert not any("Unsplash" in s.action
                   for c in journeys for s in c.steps)


def test_grounded_journey_on_multilabel_host_is_same_app_not_cross_host():
    """Live regression (VKPower Life on vkpowerlife.35-186-147-245.sslip.io): the
    visit's ``canonical_host`` is the registrable-domain REDUCTION (``sslip.io``),
    while every navigation click's captured destination carries the FULL hostname.
    Comparing the reduction to the full host mis-killed all 9 same-app navigations
    as "cross-host" → an 11-page crawl emitted ZERO journeys. The real page host
    (``url_host``) must anchor both the same-app check and the opening goto."""
    full = "vkpowerlife.35-186-147-245.sslip.io"
    visits = [
        PV(page_visit_id="q", sequence_index=0, location="Get a quote",
           url_host=full, url_path="/quote", url_query="plan=term",
           canonical_host="sslip.io", source="ground_truth", form_snapshot={}),
        PV(page_visit_id="p", sequence_index=1, location="Products",
           url_host=full, url_path="/products", url_query="",
           canonical_host="sslip.io", source="ground_truth", form_snapshot={}),
    ]
    actions = [
        PA(page_visit_id="q", subaction_index=0, verb="click", target_label="Products",
           target_kind="link", value=None, after_outcome="navigation",
           after_detail=f"https://{full}/products", navigated=True,
           control_role="link", css_hint="a.nav-link", expanded=""),
        # a genuinely external link must STILL be excluded (the check keeps teeth)
        PA(page_visit_id="q", subaction_index=1, verb="click", target_label="Twitter",
           target_kind="link", value=None, after_outcome="navigation",
           after_detail="https://twitter.com/vkpower", navigated=True,
           control_role="link", css_hint="a", expanded=""),
    ]
    journeys = gen.generate_grounded_journeys(
        artifact_id="t", page_visits=visits, page_actions=actions)
    prods = [c for c in journeys
             if any("/products" in (s.action or "") for s in c.steps)]
    assert prods, "the same-app navigation on a multi-label host must emit a journey"
    open_step = prods[0].steps[0]
    assert open_step.action == f"Open https://{full}/quote", open_step.action
    assert not any("twitter" in (s.action or "").lower()
                   for c in journeys for s in c.steps), "external nav must stay excluded"


def test_grounded_journey_dedups_and_requires_grounded_nav():
    """No journey for a non-navigating click; identical (src,dest,label) collapses."""
    visits = _journey_visits()
    actions = [
        PA(page_visit_id="h1", subaction_index=0, verb="click", target_label="Deals",
           target_kind="link", value=None, after_outcome="none", after_detail="",
           navigated=False, control_role="link", css_hint="a.nav-link", expanded=""),
    ]
    journeys = gen.generate_grounded_journeys(
        artifact_id="t", page_visits=visits, page_actions=actions)
    assert journeys == [], "a non-navigating click must not produce a journey"


def _quote_form_visits():
    full = "vkpowerlife.35-186-147-245.sslip.io"
    return [
        PV(page_visit_id="q", sequence_index=0, location="Get a quote",
           url_host=full, url_path="/quote", url_query="plan=term",
           canonical_host="sslip.io", source="ground_truth", form_snapshot={}),
        PV(page_visit_id="r", sequence_index=1, location="Quote result",
           url_host=full, url_path="/quote",
           url_query="submitted=1&plan=term&age=18", canonical_host="sslip.io",
           source="ground_truth", form_snapshot={}),
    ]


def _quote_form_actions(*, submit_outcome="navigation", submit_navigated=True,
                        submit_detail="https://vkpowerlife.35-186-147-245.sslip.io/quote?submitted=1&plan=term&age=18",
                        submit_visit="r"):
    """Live-verified shape: the explorer records the submit ACTION on the
    DESTINATION visit ('r'); the fills live on the form visit ('q')."""
    return [
        PA(page_visit_id="q", subaction_index=0, verb="select", target_label="Product",
           target_kind="dropdown", value="term", after_outcome="value_committed",
           after_detail="", navigated=False, control_role="combobox", css_hint="", expanded=""),
        PA(page_visit_id="q", subaction_index=1, verb="type", target_label="Age",
           target_kind="text_field", value="18", after_outcome="value_committed",
           after_detail="", navigated=False, control_role="spinbutton", css_hint="", expanded=""),
        # a valueless fill artifact must never become a replay step
        PA(page_visit_id="q", subaction_index=2, verb="type", target_label="Nickname",
           target_kind="text_field", value=None, after_outcome="none",
           after_detail="", navigated=False, control_role="textbox", css_hint="", expanded=""),
        PA(page_visit_id=submit_visit, subaction_index=0, verb="submit",
           target_label="Calculate my premium", target_kind="button", value=None,
           after_outcome=submit_outcome, after_detail=submit_detail,
           navigated=submit_navigated, control_role="button", css_hint="", expanded=""),
    ]


def test_grounded_form_flow_from_demonstrated_submit():
    """The live VKPower quote incident: fills committed on the FORM visit +
    operator-approved Phase-B submit recorded on the DESTINATION visit, PROVEN to
    navigate to /quote?submitted=1&… — must emit ONE P0 form-flow case: open (with
    the entry query) → replay fills → click submit (hard URL oracle) → verify the
    FULL demonstrated destination."""
    flows = gen.generate_form_flow_journeys(
        artifact_id="t", page_visits=_quote_form_visits(),
        page_actions=_quote_form_actions())
    assert len(flows) == 1, "exactly one form flow for one demonstrated submit"
    case = flows[0]
    acts = [s.action for s in case.steps]
    assert acts[0] == "Open https://vkpowerlife.35-186-147-245.sslip.io/quote?plan=term", acts
    assert "Select 'term' in 'Product'" in acts[1], acts
    assert "Enter '18' in 'Age'" in acts[2], acts
    assert not any("Nickname" in a for a in acts), "valueless fill must not be replayed"
    assert "Click 'Calculate my premium'" in acts[3], acts
    sub = case.steps[3]
    assert (sub.observed or {}).get("navigation_grounded") is True
    assert "submitted=1" in (sub.observed or {}).get("next_url", "")
    verify = case.steps[4]
    assert verify.action.startswith("Verify the application navigated to https://")
    assert "submitted=1" in verify.action
    assert getattr(verify, "provenance", "") == "demonstrated"
    assert case.priority == "P0_critical"
    assert "grounded-form-flow" in case.tags


def test_form_flow_requires_proven_submit_navigation():
    """No navigation proof (outcome=none — the min-blocked submit) → NO case.
    A form flow is never fabricated from an unproven submit (never green-wash)."""
    flows = gen.generate_form_flow_journeys(
        artifact_id="t", page_visits=_quote_form_visits(),
        page_actions=_quote_form_actions(
            submit_outcome="none", submit_navigated=False, submit_detail=""))
    assert flows == []


def test_form_flow_skips_external_and_selfsame_destinations():
    """A submit landing byte-identically on the source URL proves nothing moved;
    a cross-host destination left the app — neither becomes a flow."""
    flows_self = gen.generate_form_flow_journeys(
        artifact_id="t", page_visits=_quote_form_visits(),
        page_actions=_quote_form_actions(
            submit_detail="https://vkpowerlife.35-186-147-245.sslip.io/quote?plan=term"))
    assert flows_self == []
    flows_ext = gen.generate_form_flow_journeys(
        artifact_id="t", page_visits=_quote_form_visits(),
        page_actions=_quote_form_actions(
            submit_detail="https://evil.example.com/quote?submitted=1"))
    assert flows_ext == []


def test_form_flow_same_visit_shape_also_emits():
    """A recorder that attaches the submit to the FORM visit itself (fills and
    submit on one visit) must yield the same single flow — both shapes covered."""
    flows = gen.generate_form_flow_journeys(
        artifact_id="t", page_visits=_quote_form_visits(),
        page_actions=_quote_form_actions(submit_visit="q"))
    assert len(flows) == 1
    acts = [s.action for s in flows[0].steps]
    assert acts[0].startswith("Open https://"), acts
    assert any("Calculate my premium" in a for a in acts)


def test_incoherent_flatten_is_suppressed_with_honest_reason():
    """A long flow the crawler reached by LINK-FOLLOWING (no proven click between any
    two pages) is the BFS traversal flattened — it would wander home→cart→login→…,
    fill a signup form it landed on, and confuse. It must be SUPPRESSED (not shipped)
    and say WHY, rather than presented as a runnable 'Functional E2E'."""
    paths = ["/", "/products", "/cart", "/login", "/contact"]
    visits = [
        PV(page_visit_id=f"v{i}", sequence_index=i, location=f"Page{i}",
           url_host="shop.test", url_path=p, url_query="",
           canonical_host="shop.test", source="ground_truth", form_snapshot={})
        for i, p in enumerate(paths)
    ]
    # every click stays on its page (nothing navigated) → every transition is UNPROVEN
    actions = [
        PA(page_visit_id=f"v{i}", subaction_index=0, verb="click",
           target_label=f"Link{i}", target_kind="link", value=None,
           after_outcome="none", after_detail="", navigated=False)
        for i in range(len(paths))
    ]
    res = gen.generate_demonstrated_test_cases(
        artifact_id="t", page_visits=visits, page_actions=actions)
    assert res.test_cases == [], "an all-ungrounded BFS flatten must not ship as a test"
    reason = (res.no_flow_reason or "").lower()
    assert "coherent" in reason or "wander" in reason or "link" in reason, res.no_flow_reason


def test_short_flow_with_one_gap_is_still_emitted():
    """Guard the suppression threshold: a SHORT flow with a single un-captured
    transition is coherent and must still be emitted (not swept up as a 'flatten')."""
    visits = _menu_visits("/category/hand-tools")  # 2 real milestones
    actions = [
        PA(page_visit_id="v1", subaction_index=0, verb="click", target_label="Proceed",
           target_kind="link", value=None, after_outcome="none", after_detail="",
           navigated=False, control_role="link", css_hint="a"),
    ]
    res = gen.generate_demonstrated_test_cases(
        artifact_id="t", page_visits=visits, page_actions=actions)
    assert res.test_cases, "a 2-milestone flow with one gap must still be emitted"


if __name__ == "__main__":  # allow running without pytest
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS  {fn.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL  {fn.__name__}: {e}")
    print("ALL PASS" if not failed else f"{failed} FAILED")
    sys.exit(1 if failed else 0)
