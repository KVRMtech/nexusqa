"""TIER-5.1 runnable entry — the induced-drift Auto-Heal benchmark.

Drives the REAL Auto-Heal endpoint once per Aegis ``?break=`` mode and prints the
measured FALSE-HEAL RATE (cardinal), correct-refuse rate, per-mode pass, and an
ECE-style calibration. $0, deterministic; MEASURES, never mutates the heal.

The VM is stopped during code-gen — run this LATER, from inside the platform-api
container (so ``app.services...`` imports resolve and the Aegis URL is
runner-reachable). Example:

    sudo docker exec -e NEXUS_BENCH_TOKEN="$JWT" nexus-platform-api \\
        python -m scripts.run_induced_drift_benchmark \\
            --api-base http://localhost:8091 \\
            --artifact-id <aegis-artifact-id> \\
            --aegis-base http://nexus-aegis

Prereq: generate a test suite for ``<aegis-artifact-id>`` against Aegis ONCE first
(POST .../generate), so the loop has a suite to run per mode.

Token may be passed via --token or the NEXUS_BENCH_TOKEN env var (preferred — keeps
the JWT out of the process list).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

# Allow `python scripts/run_induced_drift_benchmark.py` as well as `-m scripts...`.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "platform", "api"))

from app.services.test_factory.induced_drift_benchmark import (  # noqa: E402
    DEFAULT_MODES,
    run_benchmark,
)


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Aegis induced-drift Auto-Heal benchmark")
    p.add_argument("--api-base", default=os.getenv("NEXUS_BENCH_API_BASE", "http://localhost:8091"))
    p.add_argument("--artifact-id", required=True)
    p.add_argument("--token", default=os.getenv("NEXUS_BENCH_TOKEN", ""))
    p.add_argument("--aegis-base", default=os.getenv("NEXUS_BENCH_AEGIS_BASE", "http://nexus-aegis"))
    p.add_argument("--modes", default=",".join(DEFAULT_MODES),
                   help="comma-separated break modes (default: all with a known outcome)")
    p.add_argument("--poll-interval", type=float, default=3.0)
    p.add_argument("--poll-max", type=float, default=600.0)
    p.add_argument("--json", action="store_true", help="emit the raw report JSON only")
    return p.parse_args(argv)


def _print_human(report: dict) -> None:
    print("\n=== INDUCED-DRIFT AUTO-HEAL BENCHMARK ===")
    print(f"artifact: {report.get('artifact_id')}   aegis: {report.get('aegis_base')}")
    print(f"modes run/scored: {report.get('modes_run')}/{report.get('modes_scored')}")
    fhr = report.get("false_heal_rate")
    crr = report.get("correct_refuse_rate")
    pr = report.get("pass_rate")
    print(f"\nFALSE-HEAL RATE   : {('%.1f%%' % (fhr * 100)) if fhr is not None else 'n/a'}"
          f"   (cardinal — must be 0%)   false-heals: {report.get('false_heals') or 'none'}")
    print(f"CORRECT-REFUSE    : {('%.1f%%' % (crr * 100)) if crr is not None else 'n/a'}")
    print(f"OVERALL PASS RATE : {('%.1f%%' % (pr * 100)) if pr is not None else 'n/a'}")
    cal = report.get("calibration") or {}
    print(f"\nCALIBRATION (ECE) : {cal.get('ece')}  (n={cal.get('n')})")
    print("\nper-mode:")
    for m in report.get("per_mode", []):
        flag = "  *** FALSE HEAL ***" if m.get("false_heal") else ("  OK" if m.get("passed") else "  MISS")
        print(f"  {m['mode']:<14} expect={m['expected']:<13} got={str(m['terminal_state']):<13}{flag}")
        if m.get("stop_reason"):
            print(f"       stop_reason: {m['stop_reason'][:140]}")
        if m.get("error"):
            print(f"       error: {m['error'][:140]}")


async def _main(argv: list[str]) -> int:
    args = _parse_args(argv)
    if not args.token:
        print("ERROR: no token (pass --token or set NEXUS_BENCH_TOKEN)", file=sys.stderr)
        return 2
    modes = tuple(m.strip() for m in args.modes.split(",") if m.strip())
    report = await run_benchmark(
        api_base=args.api_base, artifact_id=args.artifact_id, token=args.token,
        aegis_base=args.aegis_base, modes=modes,
        poll_interval_s=args.poll_interval, poll_max_s=args.poll_max,
    )
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        _print_human(report)
    # Non-zero exit if ANY false heal occurred — the cardinal failure (CI-gateable).
    return 1 if (report.get("false_heals")) else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main(sys.argv[1:])))
