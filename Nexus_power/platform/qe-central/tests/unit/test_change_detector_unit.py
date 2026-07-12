"""QE-Central S5 — change detector unit tests (pure logic, synthetic inputs).

Proves ``detect(...)`` = UNION of the repo-SHA diff (fail-safe-to-full) and the
journey-graph fingerprint diff (fail-safe-to-CHANGED), per design §3.5:

  * a fingerprint change → MODE_INCREMENTAL with precise changed pages/atoms;
  * a whole-uncomputable live graph → MODE_FULL (re-run everything);
  * a vanished page → a ``possible_deletion`` honest gap;
  * repo stack_supported=False → MODE_FULL;
  * repo diff unavailable while a repo change is in scope → MODE_FULL;
  * a good repo diff folds its mapped atoms + page mappings into the change set.
"""
from __future__ import annotations

from app.controlplane.cycle.change_detector import (
    MODE_FULL,
    MODE_INCREMENTAL,
    AppChangeContext,
    ChangeSet,
    RepoAtom,
    RepoDiff,
    detect,
)
from app.controlplane.cycle.fingerprints import (
    FingerprintSnapshot,
    PageFingerprint,
    parse_journey_graph,
)


# ── helpers ───────────────────────────────────────────────────────────────
def _edge(frm, to, *, fp):
    return {"from_page": frm, "to_page": to, "verb": "click", "control_fp": fp}


def _snap_from(nodes, edges):
    return parse_journey_graph({
        "nodes": [{"page_key": n} for n in nodes],
        "edges": edges,
    })


def _ctx(baseline, *, repo_bound=False):
    return AppChangeContext(
        app_id="app1", tenant_id="t1", baseline=baseline, repo_bound=repo_bound,
    )


# ── fingerprint-only detection ──────────────────────────────────────────────
def test_incremental_on_fingerprint_change():
    base = _snap_from(["/a", "/b"], [_edge("/a", "/b", fp="FP_OLD")])
    live = _snap_from(["/a", "/b"], [_edge("/a", "/b", fp="FP_NEW")])
    cs = detect(_ctx(base), None, None, live, None)
    assert cs.mode == MODE_INCREMENTAL
    assert "/a" in cs.changed_pages
    assert {"FP_OLD", "FP_NEW"} <= cs.changed_atoms


def test_no_change_yields_empty_incremental():
    base = _snap_from(["/a", "/b"], [_edge("/a", "/b", fp="FP")])
    live = _snap_from(["/a", "/b"], [_edge("/a", "/b", fp="FP")])
    cs = detect(_ctx(base), None, None, live, None)
    assert cs.mode == MODE_INCREMENTAL
    assert cs.changed_pages == frozenset()
    assert cs.changed_atoms == frozenset()


def test_live_graph_uncomputable_forces_full():
    base = _snap_from(["/a", "/b"], [_edge("/a", "/b", fp="FP")])
    live = FingerprintSnapshot.unavailable()
    cs = detect(_ctx(base), None, None, live, None)
    assert cs.mode == MODE_FULL
    assert cs.honest_gaps.live_graph_uncomputable is True
    assert set(cs.honest_gaps.uncomputable_pages_treated_changed) == {"/a", "/b"}
    assert cs.honest_gaps.vanished_pages_possible_deletion == ()


def test_single_uncomputable_page_is_changed_but_stays_incremental():
    base = FingerprintSnapshot(available=True, pages={
        "/a": PageFingerprint(structural_hash="h", control_fps=("FA",)),
    })
    live = FingerprintSnapshot(available=True, pages={
        "/a": PageFingerprint.uncomputable(),
    })
    cs = detect(_ctx(base), None, None, live, None)
    assert cs.mode == MODE_INCREMENTAL
    assert "/a" in cs.changed_pages
    assert "/a" in cs.honest_gaps.uncomputable_pages_treated_changed


def test_vanished_page_raises_possible_deletion_gap():
    base = _snap_from(["/hub", "/a", "/gone"], [
        _edge("/hub", "/a", fp="FA"), _edge("/hub", "/gone", fp="FG"),
    ])
    live = _snap_from(["/hub", "/a"], [_edge("/hub", "/a", fp="FA")])
    cs = detect(_ctx(base), None, None, live, None)
    assert cs.mode == MODE_INCREMENTAL
    assert "/gone" in cs.changed_pages
    assert "/gone" in cs.honest_gaps.vanished_pages_possible_deletion
    assert cs.honest_gaps.has_possible_deletion is True


# ── repo-SHA diff (fail-safe-to-full) ────────────────────────────────────────
def _unchanged_pair():
    base = _snap_from(["/a", "/b"], [_edge("/a", "/b", fp="FP")])
    live = _snap_from(["/a", "/b"], [_edge("/a", "/b", fp="FP")])
    return base, live


def test_repo_stack_unsupported_forces_full():
    base, live = _unchanged_pair()
    cs = detect(_ctx(base, repo_bound=True), "sha_old", "sha_new", live,
                RepoDiff(stack_supported=False))
    assert cs.mode == MODE_FULL
    assert cs.honest_gaps.repo_stack_unsupported is True


def test_repo_diff_none_but_change_in_scope_forces_full():
    base, live = _unchanged_pair()
    cs = detect(_ctx(base, repo_bound=True), "sha_old", "sha_new", live, None)
    assert cs.mode == MODE_FULL
    assert cs.honest_gaps.repo_diff_unavailable is True


def test_repo_new_sha_without_old_sha_cannot_scope_forces_full():
    base, live = _unchanged_pair()
    cs = detect(_ctx(base, repo_bound=True), "", "sha_new", live,
                RepoDiff(stack_supported=True, mapped_atoms=(RepoAtom(key="k1"),)))
    assert cs.mode == MODE_FULL
    assert cs.honest_gaps.repo_diff_unavailable is True


def test_repo_not_in_scope_when_no_new_sha():
    base, live = _unchanged_pair()
    # No new_sha → a probe/schedule cycle → repo contributes nothing, no full.
    cs = detect(_ctx(base), None, None, live, None)
    assert cs.mode == MODE_INCREMENTAL
    assert cs.honest_gaps.repo_diff_unavailable is False


def test_good_repo_diff_folds_atoms_and_page_mappings():
    base, live = _unchanged_pair()
    diff = RepoDiff(
        stack_supported=True,
        changed_files=("src/pages/transfer.tsx",),
        mapped_atoms=(
            RepoAtom(key="route:/transfer", kind="route", page_key="https://x/transfer"),
            RepoAtom(key="api:/v1/pay", kind="api_endpoint"),
        ),
    )
    cs = detect(_ctx(base, repo_bound=True), "sha_old", "sha_new", live, diff)
    assert cs.mode == MODE_INCREMENTAL
    assert "route:/transfer" in cs.changed_atoms
    assert "api:/v1/pay" in cs.changed_atoms
    assert "/transfer" in cs.changed_pages  # page_key normalised from the atom


def test_good_repo_diff_with_empty_mapping_stays_incremental():
    """A stack-supported diff that maps to no atoms is trusted (no false full)."""
    base, live = _unchanged_pair()
    diff = RepoDiff(stack_supported=True, changed_files=("README.md",), mapped_atoms=())
    cs = detect(_ctx(base, repo_bound=True), "sha_old", "sha_new", live, diff)
    assert cs.mode == MODE_INCREMENTAL
    assert cs.changed_pages == frozenset()


def test_union_of_repo_and_fingerprint_changes():
    base = _snap_from(["/a", "/b"], [_edge("/a", "/b", fp="FP_OLD")])
    live = _snap_from(["/a", "/b"], [_edge("/a", "/b", fp="FP_NEW")])  # /a changed
    diff = RepoDiff(
        stack_supported=True,
        mapped_atoms=(RepoAtom(key="route:/c", kind="route", page_key="/c"),),
    )
    cs = detect(_ctx(base, repo_bound=True), "sha_old", "sha_new", live, diff)
    assert cs.mode == MODE_INCREMENTAL
    assert {"/a", "/c"} <= cs.changed_pages  # union of both signals


# ── ChangeSet surface ────────────────────────────────────────────────────────
def test_changeset_full_constructor_and_as_dict():
    cs = ChangeSet.full("manual full-floor")
    assert cs.is_full is True
    d = cs.as_dict()
    assert d["mode"] == MODE_FULL
    assert d["changed_pages"] == [] and d["changed_atoms"] == []
    assert "honest_gaps" in d and "reason" in d


# ── repo_diff_from_result adapter (CODE P4 — the honesty seam) ──────────────
import types as _types

from app.controlplane.cycle.change_detector import repo_diff_from_result


def _result(**kw):
    base = {"stack_supported": True, "fail_safe_to_full": False,
            "changed_files": [], "changed_atoms": []}
    base.update(kw)
    return _types.SimpleNamespace(**base)


def test_adapter_none_is_full():
    assert repo_diff_from_result(None).stack_supported is False


def test_adapter_fail_safe_collapses_to_full_even_with_atoms():
    # THE seam: a partial map (fail_safe_to_full) must NOT narrow, even though it
    # carries atoms — else a config-file change silently green-washes.
    rd = repo_diff_from_result(_result(
        fail_safe_to_full=True,
        changed_atoms=[{"atom_id": "a1", "kind": "route", "value": {"path_pattern": "/x"}}]))
    assert rd.stack_supported is False


def test_adapter_unsupported_stack_is_full():
    assert repo_diff_from_result(_result(stack_supported=False)).stack_supported is False


def test_adapter_good_diff_maps_atoms_and_pages():
    rd = repo_diff_from_result(_result(
        changed_files=["src/quote.py"],
        changed_atoms=[
            {"atom_id": "a1", "kind": "route", "value": {"path_pattern": "/quote"}},
            {"atom_id": "a2", "kind": "api_endpoint", "value": {"path": "/api/bind"}},
        ]))
    assert rd.stack_supported is True
    assert rd.changed_files == ("src/quote.py",)
    keys = {(a.key, a.page_key) for a in rd.mapped_atoms}
    assert keys == {("a1", "/quote"), ("a2", "/api/bind")}


def test_adapter_atom_without_id_derives_key():
    rd = repo_diff_from_result(_result(
        changed_atoms=[{"kind": "route", "value": {"path_pattern": "/home"}}]))
    assert rd.mapped_atoms[0].key == "route:/home"
