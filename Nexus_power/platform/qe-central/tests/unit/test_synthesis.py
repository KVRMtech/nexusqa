"""QE-Central S4 — journey synthesis unit tests (pure, no DB).

Fingerprint goldens (design Phase-3 exit criteria):
  * value / locator / capture-ORDER changes do NOT move the fingerprint;
  * page / field / verb / target changes DO move it;
  * an identical re-crawl ⇒ 100% unchanged, ZERO approval-queue entries;
  * the ``_split_revisit_branch`` idea (generator.py:712-729) yields a trunk +
    a demonstrated side-exploration branch;
  * per-terminal-form flows are emitted for intermediate submits;
  * a vanished scenario is flagged ``missing`` (shrinkage), never dropped.
"""
from __future__ import annotations

import re

from app.services import synthesis as S
from app.services.synthesis import (
    DIFF_CHANGED,
    DIFF_MISSING,
    DIFF_NEW,
    DIFF_UNCHANGED,
    KIND_REVISIT_BRANCH,
    KIND_TERMINAL_FORM,
    KIND_TRUNK,
    PageNode,
    build_journeys,
    compute_diff,
)

APP = "app-golden"

# Canonical 3-page linear crawl: login → transfer → confirm (submit at end).
GOLDEN_NODES = [
    PageNode(1, "acme.example", "/login", ("type", "click"),
             ("Username", "Password"), ("Log in",), False),
    PageNode(2, "acme.example", "/transfer", ("type", "select"),
             ("Amount", "To account"), ("Continue",), False),
    PageNode(3, "acme.example", "/confirm", ("click", "submit"),
             (), ("Confirm",), True),
]

# Golden hashes (regression guard against accidental normalisation drift).
GOLDEN_FP = "b232e3d834b98cb9fe8dc87d7f7124c7101f3dda02ce265cf778a3a96d757a15"
GOLDEN_SID = "8abca80e-abf9-5f15-8eb1-765dfd6c6d5b"

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-5[0-9a-f]{3}-[0-9a-f]{4}-[0-9a-f]{12}$")


def _trunk(nodes):
    js = build_journeys(nodes)
    assert js and js[0].kind == KIND_TRUNK
    return js[0]


# ── Golden identity + shape ───────────────────────────────────────────────

def test_golden_fingerprint_and_scenario_id_are_pinned() -> None:
    j = _trunk(GOLDEN_NODES)
    assert j.fingerprint() == GOLDEN_FP
    assert j.scenario_id(APP) == GOLDEN_SID
    assert _HEX64.match(j.fingerprint())
    assert _UUID.match(j.scenario_id(APP))
    assert j.name() == "login -> transfer -> confirm"


def test_linear_crawl_yields_single_trunk_journey() -> None:
    js = build_journeys(GOLDEN_NODES)
    assert [x.kind for x in js] == [KIND_TRUNK]  # submit only at the last page


def test_determinism_across_rebuilds() -> None:
    a = _trunk(GOLDEN_NODES)
    b = _trunk(GOLDEN_NODES)
    assert a.fingerprint() == b.fingerprint()
    assert a.scenario_id(APP) == b.scenario_id(APP)


# ── Fingerprint invariance / sensitivity ──────────────────────────────────

def test_capture_order_and_case_do_not_move_the_fingerprint() -> None:
    """Order/case are the observable proxy for value/locator churn — the
    skeleton captures neither values nor locators, so a re-crawl that differs
    only in capture order or casing is byte-identical."""
    reordered = [
        PageNode(1, "ACME.example", "/login", ("click", "type"),
                 ("password", "USERNAME"), ("log IN",), False),
        PageNode(2, "acme.example", "/transfer", ("select", "type"),
                 ("to account", "amount"), ("Continue",), False),
        PageNode(3, "acme.example", "/confirm", ("submit", "click"),
                 (), ("confirm",), True),
    ]
    assert _trunk(reordered).fingerprint() == GOLDEN_FP


def test_field_change_moves_fingerprint_but_keeps_scenario_id() -> None:
    changed = list(GOLDEN_NODES)
    changed[1] = PageNode(2, "acme.example", "/transfer", ("type", "select"),
                          ("Amount", "To account", "Memo"), ("Continue",), False)
    j = _trunk(changed)
    assert j.scenario_id(APP) == GOLDEN_SID          # same route identity
    assert j.fingerprint() != GOLDEN_FP              # but the detail moved


def test_verb_change_moves_fingerprint_but_keeps_scenario_id() -> None:
    changed = list(GOLDEN_NODES)
    changed[1] = PageNode(2, "acme.example", "/transfer", ("type", "hover"),
                          ("Amount", "To account"), ("Continue",), False)
    j = _trunk(changed)
    assert j.scenario_id(APP) == GOLDEN_SID
    assert j.fingerprint() != GOLDEN_FP


def test_target_change_moves_fingerprint() -> None:
    changed = list(GOLDEN_NODES)
    changed[2] = PageNode(3, "acme.example", "/confirm", ("click", "submit"),
                          (), ("Submit payment",), True)
    assert _trunk(changed).fingerprint() != GOLDEN_FP


def test_page_change_creates_a_new_scenario_id() -> None:
    changed = list(GOLDEN_NODES)
    changed[1] = PageNode(2, "acme.example", "/payments", ("type", "select"),
                          ("Amount", "To account"), ("Continue",), False)
    j = _trunk(changed)
    assert j.scenario_id(APP) != GOLDEN_SID


# ── Diff semantics ────────────────────────────────────────────────────────

def test_identical_recrawl_is_all_unchanged_zero_queue() -> None:
    fresh = build_journeys(GOLDEN_NODES)
    stored = {j.scenario_id(APP): j.fingerprint() for j in fresh}
    result = compute_diff(APP, build_journeys(GOLDEN_NODES), stored)
    assert result.counts[DIFF_UNCHANGED] == result.counts["total"]
    assert result.counts[DIFF_NEW] == 0
    assert result.counts[DIFF_CHANGED] == 0
    assert result.queue == []


def test_first_crawl_is_all_new_and_queued() -> None:
    result = compute_diff(APP, build_journeys(GOLDEN_NODES), {})
    assert result.counts[DIFF_NEW] == result.counts["total"]
    assert set(result.queue) == {s.scenario_id for s in result.scenarios}


def test_field_change_recrawl_is_changed_and_queued() -> None:
    fresh = build_journeys(GOLDEN_NODES)
    stored = {j.scenario_id(APP): j.fingerprint() for j in fresh}
    changed = list(GOLDEN_NODES)
    changed[1] = PageNode(2, "acme.example", "/transfer", ("type", "select"),
                          ("Amount", "To account", "Memo"), ("Continue",), False)
    result = compute_diff(APP, build_journeys(changed), stored)
    assert result.counts[DIFF_CHANGED] == 1
    assert result.queue == [GOLDEN_SID]


def test_vanished_scenario_is_flagged_missing_not_dropped() -> None:
    fresh = build_journeys(GOLDEN_NODES)
    stored = {j.scenario_id(APP): j.fingerprint() for j in fresh}
    stored["ghost-scenario-id"] = "0" * 64
    result = compute_diff(APP, build_journeys(GOLDEN_NODES), stored)
    assert result.counts[DIFF_MISSING] == 1
    missing = [s for s in result.scenarios if s.diff_state == DIFF_MISSING]
    assert len(missing) == 1
    assert missing[0].scenario_id == "ghost-scenario-id"
    # A missing scenario never lands in the approval queue.
    assert "ghost-scenario-id" not in result.queue


# ── Trunk / revisit-branch split (generator.py:712-729 idea) ──────────────

def test_revisit_branch_split() -> None:
    nodes = [
        PageNode(1, "h", "/home", ("click",), (), ("Menu",)),
        PageNode(2, "h", "/a", ("click",), (), ("A",)),
        PageNode(3, "h", "/b", ("click",), (), ("B",)),
        PageNode(4, "h", "/home", ("click",), (), ("Menu",)),
        PageNode(5, "h", "/d", ("click",), (), ("D",)),
    ]
    js = build_journeys(nodes)
    kinds = [j.kind for j in js]
    assert KIND_TRUNK in kinds and KIND_REVISIT_BRANCH in kinds
    trunk = next(j for j in js if j.kind == KIND_TRUNK)
    branch = next(j for j in js if j.kind == KIND_REVISIT_BRANCH)
    assert [p["path"] for p in trunk.pages] == ["/home", "/d"]
    assert [p["path"] for p in branch.pages] == ["/home", "/a", "/b", "/home"]
    # Trunk and branch are distinct scenarios.
    assert trunk.scenario_id(APP) != branch.scenario_id(APP)


def test_no_revisit_yields_only_trunk() -> None:
    nodes = [
        PageNode(1, "h", "/a", ("click",), (), ("A",)),
        PageNode(2, "h", "/b", ("click",), (), ("B",)),
    ]
    assert [j.kind for j in build_journeys(nodes)] == [KIND_TRUNK]


# ── Per-terminal-form flows ───────────────────────────────────────────────

def test_terminal_form_journey_for_intermediate_submit() -> None:
    nodes = [
        PageNode(1, "h", "/s1", ("type",), ("A",), ("Next",), False),
        PageNode(2, "h", "/s2", ("submit",), ("B",), ("Save",), True),   # intermediate submit
        PageNode(3, "h", "/s3", ("submit",), (), ("Done",), True),       # final submit
    ]
    js = build_journeys(nodes)
    kinds = [j.kind for j in js]
    assert KIND_TRUNK in kinds and KIND_TERMINAL_FORM in kinds
    tf = next(j for j in js if j.kind == KIND_TERMINAL_FORM)
    assert [p["path"] for p in tf.pages] == ["/s1", "/s2"]


def test_terminal_form_prefix_equal_to_trunk_is_deduped_in_diff() -> None:
    """A terminal-form prefix that equals the trunk collapses to one scenario."""
    nodes = [
        PageNode(1, "h", "/s1", ("type",), ("A",), ("Next",), False),
        PageNode(2, "h", "/s2", ("submit",), ("B",), ("Save",), True),
    ]
    js = build_journeys(nodes)
    result = compute_diff(APP, js, {})
    ids = [s.scenario_id for s in result.scenarios]
    assert len(ids) == len(set(ids))  # no duplicate scenario rows


# ── Criticality wiring: money route → P0 scenario ─────────────────────────

def test_synthesised_money_scenario_is_banded_p0() -> None:
    result = compute_diff(APP, build_journeys(GOLDEN_NODES), {})
    trunk = next(s for s in result.scenarios if s.kind == KIND_TRUNK)
    # /transfer route + multi-page submit → P0.
    assert trunk.criticality_band == "P0"
    assert trunk.criticality_evidence  # named evidence, never empty on a hit


def test_empty_crawl_yields_no_journeys() -> None:
    assert build_journeys([]) == []
    result = compute_diff(APP, [], {})
    assert result.counts["total"] == 0
    assert result.queue == []


def test_result_as_dict_is_json_shaped() -> None:
    result = compute_diff(APP, build_journeys(GOLDEN_NODES), {})
    d = result.as_dict()
    assert d["app_id"] == APP
    assert isinstance(d["scenarios"], list) and d["scenarios"]
    first = d["scenarios"][0]
    assert set(first) >= {
        "scenario_id", "name", "kind", "criticality_band",
        "criticality_evidence", "fingerprint", "diff_state", "journey",
    }
