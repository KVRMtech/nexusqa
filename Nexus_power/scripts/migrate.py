"""
Nexus QA — Migration Helper
============================

Convenience wrapper around Alembic commands with safety checks.
Handles environment setup (DATABASE_URL, PYTHONPATH) automatically.

Usage:
    python scripts/migrate.py status          # Show current revision
    python scripts/migrate.py upgrade         # Upgrade to head
    python scripts/migrate.py upgrade REV     # Upgrade to specific revision
    python scripts/migrate.py downgrade REV   # Downgrade to specific revision
    python scripts/migrate.py history         # Show migration history
    python scripts/migrate.py check           # Verify schema is at head
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ALEMBIC_INI = ROOT / "alembic.ini"
SDK_PATH = ROOT / "sdk" / "nexus-sdk"

EXPECTED_HEAD = "009_semantic_completeness"


def _env():
    """Build environment with DATABASE_URL and PYTHONPATH."""
    env = {**os.environ}
    if "DATABASE_URL" not in env:
        pg_user = env.get("POSTGRES_USER", "nexus")
        pg_pass = env.get("POSTGRES_PASSWORD", "nexus-dev")
        pg_host = env.get("POSTGRES_HOST", "localhost")
        pg_port = env.get("POSTGRES_PORT", "5432")
        pg_db = env.get("POSTGRES_DB", "nexus")
        env["DATABASE_URL"] = (
            f"postgresql+asyncpg://{pg_user}:{pg_pass}@{pg_host}:{pg_port}/{pg_db}"
        )
    # Ensure SDK is on PYTHONPATH so alembic env.py can import nexus_sdk
    existing = env.get("PYTHONPATH", "")
    sdk_str = str(SDK_PATH)
    if sdk_str not in existing:
        env["PYTHONPATH"] = f"{sdk_str}{os.pathsep}{existing}" if existing else sdk_str
    return env


def _run_alembic(*args: str, capture: bool = False) -> subprocess.CompletedProcess:
    """Run an alembic command."""
    cmd = [sys.executable, "-m", "alembic"] + list(args)
    if capture:
        result = subprocess.run(
            cmd, cwd=str(ROOT), env=_env(),
            capture_output=True, text=True,
        )
    else:
        # Merge stderr into stdout so alembic's INFO logging doesn't
        # trigger PowerShell NativeCommandError on Windows
        result = subprocess.run(
            cmd, cwd=str(ROOT), env=_env(),
            stderr=subprocess.STDOUT,
        )
    return result


def cmd_status():
    r = _run_alembic("current", capture=True)
    # Print combined output, stripping alembic INFO noise
    for line in (r.stdout + r.stderr).splitlines():
        if "INFO" not in line:
            print(line)
        else:
            # Still show it, but clearly as info
            print(f"  {line}")
    return 0 if EXPECTED_HEAD in (r.stdout + r.stderr) else r.returncode


def cmd_upgrade(target: str = "head"):
    print(f"Upgrading to: {target}")
    r = _run_alembic("upgrade", target)
    return r.returncode


def cmd_downgrade(target: str):
    if not target:
        print("Error: downgrade requires a target revision")
        return 1
    print(f"Downgrading to: {target}")
    r = _run_alembic("downgrade", target)
    return r.returncode


def cmd_history():
    r = _run_alembic("history", "--verbose")
    return r.returncode


def cmd_check():
    """Verify the database is at the expected migration head.

    Uses 'alembic current' rather than 'alembic check' because
    autogenerate can report false-positive nullable diffs that
    do not represent real schema problems.
    """
    r = _run_alembic("current", capture=True)
    output = r.stdout + r.stderr
    if EXPECTED_HEAD in output and "(head)" in output:
        print(f"Schema is up to date: {EXPECTED_HEAD} (head)")
        return 0
    elif EXPECTED_HEAD in output:
        print(f"At revision {EXPECTED_HEAD} but may not be head — run 'upgrade'")
        return 1
    else:
        print("Schema is NOT at expected head:")
        for line in output.splitlines():
            if line.strip():
                print(f"  {line.strip()}")
        return 1


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 1

    command = sys.argv[1]
    target = sys.argv[2] if len(sys.argv) > 2 else ""

    commands = {
        "status": lambda: cmd_status(),
        "upgrade": lambda: cmd_upgrade(target or "head"),
        "downgrade": lambda: cmd_downgrade(target),
        "history": lambda: cmd_history(),
        "check": lambda: cmd_check(),
    }

    if command not in commands:
        print(f"Unknown command: {command}")
        print(f"Available: {', '.join(commands)}")
        return 1

    return commands[command]()


if __name__ == "__main__":
    sys.exit(main() or 0)
