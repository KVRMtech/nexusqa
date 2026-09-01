#!/usr/bin/env python3
"""
Nexus QA — Load Test Runner with SLA Gate

Wraps Locust execution and enforces SLA thresholds for CI/CD pipelines.
Generates HTML reports and CSV stats, then checks P95/P99/error-rate gates.

Usage:
    # Full load test (production SLA)
    python tests/load/run_load_test.py --profile load-test --host http://localhost:8080

    # Quick smoke test
    python tests/load/run_load_test.py --profile smoke-test --host http://localhost:8091

    # Custom parameters
    python tests/load/run_load_test.py --users 200 --spawn-rate 20 --run-time 10m --host http://localhost:8080

Exit codes:
    0 = load test passed SLA gates
    1 = SLA gate failed (P95 too high, error rate too high, etc.)
    2 = load test infrastructure error
"""
from __future__ import annotations

import argparse
import configparser
import csv
import json
import os
import subprocess
import sys
from pathlib import Path


def parse_duration(duration_str: str) -> str:
    """Normalize duration string for Locust CLI."""
    duration_str = duration_str.strip().lower()
    # Locust accepts: 10s, 5m, 1h
    if duration_str[-1] in ("s", "m", "h"):
        return duration_str
    # Assume seconds if no suffix
    return f"{duration_str}s"


def load_profile(config_path: Path, profile: str) -> dict:
    """Load a named profile from config.ini."""
    config = configparser.ConfigParser()
    config.read(config_path)

    if profile not in config:
        print(f"ERROR: Profile '{profile}' not found in {config_path}")
        print(f"  Available: {', '.join(config.sections())}")
        sys.exit(2)

    section = config[profile]
    return {
        "host": section.get("host", "http://localhost:8080"),
        "users": int(section.get("users", "100")),
        "spawn_rate": int(section.get("spawn_rate", "10")),
        "run_time": section.get("run_time", "5m"),
        "p95_max_ms": float(section.get("p95_max_ms", "500")),
        "p99_max_ms": float(section.get("p99_max_ms", "2000")),
        "error_rate_max_pct": float(section.get("error_rate_max_pct", "1.0")),
        "min_throughput_rps": float(section.get("min_throughput_rps", "50")),
        "html_report": section.get("html_report", "reports/load-test-report.html"),
        "csv_prefix": section.get("csv_prefix", "reports/load-test"),
    }


def run_locust(
    host: str,
    users: int,
    spawn_rate: int,
    run_time: str,
    html_report: str,
    csv_prefix: str,
    locustfile: str,
) -> int:
    """Execute Locust in headless mode and return the exit code."""
    # Ensure report directory exists
    report_dir = Path(html_report).parent
    report_dir.mkdir(parents=True, exist_ok=True)
    Path(csv_prefix).parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        sys.executable, "-m", "locust",
        "-f", locustfile,
        "--host", host,
        "--headless",
        "-u", str(users),
        "-r", str(spawn_rate),
        "-t", parse_duration(run_time),
        "--html", html_report,
        "--csv", csv_prefix,
        "--csv-full-history",
        "--only-summary",
    ]

    print("=" * 60)
    print("  NEXUS QA — LOAD TEST")
    print("=" * 60)
    print(f"  Host:       {host}")
    print(f"  Users:      {users}")
    print(f"  Spawn rate: {spawn_rate}/s")
    print(f"  Duration:   {run_time}")
    print(f"  Report:     {html_report}")
    print("=" * 60)
    print()

    result = subprocess.run(cmd, cwd=str(Path(__file__).parent.parent.parent))
    return result.returncode


def check_sla_gates(
    csv_prefix: str,
    p95_max_ms: float,
    p99_max_ms: float,
    error_rate_max_pct: float,
    min_throughput_rps: float,
) -> list[str]:
    """Check SLA thresholds against Locust CSV stats output."""
    failures: list[str] = []

    stats_file = Path(f"{csv_prefix}_stats.csv")
    if not stats_file.exists():
        return [f"Stats file not found: {stats_file}"]

    # Read the "Aggregated" row from Locust stats CSV
    aggregated = None
    with open(stats_file, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row.get("Name", "").strip() == "Aggregated":
                aggregated = row
                break

    if not aggregated:
        return ["No 'Aggregated' row found in Locust stats CSV"]

    # Extract metrics
    total_requests = int(aggregated.get("Request Count", "0"))
    total_failures = int(aggregated.get("Failure Count", "0"))
    p95 = float(aggregated.get("95%", "0") or "0")
    p99 = float(aggregated.get("99%", "0") or "0")
    rps = float(aggregated.get("Requests/s", "0") or "0")

    error_pct = (total_failures / total_requests * 100) if total_requests > 0 else 0

    print()
    print("=" * 60)
    print("  SLA GATE RESULTS")
    print("=" * 60)
    print(f"  Total Requests:  {total_requests}")
    print(f"  Total Failures:  {total_failures}")
    print(f"  Error Rate:      {error_pct:.2f}%")
    print(f"  P95 Latency:     {p95:.0f}ms")
    print(f"  P99 Latency:     {p99:.0f}ms")
    print(f"  Throughput:      {rps:.1f} req/s")
    print()

    # Check gates
    def gate(name: str, actual: float, threshold: float, unit: str, lower_is_better: bool = True):
        if lower_is_better:
            passed = actual <= threshold
        else:
            passed = actual >= threshold
        icon = "PASS" if passed else "FAIL"
        print(f"  [{icon}] {name}: {actual:.1f}{unit} (threshold: {threshold:.1f}{unit})")
        if not passed:
            failures.append(f"{name}: {actual:.1f}{unit} exceeds {threshold:.1f}{unit}")

    gate("P95 Latency", p95, p95_max_ms, "ms")
    gate("P99 Latency", p99, p99_max_ms, "ms")
    gate("Error Rate", error_pct, error_rate_max_pct, "%")
    gate("Throughput", rps, min_throughput_rps, " req/s", lower_is_better=False)

    print("=" * 60)
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run Nexus QA load tests with SLA gates",
    )
    parser.add_argument("--profile", default="load-test", help="Config profile name")
    parser.add_argument("--host", help="Override target host URL")
    parser.add_argument("--users", type=int, help="Override concurrent users")
    parser.add_argument("--spawn-rate", type=int, help="Override spawn rate")
    parser.add_argument("--run-time", help="Override run time (e.g., 5m, 30s)")
    parser.add_argument(
        "--locustfile",
        default=str(Path(__file__).parent / "locustfile.py"),
        help="Path to locustfile.py",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path(__file__).parent / "config.ini",
        help="Path to config.ini",
    )
    parser.add_argument("--skip-sla", action="store_true", help="Skip SLA gate checks")
    args = parser.parse_args()

    # Load profile defaults
    profile = load_profile(args.config, args.profile)

    # Apply CLI overrides
    host = args.host or profile["host"]
    users = args.users or profile["users"]
    spawn_rate = args.spawn_rate or profile["spawn_rate"]
    run_time = args.run_time or profile["run_time"]

    # Run Locust
    exit_code = run_locust(
        host=host,
        users=users,
        spawn_rate=spawn_rate,
        run_time=run_time,
        html_report=profile["html_report"],
        csv_prefix=profile["csv_prefix"],
        locustfile=args.locustfile,
    )

    if exit_code != 0:
        print(f"\nERROR: Locust exited with code {exit_code}")
        return 2

    # Check SLA gates
    if args.skip_sla:
        print("\n  SLA gate checks skipped (--skip-sla)")
        return 0

    failures = check_sla_gates(
        csv_prefix=profile["csv_prefix"],
        p95_max_ms=profile["p95_max_ms"],
        p99_max_ms=profile["p99_max_ms"],
        error_rate_max_pct=profile["error_rate_max_pct"],
        min_throughput_rps=profile["min_throughput_rps"],
    )

    if failures:
        print(f"\n  RESULT: SLA GATE FAILED — {len(failures)} threshold(s) exceeded")
        return 1
    else:
        print("\n  RESULT: SLA GATE PASSED")
        return 0


if __name__ == "__main__":
    sys.exit(main())
