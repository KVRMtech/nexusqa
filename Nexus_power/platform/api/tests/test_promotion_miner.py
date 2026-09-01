"""R6 — promote a recurring cross-app heal into a permanent-capability candidate.

Doctrine: only PROVEN (confirmed, non-quarantined) heals count; breadth is by
DISTINCT app (ten scenarios in one app is not a cross-client pattern);
promotable kinds only; every candidate is a human-gated proposal.
"""
from app.services.agentic.promotion_miner import (
    DEFAULT_MIN_APPS,
    mine_promotions,
    mine_to_dicts,
)


def _entry(app, fix_kind="interaction", recipe="open_then_click", confirmed=1,
           invalidated=False, label="Product"):
    return {"app_key": app, "fix_kind": fix_kind, "confirmed_count": confirmed,
            "invalidated_at": ("2026-01-01T00:00:00" if invalidated else None),
            "label": label, "payload": {"recipe": recipe}}


def test_recurring_cross_app_heal_becomes_a_candidate():
    entries = [_entry("app-1"), _entry("app-2"), _entry("app-3")]
    cands = mine_promotions(entries, min_apps=3)
    assert len(cands) == 1
    c = cands[0]
    assert c.fix_kind == "interaction" and c.strategy_key == "open_then_click"
    assert c.app_count == 3 and c.total_confirmations == 3


def test_breadth_is_distinct_apps_not_scenarios():
    # same app, many confirmations — NOT a cross-client pattern
    entries = [_entry("app-1", confirmed=9), _entry("app-1", confirmed=9)]
    assert mine_promotions(entries, min_apps=3) == []


def test_quarantined_and_unconfirmed_are_excluded():
    entries = [
        _entry("app-1", invalidated=True), _entry("app-2", invalidated=True),
        _entry("app-3", confirmed=0), _entry("app-4"),
    ]
    # only app-4 is a durable signal -> below min_apps -> no candidate
    assert mine_promotions(entries, min_apps=3) == []


def test_non_promotable_kinds_are_ignored():
    entries = [_entry(f"app-{i}", fix_kind="nav") for i in range(5)]
    assert mine_promotions(entries, min_apps=3) == []


def test_ranked_by_breadth_then_confirmations():
    entries = (
        [_entry(f"a{i}", recipe="wideA") for i in range(5)] +
        [_entry(f"b{i}", recipe="narrowB") for i in range(3)]
    )
    cands = mine_promotions(entries, min_apps=3)
    assert [c.strategy_key for c in cands] == ["wideA", "narrowB"]  # breadth desc


def test_proposal_shape_is_human_gated():
    p = mine_to_dicts([_entry(f"a{i}") for i in range(3)], min_apps=3)[0]
    assert p["kind"] == "capability_promotion_proposal"
    assert p["status"] == "proposed"
    assert "never self-modifies" in p["apply_requires"]
    assert p["app_count"] == 3
    assert "distinct_apps" in p["evidence"]


def test_default_min_apps_is_three():
    assert DEFAULT_MIN_APPS == 3
    two = [_entry("a1"), _entry("a2")]
    assert mine_promotions(two) == []                 # 2 < default 3
    assert len(mine_promotions(two, min_apps=2)) == 1  # explicit lower bar
