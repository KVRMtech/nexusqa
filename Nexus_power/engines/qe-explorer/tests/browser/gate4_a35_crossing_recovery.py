"""A35 — the write-ahead crossing journal, proven against a REAL process kill.

WHAT WAS ALREADY PROVEN, AND WHAT WAS NOT
=========================================
``tests/test_resume_crossing_journal_m34.py`` proves the journal's logic against
a scripted browser, and simulates the crash by TRUNCATING the manifest after the
reserved record. That is a faithful model of the byte-level state a SIGKILL
leaves behind, and it is a good test.

It is not, however, evidence about a real failure, for three reasons:

  1. nothing is actually killed — the process that writes the journal is the
     same one that reads it back, so an in-memory ledger that never flushed
     would still pass;
  2. the "application" is a fixture whose submit button does nothing, so
     "did not submit twice" is unfalsifiable there;
  3. truncation is the crash shape the author EXPECTED. A real kill can land
     anywhere, including inside the HTTP request the crossing performs.

This harness fixes all three. A real Chromium crawl, in a real child process,
is SIGKILLed the instant the write-ahead record appears on disk. The
application is ``proving-grounds/crossing-ledger``, a real HTTP server that
records every bind and deliberately does NOT deduplicate, so the count it
reports is a measurement of the crawler and of nothing else.

THE ASSERTION THAT MATTERS
==========================
Not "the crawler says it refused". The SERVER's ledger must hold at most ONE
bind after: crossing -> kill -> restart -> resume -> crawl completes.

WHY "AT MOST ONE" AND NOT "EXACTLY ONE"
=======================================
The kill is deliberately racy — that is what fault injection means. It can land
before the POST is issued (ledger 0) or after the server has recorded it but
before the browser sees the response (ledger 1). Both are legitimate crash
states and the harness records which one occurred. What must NEVER happen, in
either case, is the resumed crawl adding a second bind: with 0 it must not
re-cross a boundary it already spent, and with 1 it must not "retry the request
that never completed". The delta across the resume is therefore the real
assertion, and it must be ZERO.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:                                            # pragma: no cover
    pass

_HERE = Path(__file__).resolve()
SERVICE_ROOT = _HERE.parents[2]                  # …/engines/qe-explorer
NEXUS_ROOT = SERVICE_ROOT.parents[1]             # …/Nexus_power
APP_SERVER = NEXUS_ROOT / "proving-grounds" / "crossing-ledger" / "server.py"

BOUNDARY_CONTROL = "Bind policy"
CRAWL_ID = "gate4-a35-crossing"
TENANT_ID = "gate4-a35"


def ledger(port: int) -> dict:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/_ledger", timeout=10) as r:
        return json.loads(r.read().decode())


def wait_http(port: int, timeout_s: int = 30) -> None:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            ledger(port)
            return
        except Exception:
            time.sleep(0.5)
    raise RuntimeError(f"crossing-ledger never answered on {port}")


def crossing_records(manifest: Path) -> list[dict]:
    """Every crossing record currently durable in the manifest."""
    if not manifest.exists():
        return []
    out = []
    for line in manifest.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue          # a torn last line is exactly what a SIGKILL leaves
        if rec.get("type") == "crossing":
            out.append(rec)
    return out


def run_crawl(work_dir: Path, url: str, *, resume: bool,
              kill_on_reserved: bool, timeout_s: int = 240,
              kill_on_bind_port: int = 0) -> dict:
    """Run ONE crawl in a child process; optionally SIGKILL it mid-crossing.

    The crawl runs as a separate OS process precisely so it can be killed
    without unwinding — no atexit hooks, no finally blocks, no flush. That is
    what makes this a crash rather than a shutdown.
    """
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SERVICE_ROOT)
    env["PYTHONUNBUFFERED"] = "1"
    env["A35_WORK_DIR"] = str(work_dir)
    env["A35_URL"] = url
    env["A35_RESUME"] = "1" if resume else "0"
    proc = subprocess.Popen(
        [sys.executable, "-u", str(_HERE.parent / "gate4_a35_child.py")],
        cwd=str(SERVICE_ROOT), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

    manifest = work_dir / CRAWL_ID / "manifest.jsonl"
    killed_at = None
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        # THE DANGEROUS CRASH SHAPE. Killing on the reserved record always lands
        # BEFORE the request leaves the browser, which is the easy half of the
        # problem. Killing the moment the SERVER records the bind lands inside
        # the response delay: the irreversible effect has happened and the
        # crawler will never learn that it did. That is the state a naive
        # "retry what did not complete" turns into a second policy.
        if kill_on_bind_port:
            try:
                if ledger(kill_on_bind_port)["binds"] >= 1:
                    proc.kill()
                    killed_at = {"trigger": "server recorded the bind",
                                 "status": "in_flight"}
                    break
            except Exception:
                pass
        if kill_on_reserved:
            recs = crossing_records(manifest)
            reserved = [r for r in recs if r.get("status") == "reserved"]
            if reserved:
                # THE FAULT INJECTION. SIGKILL (TerminateProcess on Windows) —
                # never SIGTERM, which the runtime could catch and flush.
                proc.kill()
                killed_at = reserved[0]
                break
        if proc.poll() is not None:
            break
        time.sleep(0.05)

    try:
        out, _ = proc.communicate(timeout=45)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, _ = proc.communicate(timeout=30)
    return {"returncode": proc.returncode, "killed_record": killed_at,
            "output_tail": (out or "")[-1500:]}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8710)
    ap.add_argument("--out", default="")
    ap.add_argument("--bind-delay-ms", type=int, default=4000)
    args = ap.parse_args()

    work_dir = Path(os.environ.get("A35_ROOT") or (SERVICE_ROOT / ".gate4_a35"))
    shutil.rmtree(work_dir, ignore_errors=True)
    work_dir.mkdir(parents=True, exist_ok=True)
    ledger_path = work_dir / "crossing_ledger.jsonl"

    env = dict(os.environ)
    env["CROSSING_BIND_DELAY_MS"] = str(args.bind_delay_ms)
    app = subprocess.Popen(
        [sys.executable, "-u", str(APP_SERVER), "--port", str(args.port),
         "--ledger", str(ledger_path)],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    findings: dict = {"milestone": "A35"}
    try:
        wait_http(args.port)
        url = f"http://127.0.0.1:{args.port}/"
        assert ledger(args.port)["binds"] == 0, "the ledger did not start empty"

        # ── PHASE 0 — THE CONTROL. Without it this whole harness is vacuous. ──
        #
        # The first run of A35 reported "zero double-submits" with a ledger of
        # 0 binds in BOTH runs. That is not exactly-once semantics; it is a
        # crawl that never submitted at all, and the assertion "at most one"
        # was satisfied by nothing ever happening. A kill test whose subject
        # never performs the action cannot fail, which makes it worthless.
        #
        # So an UNKILLED crawl must first bind, and the ledger must count it.
        # Only then does a later zero mean the journal suppressed something.
        control_dir = work_dir / "control"
        control_dir.mkdir(parents=True, exist_ok=True)
        control = run_crawl(control_dir, url, resume=False, kill_on_reserved=False)
        control_ledger = ledger(args.port)
        findings["control"] = {
            "returncode": control["returncode"],
            "binds": control_ledger["binds"],
            "entries": control_ledger["entries"],
            "output_tail": control["output_tail"],
        }
        if control_ledger["binds"] < 1:
            findings["verdict"] = (
                "INVALID - an unkilled crawl bound ZERO policies, so the "
                "boundary is not reachable in this configuration. Every "
                "'no double submit' result below would be satisfied by the "
                "crawl never submitting, which proves nothing.")
            return _emit(findings, args.out, 3)
        # More than one bind from a SINGLE crawl holding a max_crossings=1 grant
        # is itself a finding, and it must not be silently absorbed.
        findings["control"]["exceeded_grant"] = control_ledger["binds"] > 1

        # Reset the ledger so the fault-injection phases start from zero.
        urllib.request.urlopen(f"http://127.0.0.1:{args.port}/_reset",
                               timeout=10).read()
        assert ledger(args.port)["binds"] == 0

        # ── RUN 1 — crawl until the crossing is journalled, then KILL ───────
        run1 = run_crawl(work_dir, url, resume=False, kill_on_reserved=True)
        after_kill = ledger(args.port)
        manifest = work_dir / CRAWL_ID / "manifest.jsonl"
        recs1 = crossing_records(manifest)

        findings["run1"] = {
            "killed": run1["killed_record"] is not None,
            "kill_landed_on": run1["killed_record"],
            "returncode": run1["returncode"],
            "crossing_records_durable": recs1,
            "ledger_binds_after_kill": after_kill["binds"],
        }
        if run1["killed_record"] is None:
            findings["verdict"] = (
                "INVALID - the crawl never journalled a reserved crossing, so "
                "nothing was killed mid-crossing and the resume below proves "
                "nothing about exactly-once.")
            findings["run1"]["output_tail"] = run1["output_tail"]
            return _emit(findings, args.out, 3)

        # THE WRITE-AHEAD PROPERTY, stated as an assertion rather than assumed:
        # the reservation is on disk even though the process was killed and
        # never got to write an outcome.
        findings["write_ahead_durable"] = bool(
            [r for r in recs1 if r.get("status") == "reserved"])

        # ── RUN 2 — RESUME, same crawl id, same work dir ────────────────────
        run2 = run_crawl(work_dir, url, resume=True, kill_on_reserved=False)
        after_resume = ledger(args.port)
        recs2 = crossing_records(manifest)

        delta = after_resume["binds"] - after_kill["binds"]
        refused = [r for r in recs2 if r.get("status") == "refused"]
        findings["run2"] = {
            "returncode": run2["returncode"],
            "completed": run2["returncode"] == 0,
            "crossing_records_after_resume": recs2,
            "refusals": refused,
            "ledger_binds_after_resume": after_resume["binds"],
            "output_tail": run2["output_tail"],
        }
        findings["ledger"] = {
            "binds_after_kill": after_kill["binds"],
            "binds_after_resume": after_resume["binds"],
            "delta_across_resume": delta,
            "entries": after_resume["entries"],
        }
        control_binds = findings["control"]["binds"]
        # ── SCENARIO B — kill AFTER the server has recorded the bind ───────
        # Fresh work dir and a fresh ledger, so this scenario stands alone.
        urllib.request.urlopen(f"http://127.0.0.1:{args.port}/_reset",
                               timeout=10).read()
        work_b = work_dir / "scenario_b"
        work_b.mkdir(parents=True, exist_ok=True)
        runb1 = run_crawl(work_b, url, resume=False, kill_on_reserved=False,
                          kill_on_bind_port=args.port)
        b_after_kill = ledger(args.port)
        runb2 = run_crawl(work_b, url, resume=True, kill_on_reserved=False)
        b_after_resume = ledger(args.port)
        b_delta = b_after_resume["binds"] - b_after_kill["binds"]
        findings["scenario_b"] = {
            "description": ("process killed while the bind response was still "
                            "in flight - the effect had already landed"),
            "killed": runb1["killed_record"] is not None,
            "binds_after_kill": b_after_kill["binds"],
            "binds_after_resume": b_after_resume["binds"],
            "delta_across_resume": b_delta,
            "entries": b_after_resume["entries"],
            "resume_completed": runb2["returncode"] == 0,
            "verdict": ("PASS - the effect had landed, the crawler never saw "
                        "the outcome, and the resume did NOT bind again"
                        if (b_delta == 0 and b_after_kill["binds"] == 1
                            and b_after_resume["binds"] == 1)
                        else f"FAIL - after_kill={b_after_kill['binds']} "
                             f"after_resume={b_after_resume['binds']} "
                             f"delta={b_delta}"),
        }

        ok = ("FAIL" not in findings["scenario_b"]["verdict"]
              and delta == 0 and after_resume["binds"] <= 1
              and findings["write_ahead_durable"] and control_binds >= 1)
        findings["crash_shape"] = (
            "kill landed AFTER the server recorded the bind - the effect "
            "happened and the crawler never learned it did"
            if after_kill["binds"] == 1 else
            "kill landed BEFORE the request reached the server - the boundary "
            "was spent in the journal but never actuated")
        findings["verdict"] = (
            "PASS - the crossing was journalled before the click, the process "
            "was killed mid-crossing, the resumed crawl inherited the journal "
            "and did NOT repeat the crossing: zero double-submits at the "
            "application." if ok else
            f"FAIL - delta={delta} total_binds={after_resume['binds']} "
            f"write_ahead_durable={findings['write_ahead_durable']}")
        return _emit(findings, args.out, 0 if ok else 1)
    finally:
        app.kill()


def _emit(findings: dict, out: str, code: int) -> int:
    print("\n=== A35 - write-ahead crossing journal, real kill ===")
    r1 = findings.get("run1", {})
    print(f"  run 1: killed={r1.get('killed')} "
          f"reserved_record={bool(r1.get('kill_landed_on'))} "
          f"ledger_after_kill={r1.get('ledger_binds_after_kill')}")
    print(f"  write-ahead record survived the kill: "
          f"{findings.get('write_ahead_durable')}")
    r2 = findings.get("run2", {})
    print(f"  run 2 (resume): completed={r2.get('completed')} "
          f"refusals={len(r2.get('refusals') or [])} "
          f"ledger_after_resume={r2.get('ledger_binds_after_resume')}")
    led = findings.get("ledger", {})
    print(f"  DELTA ACROSS RESUME: {led.get('delta_across_resume')} "
          f"(must be 0)")
    b = findings.get("scenario_b")
    if b:
        print("  -- scenario B: killed with the bind ALREADY recorded --")
        print(f"     binds after kill={b['binds_after_kill']} "
              f"after resume={b['binds_after_resume']} delta={b['delta_across_resume']}")
        print(f"     {b['verdict']}")
    print(f"  VERDICT: {findings.get('verdict')}")
    if out:
        Path(out).write_text(json.dumps(findings, indent=2), encoding="utf-8")
        print(f"\nevidence -> {out}")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
