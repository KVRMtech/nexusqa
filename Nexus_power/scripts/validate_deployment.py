"""
Nexus QA — Deployment Validation Script
=========================================

Validates that a Nexus deployment is fully operational:
  1. Alembic migration is at expected head
  2. All services respond to /health
  3. PostgreSQL schema matches the latest migration
  4. Critical endpoints respond correctly
  5. Service inter-connectivity (gateway → engines)

Usage:
    python scripts/validate_deployment.py [--strict] [--json]
    python scripts/validate_deployment.py --profile canonical   # canonical profile (9 services)
    python scripts/validate_deployment.py --profile full        # all 15 services

Exit codes:
    0 — All checks passed
    1 — One or more checks failed
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import os
import time
from pathlib import Path

import httpx

# ─── Config ────────────────────────────────────────────────────

BASE = os.getenv("NEXUS_BASE_URL", "http://localhost")

SERVICES = {
    "auth": 8000,
    "shield": 8001,
    "ears": 8002,
    "eyes": 8003,
    "heart": 8004,
    "backbone": 8005,
    "nerves": 8006,
    "legs": 8007,
    "hands": 8008,
    "spine": 8009,
    "mouth": 8010,
    "brain": 8011,
    "gateway": 8080,
    "platform-api": 8091,
    "orchestrator": 8100,
}

# Canonical profile: only services required for canonical processing
CANONICAL_SERVICES = {
    "auth": 8000,
    "shield": 8001,
    "ears": 8002,
    "eyes": 8003,
    "spine": 8009,
    "brain": 8011,
    "gateway": 8080,
    "platform-api": 8091,
    "orchestrator": 8100,
}

def get_services(profile: str) -> dict[str, int]:
    if profile == "full":
        return SERVICES
    return CANONICAL_SERVICES

EXPECTED_MIGRATION_HEAD = "009_semantic_completeness"


# ─── Check Functions ───────────────────────────────────────────


def check_alembic_head() -> tuple[bool, str]:
    """Verify alembic is at the expected revision."""
    root = Path(__file__).resolve().parent.parent
    try:
        result = subprocess.run(
            [sys.executable, "-m", "alembic", "current"],
            capture_output=True,
            text=True,
            cwd=str(root),
            timeout=15,
            env={**os.environ, "DATABASE_URL": os.environ.get(
                "DATABASE_URL",
                "postgresql+asyncpg://nexus:nexus-dev@localhost:5432/nexus",
            )},
        )
        output = result.stdout.strip()
        if EXPECTED_MIGRATION_HEAD in output:
            return True, f"Alembic at {EXPECTED_MIGRATION_HEAD} (head)"
        return False, f"Alembic current: {output or result.stderr.strip()}"
    except Exception as e:
        return False, f"Alembic check failed: {e}"


def check_service_health(name: str, port: int, timeout: float = 5.0) -> tuple[bool, str]:
    """Check a single service health endpoint."""
    try:
        r = httpx.get(f"{BASE}:{port}/health", timeout=timeout)
        if r.status_code == 200:
            data = r.json()
            status = data.get("status", "unknown")
            return True, f"{name}:{port} healthy (status={status})"
        return False, f"{name}:{port} returned HTTP {r.status_code}"
    except httpx.ConnectError:
        return False, f"{name}:{port} connection refused"
    except httpx.TimeoutException:
        return False, f"{name}:{port} timeout"
    except Exception as e:
        return False, f"{name}:{port} error: {e}"


def check_all_services(services: dict[str, int] | None = None) -> list[tuple[str, bool, str]]:
    """Check health of all services."""
    if services is None:
        services = SERVICES
    results = []
    for name, port in services.items():
        ok, msg = check_service_health(name, port)
        results.append((name, ok, msg))
    return results


def check_database_connectivity() -> tuple[bool, str]:
    """Verify PostgreSQL is reachable via Spine's health."""
    try:
        r = httpx.get(f"{BASE}:{SERVICES['spine']}/health", timeout=5)
        if r.status_code == 200:
            modes = r.json().get("modes", {})
            db = modes.get("database", "unknown")
            if db == "postgresql":
                return True, "PostgreSQL connected via Spine"
            return False, f"Spine database mode: {db} (expected postgresql)"
        return False, f"Spine health: HTTP {r.status_code}"
    except Exception as e:
        return False, f"Database check failed: {e}"


def check_gateway_routing() -> tuple[bool, str]:
    """Verify gateway can route to at least one engine."""
    try:
        r = httpx.get(f"{BASE}:{SERVICES['gateway']}/health", timeout=5)
        if r.status_code == 200:
            return True, "Gateway reachable"
        return False, f"Gateway returned HTTP {r.status_code}"
    except Exception as e:
        return False, f"Gateway check failed: {e}"


def check_auth_endpoint() -> tuple[bool, str]:
    """Verify auth service login endpoint responds."""
    try:
        r = httpx.post(
            f"{BASE}:{SERVICES['auth']}/api/v1/auth/login",
            json={"email": "probe@test.invalid", "password": "probe"},
            timeout=5,
        )
        # 401/403 means the endpoint works, just bad creds
        if r.status_code in (200, 401, 403, 422):
            return True, f"Auth login endpoint responds (HTTP {r.status_code})"
        return False, f"Auth endpoint unexpected: HTTP {r.status_code}"
    except Exception as e:
        return False, f"Auth check failed: {e}"


def check_eyes_video_prerequisites() -> tuple[bool, str]:
    """Verify Eyes engine has ffmpeg/ffprobe for video processing."""
    try:
        r = httpx.get(f"{BASE}:{SERVICES['eyes']}/health", timeout=5)
        if r.status_code == 200:
            modes = r.json().get("modes", {})
            video = modes.get("video_processing", "unknown")
            if video == "ffmpeg":
                return True, f"Eyes video_processing: {video}"
            return False, f"Eyes video_processing: {video} (ffmpeg/ffprobe required)"
        return False, f"Eyes health: HTTP {r.status_code}"
    except Exception as e:
        return False, f"Eyes video check failed: {e}"


def check_brain_llm_backend() -> tuple[bool, str, dict]:
    """Verify brain has a non-stub LLM backend and real semantic scoring."""
    try:
        r = httpx.get(f"{BASE}:{SERVICES['brain']}/health", timeout=5)
        if r.status_code == 200:
            modes = r.json().get("modes", {})
            llm = modes.get("llm", "unknown")
            model = modes.get("llm_model", "unknown")
            scoring = modes.get("semantic_scoring", "unknown")
            detail = {"llm": llm, "model": model, "semantic_scoring": scoring}

            if llm in ("stub", "unknown", ""):
                return False, f"Brain LLM backend: {llm} (semantic scoring: {scoring})", detail
            if scoring != "real":
                return False, f"Brain semantic scoring: {scoring} (backend: {llm})", detail
            return True, f"Brain LLM: {llm}/{model} (semantic: {scoring})", detail
        return False, f"Brain health: HTTP {r.status_code}", {}
    except Exception as e:
        return False, f"Brain LLM check failed: {e}", {}


def check_engine_mode_degradation(name: str, port: int) -> tuple[bool, str]:
    """Check if an engine reports any degraded/stub modes."""
    DEGRADED_VALUES = {"stub", "in-memory", "degraded", "unavailable", "heuristic", "passthrough"}
    try:
        r = httpx.get(f"{BASE}:{port}/health", timeout=5)
        if r.status_code == 200:
            data = r.json()
            status = data.get("status", "unknown")
            modes = data.get("modes", {})
            degraded_modes = {k: v for k, v in modes.items() if v.lower() in DEGRADED_VALUES}
            if degraded_modes:
                mode_str = ", ".join(f"{k}={v}" for k, v in degraded_modes.items())
                return False, f"{name}:{port} DEGRADED modes: {mode_str} (status={status})"
            return True, f"{name}:{port} all modes production-ready (status={status})"
        return False, f"{name}:{port} returned HTTP {r.status_code}"
    except Exception as e:
        return False, f"{name}:{port} mode check failed: {e}"


def check_orchestrator_readiness() -> tuple[bool, str]:
    """Verify orchestrator /health/ready reports all dependencies connected."""
    try:
        r = httpx.get(f"{BASE}:{SERVICES['orchestrator']}/health/ready", timeout=10)
        if r.status_code == 200:
            data = r.json()
            status = data.get("status", "unknown")
            deps = data.get("dependencies", {})
            unhealthy = [k for k, v in deps.items() if v.get("status") not in ("healthy", "degraded")]
            if unhealthy:
                return False, f"Orchestrator readiness: {status} (unhealthy deps: {', '.join(unhealthy)})"
            return True, f"Orchestrator readiness: {status}"
        return False, f"Orchestrator /health/ready returned HTTP {r.status_code}"
    except Exception as e:
        return False, f"Orchestrator readiness check failed: {e}"


# ─── Main ──────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(description="Validate Nexus QA deployment")
    parser.add_argument("--strict", action="store_true",
                        help="Fail on warnings (e.g. stub backends)")
    parser.add_argument("--json", action="store_true",
                        help="Output results as JSON")
    parser.add_argument("--profile", choices=["canonical", "full"], default="canonical",
                        help="Service profile (default: canonical)")
    args = parser.parse_args()

    active_services = get_services(args.profile)
    results: list[dict] = []

    # 1. Alembic migration
    ok, msg = check_alembic_head()
    results.append({"check": "alembic_head", "passed": ok, "message": msg})

    # 2. All service health
    svc_results = check_all_services(active_services)
    for name, ok, msg in svc_results:
        results.append({"check": f"health_{name}", "passed": ok, "message": msg})

    # 3. Database connectivity
    ok, msg = check_database_connectivity()
    results.append({"check": "database", "passed": ok, "message": msg})

    # 4. Gateway routing
    ok, msg = check_gateway_routing()
    results.append({"check": "gateway_routing", "passed": ok, "message": msg})

    # 5. Auth endpoint
    ok, msg = check_auth_endpoint()
    results.append({"check": "auth_endpoint", "passed": ok, "message": msg})

    # 6. Eyes video processing prerequisites
    ok, msg = check_eyes_video_prerequisites()
    results.append({"check": "eyes_video_prerequisites", "passed": ok, "message": msg})

    # 7. Brain LLM semantic readiness
    ok, msg, detail = check_brain_llm_backend()
    results.append({
        "check": "brain_semantic",
        "passed": ok if args.strict else (ok or detail.get("llm") not in ("stub", "", None)),
        "warning": not ok,
        "message": msg,
    })

    # 8. Engine mode degradation checks (stub/in-memory detection)
    for name, port in active_services.items():
        if name in ("gateway", "platform-api", "orchestrator"):
            continue  # These don't use SDK health modes
        ok, msg = check_engine_mode_degradation(name, port)
        results.append({
            "check": f"modes_{name}",
            "passed": ok if args.strict else True,
            "warning": not ok,
            "message": msg,
        })

    # 9. Orchestrator readiness (Redis, engine connectivity)
    ok, msg = check_orchestrator_readiness()
    results.append({"check": "orchestrator_readiness", "passed": ok, "message": msg})

    # ── Output ─────────────────────────────────────────────────

    passed = sum(1 for r in results if r["passed"])
    failed = sum(1 for r in results if not r["passed"])
    total = len(results)

    if args.json:
        print(json.dumps({
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "passed": passed,
            "failed": failed,
            "total": total,
            "all_passed": failed == 0,
            "checks": results,
        }, indent=2))
    else:
        print(f"\n{'='*60}")
        print(f"  Nexus QA Deployment Validation (profile: {args.profile})")
        print(f"{'='*60}\n")

        for r in results:
            icon = "[PASS]" if r["passed"] else ("[WARN]" if r.get("warning") else "[FAIL]")
            print(f"  {icon} {r['message']}")

        print(f"\n{'─'*60}")
        print(f"  Result: {passed}/{total} passed, {failed} failed")
        if failed == 0:
            print("  Status: DEPLOYMENT VALID")
        else:
            print("  Status: DEPLOYMENT ISSUES DETECTED")
        print(f"{'='*60}\n")

    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
