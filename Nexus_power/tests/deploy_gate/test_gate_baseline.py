"""M0.4 — the ratchet, the baseline writers, and the runtime gap state.

T-GT-03 (both missing metrics ratchet), T-GT-04 (the git baseline is immutable
during a run), T-GT-06 (catalog shrink blocks the deploy).
"""
from __future__ import annotations

import datetime
import json
import subprocess
import sys

import pytest

import gate_baseline as gb


# A crawl that met every floor in the shipped baseline, plus the two metrics the
# writers used to forget and the catalog floor this milestone adds.
GOOD = {
    "pages": 22, "forms": 7, "auto_filled": 29, "selects_filled": 6,
    "forms_confirmed": 3, "submitted": 9, "flows": 7, "deepest_flow": 5,
    "wizard_advances": 4, "tests": 9, "catalog_questions": 67,
}


def _write(path: str, data: dict) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, sort_keys=True)


# ══════════════════════════════════════════════════════════════════════════
#  T-GT-03 — every EVALUATED metric is also PERSISTED
# ══════════════════════════════════════════════════════════════════════════
def test_every_ratcheted_metric_round_trips_through_the_raise_writer(baseline_path):
    """Writer -> reader -> writer is identity. The defect this pins: a metric
    could be checked by the gate and absent from the writer's dict, so its floor
    stayed 0 forever and it could never regress."""
    _write(baseline_path, {})
    raised, _ = gb.raise_baseline(gb.load_json(baseline_path), GOOD)
    gb.write_baseline(baseline_path, raised)

    reread = gb.load_json(baseline_path)
    for metric in gb.RATCHETED_METRICS:
        assert metric in reread, f"{metric} is evaluated but never persisted"
        assert reread[metric] == GOOD[metric]

    again, _ = gb.raise_baseline(reread, GOOD)
    assert again == reread, "writer->reader->writer is not identity"


@pytest.mark.parametrize("metric", ["selects_filled", "forms_confirmed"])
def test_the_two_forgotten_metrics_now_ratchet(baseline_path, metric):
    """The precise T-GT-03 regression. Before the fix these were checked on every
    run, written by neither writer, and therefore permanently unenforced."""
    _write(baseline_path, {})
    raised, _ = gb.raise_baseline(gb.load_json(baseline_path), GOOD)
    gb.write_baseline(baseline_path, raised)

    regressed = dict(GOOD, **{metric: GOOD[metric] - 1})
    report = gb.evaluate(regressed, gb.load_json(baseline_path))
    assert report["failed"] is True
    row = next(r for r in report["results"] if r["metric"] == metric)
    assert row["verdict"] == gb.V_REGRESSED


def test_metric_list_is_declared_once():
    """Structural guard on the defect CLASS. Three hand-maintained copies of the
    metric list is what let two of them drift out of the writers."""
    assert len(set(gb.RATCHETED_METRICS)) == len(gb.RATCHETED_METRICS)
    for metric in ("selects_filled", "forms_confirmed", "catalog_questions"):
        assert metric in gb.RATCHETED_METRICS


# ══════════════════════════════════════════════════════════════════════════
#  The ratchet itself
# ══════════════════════════════════════════════════════════════════════════
def test_regression_below_a_floor_fails(baseline_path):
    _write(baseline_path, dict(GOOD))
    report = gb.evaluate(dict(GOOD, pages=21), gb.load_json(baseline_path))
    assert report["failed"] is True
    assert next(r for r in report["results"] if r["metric"] == "pages")["verdict"] == gb.V_REGRESSED


def test_a_value_above_the_floor_is_a_rise_not_a_failure(baseline_path):
    _write(baseline_path, dict(GOOD))
    report = gb.evaluate(dict(GOOD, pages=30), gb.load_json(baseline_path))
    assert report["failed"] is False
    assert next(r for r in report["results"] if r["metric"] == "pages")["verdict"] == gb.V_RISE


def test_a_never_met_floor_is_a_gap_and_never_reads_as_ok(baseline_path):
    """An unenforced floor that printed OK is how forms_confirmed sat at 0 behind
    a PASSING gate while nine submits fired and none were confirmed."""
    _write(baseline_path, dict(GOOD, forms_confirmed=0))
    report = gb.evaluate(dict(GOOD, forms_confirmed=0), gb.load_json(baseline_path))
    row = next(r for r in report["results"] if r["metric"] == "forms_confirmed")
    assert row["verdict"] == gb.V_GAP
    assert row["verdict"] != gb.V_OK
    assert "forms_confirmed" in report["gaps"]


def test_a_gap_self_enforces_the_moment_data_arrives(baseline_path):
    _write(baseline_path, dict(GOOD, forms_confirmed=0))
    report = gb.evaluate(dict(GOOD, forms_confirmed=1), gb.load_json(baseline_path))
    assert report["gaps"] == []
    assert next(r for r in report["results"] if r["metric"] == "forms_confirmed")["verdict"] == gb.V_RISE


def test_a_missing_measurement_is_read_as_zero_not_as_a_pass(baseline_path):
    """An absent metric must not be silently tolerated: no measurement is not
    evidence of a passing one."""
    _write(baseline_path, dict(GOOD))
    partial = {k: v for k, v in GOOD.items() if k != "deepest_flow"}
    report = gb.evaluate(partial, gb.load_json(baseline_path))
    assert report["failed"] is True


def test_a_corrupted_baseline_is_told_apart_from_a_missing_one(tmp_path):
    """Failure injection, and the reason the distinction exists: collapsing both
    to {} means every floor reads 0, every metric 'rises' above it, and a
    truncated baseline makes the gate pass EVERYTHING while printing a wall of
    new floors — green-wash arriving through the gate's own front door."""
    corrupt = str(tmp_path / "corrupt.json")
    with open(corrupt, "w", encoding="utf-8") as fh:
        fh.write("{not json")
    assert gb.load_baseline(corrupt) == ({}, "corrupt")
    assert gb.load_baseline(str(tmp_path / "absent.json")) == ({}, "missing")
    gb.write_baseline(str(tmp_path / "fine.json"), dict(GOOD))
    assert gb.load_baseline(str(tmp_path / "fine.json"))[1] == "ok"


def test_a_corrupt_baseline_fails_the_gate_rather_than_passing_everything(
        scripts_dir, tmp_path):
    path = str(tmp_path / "corrupt.json")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write('{"pages": 22,')  # a half-written file, the realistic case
    res = _cli(scripts_dir, "evaluate", "--baseline", path,
               "--current", json.dumps(GOOD))
    assert res.returncode == 1, "a corrupt baseline was scored as a clean run"
    assert "UNREADABLE" in res.stdout


def test_a_missing_baseline_is_a_legitimate_first_run(scripts_dir, tmp_path):
    res = _cli(scripts_dir, "evaluate", "--baseline", str(tmp_path / "none.json"),
               "--current", json.dumps(GOOD))
    assert res.returncode == 0
    assert "none yet" in res.stdout


# ══════════════════════════════════════════════════════════════════════════
#  T-GT-06 — catalog completeness
# ══════════════════════════════════════════════════════════════════════════
def test_catalog_shrink_fails_the_gate(baseline_path):
    """Removing catalog entries must block deployment. Every crawl floor holds
    in this scenario — only the catalog moved."""
    _write(baseline_path, dict(GOOD))
    report = gb.evaluate(dict(GOOD, catalog_questions=66), gb.load_json(baseline_path))
    assert report["failed"] is True
    row = next(r for r in report["results"] if r["metric"] == "catalog_questions")
    assert row["verdict"] == gb.V_REGRESSED
    assert row["floor"] == 67 and row["current"] == 66


def test_catalog_growth_ratchets_the_floor(baseline_path):
    _write(baseline_path, dict(GOOD))
    raised, moved = gb.raise_baseline(gb.load_json(baseline_path),
                                      dict(GOOD, catalog_questions=80))
    assert moved["catalog_questions"] == [67, 80]
    assert raised["catalog_questions"] == 80


# ══════════════════════════════════════════════════════════════════════════
#  Writers
# ══════════════════════════════════════════════════════════════════════════
def test_raise_only_ever_raises(baseline_path):
    _write(baseline_path, dict(GOOD))
    raised, moved = gb.raise_baseline(gb.load_json(baseline_path), dict(GOOD, pages=1))
    assert raised["pages"] == 22, "a raise writer lowered a floor"
    assert moved == {}


def test_rebaseline_may_lower_and_records_why(baseline_path):
    _write(baseline_path, dict(GOOD))
    out, lowered = gb.rebaseline(gb.load_json(baseline_path), dict(GOOD, pages=20),
                                 "funnel consolidation collapsed duplicate fragments",
                                 exploration="expl-1")
    assert out["pages"] == 20
    assert lowered["pages"] == [22, 20]
    assert "consolidation" in out["_rebaselined"]["reason"]
    assert out["_rebaselined"]["exploration"] == "expl-1"


def test_rebaseline_refuses_without_a_reason(baseline_path):
    _write(baseline_path, dict(GOOD))
    with pytest.raises(ValueError):
        gb.rebaseline(gb.load_json(baseline_path), GOOD, "   ")


def test_serialization_is_byte_stable(baseline_path):
    """A write that changes no value must produce no diff, or `git status` after
    a gate run is noise instead of a signal."""
    gb.write_baseline(baseline_path, dict(GOOD))
    with open(baseline_path, "rb") as fh:
        first = fh.read()
    gb.write_baseline(baseline_path, gb.load_json(baseline_path))
    with open(baseline_path, "rb") as fh:
        assert fh.read() == first
    assert first.endswith(b"\n")


def test_annotations_survive_a_raise(baseline_path):
    """The `_rebaselined` note explaining a past lowering must not be dropped by
    a later raise, or the justification for a floor outlives its explanation."""
    _write(baseline_path, dict(GOOD, _rebaselined={"reason": "historic"}))
    raised, _ = gb.raise_baseline(gb.load_json(baseline_path), GOOD)
    assert raised["_rebaselined"]["reason"] == "historic"


# ══════════════════════════════════════════════════════════════════════════
#  T-GT-04 — runtime state never touches the tracked baseline
# ══════════════════════════════════════════════════════════════════════════
def test_evaluate_never_writes_the_baseline(baseline_path):
    gb.write_baseline(baseline_path, dict(GOOD, forms_confirmed=0))
    with open(baseline_path, "rb") as fh:
        before = fh.read()
    gb.evaluate(dict(GOOD, forms_confirmed=0), gb.load_json(baseline_path))
    with open(baseline_path, "rb") as fh:
        assert fh.read() == before


def test_gap_bookkeeping_goes_to_runtime_state_not_the_baseline(tmp_path, monkeypatch):
    """The whole of T-GT-04, end to end through the CLI: a full gate-style gaps
    update must leave the tracked baseline byte-identical."""
    baseline = tmp_path / "golden_crawl_baseline.json"
    state = tmp_path / "runtime.json"
    monkeypatch.setenv(gb.STATE_ENV, str(state))
    gb.write_baseline(str(baseline), dict(GOOD, forms_confirmed=0))
    before = baseline.read_bytes()

    rc = gb.main(["gaps", "--baseline", str(baseline), "--gap", "forms_confirmed"])
    assert rc == 0
    assert baseline.read_bytes() == before, "a gate run modified the tracked baseline"
    assert state.exists(), "gap state was not persisted anywhere"
    assert json.loads(state.read_text())["gaps"]["forms_confirmed"]["runs"] == 1


def test_runtime_state_path_defaults_beside_the_baseline_and_is_a_dotfile(monkeypatch):
    monkeypatch.delenv(gb.STATE_ENV, raising=False)
    path = gb.runtime_state_path("/srv/app/scripts/golden_crawl_baseline.json")
    assert path.endswith(gb.STATE_BASENAME)
    assert gb.STATE_BASENAME.startswith(".")


def test_gap_becomes_overdue_after_max_runs():
    state: dict = {}
    for run in range(1, 4):
        state, overdue, young = gb.update_gaps(state, ["forms_confirmed"],
                                               max_runs=3, max_days=7)
        assert overdue == [], f"went red on run {run}, before the tolerance expired"
        assert young
    state, overdue, _ = gb.update_gaps(state, ["forms_confirmed"], max_runs=3, max_days=7)
    assert overdue and "forms_confirmed" in overdue[0]


def test_gap_becomes_overdue_after_max_days():
    old = (datetime.date(2026, 1, 1)).isoformat()
    state = {"gaps": {"selects_filled": {"runs": 1, "since": old}}}
    _, overdue, _ = gb.update_gaps(state, ["selects_filled"], max_runs=99,
                                   max_days=7, today=datetime.date(2026, 2, 1))
    assert overdue and "selects_filled" in overdue[0]


def test_a_met_gap_loses_its_history():
    """A capability that lands and later regresses must be judged by the ratchet,
    which knows its best-ever — never by a stale unmet-since date."""
    state, _, _ = gb.update_gaps({}, ["forms_confirmed"], max_runs=3, max_days=7)
    state, _, _ = gb.update_gaps(state, [], max_runs=3, max_days=7)
    assert state["gaps"] == {}


def test_legacy_gaps_are_lifted_out_of_a_tracked_baseline_without_rewriting_it(tmp_path):
    """Hosts that ran the old gate have `_gaps` inside the baseline. Dropping it
    would reset every counter and buy an overdue floor another full tolerance
    window — so the history moves. The baseline is NOT rewritten here: doing that
    would be this milestone's own bug."""
    baseline = tmp_path / "b.json"
    gb.write_baseline(str(baseline), dict(GOOD, _gaps={"forms_confirmed": {"runs": 9, "since": "2026-01-01"}}))
    before = baseline.read_bytes()

    state, migrated = gb.migrate_legacy_gaps(str(baseline), {})
    assert migrated is True
    assert state["gaps"]["forms_confirmed"]["runs"] == 9
    assert baseline.read_bytes() == before

    _, overdue, _ = gb.update_gaps(state, ["forms_confirmed"], max_runs=3, max_days=7)
    assert overdue, "a migrated overdue gap was silently forgiven"


def test_unwritable_state_warns_rather_than_silently_forgetting(tmp_path, monkeypatch, capsys):
    """Failure injection. Silently losing the counter resets tolerance on every
    run, so an overdue floor is never overdue."""
    monkeypatch.setenv(gb.STATE_ENV, str(tmp_path / "nodir" / "x" / "s.json"))
    monkeypatch.setattr(gb, "write_baseline",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("read-only fs")))
    baseline = tmp_path / "b.json"
    baseline.write_text(json.dumps(GOOD))
    assert gb.main(["gaps", "--baseline", str(baseline), "--gap", "forms_confirmed"]) == 0
    assert "gap state not persisted" in capsys.readouterr().err


# ══════════════════════════════════════════════════════════════════════════
#  CLI contract — this is what golden_crawl_gate.sh actually calls
# ══════════════════════════════════════════════════════════════════════════
def _cli(scripts_dir, *args):
    return subprocess.run([sys.executable, f"{scripts_dir}/gate_baseline.py", *args],
                          capture_output=True, text=True)


def test_cli_evaluate_exit_code_signals_regression(scripts_dir, baseline_path):
    _write(baseline_path, dict(GOOD))
    ok = _cli(scripts_dir, "evaluate", "--baseline", baseline_path,
              "--current", json.dumps(GOOD))
    assert ok.returncode == 0
    bad = _cli(scripts_dir, "evaluate", "--baseline", baseline_path,
               "--current", json.dumps(dict(GOOD, catalog_questions=1)))
    assert bad.returncode == 1
    assert "FAIL  catalog_questions" in bad.stdout


def test_cli_evaluate_emits_the_gap_list_the_shell_parses(scripts_dir, baseline_path):
    _write(baseline_path, dict(GOOD, forms_confirmed=0, selects_filled=0))
    res = _cli(scripts_dir, "evaluate", "--baseline", baseline_path,
               "--current", json.dumps(dict(GOOD, forms_confirmed=0, selects_filled=0)))
    gaps = [l for l in res.stderr.splitlines() if l.startswith("GAPS=")][0]
    assert "forms_confirmed" in gaps and "selects_filled" in gaps


def test_cli_raise_persists_all_metrics(scripts_dir, baseline_path):
    _write(baseline_path, {})
    res = _cli(scripts_dir, "raise", "--baseline", baseline_path,
               "--current", json.dumps(GOOD))
    assert res.returncode == 0
    stored = gb.load_json(baseline_path)
    assert all(stored.get(m) == GOOD[m] for m in gb.RATCHETED_METRICS)
