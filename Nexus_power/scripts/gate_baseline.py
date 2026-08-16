#!/usr/bin/env python3
"""GOLDEN-CRAWL GATE — the ratchet, the baseline writers, and the runtime state.

WHY THIS IS A MODULE (M0.4 / T-GT-03, T-GT-04, T-GT-06).
Every one of these behaviours used to live in a ``python3 -c`` heredoc inside
``golden_crawl_gate.sh``. Three consequences, all of them observed:

  1. NOTHING COULD BE TESTED. A heredoc is not importable, so the ratchet — the
     single most load-bearing release decision in the pipeline — had no unit
     test, and a metric could be evaluated by the gate while being silently
     absent from the writer that persists it.

  2. AND ONE WAS. ``selects_filled`` and ``forms_confirmed`` were checked on
     every run and appeared in NEITHER writer's ``cur`` dict. Their floor was
     therefore permanently 0, they printed ``GAP NOT YET ENFORCED`` forever, and a
     regression in either could never be caught. That is T-GT-03, and it is a
     direct consequence of the metric list existing three times in three places.
     Here it exists ONCE — ``RATCHETED_METRICS`` — and the reader, the checker
     and both writers are all derived from it, so the defect class is structurally
     gone rather than merely fixed.

  3. RUNTIME STATE WAS WRITTEN INTO A GIT-TRACKED FILE. The gap bookkeeping
     (``_gaps``: how many runs a never-met floor has been unmet) was persisted
     into ``golden_crawl_baseline.json``, which is committed. Every gate run
     therefore dirtied the working tree, and on the VM — where deploy does
     ``git pull`` — a dirty baseline is a merge conflict that blocks the next
     deploy. Worse, the count is per-HOST runtime state being smuggled into a
     file whose entire purpose is to be reviewable version-controlled evidence.
     That is T-GT-04: the baseline is now READ-ONLY except under an explicit
     ``raise``/``rebaseline`` command, and gap bookkeeping lives in a separate,
     untracked runtime state file.

THE CONTRACT.
  * The BASELINE (git-tracked) holds floors only. It is written ONLY by an
    operator-invoked ``raise`` or ``rebaseline``. A plain gate run never touches it.
  * The RUNTIME STATE (untracked, per-host) holds gap bookkeeping. A gate run
    writes only here.
  * A floor may only go UP under ``raise``. It may go DOWN only under
    ``rebaseline``, which demands a written reason recorded next to the numbers.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import sys
from typing import Any, Iterable

# ── The metrics under ratchet — ONE list, used by every consumer ─────────────
# Adding a floor means adding it here and nowhere else: the checker, the raise
# writer and the rebaseline writer all iterate this tuple. T-GT-03 existed
# because this list was written out by hand in three places and drifted.
RATCHETED_METRICS: tuple[str, ...] = (
    "pages",
    "forms",
    "auto_filled",
    # T-GT-03 — evaluated since the read-back work landed, persisted by nobody.
    "selects_filled",
    # T-GT-03 — a submit that FIRED is not a submit that WORKED. Nine submits
    # with zero confirmations scored identically to nine completed transactions.
    "forms_confirmed",
    "submitted",
    "flows",
    "deepest_flow",
    "wizard_advances",
    "tests",
    # T-GT-06 — the Master Catalog is the product's actual deliverable. Crawl and
    # coverage floors all held while the catalog silently shrank, because nothing
    # compared its size to the last known good.
    "catalog_questions",
)

# Keys in the baseline that are annotations, not floors.
_ANNOTATION_PREFIX = "_"

# Verdicts the ratchet can return for one metric.
V_REGRESSED = "REGRESSED"
V_RISE = "RISE"
V_GAP = "GAP"
V_OK = "OK"

STATE_ENV = "GOLDEN_GATE_STATE"
STATE_BASENAME = ".gate_runtime_state.json"


# ══════════════════════════════════════════════════════════════════════════
#  Baseline I/O
# ══════════════════════════════════════════════════════════════════════════
def load_json(path: str) -> dict:
    """Read a JSON object, or {} if it is missing/unreadable/not an object."""
    return load_baseline(path)[0]


def load_baseline(path: str) -> tuple[dict, str]:
    """Read a baseline AND say whether it could be trusted.

    Status is ``ok``, ``missing`` or ``corrupt``, and the distinction is
    load-bearing. Collapsing all three to ``{}`` — which is what this used to do
    — means every floor reads 0, every metric therefore RISES above it, and a
    truncated or half-written baseline makes the gate pass EVERYTHING while
    printing a wall of cheerful new floors. That is the exact green-wash this
    gate exists to prevent, arriving through the gate's own front door.

    ``missing`` is legitimate (a brand-new app has no floors yet). ``corrupt``
    is not: the evidence was supposed to be there and is unreadable, so no
    verdict about a regression is possible."""
    if not os.path.exists(path):
        return {}, "missing"
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        return {}, "corrupt"
    if not isinstance(data, dict):
        return {}, "corrupt"
    return data, "ok"


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def floors(baseline: dict) -> dict[str, int]:
    """The floor for every ratcheted metric — absent means 0 (unenforced)."""
    return {name: _as_int(baseline.get(name, 0)) for name in RATCHETED_METRICS}


def write_baseline(path: str, baseline: dict) -> None:
    """Serialise a baseline deterministically.

    ``sort_keys`` + fixed indent + trailing newline: byte-stable output means a
    write that changes no value produces no diff, so `git status` after a gate
    run is a real signal (T-GT-04) rather than noise."""
    payload = json.dumps(baseline, indent=2, sort_keys=True) + "\n"
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(payload)


# ══════════════════════════════════════════════════════════════════════════
#  The ratchet
# ══════════════════════════════════════════════════════════════════════════
def evaluate(current: dict, baseline: dict) -> dict:
    """Compare a crawl's metrics against the floors.

    Returns ``{"results": [{metric, current, floor, verdict}], "failed": bool,
    "gaps": [...]}``. A metric missing from ``current`` is treated as 0, which
    is the conservative reading: an absent measurement is not evidence of a
    passing one."""
    base = floors(baseline)
    results = []
    gaps: list[str] = []
    failed = False
    for name in RATCHETED_METRICS:
        cur = _as_int(current.get(name, 0))
        flo = base[name]
        if cur < flo:
            verdict = V_REGRESSED
            failed = True
        elif cur > flo:
            verdict = V_RISE
        elif flo == 0:
            # NOT A PASS. A floor nobody has ever met is unenforced, and an
            # unenforced floor that reads as green is how forms_confirmed sat at
            # 0 behind a PASSING gate while nine submits fired and the
            # application confirmed none of them.
            verdict = V_GAP
            gaps.append(name)
        else:
            verdict = V_OK
        results.append({"metric": name, "current": cur, "floor": flo,
                        "verdict": verdict})
    return {"results": results, "failed": failed, "gaps": gaps}


def raise_baseline(baseline: dict, current: dict) -> tuple[dict, dict]:
    """Today's proven reality becomes tomorrow's floor. RAISES ONLY.

    Returns ``(new_baseline, raised)`` where ``raised`` maps metric -> [old, new]
    for every floor that actually moved."""
    out = dict(baseline)
    raised: dict[str, list[int]] = {}
    for name in RATCHETED_METRICS:
        old = _as_int(out.get(name, 0))
        cur = _as_int(current.get(name, 0))
        new = max(old, cur)
        if new != old:
            raised[name] = [old, new]
        out[name] = new
    return out, raised


def rebaseline(baseline: dict, current: dict, reason: str,
               exploration: str = "") -> tuple[dict, dict]:
    """Set every floor to the observed value — the only path that may LOWER one.

    A metric can legitimately fall when the funnel gets BETTER (consolidating
    duplicated one-step fragments into one real journey lowers the page, flow and
    submit counts while strictly improving the result). With only "fail" and
    "silently raise" available the operator's options were to ignore a red gate
    or rubber-stamp it, and the second turns a regression detector into a
    formality on its first disagreement. So lowering demands a WRITTEN REASON,
    recorded next to the values it justifies, reviewable in git forever after."""
    if not str(reason).strip():
        raise ValueError("rebaseline requires a non-empty reason")
    out = dict(baseline)
    lowered: dict[str, list[int]] = {}
    for name in RATCHETED_METRICS:
        old = _as_int(out.get(name, 0))
        cur = _as_int(current.get(name, 0))
        if cur < old:
            lowered[name] = [old, cur]
        out[name] = cur
    out["_rebaselined"] = {
        "reason": str(reason)[:500],
        "exploration": str(exploration or ""),
        "lowered": lowered,
    }
    return out, lowered


# ══════════════════════════════════════════════════════════════════════════
#  Runtime gap state (T-GT-04 — never the git baseline)
# ══════════════════════════════════════════════════════════════════════════
def runtime_state_path(baseline_path: str) -> str:
    """Where per-host gap bookkeeping lives.

    Overridable via ``$GOLDEN_GATE_STATE`` so a CI runner or a container can put
    it on a writable volume; otherwise a dotfile beside the baseline, which is
    gitignored. It is deliberately NOT the baseline: this is mutable runtime
    state whose value is meaningless to a reviewer, and writing it into tracked
    evidence is what dirtied the tree on every gate run."""
    env = os.environ.get(STATE_ENV, "").strip()
    if env:
        return env
    return os.path.join(os.path.dirname(os.path.abspath(baseline_path)),
                        STATE_BASENAME)


def update_gaps(state: dict, gaps: Iterable[str], *, max_runs: int,
                max_days: int, today: datetime.date | None = None
                ) -> tuple[dict, list[str], list[str]]:
    """Advance the unmet-floor bookkeeping and report which floors are overdue.

    Rule 1: a GAP never counts as green.
    Rule 2: a GAP self-enforces the moment a crawl carries its data — that is the
            RISE branch in ``evaluate``, since any value above a zero floor
            raises it.
    Rule 3: a floor still unmet after ``max_runs`` runs or ``max_days`` days
            turns the gate RED. Either the capability landed and the floor should
            hold, or it did not — and that is the news.

    Returns ``(new_state, overdue_messages, young_messages)``."""
    today = today or datetime.date.today()
    current = set(gaps)
    known = dict(state.get("gaps") or {})

    # A floor that stopped being a gap has been MET — drop its history entirely,
    # so a capability that lands and later regresses is judged by the ratchet
    # (which knows its best-ever) and never by a stale unmet-since date.
    for name in list(known):
        if name not in current:
            known.pop(name, None)

    overdue: list[str] = []
    young: list[str] = []
    for name in sorted(current):
        rec = dict(known.get(name) or {})
        rec["runs"] = _as_int(rec.get("runs", 0)) + 1
        rec.setdefault("since", today.isoformat())
        known[name] = rec
        try:
            age = (today - datetime.date.fromisoformat(str(rec["since"]))).days
        except Exception:
            age = 0
        if rec["runs"] > max_runs or age > max_days:
            overdue.append(f"{name} unmet for {rec['runs']} runs / {age} days")
        else:
            young.append(f"{name} unenforced, run {rec['runs']} of {max_runs}")

    new_state = dict(state)
    new_state["gaps"] = known
    new_state["state_version"] = 1
    new_state["updated"] = today.isoformat()
    return new_state, overdue, young


def migrate_legacy_gaps(baseline_path: str, state: dict) -> tuple[dict, bool]:
    """Lift a pre-M0.4 ``_gaps`` block out of a tracked baseline, once.

    Hosts that ran the old gate have ``_gaps`` committed or sitting dirty in
    their checkout. Dropping it on the floor would reset every gap's run counter
    and silently buy an overdue floor another three runs of tolerance — so the
    history moves rather than dies. The baseline itself is NOT rewritten here:
    that would be this milestone's own bug. It is left for the operator's next
    commit, and the gate reports it."""
    baseline = load_json(baseline_path)
    legacy = baseline.get("_gaps")
    if not isinstance(legacy, dict) or not legacy:
        return state, False
    merged = dict(state.get("gaps") or {})
    for name, rec in legacy.items():
        if name not in merged and isinstance(rec, dict):
            merged[name] = rec
    out = dict(state)
    out["gaps"] = merged
    out["migrated_from_baseline"] = True
    return out, True


# ══════════════════════════════════════════════════════════════════════════
#  CLI — the shell gate's only interface to any of the above
# ══════════════════════════════════════════════════════════════════════════
def _parse_current(text: str) -> dict:
    data = json.loads(text or "{}")
    if not isinstance(data, dict):
        raise ValueError("--current must be a JSON object")
    return data


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("command",
                    choices=("evaluate", "raise", "rebaseline", "gaps",
                             "state-path"))
    ap.add_argument("--baseline", required=True)
    ap.add_argument("--current", default="{}",
                    help="JSON object of metric -> value")
    ap.add_argument("--reason", default="")
    ap.add_argument("--exploration", default="")
    ap.add_argument("--gap", action="append", default=[],
                    help="metric name that is currently an unmet floor")
    ap.add_argument("--max-runs", type=int, default=3)
    ap.add_argument("--max-days", type=int, default=7)
    args = ap.parse_args(argv)

    baseline_path = args.baseline

    if args.command == "state-path":
        print(runtime_state_path(baseline_path))
        return 0

    if args.command == "evaluate":
        baseline, status = load_baseline(baseline_path)
        if status == "corrupt":
            # Not a regression, and emphatically not a pass: the floors are
            # unreadable, so the gate has nothing to compare against.
            print("FAIL  baseline           UNREADABLE (%s)" % baseline_path)
            print("      Every floor would read 0 and every metric would 'rise' —")
            print("      a corrupt baseline must never be scored as a clean run.")
            sys.stderr.write("GAPS=\n")
            return 1
        if status == "missing":
            print("INFO  baseline           none yet — every floor starts unenforced")
        report = evaluate(_parse_current(args.current), baseline)
        for row in report["results"]:
            v = row["verdict"]
            if v == V_REGRESSED:
                print("FAIL  %-18s %s  (regressed from best %s)"
                      % (row["metric"], row["current"], row["floor"]))
            elif v == V_RISE:
                print("RISE  %-18s %s  (was %s — new floor)"
                      % (row["metric"], row["current"], row["floor"]))
            elif v == V_GAP:
                print("GAP   %-18s %s  (NOT YET ENFORCED — never achieved)"
                      % (row["metric"], row["current"]))
            else:
                print("OK    %-18s %s  (holds at %s)"
                      % (row["metric"], row["current"], row["floor"]))
        # Machine-readable tail the shell parses; humans read the block above.
        sys.stderr.write("GAPS=%s\n" % " ".join(report["gaps"]))
        return 1 if report["failed"] else 0

    if args.command == "raise":
        new, raised = raise_baseline(load_json(baseline_path),
                                     _parse_current(args.current))
        write_baseline(baseline_path, new)
        print("baseline RAISED (%s) — commit %s"
              % (", ".join(sorted(raised)) or "no floor moved",
                 os.path.basename(baseline_path)))
        return 0

    if args.command == "rebaseline":
        new, lowered = rebaseline(load_json(baseline_path),
                                  _parse_current(args.current),
                                  args.reason, args.exploration)
        write_baseline(baseline_path, new)
        print("baseline RE-BASELINED (lowered: %s)"
              % (", ".join(sorted(lowered)) or "none"))
        print("reason recorded — commit %s" % os.path.basename(baseline_path))
        return 0

    if args.command == "gaps":
        state_path = runtime_state_path(baseline_path)
        state = load_json(state_path)
        state, migrated = migrate_legacy_gaps(baseline_path, state)
        if migrated:
            sys.stderr.write(
                "INFO  lifted a legacy _gaps block out of the tracked baseline "
                "into %s — remove _gaps from the baseline and commit\n" % state_path)
        state, overdue, young = update_gaps(
            state, args.gap, max_runs=args.max_runs, max_days=args.max_days)
        try:
            os.makedirs(os.path.dirname(os.path.abspath(state_path)) or ".",
                        exist_ok=True)
            write_baseline(state_path, state)
        except OSError as exc:
            # A gate that cannot persist its bookkeeping must SAY so; silently
            # forgetting resets every counter and buys an overdue floor another
            # full tolerance window on every run.
            sys.stderr.write("WARN  gap state not persisted (%s): %s\n"
                             % (state_path, exc))
        for msg in young:
            sys.stderr.write("INFO  %s\n" % msg)
        sys.stdout.write("|".join(overdue))
        return 0

    return 2  # unreachable: argparse constrains `command`


if __name__ == "__main__":
    raise SystemExit(main())
