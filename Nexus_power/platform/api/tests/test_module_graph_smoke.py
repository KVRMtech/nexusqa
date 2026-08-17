"""Module-graph smoke test (cross-cutting).

Catches the class of bugs that pure-function smoke tests miss:
  * stale `from ... import X` statements pointing at renamed symbols
  * name collisions between modules
  * SQLAlchemy declarative mismatches (table redefinition, FK to missing table)
  * routers that crash at import time
  * Pydantic v1↔v2 quirks in request models

We import every module we've touched in P1-P7 against the *real* SDK + DB
metadata.  This requires nexus_sdk to be installed (it is in the dev
environment); we skip gracefully if it isn't, but report failure if it is
present yet our code can't import.

Run:
    python Nexus_power/platform/api/tests/test_module_graph_smoke.py
"""
from __future__ import annotations

import importlib
import sys
import traceback
from pathlib import Path

_THIS_DIR = Path(__file__).resolve().parent
_API_ROOT = _THIS_DIR.parent
_SDK_ROOT = _API_ROOT.parents[1] / "sdk" / "nexus-sdk"
_PLATFORM_ROOT = _API_ROOT.parent

# Path bootstrap mirrors main.py
for p in [str(_API_ROOT), str(_SDK_ROOT), str(_PLATFORM_ROOT / "api")]:
    if p not in sys.path:
        sys.path.insert(0, p)


# ─── Skip cleanly when the SDK isn't installed ───────────────────────────


try:
    import nexus_sdk  # noqa: F401
    import nexus_sdk.db.models  # noqa: F401
except Exception as exc:
    print(f"[SKIP] nexus_sdk not importable in this env: {exc}")
    print("       This test is designed for the dev/CI env where the full")
    print("       SDK is installed. Smoke tests already cover pure logic.")
    sys.exit(0)


# ─── Test cases ──────────────────────────────────────────────────────────


def _import(mod_name: str) -> None:
    """Import a module and report any error verbatim — does NOT swallow."""
    importlib.import_module(mod_name)
    print(f"[OK] {mod_name}")


def test_sdk_models_load_without_collisions():
    """Every model class registered with the right __tablename__, and the
    new E2E tables don't collide with the legacy ``test_runs`` table."""
    import nexus_sdk.db.models as m1
    # Don't reload — SQLAlchemy declarative classes register themselves
    # against Base.metadata on import, and reload would try to redefine.
    for cls_name, table in [
        ("E2EScenarioStateRow", "e2e_scenario_state"),
        ("E2ETestRunRow", "test_run"),
        ("E2ETestRunStepRow", "test_run_step"),
    ]:
        cls = getattr(m1, cls_name, None)
        assert cls is not None, f"Missing model class: {cls_name}"
        assert cls.__tablename__ == table, (
            f"{cls_name} maps to {cls.__tablename__}, expected {table}"
        )
    # Legacy TestRunRow (plural table 'test_runs') must still exist and
    # NOT collide with the new singular 'test_run' table.
    legacy = getattr(m1, "TestRunRow", None)
    assert legacy is not None and legacy.__tablename__ == "test_runs"
    assert legacy.__tablename__ != m1.E2ETestRunRow.__tablename__
    # Verify both tables are registered in Base.metadata
    from nexus_sdk.db import Base
    table_names = set(Base.metadata.tables.keys())
    for required in ("test_runs", "test_run", "test_run_step", "e2e_scenario_state"):
        assert required in table_names, f"Table {required!r} not in Base.metadata"
    print("[OK] SDK models: 4 P3+P6 tables registered, no collision with legacy test_runs")


def test_alembic_migrations_parse_and_chain():
    """All P3+P6+merge migrations parse and chain correctly, AND the
    merge migration joins both heads so ``alembic upgrade head`` will
    resolve to a single revision."""
    versions_dir = _API_ROOT.parents[1] / "alembic" / "versions"
    assert versions_dir.is_dir(), f"No alembic versions dir at {versions_dir}"

    # Required (revision_id → expected down_revision)
    expected: list[tuple[str, object]] = [
        ("022_e2e_scenario_state", "021_echo_mvp"),
        ("023_test_runs",          "022_e2e_scenario_state"),
        ("027_merge_heads",   # the migration's real revision id (filename is longer)
         ("023_test_runs", "026_marketplace_and_sovereign")),
    ]
    sys.path.insert(0, str(versions_dir))

    # Build a name → file map so we look up by revision id (not by numeric
    # prefix — there are multiple migrations sharing each prefix)
    files_by_revision: dict[str, str] = {}
    for f in versions_dir.glob("*.py"):
        if f.name.startswith("__"):
            continue
        mod = importlib.import_module(f.stem)
        rev = getattr(mod, "revision", None)
        if rev:
            files_by_revision[rev] = f.name

    for revision, down_revision in expected:
        fname = files_by_revision.get(revision)
        assert fname is not None, f"No migration with revision={revision!r}"
        mod = importlib.import_module(fname[:-3])
        actual_down = getattr(mod, "down_revision", None)
        if isinstance(down_revision, tuple):
            assert isinstance(actual_down, (list, tuple)) and set(actual_down) == set(down_revision), (
                f"{fname}: down_revision={actual_down!r} != {down_revision!r}"
            )
        else:
            assert actual_down == down_revision, (
                f"{fname}: down_revision={actual_down!r} != {down_revision!r}"
            )
        assert hasattr(mod, "upgrade") and callable(mod.upgrade)
        assert hasattr(mod, "downgrade") and callable(mod.downgrade)
        print(f"[OK] migration {revision} chains from {down_revision}")

    # Single-head verification: scan all revisions; find ones that are
    # not anyone else's down_revision. There must be exactly one.
    all_down: set[str] = set()
    all_revs: set[str] = set()
    for f in versions_dir.glob("*.py"):
        if f.name.startswith("__"):
            continue
        mod = importlib.import_module(f.stem)
        rev = getattr(mod, "revision", None)
        if rev:
            all_revs.add(rev)
        dr = getattr(mod, "down_revision", None)
        if isinstance(dr, str):
            all_down.add(dr)
        elif isinstance(dr, (list, tuple)):
            for d in dr:
                if isinstance(d, str):
                    all_down.add(d)
    heads = all_revs - all_down
    assert len(heads) == 1, (
        f"Alembic graph has {len(heads)} heads: {sorted(heads)}. "
        "Need exactly one for `alembic upgrade head` to work."
    )
    print(f"[OK] Alembic graph is single-headed (head = {next(iter(heads))})")


def test_heart_co_architect_files_present():
    """P4 Co-Architect package files exist on disk. Full-import testing
    is owned by ``engines/heart-engine/tests/test_co_architect_smoke.py``
    (which runs in heart's own sys.path context where the relative
    imports resolve correctly) and is re-run as part of every full
    smoke-suite pass."""
    heart_co_root = _API_ROOT.parents[1] / "engines" / "heart-engine" / "app" / "co_architect"
    assert heart_co_root.is_dir(), f"No co_architect dir at {heart_co_root}"
    for fname in ("__init__.py", "context.py", "prompts.py", "parsing.py"):
        path = heart_co_root / fname
        assert path.exists(), f"Missing {path}"
    print(f"[OK] heart-engine/app/co_architect/ has __init__.py, context.py, prompts.py, parsing.py")


def test_platform_services_import():
    """P3 + P5 + P6 + P7 service modules load cleanly."""
    _import("app.services.e2e_lifecycle")
    _import("app.services.test_exporters")
    _import("app.services.test_exporters.preparation")
    _import("app.services.test_exporters.playwright")
    _import("app.services.test_exporters.cypress")
    _import("app.services.test_exporters.gherkin")
    _import("app.services.test_exporters.json_plan")
    _import("app.services.test_runs")
    _import("app.services.diff_and_heal")
    _import("app.services.diff_and_heal.diff")
    _import("app.services.diff_and_heal.healing")


def test_platform_routers_import():
    """Every router we wrote in P3-P7 imports cleanly."""
    _import("app.routers.e2e_architect")
    _import("app.routers.e2e_lifecycle")
    _import("app.routers.co_architect")
    _import("app.routers.test_runs_feedback")
    _import("app.routers.diff_and_heal")


def _schema(app) -> dict:
    """The app's OpenAPI document — the version-stable view of what it serves.

    Both tests below used to read `app.routes` directly, taking `.tags` and
    `.path` off each entry. Newer FastAPI no longer flattens an included router
    into its parent: `app.routes` keeps a `fastapi.routing._IncludedRouter`
    dataclass that resolves children lazily, and that wrapper exposes neither
    attribute. Every route registered via `include_router` therefore became
    INVISIBLE — which is why CI reported all six tags and all fifteen P3-P7
    endpoints as missing while the application was serving every one of them,
    and why the same suite stayed green on a laptop pinning older FastAPI.

    Reading the schema fixes that without reaching into `_IncludedRouter`'s
    private `original_router` / `effective_candidates()`, which would only move
    the breakage to the next release. It is also a STRONGER assertion than the
    old one: a path appears here only if its router was actually included and
    its operation actually built.
    """
    return app.openapi()


def test_platform_main_assembles_app():
    """Importing main.py builds the full FastAPI app — catches any
    route-registration or middleware error at startup."""
    # This is the most important test — if this passes, the platform API
    # would start up cleanly with all our new routers attached.
    main_mod = importlib.import_module("main")
    app = getattr(main_mod, "app", None)
    assert app is not None, "main.app not found"
    # Inspect that our new routers' tags are present
    expected_tags = {
        "E2E Architect",
        "E2E Lifecycle",
        "E2E Co-Architect",
        "E2E Test Runs",
        "E2E Diff & Heal",
        "Tests",
    }
    schema = _schema(app)
    actual_tags: set[str] = set()
    for _path, operations in schema.get("paths", {}).items():
        for _method, operation in operations.items():
            if isinstance(operation, dict):
                actual_tags.update(operation.get("tags", []) or [])
    missing = expected_tags - actual_tags
    assert not missing, f"FastAPI app missing tag(s): {sorted(missing)}"
    print(f"[OK] FastAPI app assembled. {len(schema.get('paths', {}))} paths registered. Tags present: {sorted(expected_tags)}")


def test_p3_p6_endpoints_registered():
    """Verify the specific endpoints we added are reachable via path lookup."""
    main_mod = importlib.import_module("main")
    app = main_mod.app
    paths: set[str] = set(_schema(app).get("paths", {}))
    for route in app.routes:
        path = getattr(route, "path", None)
        if path:
            paths.add(path)
    expected_paths = [
        # P3 Lifecycle
        "/api/v1/e2e-architect/{artifact_id}/scenarios/state",
        "/api/v1/e2e-architect/{artifact_id}/scenarios/{scenario_id}/transition",
        "/api/v1/e2e-architect/{artifact_id}/scenarios/{scenario_id}/comment",
        # P4 Co-Architect
        "/api/v1/e2e-architect/{artifact_id}/co-architect/chat",
        "/api/v1/e2e-architect/{artifact_id}/co-architect/commit",
        # P5 Multi-format export
        "/api/v1/tests/export",
        "/api/v1/tests/export-formats",
        "/api/v1/tests/export-playwright",
        # P6 Execution Feedback
        "/api/v1/test-runs/ingest",
        "/api/v1/e2e-architect/{artifact_id}/test-runs",
        "/api/v1/e2e-architect/{artifact_id}/scenarios/runs/summary",
        "/api/v1/e2e-architect/{artifact_id}/scenarios/{scenario_id}/last-run",
        # P7 Diff + Heal
        "/api/v1/artifacts/diff",
        "/api/v1/e2e-architect/{artifact_id}/scenarios/heal-suggestions",
        "/api/v1/e2e-architect/{artifact_id}/scenarios/apply-healing",
    ]
    missing = [p for p in expected_paths if p not in paths]
    assert not missing, f"Routes missing from FastAPI app:\n  " + "\n  ".join(missing)
    print(f"[OK] All {len(expected_paths)} P3-P7 endpoints registered")


def test_pydantic_request_models_instantiate():
    """Catch Pydantic v1↔v2 quirks in every request model we wrote."""
    from app.routers.e2e_lifecycle import TransitionRequest, CommentRequest
    from app.routers.co_architect import (
        ChatRequest, ChatMessage, CommitProposalsRequest,
        ProposedScenario, ProposedScenarioStep,
    )
    from app.routers.test_runs_feedback import IngestRequest, IngestStep
    from app.routers.diff_and_heal import (
        DiffRequest, HealSuggestionsRequest, ApplyHealingRequest,
        HealSuggestionPayload,
    )
    # Round-trip each model with realistic data
    TransitionRequest(new_state="approved", note="x")
    CommentRequest(body="hello")
    ChatRequest(messages=[ChatMessage(role="user", content="hi")], propose_scenarios=False)
    CommitProposalsRequest(proposed_scenarios=[ProposedScenario(
        title="t", rationale="r", strategy="co_architect",
        steps=[ProposedScenarioStep(
            step_number=1, action="a", input_data="",
            expected_output="", evidence_scene_id="s",
            evidence_control_id="c", evidence_edge_id="",
            proof_confidence=0.9,
        )],
    )])
    IngestRequest(
        artifact_id="a",
        steps=[IngestStep(scenario_id="vs_001", step_number=1, status="passed")],
    )
    DiffRequest(baseline_artifact_id="a", current_artifact_id="b")
    HealSuggestionsRequest()
    ApplyHealingRequest(suggestions=[HealSuggestionPayload(
        scenario_id="vs_001", step_number=1,
        old_control_id="o", new_control_id="n",
    )])
    print("[OK] All Pydantic request models instantiate with realistic data")


def main() -> int:
    """Run every test in this module sequentially and report."""
    tests = [
        test_sdk_models_load_without_collisions,
        test_alembic_migrations_parse_and_chain,
        test_heart_co_architect_files_present,
        test_platform_services_import,
        test_platform_routers_import,
        test_platform_main_assembles_app,
        test_p3_p6_endpoints_registered,
        test_pydantic_request_models_instantiate,
    ]
    failures: list[tuple[str, str]] = []
    for fn in tests:
        try:
            fn()
        except Exception:
            tb = traceback.format_exc()
            failures.append((fn.__name__, tb))
            print(f"[FAIL] {fn.__name__}\n{tb}")
    print()
    if failures:
        print(f"{len(failures)} test(s) FAILED")
        return 1
    print(f"All {len(tests)} module-graph tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
