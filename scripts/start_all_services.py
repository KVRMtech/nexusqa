"""
Nexus QA — Start all backend services for E2E testing.

Launches services as background subprocesses.

Usage:
    python scripts/start_all_services.py                  # canonical profile (9 services)
    python scripts/start_all_services.py --profile full   # all 16 services
    python scripts/start_all_services.py --stop           # kill all
"""

import subprocess
import sys
import os
import time
import signal
import json
import argparse
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VENV_PYTHON = str(ROOT / ".venv" / "Scripts" / "python.exe")
DEFAULT_OLLAMA_BASE_URL = "http://localhost:11434"
DEFAULT_EYES_OLLAMA_MODEL = "llama3.2-vision:11b"
DEFAULT_EYES_FAST_OLLAMA_MODEL = "llava:7b"
DEFAULT_HEART_LOCAL_MODEL = "llama3.2:1b"
DEFAULT_BRAIN_LOCAL_MODEL = "llama3.2:1b"

# (name, working_dir relative to ROOT, command, port)
ALL_SERVICES = [
    ("auth-service",  "platform/auth-service",               [VENV_PYTHON, "main.py"],       8000),
    ("shield",        "engines/shield-engine",                [VENV_PYTHON, "main.py"],       8001),
    ("ears",          "engines/ears-engine",                   [VENV_PYTHON, "main.py"],       8002),
    ("eyes",          "engines/eyes-engine",                   [VENV_PYTHON, "main.py"],       8003),
    ("heart",         "engines/heart-engine",                  [VENV_PYTHON, "main.py"],       8004),
    ("backbone",      "engines/backbone-engine",              [VENV_PYTHON, "main.py"],       8005),
    ("nerves",        "engines/nerves-engine",                [VENV_PYTHON, "main.py"],       8006),
    ("legs",          "engines/legs-engine",                   [VENV_PYTHON, "main.py"],       8007),
    ("hands",         "engines/hands-engine",                  [VENV_PYTHON, "main.py"],       8008),
    ("spine",         "engines/spine-engine",                  [VENV_PYTHON, "main.py"],       8009),
    ("mouth",         "engines/mouth-engine",                  [VENV_PYTHON, "main.py"],       8010),
    ("brain",         "engines/brain-engine",                  [VENV_PYTHON, "main.py"],       8011),
    ("gateway",       "platform/gateway",                      [VENV_PYTHON, "main.py"],       8080),
    ("platform-api",  "platform/api",                          [VENV_PYTHON, "main.py"],       8091),
    ("orchestrator",  "products/nexus-qa-orchestrator",       [VENV_PYTHON, "-m", "app.main"], 8100),
    ("qa-orchestrator", "products/qa-orchestrator",           [VENV_PYTHON, "main.py"],        8092),
]

# Canonical profile: only services required for canonical processing
CANONICAL_SERVICE_NAMES = {
    "auth-service", "shield", "ears", "eyes", "spine", "brain",
    "gateway", "platform-api", "orchestrator",
}

def get_services(profile: str):
    """Return service list filtered by profile."""
    if profile == "full":
        return ALL_SERVICES
    return [s for s in ALL_SERVICES if s[0] in CANONICAL_SERVICE_NAMES]

PID_FILE = ROOT / "scripts" / ".service_pids.json"


def start_all(profile: str = "canonical"):
    services = get_services(profile)
    os.makedirs(ROOT / "scripts", exist_ok=True)
    os.makedirs(ROOT / "logs", exist_ok=True)

    pids = {}
    env = os.environ.copy()
    if os.name == "nt":
        machine_path = os.environ.get("Path", "")
        user_path = subprocess.run(
            ["powershell", "-NoProfile", "-Command", "[System.Environment]::GetEnvironmentVariable('Path','User')"],
            capture_output=True,
            text=True,
            creationflags=subprocess.CREATE_NO_WINDOW,
        ).stdout.strip()
        if user_path:
            env["Path"] = machine_path + os.pathsep + user_path
    env["PYTHONPATH"] = str(ROOT / "sdk" / "nexus-sdk") + os.pathsep + env.get("PYTHONPATH", "")
    env["NEXUS_ENV"] = "development"
    env["JWT_SECRET"] = "test-secret-do-not-use-in-production"
    env["NEXUS_JWT_SECRET"] = "test-secret-do-not-use-in-production"
    env["REDIS_HOST"] = "localhost"
    env["REDIS_PORT"] = "6379"
    env["POSTGRES_HOST"] = "localhost"
    env["POSTGRES_PORT"] = "5432"
    env["POSTGRES_USER"] = "nexus"
    env["POSTGRES_PASSWORD"] = "nexus-dev"
    env["POSTGRES_DB"] = "nexus"
    env["NEO4J_URI"] = "bolt://localhost:7687"
    env["NEO4J_USER"] = "neo4j"
    env["NEO4J_PASSWORD"] = "nexus-neo4j-dev"
    env["GATEWAY_MAX_REQUEST_BODY_MB"] = "500"
    env["MAX_REQUEST_BODY_MB"] = "500"
    env["LLM_BACKEND"] = "ollama"
    env.setdefault("OLLAMA_BASE_URL", DEFAULT_OLLAMA_BASE_URL)
    env.setdefault("EYES_OLLAMA_BASE_URL", env["OLLAMA_BASE_URL"])
    env.setdefault("EYES_OLLAMA_MODEL", DEFAULT_EYES_OLLAMA_MODEL)
    env.setdefault("EYES_FAST_OLLAMA_MODEL", DEFAULT_EYES_FAST_OLLAMA_MODEL)
    # Use per-engine local model settings instead of a single global OLLAMA_MODEL.
    env.setdefault("HEART_TIER3_PROVIDER", "ollama")
    env.setdefault("HEART_TIER3_OLLAMA_BASE_URL", env["OLLAMA_BASE_URL"])
    env.setdefault("HEART_TIER3_MODEL", DEFAULT_HEART_LOCAL_MODEL)
    env.setdefault("HEART_OLLAMA_BASE_URL", env["OLLAMA_BASE_URL"])
    env.setdefault("HEART_OLLAMA_MODEL", env["HEART_TIER3_MODEL"])
    env.setdefault("HEART_TIER3_TIMEOUT", "600")
    env.setdefault("HEART_TIER3_MAX_RETRIES", "1")
    env["LLM_MAX_TOKENS"] = "2048"
    env.setdefault("BRAIN_TIER3_PROVIDER", "ollama")
    env.setdefault("BRAIN_TIER3_OLLAMA_BASE_URL", env["OLLAMA_BASE_URL"])
    env.setdefault("BRAIN_TIER3_MODEL", DEFAULT_BRAIN_LOCAL_MODEL)
    env.setdefault("BRAIN_OLLAMA_BASE_URL", env["OLLAMA_BASE_URL"])
    env.setdefault("BRAIN_OLLAMA_MODEL", env["BRAIN_TIER3_MODEL"])
    env["NEXUS_PRODUCT_MODE"] = profile

    print(f"\n  Profile: {profile} ({len(services)} services)")
    for name, cwd_rel, cmd, port in services:
        cwd = str(ROOT / cwd_rel)
        log_file = ROOT / "logs" / f"{name}.log"

        print(f"  Starting {name:16s} on :{port} ... ", end="", flush=True)
        try:
            log_fh = open(log_file, "w")
            proc = subprocess.Popen(
                cmd,
                cwd=cwd,
                env=env,
                stdout=log_fh,
                stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
            )
            pids[name] = {"pid": proc.pid, "port": port}
            print(f"PID {proc.pid}")
        except Exception as e:
            print(f"FAILED: {e}")

    # Save PIDs for later cleanup
    with open(PID_FILE, "w") as f:
        json.dump(pids, f, indent=2)

    print(f"\n  {len(pids)} services launched. PIDs saved to {PID_FILE}")
    print("  Waiting 8 seconds for startup...")
    time.sleep(8)

    # Health check
    print("\n  Health check:")
    try:
        import httpx
        for name, info in pids.items():
            port = info["port"]
            try:
                r = httpx.get(f"http://localhost:{port}/health", timeout=3)
                status = "UP" if r.status_code == 200 else f"HTTP {r.status_code}"
            except Exception:
                status = "DOWN"
            print(f"    {name:16s} :{port}  {status}")
    except ImportError:
        print("    (httpx not installed, skipping health check)")


def stop_all():
    if not PID_FILE.exists():
        print("  No PID file found.")
        return
    with open(PID_FILE) as f:
        pids = json.load(f)

    for name, info in pids.items():
        pid = info["pid"]
        try:
            if os.name == "nt":
                result = subprocess.run(
                    ["taskkill", "/PID", str(pid), "/T", "/F"],
                    capture_output=True,
                    text=True,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
                if result.returncode not in (0, 128):
                    detail = (result.stderr or result.stdout).strip()
                    raise RuntimeError(detail or f"taskkill failed with code {result.returncode}")
            else:
                os.kill(pid, signal.SIGTERM)
            print(f"  Stopped {name} (PID {pid})")
        except ProcessLookupError:
            print(f"  {name} (PID {pid}) already stopped")
        except Exception as e:
            print(f"  Failed to stop {name}: {e}")

    PID_FILE.unlink(missing_ok=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--stop", action="store_true", help="Stop all services")
    parser.add_argument("--profile", choices=["canonical", "full"], default="canonical",
                        help="Service profile (default: canonical)")
    args = parser.parse_args()

    if args.stop:
        stop_all()
    else:
        start_all(profile=args.profile)
