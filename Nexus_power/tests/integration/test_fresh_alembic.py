"""
Integration test: alembic upgrade head runs cleanly on a fresh DB.

This test exists to catch the class of bug we surfaced during the
clean-state reset:
  - Two migrations creating the same table (`transcript_segments`)
  - `alembic_version.version_num` VARCHAR(32) overflow when a revision
    name exceeded 32 chars
  - Migration logic that worked against a partially-applied DB but
    failed against an empty one

Strategy:
  1. Start a throwaway Postgres container.
  2. Run `alembic upgrade head` against it.
  3. Assert no errors AND `alembic_version` ends at the latest revision.

Requires Docker on the CI runner. Skipped automatically when Docker
is unavailable.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = REPO_ROOT / "alembic.ini"
ALEMBIC_DIR = REPO_ROOT / "alembic"
SDK_DIR = REPO_ROOT / "sdk" / "nexus-sdk"
BASE_IMAGE = os.environ.get("NEXUS_BASE_IMAGE", "nexus-base:dev")


def _docker_available() -> bool:
    return shutil.which("docker") is not None


def _image_present(image: str) -> bool:
    rc = subprocess.run(
        ["docker", "image", "inspect", image],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    ).returncode
    return rc == 0


pytestmark = [
    pytest.mark.skipif(not _docker_available(), reason="docker not on PATH"),
    pytest.mark.skipif(
        not ALEMBIC_INI.is_file(), reason="alembic.ini missing"
    ),
]


@pytest.fixture
def fresh_postgres():
    """Spin up an ephemeral Postgres, yield its connection params,
    tear down on exit. Network-isolated; no port collision with the
    dev compose stack."""
    name = f"alembic-test-pg-{uuid.uuid4().hex[:8]}"
    network = f"alembic-test-net-{uuid.uuid4().hex[:8]}"
    user = "nexus"
    password = "alembic-test-pw"
    db = "nexus"

    subprocess.run(
        ["docker", "network", "create", network],
        check=True, stdout=subprocess.DEVNULL,
    )
    try:
        subprocess.run(
            [
                "docker", "run", "-d",
                "--name", name,
                "--network", network,
                "--network-alias", "postgres",
                "-e", f"POSTGRES_USER={user}",
                "-e", f"POSTGRES_PASSWORD={password}",
                "-e", f"POSTGRES_DB={db}",
                "postgres:16-alpine",
            ],
            check=True, stdout=subprocess.DEVNULL,
        )

        # Wait until pg_isready returns 0 or we hit the timeout.
        deadline = time.time() + 60
        while time.time() < deadline:
            rc = subprocess.run(
                ["docker", "exec", name, "pg_isready", "-U", user],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            ).returncode
            if rc == 0:
                break
            time.sleep(1)
        else:
            pytest.fail(f"postgres container {name} did not become ready")

        yield {
            "container": name,
            "network": network,
            "user": user,
            "password": password,
            "db": db,
        }
    finally:
        subprocess.run(
            ["docker", "rm", "-f", name],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        subprocess.run(
            ["docker", "network", "rm", network],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )


@pytest.mark.skipif(
    not _image_present(BASE_IMAGE),
    reason=f"{BASE_IMAGE} not built locally — run scripts/rebuild_canonical.sh first",
)
def test_alembic_upgrade_head_on_fresh_db(fresh_postgres):
    """alembic upgrade head must succeed end-to-end on an empty DB."""
    pg = fresh_postgres
    db_url = (
        f"postgresql+asyncpg://{pg['user']}:{pg['password']}@postgres:5432/{pg['db']}"
    )

    # Run alembic inside the nexus-base image so we get the exact SDK
    # the production engines see.
    cmd = [
        "docker", "run", "--rm",
        "--network", pg["network"],
        "-v", f"{ALEMBIC_DIR}:/app/alembic:ro",
        "-v", f"{ALEMBIC_INI}:/app/alembic.ini:ro",
        "-v", f"{SDK_DIR}:/app/sdk:ro",
        "-w", "/app",
        "-e", "PYTHONPATH=/app/sdk/nexus-sdk",
        "-e", f"DATABASE_URL={db_url}",
        BASE_IMAGE,
        "sh", "-c",
        "python -m alembic -c /app/alembic.ini upgrade head",
    ]
    # On Windows hosts, MSYS path translation mangles -v / -w args. The
    # env var disables it. Linux/macOS ignore the variable.
    env = {**os.environ, "MSYS_NO_PATHCONV": "1"}

    result = subprocess.run(
        cmd, capture_output=True, text=True, env=env, timeout=300,
    )
    assert result.returncode == 0, (
        f"alembic upgrade head failed:\n"
        f"stdout:\n{result.stdout[-2000:]}\n"
        f"stderr:\n{result.stderr[-2000:]}"
    )

    # Verify the head revision was reached.
    psql = subprocess.run(
        [
            "docker", "exec", pg["container"],
            "psql", "-U", pg["user"], "-d", pg["db"], "-t", "-c",
            "SELECT version_num FROM alembic_version;",
        ],
        capture_output=True, text=True, timeout=30,
    )
    assert psql.returncode == 0, psql.stderr
    head = psql.stdout.strip()
    assert head, "alembic_version table empty after upgrade"
    # The latest filename-prefix in the versions dir is the head.
    # Don't pin to a specific revision — that would force updates here
    # every time a new migration is added.
    versions = sorted((ALEMBIC_DIR / "versions").glob("*.py"))
    latest_filename_prefix = versions[-1].stem.split("_")[0]
    assert head.startswith(latest_filename_prefix), (
        f"alembic head is {head!r}, expected something starting with "
        f"{latest_filename_prefix!r} (the latest migration in alembic/versions)"
    )


def test_no_duplicate_create_table_across_migrations():
    """Static check: no two migrations call `op.create_table('foo')` for
    the same table name. Catches the 004/020 transcript_segments bug
    class at PR time."""
    import re

    versions_dir = ALEMBIC_DIR / "versions"
    if not versions_dir.is_dir():
        pytest.skip("alembic/versions not found")

    pattern = re.compile(
        r'op\.create_table\(\s*[\'"]([a-z_][a-z0-9_]*)[\'"]'
    )
    seen: dict[str, list[str]] = {}
    for f in sorted(versions_dir.glob("*.py")):
        src = f.read_text(encoding="utf-8")
        for m in pattern.finditer(src):
            seen.setdefault(m.group(1), []).append(f.name)

    duplicates = {t: files for t, files in seen.items() if len(files) > 1}
    if duplicates:
        # Each entry must be a documented redesign with a DROP TABLE
        # IF EXISTS guard before the second create. Allow-list those.
        allowed_redesigns = {
            # transcript_segments: 004 created v1, 020 redesigned with
            # PII-safe + canonical linkage. 020 has a defensive DROP.
            "transcript_segments": {
                "004_media_processing.py",
                "020_knowledge_substrate.py",
            },
        }
        unexpected = {
            t: files for t, files in duplicates.items()
            if t not in allowed_redesigns
            or set(files) != allowed_redesigns[t]
        }
        assert not unexpected, (
            f"Duplicate op.create_table found across migrations:\n"
            f"{unexpected}\n\n"
            f"Either add a DROP TABLE IF EXISTS guard in the later "
            f"migration and append to the allow-list above, or rename "
            f"the table."
        )


def test_revision_ids_fit_alembic_version_column():
    """Static check: every revision id is ≤ 32 chars. Alembic's default
    `alembic_version.version_num` column is VARCHAR(32); a longer id
    overflows on upgrade. Catches the `027_merge_e2e_and_knowledge_heads`
    bug class at PR time."""
    import re

    versions_dir = ALEMBIC_DIR / "versions"
    if not versions_dir.is_dir():
        pytest.skip("alembic/versions not found")

    # Pattern: `revision: str = "..."`  (capture the literal)
    pattern = re.compile(
        r'^revision\s*(?::\s*[A-Za-z_\[\], ]+)?\s*=\s*[\'"]([^\'"]+)[\'"]',
        re.MULTILINE,
    )
    too_long: list[tuple[str, str, int]] = []
    for f in sorted(versions_dir.glob("*.py")):
        src = f.read_text(encoding="utf-8")
        m = pattern.search(src)
        if not m:
            continue
        rev = m.group(1)
        if len(rev) > 32:
            too_long.append((f.name, rev, len(rev)))

    assert not too_long, (
        "Revision IDs longer than 32 chars will overflow the default "
        "alembic_version.version_num column:\n"
        + "\n".join(f"  {f}: {rev!r} ({n} chars)" for f, rev, n in too_long)
    )
