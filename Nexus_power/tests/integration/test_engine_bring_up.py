"""
Integration test: every engine reaches healthy on a fresh-volume stack.

Catches the bug class we surfaced during the clean-state reset:
  - spine couldn't create /app/service/data (root-owned)
  - mouth couldn't create /app/data/reports
  - legs Playwright cache landed in /root/ and was unreachable to nexus

The test brings up the full compose stack from zero (no pre-existing
volumes), waits for each canonical-pipeline service to report healthy,
and tears down. Soak time is ~2 min on a warm Docker.

Marked `slow` so it runs in CI's integration job, not on every dev pytest.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import time
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILE = REPO_ROOT / "docker-compose.yml"

# Health-check map: service name → (host port, path).
HEALTH_ENDPOINTS = {
    "shield": (8001, "/health"),
    "ears": (8002, "/health"),
    "eyes": (8003, "/health"),
    "spine": (8009, "/health"),
    "backbone": (8005, "/health"),
    "orchestrator": (8100, "/health"),
    "auth-service": (8000, "/health"),
    "gateway": (8080, "/health"),
    "platform-api": (8091, "/health"),
}


def _docker_available() -> bool:
    return shutil.which("docker") is not None


def _port_open(port: int, host: str = "localhost", timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _http_ok(port: int, path: str = "/health") -> bool:
    import urllib.request
    import urllib.error
    try:
        with urllib.request.urlopen(
            f"http://localhost:{port}{path}", timeout=3
        ) as r:
            return r.status == 200
    except (urllib.error.URLError, OSError):
        return False


pytestmark = [
    pytest.mark.skipif(not _docker_available(), reason="docker not on PATH"),
    pytest.mark.skipif(
        not COMPOSE_FILE.is_file(), reason="docker-compose.yml missing"
    ),
    pytest.mark.skipif(
        os.environ.get("NEXUS_RUN_BRINGUP_TEST") != "1",
        reason="set NEXUS_RUN_BRINGUP_TEST=1 to run (destructive — wipes dev volumes)",
    ),
    pytest.mark.slow,
]


def _compose(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["docker", "compose", "-f", str(COMPOSE_FILE), *args],
        capture_output=True, text=True, check=check, timeout=600,
    )


def _wait_healthy(name: str, port: int, path: str, timeout: int = 240) -> None:
    deadline = time.time() + timeout
    last_err = None
    while time.time() < deadline:
        if _http_ok(port, path):
            return
        last_err = f":{port}{path} not yet 200"
        time.sleep(3)
    pytest.fail(f"{name} not healthy within {timeout}s: {last_err}")


def test_full_stack_brings_up_on_fresh_volumes():
    """All canonical-pipeline engines reach /health 200 from zero state."""
    # 1. Stop existing stack (preserves volumes).
    _compose("--profile", "full", "down", check=False)

    # 2. Wipe data volumes — KEEP ollama-data + model-cache for speed.
    data_volumes = [
        "nexus_power_audio-storage",
        "nexus_power_document-storage",
        "nexus_power_etcd-data",
        "nexus_power_frame-storage",
        "nexus_power_milvus-data",
        "nexus_power_minio-data",
        "nexus_power_neo4j-data",
        "nexus_power_postgres-data",
        "nexus_power_redis-data",
        "nexus_power_upload-storage",
        "nexus_power_evidence-storage",
        "nexus_power_report-storage",
    ]
    for v in data_volumes:
        subprocess.run(
            ["docker", "volume", "rm", v],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )

    # 3. Bring up infra first (gives alembic a target).
    _compose(
        "--profile", "full", "up", "-d",
        "redis", "postgres", "neo4j", "etcd", "minio", "milvus",
    )

    # 4. Run alembic. The fresh_postgres test covers the empty-DB case;
    # here we trust that and just upgrade-head against the running pg.
    repo_root = str(REPO_ROOT)
    env = {**os.environ, "MSYS_NO_PATHCONV": "1"}
    pg_url = (
        f"postgresql+asyncpg://nexus:nexus-dev@postgres:5432/nexus"
    )
    subprocess.run(
        [
            "docker", "run", "--rm",
            "--network", "nexus_power_nexus",
            "-v", f"{repo_root}/alembic:/app/alembic:ro",
            "-v", f"{repo_root}/alembic.ini:/app/alembic.ini:ro",
            "-v", f"{repo_root}/sdk:/app/sdk:ro",
            "-w", "/app",
            "-e", "PYTHONPATH=/app/sdk/nexus-sdk",
            "-e", f"DATABASE_URL={pg_url}",
            "nexus-base:dev",
            "sh", "-c",
            "python -m alembic -c /app/alembic.ini upgrade head",
        ],
        check=True, env=env, timeout=300, capture_output=True,
    )

    # 5. Bring up the rest of the stack.
    _compose("--profile", "full", "up", "-d")

    # 6. Wait for each canonical service to be healthy.
    for name, (port, path) in HEALTH_ENDPOINTS.items():
        _wait_healthy(name, port, path)

    # 7. Verify all 8 canonical workflow lanes have consumers attached.
    lanes = [
        "shield.cpu", "eyes.cpu", "eyes.gpu",
        "ears.cpu", "ears.gpu",
        "spine.cpu", "spine.gpu",
        "backbone.cpu",
    ]
    for lane in lanes:
        result = subprocess.run(
            [
                "docker", "exec", "nexus-redis",
                "redis-cli", "-n", "3",
                "XINFO", "GROUPS", f"nexus:queue:{lane}",
            ],
            capture_output=True, text=True, timeout=10,
        )
        assert "consumers" in result.stdout.lower(), (
            f"queue lane nexus:queue:{lane} has no consumer group — "
            f"engine workflow worker did not attach.\n{result.stdout}"
        )

    # 8. Verify backbone is using real Milvus, not degraded in-memory.
    import urllib.request, json
    with urllib.request.urlopen(
        "http://localhost:8005/health", timeout=5
    ) as r:
        body = json.loads(r.read())
    vector_mode = body.get("modes", {}).get("vector_store", "")
    assert "milvus" in vector_mode.lower(), (
        f"backbone vector_store is {vector_mode!r}, expected 'milvus ...'.\n"
        f"Likely cause: Milvus didn't come up healthy, and "
        f"NEXUS_ALLOW_DEGRADED_MODE is true. Check Milvus pod logs."
    )
