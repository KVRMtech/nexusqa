"""Smoke test for Phase 0 audit-gap closure.

Covers the pure-Python pieces of the foundation-hardening change:

  * ``safe_frame_asset_path`` accepts well-formed same-tenant paths,
    rejects path traversal, cross-tenant paths, bad extensions, and
    empty input.
  * Alembic migration 029 references every visual-evidence table the
    audit flagged + the new composite scene-index index.

The migration runs against a real DB are exercised by
``integration_smoke.sh`` (P0 section).  This file stays self-contained
so it can run in CI without Postgres.

Run:
    python Nexus_power/platform/api/tests/test_phase0_hardening_smoke.py
"""
from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

# ── Path setup ────────────────────────────────────────────────────────────
_API_ROOT = Path(__file__).resolve().parents[1]
_REPO_ROOT = _API_ROOT.parents[1]
_SDK_ROOT = _REPO_ROOT / "sdk" / "nexus-sdk"
sys.path.insert(0, str(_API_ROOT))
sys.path.insert(0, str(_SDK_ROOT))


# ── Stub out heavy imports we don't need ──────────────────────────────────
# ``app.database`` imports ``nexus_sdk.db.models.Base`` and SQLAlchemy
# bits.  ``safe_frame_asset_path`` itself has no DB dependency, so we
# bypass the module import by loading the file directly.
def _load_module_from_path(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _load_safe_frame_asset_path():
    """Import only ``safe_frame_asset_path`` without dragging in the DB layer."""
    # Stub the upstream imports that database.py touches at import time.
    sys.modules.setdefault("nexus_sdk", types.ModuleType("nexus_sdk"))
    sys.modules.setdefault("nexus_sdk.db", types.ModuleType("nexus_sdk.db"))
    models_stub = types.ModuleType("nexus_sdk.db.models")
    models_stub.Base = type("Base", (), {})
    sys.modules["nexus_sdk.db.models"] = models_stub

    # Stub ``app.config`` so PlatformAPIConfig import succeeds.
    if "app" not in sys.modules:
        app_pkg = types.ModuleType("app")
        app_pkg.__path__ = [str(_API_ROOT / "app")]
        sys.modules["app"] = app_pkg
    config_stub = types.ModuleType("app.config")
    config_stub.PlatformAPIConfig = type("PlatformAPIConfig", (), {})
    sys.modules["app.config"] = config_stub

    mod = _load_module_from_path("app.database_under_test", _API_ROOT / "app" / "database.py")
    return mod.safe_frame_asset_path


# ── Tests ─────────────────────────────────────────────────────────────────
PASS = 0
FAIL = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global PASS, FAIL
    if condition:
        print(f"  [OK]   {label}")
        PASS += 1
    else:
        print(f"  [FAIL] {label}  {detail}")
        FAIL += 1


def test_safe_frame_asset_path() -> None:
    print("\n=== safe_frame_asset_path ===")
    safe = _load_safe_frame_asset_path()

    tenant = "tenant-a"
    other = "tenant-b"

    # Happy path — frame written by the orchestrator under the tenant prefix
    good = f"{tenant}/sess-1/job-1_frames/frame_00001.png"
    check("accepts well-formed same-tenant png", safe(tenant, good) == good)

    # jpeg / webp / jpg variants — all valid frame asset types
    check(
        "accepts .jpg variant",
        safe(tenant, f"{tenant}/s/j_frames/x.jpg") == f"{tenant}/s/j_frames/x.jpg",
    )
    check(
        "accepts .jpeg variant",
        safe(tenant, f"{tenant}/s/j_frames/x.jpeg") == f"{tenant}/s/j_frames/x.jpeg",
    )
    check(
        "accepts .webp variant",
        safe(tenant, f"{tenant}/s/j_frames/x.webp") == f"{tenant}/s/j_frames/x.webp",
    )

    # Empty / None — explicit no-asset signal, not an error
    check("empty string -> empty", safe(tenant, "") == "")
    check("None -> empty", safe(tenant, None) == "")

    # Path traversal sequences
    check(
        "rejects ../ traversal",
        safe(tenant, f"{tenant}/../{other}/sess-1/x.png") == "",
    )
    check(
        "rejects backslash separator",
        safe(tenant, rf"{tenant}\sess-1\x.png") == "",
    )

    # Cross-tenant prefix
    check(
        "rejects cross-tenant prefix",
        safe(tenant, f"{other}/sess-1/job-1_frames/x.png") == "",
    )

    # Bad extensions (no .exe / .html / unsuffixed)
    check("rejects .exe extension", safe(tenant, f"{tenant}/s/j/x.exe") == "")
    check("rejects .html extension", safe(tenant, f"{tenant}/s/j/x.html") == "")
    check("rejects extensionless", safe(tenant, f"{tenant}/s/j/x") == "")

    # Too few segments (must have tenant/session/path)
    check("rejects single-segment", safe(tenant, "x.png") == "")
    check("rejects two-segment", safe(tenant, f"{tenant}/x.png") == "")


def test_migration_029_references() -> None:
    print("\n=== migration 029_visual_evidence_rls ===")
    mig_path = _REPO_ROOT / "alembic" / "versions" / "029_visual_evidence_rls.py"
    check("migration file exists", mig_path.is_file())
    body = mig_path.read_text(encoding="utf-8")

    expected_tables = [
        "video_files",
        "visual_frames",
        "visual_scenes",
        "app_instances",
        "evidence_controls",
        "evidence_steps",
        "cursor_events",
        "visual_flow_edges",
        "visual_flows",
        "ui_dictionary_entries",
    ]
    for t in expected_tables:
        check(f"covers table: {t}", f'"{t}"' in body or f"'{t}'" in body)

    check(
        "uses set_config session variable name from migration 010",
        "nexus.current_tenant_id" in body,
    )
    check(
        "creates composite scene_index index",
        "ix_visual_scenes_artifact_scene_index" in body
        and "(artifact_id, scene_index)" in body,
    )
    check(
        "FORCE RLS is set (defense even against table owner)",
        "FORCE ROW LEVEL SECURITY" in body,
    )
    check(
        "down-revision points to 028_workflow_state",
        '"028_workflow_state"' in body,
    )


# ── Runner ────────────────────────────────────────────────────────────────
def main() -> int:
    test_safe_frame_asset_path()
    test_migration_029_references()
    print(f"\n=== Phase 0 hardening smoke: {PASS} pass, {FAIL} fail ===")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
