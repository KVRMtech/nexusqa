"""QE-Central Phase-0 — VKPower video-path golden contract pins (T1-T4).

THE FIRST PR of the QE-Central build (IMPLEMENTATION_DESIGN.md §6 Phase-0,
§7 item 1): these tests pin the exact seams QE-Central's crawler substrate
depends on, GREEN against current HEAD *before* any E1/E2/E3 patch lands.
Any VKPower refactor that moves a seam fails here loudly, in CI, not in
production.

Pinned seams (every citation read against HEAD when this file was written):
  T1  script_factory/compiler.py:1685  compile_project() — deterministic
      (same input → byte-identical project), spec imports @playwright/test,
      __nxClick emitted + called for click steps, hard toHaveURL for a
      grounded next_url, and NO data-loader scaffolding (const D / __nxTok)
      when a case has no data-driven fields (compiler.py:1128-1149).
  T2  test_factory/generator.py:1162  generate_demonstrated_test_cases() —
      PROVEN navigation gating: source='url_regex' + extraction_confidence
      >= 0.9 IS proven (generator.py:556, 580-585) → the "Verify ...
      navigated" step is provenance='demonstrated' (:904) and the commit
      click carries navigation_grounded (:1119-1122); a low-confidence
      visit is NOT proven → honest 'inferred'.
  T3  test_factory/playwright_auditor.py:139  score_spec(spec_text, steps,
      evidence) — 3-arg signature, frozen report key-set, MIN-gated overall
      score of 10/CERTIFIED for a known-good tiny spec.
  T4  storyboard/page_visit_extractor.py:1860-1897 — frameless-artifact
      honesty: zero frames → _load_artifact_signals returns None after ONE
      visual_frames SELECT (:355-365) and the extractor returns the literal
      all-zero result (visits_written=0, stale_visits_deleted=0) without
      touching a single row (pre-seeded substrate rows survive).

NO DB, NO network, NO LLM: every test drives a pure function.  Modules are
loaded by file path (the sibling test_playwright_auditor.py pattern) so
importing them does not drag in the FastAPI app package.  Tests may use
fakes/mocks even though src must not — the T4 session fake is the narrowest
object satisfying _load_artifact_signals' first query.

Run from Nexus_power/platform/api:
    python -m pytest tests/test_qec_contract_video_frozen.py -q
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import importlib
import importlib.machinery
import importlib.util
import inspect
import json
import os
import sys
import types

# ── Path anchors (absolute, derived from this file — CWD-independent) ────────
_HERE = os.path.dirname(os.path.abspath(__file__))
_API = os.path.abspath(os.path.join(_HERE, ".."))                    # platform/api
_REPO = os.path.abspath(os.path.join(_API, "..", ".."))              # Nexus_power
_SDK = os.path.join(_REPO, "sdk", "nexus-sdk")                        # nexus_sdk checkout

# nexus_sdk: prefer the installed package (container); fall back to the
# in-repo checkout (local dev / CI without pip install).
try:  # pragma: no cover - environment-dependent branch
    import nexus_sdk  # noqa: F401
except ImportError:  # pragma: no cover
    sys.path.insert(0, _SDK)

from nexus_sdk.models import ProductionTestCase, ProductionTestStep  # noqa: E402


# ── Module loaders (file-path imports; no `app` package side effects) ───────


def _load_file_module(name: str, *parts: str):
    """Load one module by file path under a private sys.modules name."""
    if name in sys.modules:
        return sys.modules[name]
    path = os.path.join(_API, *parts)
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _load_package(name: str, *parts: str):
    """Load a self-contained package (its __init__ + relative imports)."""
    if name in sys.modules:
        return sys.modules[name]
    pkg_dir = os.path.join(_API, *parts)
    spec = importlib.util.spec_from_file_location(
        name, os.path.join(pkg_dir, "__init__.py"),
        submodule_search_locations=[pkg_dir],
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _synthetic_pkg(name: str, path: str) -> None:
    """Register a bare parent package WITHOUT executing its __init__.py.

    app/__init__.py + app/services/__init__.py pull in the whole FastAPI
    app (MissionOrchestrator, routers). page_visit_extractor only needs
    them as import parents for its `..llm` relative import, so we provide
    empty package shells pointing at the real directories.
    """
    if name in sys.modules:
        return
    mod = types.ModuleType(name)
    spec = importlib.machinery.ModuleSpec(name, loader=None, is_package=True)
    spec.submodule_search_locations = [path]
    mod.__spec__ = spec
    mod.__path__ = [path]
    sys.modules[name] = mod


def _load_page_visit_extractor():
    """Import storyboard.page_visit_extractor with lightweight parents."""
    root = "nexus_qec_pin_app"
    _synthetic_pkg(root, os.path.join(_API, "app"))
    _synthetic_pkg(root + ".services", os.path.join(_API, "app", "services"))
    return importlib.import_module(root + ".services.storyboard.page_visit_extractor")


gen = _load_file_module(
    "nexus_qec_pin_generator", "app", "services", "test_factory", "generator.py",
)
aud = _load_file_module(
    "nexus_qec_pin_auditor", "app", "services", "test_factory", "playwright_auditor.py",
)
sf = _load_package(
    "nexus_qec_pin_script_factory", "app", "services", "script_factory",
)
pve = _load_page_visit_extractor()


# ── Compiler env pinning ─────────────────────────────────────────────────────
# compile_project is pure strings EXCEPT for three env knobs (compiler.py:445,
# :609, :662, :1191). Pin them to their documented defaults (unset) for the
# duration of each compile so the byte-hash is stable on any machine/CI.
_COMPILER_ENV_KNOBS = (
    "NEXUS_UNPROVEN_STEPS",
    "NEXUS_PROVEN_NAV_ORACLE",
    "NEXUS_NAV_RECOVERY",
)


@contextlib.contextmanager
def _default_compiler_env():
    saved = {k: os.environ.pop(k, None) for k in _COMPILER_ENV_KNOBS}
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v


# ═════════════════════════════════════════════════════════════════════════════
#  T1 — compile_project(): determinism + emission contracts
# ═════════════════════════════════════════════════════════════════════════════

_T1_LOGIN_SPEC_PATH = "tests/functional/login-flow-pin.spec.ts"
_T1_BROWSE_SPEC_PATH = "tests/functional/browse-only-pin.spec.ts"

# Frozen non-spec project skeleton emitted by compile_project (compiler.py:1722-1736).
_T1_SKELETON_FILES = {
    "package.json", "playwright.config.ts", "tsconfig.json",
    "vkpower-reporter.ts", ".env.example", "vkpower.auth.setup.ts",
    "vkpower.auth.config.json", ".gitignore", "README.md",
}


def _t1_fixture() -> tuple[list[ProductionTestCase], dict]:
    """A small, fully-demonstrated video-shaped fixture.

    Case 1: one grounded fill + one grounded submit (next_url +
    navigation_grounded — the compiler's hard-toHaveURL contract).
    Case 2: browse-only (no type/select step carries a value), so the
    parametrized compile must emit NO data-loader scaffolding for it.
    Rebuilt fresh on every call so the determinism pin compares two
    independent input object graphs, not one shared reference.
    """
    login = ProductionTestCase(
        test_id="qec-pin-t1-login",
        name="Login flow pin",
        description="T1 pin: demonstrated flow with a grounded fill and a grounded submit.",
        type="functional",
        steps=[
            ProductionTestStep(
                step_number=1,
                action="Open https://app.pin.test/login",
                expected="The login page is displayed",
                expected_result="The login page is displayed",
                observed={"verb": "navigate", "url": "https://app.pin.test/login"},
                provenance="demonstrated",
                confidence="high",
            ),
            ProductionTestStep(
                step_number=2,
                action="Enter 'Venkata' in the 'First Name' field",
                expected="'First Name' shows 'Venkata'",
                expected_result="'First Name' shows 'Venkata'",
                observed={"verb": "type", "label": "First Name", "kind": "field",
                          "value": "Venkata"},
                provenance="demonstrated",
                confidence="high",
            ),
            ProductionTestStep(
                step_number=3,
                action="Click 'Continue'",
                expected="The application proceeds to https://app.pin.test/dashboard",
                expected_result="The application proceeds to https://app.pin.test/dashboard",
                observed={"verb": "click", "label": "Continue", "kind": "button",
                          "next_url": "https://app.pin.test/dashboard",
                          "navigation_grounded": True},
                provenance="demonstrated",
                confidence="high",
            ),
        ],
    )
    browse = ProductionTestCase(
        test_id="qec-pin-t1-browse",
        name="Browse only pin",
        description="T1 pin: browse-only flow with no data-driven fields.",
        type="functional",
        steps=[
            ProductionTestStep(
                step_number=1,
                action="Open https://app.pin.test/reports",
                expected="The reports page is displayed",
                expected_result="The reports page is displayed",
                observed={"verb": "navigate", "url": "https://app.pin.test/reports"},
                provenance="demonstrated",
                confidence="high",
            ),
            ProductionTestStep(
                step_number=2,
                action="Click 'Refresh'",
                expected="'Refresh' is activated",
                expected_result="'Refresh' is activated",
                observed={"verb": "click", "label": "Refresh", "kind": "button"},
                provenance="demonstrated",
                confidence="high",
            ),
        ],
    )
    field_meta = {"first name": {"control": "text", "options": [], "required": False}}
    return [login, browse], field_meta


def _project_hash(files: dict) -> str:
    """Canonical byte hash of a compiled project (path → content)."""
    canon = json.dumps(files, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def test_t1_compile_project_is_byte_deterministic():
    """Same input → byte-identical project, computed twice in-process."""
    with _default_compiler_env():
        cases_a, meta_a = _t1_fixture()
        cases_b, meta_b = _t1_fixture()
        files_a = sf.compile_project(cases_a, meta_a)
        files_b = sf.compile_project(cases_b, meta_b)
    assert files_a == files_b, "compile_project drifted between two identical compiles"
    assert _project_hash(files_a) == _project_hash(files_b)
    # Parametrized compile is deterministic too (it adds nexus.config.json).
    with _default_compiler_env():
        cases_c, meta_c = _t1_fixture()
        cases_d, meta_d = _t1_fixture()
        files_c = sf.compile_project(cases_c, meta_c, parametrize=True)
        files_d = sf.compile_project(cases_d, meta_d, parametrize=True)
    assert files_c == files_d
    assert _project_hash(files_c) == _project_hash(files_d)


def test_t1_project_structure_and_spec_invariants():
    with _default_compiler_env():
        cases, meta = _t1_fixture()
        files = sf.compile_project(cases, meta)

    # Frozen project layout: skeleton + exactly the two case specs.
    assert set(files) == _T1_SKELETON_FILES | {_T1_LOGIN_SPEC_PATH, _T1_BROWSE_SPEC_PATH}, (
        sorted(files),
    )

    specs = {p: c for p, c in files.items() if p.endswith(".spec.ts")}
    for path, spec in specs.items():
        assert "import { test, expect } from '@playwright/test';" in spec, path

    login_spec = files[_T1_LOGIN_SPEC_PATH]
    # Click contract: __nxClick is defined at module scope AND called for the
    # demonstrated click step (compiler.py:1114, :943).
    assert "async function __nxClick(" in login_spec
    assert "await __nxClick(" in login_spec
    # Grounded fill replay: literal value fill + tolerant committed-value oracle.
    assert "await field.fill('Venkata'" in login_spec
    assert "toHaveValue(/Venkata/i)" in login_spec
    # Grounded next_url → hard navigation oracle (never dropped, never soft).
    assert "await expect(page).toHaveURL(" in login_spec
    # Entry navigation invariant: goto is emitted for the entry page only.
    assert login_spec.count("await page.goto(") == 1

    browse_spec = files[_T1_BROWSE_SPEC_PATH]
    assert "await __nxClick(" in browse_spec
    # A browse-only DEFAULT compile carries no data plumbing either.
    assert "vkpower.data.json" not in browse_spec
    assert "__nxTok" not in browse_spec


def test_t1_no_data_loader_scaffolding_without_data_fields():
    """Data plumbing (const D loader + __nxTok) is emitted ONLY for cases that
    actually have a data-driven field (compiler.py:1128-1149)."""
    with _default_compiler_env():
        cases, meta = _t1_fixture()
        files = sf.compile_project(cases, meta, parametrize=True)

    login_spec = files[_T1_LOGIN_SPEC_PATH]
    browse_spec = files[_T1_BROWSE_SPEC_PATH]

    # The data-driven case gets the loader + the tolerant token helper...
    assert "const D = " in login_spec
    assert "vkpower.data.json" in login_spec
    assert "function __nxTok(" in login_spec
    # ...the browse-only case gets NEITHER (no dead scaffolding — the parabank
    # 4/10-vs-gate-10 inconsistency this rule fixed).
    assert "const D = " not in browse_spec
    assert "vkpower.data.json" not in browse_spec
    assert "__nxTok" not in browse_spec
    # Parametrized bundle ships its standalone base-URL config with the
    # recorded origin as default (compiler.py:1734-1736).
    cfg = json.loads(files["nexus.config.json"])
    assert cfg == {"baseURL": "https://app.pin.test"}


# ═════════════════════════════════════════════════════════════════════════════
#  T2 — generate_demonstrated_test_cases(): PROVEN navigation gating
# ═════════════════════════════════════════════════════════════════════════════

_T2_ARTIFACT = "qec-pin-artifact-t2"


def _t2_visits() -> list:
    """Three URL milestones, video-shaped (source='url_regex').

    login (conf 0.9, PROVEN) → dashboard (conf 0.9, PROVEN) → reports
    (conf 0.5, NOT proven — pins the >=0.9 gate at generator.py:580-585).
    """
    mk = gen.PageVisitInput
    return [
        mk(page_visit_id="v1", sequence_index=1,
           location="https://app.pin.test/login", url_host="app.pin.test",
           url_path="/login", url_query="", canonical_host="app.pin.test",
           source="url_regex", form_snapshot={"First Name": "Venkata"},
           form_snapshot_signals={}, first_seen_ms=0, duration_ms=5000,
           extraction_confidence=0.9),
        mk(page_visit_id="v2", sequence_index=2,
           location="https://app.pin.test/dashboard", url_host="app.pin.test",
           url_path="/dashboard", url_query="", canonical_host="app.pin.test",
           source="url_regex", form_snapshot={},
           form_snapshot_signals={}, first_seen_ms=6000, duration_ms=4000,
           extraction_confidence=0.9),
        mk(page_visit_id="v3", sequence_index=3,
           location="https://app.pin.test/reports", url_host="app.pin.test",
           url_path="/reports", url_query="", canonical_host="app.pin.test",
           source="url_regex", form_snapshot={},
           form_snapshot_signals={}, first_seen_ms=11000, duration_ms=3000,
           extraction_confidence=0.5),
    ]


def _t2_actions() -> list:
    mk = gen.PageActionInput
    return [
        mk(page_visit_id="v1", subaction_index=0, verb="type",
           target_label="First Name", target_kind="text", value="Venkata"),
        mk(page_visit_id="v1", subaction_index=1, verb="click",
           target_label="Continue", target_kind="button", value=None,
           after_outcome="navigation", navigated=True),
        mk(page_visit_id="v2", subaction_index=0, verb="click",
           target_label="Open Reports", target_kind="button", value=None,
           after_outcome="navigation", navigated=True),
    ]


def _t2_generate():
    return gen.generate_demonstrated_test_cases(
        artifact_id=_T2_ARTIFACT,
        page_visits=_t2_visits(),
        page_actions=_t2_actions(),
    )


def _prov(step) -> str:
    return (getattr(step, "provenance", "") or "").strip().lower()


def test_t2_case_shape_and_proven_navigation_gating():
    result = _t2_generate()

    # Exactly ONE demonstrated functional E2E: no alternates were demonstrated,
    # no revisit branch, no required-field negative.
    assert len(result.test_cases) == 1, [c.name for c in result.test_cases]
    assert result.page_groups == 3
    assert result.visits_total == 3
    assert result.visits_used == 3
    assert result.fields_demonstrated == 1
    assert result.truncated is False

    case = result.test_cases[0]
    steps = case.steps
    assert [s.action for s in steps] == [
        "Open https://app.pin.test/login",
        "Enter 'Venkata' in the 'First Name' field",
        "Click 'Continue'",
        "Verify the application navigated to https://app.pin.test/dashboard",
        "Click 'Open Reports'",
        "Verify the application navigated to https://app.pin.test/reports",
    ]

    # PROVEN: url_regex + conf 0.9 >= 0.9 AND the previous group grounded-
    # navigated → the verify step is asserted as fact (generator.py:904).
    assert _prov(steps[3]) == "demonstrated", steps[3]
    # NOT proven: conf 0.5 < 0.9 → honest 'inferred', never a hard toHaveURL.
    assert _prov(steps[5]) == "inferred", steps[5]

    # The commit click onto a PROVEN page carries the grounded navigation
    # signal the compiler/auditor key on (generator.py:1119-1122).
    obs_continue = steps[2].observed
    assert obs_continue["next_url"] == "https://app.pin.test/dashboard"
    assert obs_continue["navigation_grounded"] is True
    # The commit click onto the UNPROVEN page still hands the URL to the
    # compiler as a hint but WITHOUT navigation_grounded (never green-wash).
    obs_reports = steps[4].observed
    assert obs_reports["next_url"] == "https://app.pin.test/reports"
    assert not obs_reports.get("navigation_grounded")

    # Honesty self-grade: exactly one inferred step → evidence grade B
    # (generator.py:824-838).
    assert "evidence-grade:B" in (case.tags or [])
    assert "inferred-steps:1" in (case.tags or [])


def test_t2_generation_is_deterministic():
    """uuid5 identity + step stream are stable across regenerations
    (idempotent upserts downstream depend on this)."""
    r1 = _t2_generate()
    r2 = _t2_generate()
    c1, c2 = r1.test_cases[0], r2.test_cases[0]
    assert c1.test_id == c2.test_id
    assert [(s.action, _prov(s)) for s in c1.steps] == \
           [(s.action, _prov(s)) for s in c2.steps]


# ═════════════════════════════════════════════════════════════════════════════
#  T3 — playwright_auditor.score_spec(): signature + report-shape pins
# ═════════════════════════════════════════════════════════════════════════════

_T3_SPEC = """import { test, expect } from '@playwright/test';
test('qec pin', async ({ page }) => {
  await page.goto('https://app.pin.test/login');
  const field = page.getByLabel('First Name').first();
  await field.fill('Venkata');
  await expect(field).toHaveValue(/Venkata/i);
  await page.getByRole('button', { name: 'Continue' }).click();
  await expect(page).toHaveURL(/\\/dashboard/, { timeout: 30000 });
});"""

_T3_STEPS = [
    {"step_number": 1, "action": "Open https://app.pin.test/login",
     "provenance": "demonstrated",
     "observed": {"verb": "navigate", "url": "https://app.pin.test/login"}},
    {"step_number": 2, "action": "Enter 'Venkata' in the 'First Name' field",
     "provenance": "demonstrated",
     "observed": {"verb": "type", "label": "First Name", "value": "Venkata"}},
    {"step_number": 3, "action": "Click 'Continue'",
     "provenance": "demonstrated",
     "observed": {"verb": "click", "label": "Continue", "kind": "button",
                  "next_url": "https://app.pin.test/dashboard",
                  "navigation_grounded": True}},
]

_T3_EVIDENCE = [
    {"verb": "type", "target_label": "First Name", "value": "Venkata"},
]

# The frozen report contract QE-Central's harness/tier-labeler will read.
_T3_REPORT_KEYS = {
    "dimension_scores", "overall_score", "per_step", "decision",
    "findings", "gaps", "source",
}


def test_t3_score_spec_signature_pin():
    params = list(inspect.signature(aud.score_spec).parameters.values())
    assert [p.name for p in params] == ["spec_text", "steps", "evidence"], params
    assert params[2].default is None
    # The 3-arg positional call QE-Central's clients rely on works.
    report = aud.score_spec(_T3_SPEC, _T3_STEPS, _T3_EVIDENCE)
    assert isinstance(report, dict)


def test_t3_report_key_set_and_known_good_score():
    report = aud.score_spec(_T3_SPEC, _T3_STEPS, evidence=_T3_EVIDENCE)

    assert set(report.keys()) == _T3_REPORT_KEYS, sorted(report)
    assert set(report["dimension_scores"].keys()) == set(aud.DIMENSIONS)
    assert report["source"] == "deterministic"

    # Known-good tiny spec: every axis clean → MIN-gated overall is a full 10,
    # certified, zero honest gaps, every step grounded_ok.
    assert report["dimension_scores"] == {d: 10 for d in aud.DIMENSIONS}, report
    assert report["overall_score"] == 10, report
    assert report["decision"] == aud.DECISION_CERTIFIED
    assert report["gaps"] == 0
    assert report["findings"] == []
    assert [p["verdict"] for p in report["per_step"]] == [aud.V_OK] * 3


# ═════════════════════════════════════════════════════════════════════════════
#  T4 — frameless extractor honesty pin
# ═════════════════════════════════════════════════════════════════════════════


class _EmptyScalarResult:
    """The narrowest Result fake: .scalars().all() → no rows."""

    def scalars(self):
        return self

    def all(self):
        return []


class _RecordingSession:
    """Narrowest AsyncSession fake for _load_artifact_signals' first query.

    Records every executed statement and any (unexpected) mutation so the
    test can prove the frameless path issues exactly ONE SELECT and never
    writes/deletes — i.e. pre-seeded substrate rows survive.  Any other
    session API the extractor were to start using would AttributeError,
    failing this pin loudly (by design).
    """

    def __init__(self):
        self.executed = []
        self.mutations = []

    async def execute(self, statement, *args, **kwargs):
        self.executed.append(statement)
        return _EmptyScalarResult()

    # Write APIs are recorded (not implemented) — the pin asserts they are
    # never touched on the zero-frame path.
    def add(self, *args, **kwargs):
        self.mutations.append("add")

    def add_all(self, *args, **kwargs):
        self.mutations.append("add_all")

    async def delete(self, *args, **kwargs):
        self.mutations.append("delete")

    async def flush(self, *args, **kwargs):
        self.mutations.append("flush")

    async def commit(self, *args, **kwargs):
        self.mutations.append("commit")


def test_t4_frameless_artifact_returns_zero_and_touches_nothing():
    session = _RecordingSession()
    config = pve.PageVisitExtractorConfig()  # documented defaults, no env needed

    result = asyncio.run(pve.extract_page_visits_for_artifact(
        session,
        artifact_id="qec-pin-artifact-t4",
        tenant_id="qec-pin-tenant",
        config=config,
        router=None,
    ))

    # The literal honest early-return (page_visit_extractor.py:1884-1897).
    assert isinstance(result, pve.PageVisitExtractorResult)
    assert result.artifact_id == "qec-pin-artifact-t4"
    assert result.visits_written == 0
    assert result.stale_visits_deleted == 0
    assert result.frames_scanned == 0
    assert result.visits_via_url_regex == 0
    assert result.visits_via_url_scene == 0
    assert result.visits_via_screen_name == 0
    assert result.visits_via_window_title == 0
    assert result.visits_via_llm == 0
    assert result.visits_with_empty_location == 0
    assert result.elapsed_ms >= 0

    # Exactly ONE read (the visual_frames scan, :355-365) and ZERO writes:
    # a frameless artifact can never clobber substrate rows.
    assert len(session.executed) == 1, session.executed
    stmt = str(session.executed[0]).lower()
    assert stmt.lstrip().startswith("select"), stmt
    assert "visual_frames" in stmt, stmt
    assert session.mutations == [], session.mutations


if __name__ == "__main__":  # pragma: no cover - manual runner, sibling-file style
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS  {fn.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL  {fn.__name__}: {e}")
    print("ALL PASS" if not failed else f"{failed} FAILED")
    sys.exit(1 if failed else 0)
