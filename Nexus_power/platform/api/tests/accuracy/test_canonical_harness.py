"""Self-test for the Canonical Trust Baseline harness (Milestone 1A).

Proves the five metric families compute correctly on a known fixture, and that the
harness CATCHES the three real bugs we verified this session — fabrication, the
dropped login (value-survival), and a fabricated page-graph edge — while a perfect
extraction scores clean (no false positives). Runnable under pytest OR standalone:
    python platform/api/tests/accuracy/test_canonical_harness.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

from harness import Action, CanonicalDoc, Edge, PageNode, aggregate, score  # noqa: E402


def _label() -> CanonicalDoc:
    """Hand-verified truth for a saucedemo-style login -> inventory -> cart flow."""
    return CanonicalDoc(
        page_nodes=[
            PageNode(page_key="/login", visible_in_video=True),
            PageNode(page_key="/inventory.html", visible_in_video=True),
            PageNode(page_key="/cart.html", visible_in_video=True),
        ],
        actions=[
            Action(verb="type", target_label="Username", value="visual_user",
                   page_key="/login", visible_in_video=True),
            Action(verb="type", target_label="Password", value="secret_sauce",
                   page_key="/login", visible_in_video=True),
            Action(verb="click", target_label="Login", page_key="/login", visible_in_video=True),
            Action(verb="click", target_label="Add to cart", page_key="/inventory.html",
                   visible_in_video=True),
            Action(verb="click", target_label="Shopping cart", page_key="/inventory.html",
                   visible_in_video=True),
        ],
        edges=[Edge("/login", "/inventory.html"), Edge("/inventory.html", "/cart.html")],
    )


def _flawed_extraction() -> CanonicalDoc:
    """What today's pipeline produced: dropped the login page+values, fabricated a
    high-confidence 'navigate to checkout', and a fabricated /inventory->/checkout edge."""
    return CanonicalDoc(
        page_nodes=[
            PageNode(page_key="https://www.saucedemo.com/inventory.html", confidence=1.0,
                     source="url_regex"),
            PageNode(page_key="/cart.html", confidence=0.7, source="ocr"),
        ],
        actions=[
            Action(verb="click", target_label="Add to cart", page_key="/inventory.html",
                   confidence=0.9, automation_ready=True, source="ocr"),
            Action(verb="click", target_label="Shopping cart", page_key="/inventory.html",
                   confidence=0.8, automation_ready=True, source="ocr"),
            Action(verb="navigate", target_label="checkout", page_key="/inventory.html",
                   confidence=0.85, automation_ready=True, source="llm"),  # FABRICATED
        ],
        edges=[Edge("/inventory.html", "/cart.html"),
               Edge("/inventory.html", "/checkout.html")],  # 2nd is fabricated
    )


def test_harness_catches_the_three_real_bugs():
    sc = score(_flawed_extraction(), _label())

    # 1. FAITHFULNESS — the fabricated high-confidence 'navigate checkout' is caught.
    fab = sc["faithfulness"]
    assert fab["fabricated_count"] == 1, fab
    assert fab["rate"] > 0.0, fab

    # 2. VALUE-SURVIVAL — the dropped login values are caught.
    vs = sc["value_survival"]
    assert vs["value_recall"] == 0.0, vs
    assert any("visual_user" in d for d in vs["dropped"]), vs

    # 3. PAGE-GRAPH — the fabricated edge is caught.
    pg = sc["page_graph"]
    assert any("checkout" in e for e in pg["fabricated_edges"]), pg
    assert pg["graph_edit_distance"] >= 2, pg

    # SILENT DROPS — the dropped login actions are SILENT (no placeholder emitted).
    sd = sc["completeness"]["silent_drops"]
    assert sd["silent_drops"] >= 3, sd  # Username, Password, Login all dropped silently

    # CALIBRATION — the overconfident-wrong fabrication shows up.
    cal = sc["calibration"]
    assert cal["overconfident_wrong_mass"] > 0.0, cal


def test_perfect_extraction_scores_clean():
    """An extraction identical to the label must score clean — no false positives."""
    label = _label()
    # Build a faithful extraction: same rows, high confidence, real values present.
    ext = CanonicalDoc(
        page_nodes=[PageNode(page_key=n.page_key, confidence=1.0, source="ground_truth")
                    for n in label.page_nodes],
        actions=[Action(verb=a.verb, target_label=a.target_label, value=a.value,
                        page_key=a.page_key, confidence=0.95, automation_ready=True,
                        source="ground_truth") for a in label.actions],
        edges=[Edge(e.from_key, e.to_key) for e in label.edges],
    )
    sc = score(ext, label)
    assert sc["faithfulness"]["fabricated_count"] == 0, sc["faithfulness"]
    assert sc["value_survival"]["value_recall"] == 1.0, sc["value_survival"]
    assert sc["completeness"]["silent_drops"]["silent_drops"] == 0, sc["completeness"]
    assert sc["completeness"]["actions"]["f1"] == 1.0, sc["completeness"]["actions"]
    assert sc["page_graph"]["graph_edit_distance"] == 0, sc["page_graph"]


def test_honest_placeholder_is_not_a_silent_drop():
    """A miss the pipeline FLAGGED with a placeholder is honest, not a silent drop."""
    label = _label()
    ext = CanonicalDoc(
        page_nodes=[PageNode(page_key="/inventory.html", confidence=1.0)],
        actions=[
            Action(verb="click", target_label="Add to cart", page_key="/inventory.html",
                   confidence=0.9, automation_ready=True),
            Action(verb="click", target_label="Shopping cart", page_key="/inventory.html",
                   confidence=0.8, automation_ready=True),
            # The dropped login is FLAGGED, not silent:
            Action(verb="type", target_label="Username", placeholder="MISSING_VALUE",
                   page_key="/login"),
            Action(verb="type", target_label="Password", placeholder="MISSING_VALUE",
                   page_key="/login"),
            Action(verb="click", target_label="Login", placeholder="UNCERTAIN_ACTION",
                   page_key="/login"),
        ],
        edges=[Edge("/login", "/inventory.html"), Edge("/inventory.html", "/cart.html")],
    )
    sc = score(ext, label)
    sd = sc["completeness"]["silent_drops"]
    assert sd["placeholders_emitted"] >= 3, sd
    assert sd["silent_drops"] == 0, sd  # flagged misses are honest, not silent


def test_h1_unrelated_page_placeholders_do_not_launder_dropped_actions():
    """Review finding H1: 3 silently-dropped login ACTIONS must NOT be masked by 3
    unrelated MISSING_PAGE placeholders. The honesty metric must still report 3."""
    label = _label()  # has 3 visible /login actions (Username, Password, Login)
    ext = CanonicalDoc(
        page_nodes=[
            PageNode(page_key="/inventory.html", confidence=1.0),
            # 3 unrelated MISSING_PAGE placeholders (NOT covering the login actions):
            PageNode(page_key="/about", placeholder="MISSING_PAGE"),
            PageNode(page_key="/help", placeholder="MISSING_PAGE"),
            PageNode(page_key="/terms", placeholder="MISSING_PAGE"),
        ],
        actions=[  # the 3 login actions are silently dropped; only inventory clicks kept
            Action(verb="click", target_label="Add to cart", page_key="/inventory.html",
                   confidence=0.9, automation_ready=True),
            Action(verb="click", target_label="Shopping cart", page_key="/inventory.html",
                   confidence=0.8, automation_ready=True),
        ],
        edges=[Edge("/login", "/inventory.html"), Edge("/inventory.html", "/cart.html")],
    )
    sd = score(ext, label)["completeness"]["silent_drops"]
    assert sd["silent_drops"] == 3, sd  # NOT laundered to 0 by unrelated page placeholders


def test_h1_residual_wrong_control_placeholder_does_not_launder():
    """Review residual: a same-page placeholder for a DIFFERENT control must NOT launder
    a silently-dropped control. 3 login actions dropped + 1 placeholder for 'Captcha'
    (a different control) on the same page → still 3 silent, not 2."""
    label = _label()  # 3 visible /login actions: Username, Password, Login
    ext = CanonicalDoc(
        page_nodes=[PageNode(page_key="/inventory.html", confidence=1.0)],
        actions=[
            Action(verb="click", target_label="Add to cart", page_key="/inventory.html",
                   confidence=0.9, automation_ready=True),
            Action(verb="click", target_label="Shopping cart", page_key="/inventory.html",
                   confidence=0.8, automation_ready=True),
            # A placeholder for an UNRELATED control on the same page — must not cover
            # the dropped Username/Password/Login:
            Action(verb="type", target_label="Captcha", placeholder="MISSING_VALUE",
                   page_key="/login"),
        ],
        edges=[Edge("/login", "/inventory.html"), Edge("/inventory.html", "/cart.html")],
    )
    sd = score(ext, label)["completeness"]["silent_drops"]
    assert sd["silent_drops"] == 3, sd  # the 'Captcha' placeholder launders nothing


def test_underscore_value_survives_space_variant():
    """A benign 'visual_user' vs 'visual user' variance must NOT be a false drop."""
    label = CanonicalDoc(actions=[
        Action(verb="type", target_label="Username", value="visual_user",
               page_key="/login", visible_in_video=True)])
    ext = CanonicalDoc(actions=[
        Action(verb="type", target_label="Username", value="visual user",
               page_key="/login", confidence=0.9)])
    assert score(ext, label)["value_survival"]["value_recall"] == 1.0


def test_h2_substring_does_not_count_as_value_survival():
    """Review finding H2: a dropped value must not 'survive' via an unrelated substring."""
    label = CanonicalDoc(
        page_nodes=[PageNode(page_key="/login", visible_in_video=True)],
        actions=[
            Action(verb="type", target_label="User", value="admin",
                   page_key="/login", visible_in_video=True),
            Action(verb="type", target_label="Code", value="1",
                   page_key="/login", visible_in_video=True),
        ],
        edges=[],
    )
    ext = CanonicalDoc(
        page_nodes=[PageNode(page_key="/login", confidence=1.0)],
        actions=[
            # unrelated values that merely CONTAIN the dropped ones as substrings:
            Action(verb="type", target_label="Org", value="administrator district",
                   page_key="/login", confidence=0.9),
            Action(verb="type", target_label="Count", value="100",
                   page_key="/login", confidence=0.9),
        ],
        edges=[],
    )
    vs = score(ext, label)["value_survival"]
    assert vs["value_recall"] == 0.0, vs            # both genuinely dropped
    assert any("admin" in d for d in vs["dropped"]), vs
    # And an EXACT / full-token value still correctly survives:
    ext2 = CanonicalDoc(actions=[Action(verb="type", target_label="User", value="admin",
                                         page_key="/login", confidence=0.9)])
    assert score(ext2, label)["value_survival"]["survived"] == 1


def test_aggregate_rolls_up():
    a = score(_flawed_extraction(), _label())
    b = score(_flawed_extraction(), _label())
    agg = aggregate([a, b])
    assert agg["n_videos"] == 2
    assert agg["total_silent_drops"] >= 6
    assert agg["value_recall"] == 0.0


if __name__ == "__main__":
    import json
    print("=== FLAWED extraction scorecard ===")
    print(json.dumps(score(_flawed_extraction(), _label()), indent=2))
    for fn in [test_harness_catches_the_three_real_bugs, test_perfect_extraction_scores_clean,
               test_honest_placeholder_is_not_a_silent_drop, test_aggregate_rolls_up]:
        fn()
        print(f"PASS  {fn.__name__}")
    print("\nALL HARNESS SELF-TESTS PASSED")
