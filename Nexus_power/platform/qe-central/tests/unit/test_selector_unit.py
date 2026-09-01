"""QE-Central S5 — incremental selector unit tests (design §3.5).

The honesty-critical properties:

  * THE PLANTED-DELETION fixture: a vanished page → ``possible_deletion`` gap and
    the case on it is SELECTED (re-run), never silently carried forward;
  * incremental selects only changed cases + the P0 floor — a P0 case with NO
    change STILL runs; an unchanged non-P0 case with a verdict is carried;
  * a changed control_fp selects even when the page_key set is unchanged;
  * carried-forward verdict_age increments each cycle and the max-carry TTL
    triggers a forced re-run (a carried verdict can never go stale silently);
  * mode=full selects every case;
  * a case with no prior verdict is always run (never a green from nothing).
"""
from __future__ import annotations

from app.controlplane.cycle.change_detector import (
    MODE_INCREMENTAL,
    AppChangeContext,
    ChangeSet,
    HonestGaps,
    detect,
)
from app.controlplane.cycle.fingerprints import parse_journey_graph
from app.controlplane.cycle.selector import CaseRef, default_carry_ttl_cycles, select


# ── builders ─────────────────────────────────────────────────────────────
def _incremental(changed_pages=(), changed_atoms=(), gaps=None):
    return ChangeSet(
        mode=MODE_INCREMENTAL,
        changed_pages=frozenset(changed_pages),
        changed_atoms=frozenset(changed_atoms),
        honest_gaps=gaps or HonestGaps(),
    )


def _edge(frm, to, *, fp):
    return {"from_page": frm, "to_page": to, "verb": "click", "control_fp": fp}


def _snap(nodes, edges):
    return parse_journey_graph({"nodes": [{"page_key": n} for n in nodes], "edges": edges})


def _ids(result):
    return set(result.selected_test_ids)


def _carried_ids(result):
    return {c.test_id for c in result.carried_forward}


# ── THE PLANTED-DELETION fixture (end-to-end detect → select) ───────────────
def test_planted_deletion_selects_the_case_and_never_silently_carries_it():
    # Baseline: a hub linking three pages. Live: /gone has been DELETED.
    base = _snap(["/hub", "/a", "/gone"], [
        _edge("/hub", "/a", fp="FA"), _edge("/hub", "/gone", fp="FG"),
    ])
    live = _snap(["/hub", "/a"], [_edge("/hub", "/a", fp="FA")])
    cs = detect(
        AppChangeContext(app_id="app1", tenant_id="t1", baseline=base),
        None, None, live, None,
    )
    # The deletion surfaces as a P0 honest gap on the change set.
    assert "/gone" in cs.honest_gaps.vanished_pages_possible_deletion

    # The case that exercised /gone HAS a prior green verdict well within TTL —
    # so WITHOUT the deletion signal it would be carried. It must be RE-RUN.
    cases = [
        CaseRef(test_id="tc_gone", page_keys=("/gone",), criticality="P1",
                last_verdict_run_id="run_green_1", verdict_age_cycles=0),
        CaseRef(test_id="tc_a", page_keys=("/a",), criticality="P1",
                last_verdict_run_id="run_green_2", verdict_age_cycles=0),
    ]
    result = select(cases, cs, carry_ttl_cycles=5)

    assert "tc_gone" in _ids(result), "a vanished-page case must be re-run"
    assert "tc_gone" not in _carried_ids(result), "a vanished-page case must NOT be silently carried"
    # The still-present, unchanged /a case is carried forward (age-labelled).
    assert "tc_a" in _carried_ids(result)
    assert "tc_a" not in _ids(result)


# ── incremental selects only changed + the P0 floor ─────────────────────────
def test_incremental_selects_changed_and_p0_floor_only():
    cs = _incremental(changed_pages={"/changed"})
    cases = [
        # changed page → selected
        CaseRef(test_id="tc_changed", page_keys=("/changed",), criticality="P2",
                last_verdict_run_id="r1", verdict_age_cycles=0),
        # P0 with NO change → STILL runs (criticality floor)
        CaseRef(test_id="tc_p0", page_keys=("/stable",), criticality="P0",
                last_verdict_run_id="r2", verdict_age_cycles=0),
        # unchanged non-P0 with a verdict → carried
        CaseRef(test_id="tc_stable", page_keys=("/stable",), criticality="P2",
                last_verdict_run_id="r3", verdict_age_cycles=0),
    ]
    result = select(cases, cs, carry_ttl_cycles=5)
    assert _ids(result) == {"tc_changed", "tc_p0"}
    assert _carried_ids(result) == {"tc_stable"}


def test_p0_floor_uses_criticality_override_map():
    """The band can come from the criticality map, not just the CaseRef field."""
    cs = _incremental()  # nothing changed
    cases = [CaseRef(test_id="tc", page_keys=("/x",), criticality="P2",
                     last_verdict_run_id="r", verdict_age_cycles=0)]
    # map overrides the case's own P2 → P0 → must run despite no change
    result = select(cases, cs, criticality={"tc": "P0"}, carry_ttl_cycles=5)
    assert _ids(result) == {"tc"}


def test_changed_control_fp_selects_even_when_page_unchanged():
    cs = _incremental(changed_atoms={"FP_X"})
    cases = [
        CaseRef(test_id="tc_ctrl", page_keys=("/unchanged",), control_fps=("FP_X",),
                criticality="P2", last_verdict_run_id="r1", verdict_age_cycles=0),
        CaseRef(test_id="tc_other", page_keys=("/unchanged",), control_fps=("FP_Y",),
                criticality="P2", last_verdict_run_id="r2", verdict_age_cycles=0),
    ]
    result = select(cases, cs, carry_ttl_cycles=5)
    assert "tc_ctrl" in _ids(result)
    assert "tc_other" in _carried_ids(result)


# ── no prior verdict is always run ──────────────────────────────────────────
def test_case_without_prior_verdict_is_always_run():
    cs = _incremental()  # nothing changed
    cases = [CaseRef(test_id="tc_new", page_keys=("/x",), criticality="P2",
                     last_verdict_run_id="", verdict_age_cycles=0)]
    result = select(cases, cs, carry_ttl_cycles=5)
    assert _ids(result) == {"tc_new"}
    assert not result.carried_forward


# ── carried-forward age increments; TTL forces a re-run ─────────────────────
def test_carry_forward_increments_age():
    cs = _incremental()  # nothing changed
    case = CaseRef(test_id="tc", page_keys=("/x",), criticality="P2",
                   last_verdict_run_id="run_green", verdict_age_cycles=0)
    result = select([case], cs, carry_ttl_cycles=3)
    assert _ids(result) == set()
    (carried,) = result.carried_forward
    assert carried.test_id == "tc"
    assert carried.verdict_run_id == "run_green"
    assert carried.verdict_age_cycles == 1  # 0 → 1


def test_carry_forward_age_accumulates_across_cycles_until_ttl():
    cs = _incremental()  # nothing ever changes across the simulated cycles
    ttl = 3
    age = 0
    run_id = "run_green"
    seen_ages = []
    for _ in range(10):
        case = CaseRef(test_id="tc", page_keys=("/x",), criticality="P2",
                       last_verdict_run_id=run_id, verdict_age_cycles=age)
        result = select([case], cs, carry_ttl_cycles=ttl)
        if result.selected_test_ids:  # TTL reached → forced re-run this cycle
            assert age >= ttl
            break
        (carried,) = result.carried_forward
        seen_ages.append(carried.verdict_age_cycles)
        age = carried.verdict_age_cycles  # feed the incremented age back in
    # ages climb 1,2,3 then the case is re-run (never carried past the TTL)
    assert seen_ages == [1, 2, 3]
    assert age >= ttl


def test_ttl_reached_triggers_rerun_with_named_reason():
    cs = _incremental()  # nothing changed
    case = CaseRef(test_id="tc", page_keys=("/x",), criticality="P2",
                   last_verdict_run_id="run_green", verdict_age_cycles=3)
    result = select([case], cs, carry_ttl_cycles=3)
    assert _ids(result) == {"tc"}
    assert "TTL" in result.per_case_reasons["tc"]


# ── mode=full ────────────────────────────────────────────────────────────────
def test_mode_full_selects_everything():
    cs = ChangeSet.full("manual full")
    cases = [
        CaseRef(test_id="a", page_keys=("/1",), last_verdict_run_id="r", verdict_age_cycles=0),
        CaseRef(test_id="b", page_keys=("/2",), last_verdict_run_id="r", verdict_age_cycles=0),
        CaseRef(test_id="c", page_keys=("/3",), criticality="P3"),
    ]
    result = select(cases, cs, carry_ttl_cycles=5)
    assert _ids(result) == {"a", "b", "c"}
    assert not result.carried_forward


# ── result shape + coercion ─────────────────────────────────────────────────
def test_result_as_dict_matches_convention_shape():
    cs = _incremental(changed_pages={"/changed"})
    cases = [
        CaseRef(test_id="run_me", page_keys=("/changed",), last_verdict_run_id="r1"),
        CaseRef(test_id="carry_me", page_keys=("/stable",), criticality="P2",
                last_verdict_run_id="r2", verdict_age_cycles=1),
    ]
    d = select(cases, cs, carry_ttl_cycles=5).as_dict()
    assert set(d) >= {"mode", "selected_test_ids", "carried_forward", "selection_reason"}
    assert d["selected_test_ids"] == ["run_me"]
    (cf,) = d["carried_forward"]
    assert set(cf) >= {"test_id", "verdict_run_id", "verdict_age_cycles"}
    assert cf["test_id"] == "carry_me"
    assert cf["verdict_age_cycles"] == 2


def test_select_accepts_plain_dicts():
    cs = _incremental(changed_pages={"/changed"})
    cases = [
        {"test_id": "d1", "page_keys": ["/changed"], "last_verdict_run_id": "r"},
        {"test_id": "d2", "page_keys": ["/stable"], "criticality": "P2",
         "last_verdict_run_id": "r", "verdict_age_cycles": 0},
    ]
    result = select(cases, cs, carry_ttl_cycles=5)
    assert "d1" in _ids(result)
    assert "d2" in _carried_ids(result)


def test_default_carry_ttl_is_env_overridable(monkeypatch):
    monkeypatch.setenv("QEC_CARRY_TTL_CYCLES", "9")
    assert default_carry_ttl_cycles() == 9
    monkeypatch.setenv("QEC_CARRY_TTL_CYCLES", "not-an-int")
    assert default_carry_ttl_cycles() == 5  # malformed → safe default
    monkeypatch.delenv("QEC_CARRY_TTL_CYCLES", raising=False)
    assert default_carry_ttl_cycles() == 5


# ── uncomputable fingerprint flows through as CHANGED (fail-safe) ────────────
def test_uncomputable_live_graph_forces_full_selection_end_to_end():
    from app.controlplane.cycle.fingerprints import FingerprintSnapshot

    base = _snap(["/a", "/b"], [_edge("/a", "/b", fp="FP")])
    live = FingerprintSnapshot.unavailable()  # journey_graph.py absent / endpoint down
    cs = detect(AppChangeContext(app_id="app1", tenant_id="t1", baseline=base),
                None, None, live, None)
    cases = [
        CaseRef(test_id="a", page_keys=("/a",), last_verdict_run_id="r", verdict_age_cycles=0),
        CaseRef(test_id="b", page_keys=("/b",), last_verdict_run_id="r", verdict_age_cycles=0),
    ]
    result = select(cases, cs, carry_ttl_cycles=5)
    assert _ids(result) == {"a", "b"}, "an uncomputable live graph must re-run everything"
    assert not result.carried_forward


def test_repo_stack_unsupported_forces_full_selection_end_to_end():
    from app.controlplane.cycle.change_detector import RepoDiff

    base = _snap(["/a", "/b"], [_edge("/a", "/b", fp="FP")])
    live = _snap(["/a", "/b"], [_edge("/a", "/b", fp="FP")])  # no live change
    cs = detect(AppChangeContext(app_id="app1", tenant_id="t1", baseline=base, repo_bound=True),
                "sha_old", "sha_new", live, RepoDiff(stack_supported=False))
    cases = [
        CaseRef(test_id="a", page_keys=("/a",), last_verdict_run_id="r", verdict_age_cycles=0),
        CaseRef(test_id="b", page_keys=("/b",), last_verdict_run_id="r", verdict_age_cycles=0),
    ]
    result = select(cases, cs, carry_ttl_cycles=5)
    assert _ids(result) == {"a", "b"}, "repo stack_supported=false must re-run everything"
