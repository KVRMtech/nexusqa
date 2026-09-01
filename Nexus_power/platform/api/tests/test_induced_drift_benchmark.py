"""Standalone unit test for the TIER-5.1 induced-drift Auto-Heal benchmark.

NO live stack, NO DB, NO LLM, NO real network: the HTTP layer is faked so the test
exercises the pure scoring/calibration math AND the trigger/poll orchestration
deterministically. The point of this benchmark is to MEASURE the heal and, above all,
to make the CARDINAL SIN visible — a MUST-REFUSE regression that goes green
(false_heal). These tests pin that semantics.

Run from Nexus_power/platform/api:
    python -m pytest tests/test_induced_drift_benchmark.py -q
or, with no pytest available, directly:
    python tests/test_induced_drift_benchmark.py
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import sys

# Load the benchmark module DIRECTLY by file path. The benchmark only depends on
# httpx + stdlib (by design — it MEASURES the heal over HTTP and never imports
# heal-state code), so we deliberately bypass `app/__init__.py`, which eagerly
# imports the nexus_sdk package that lives in the SEPARATE nexus-base image and is
# not present in this repo. This keeps the test genuinely standalone.
_API_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _API_ROOT not in sys.path:
    sys.path.insert(0, _API_ROOT)

_MOD_PATH = os.path.join(
    _API_ROOT, "app", "services", "test_factory", "induced_drift_benchmark.py"
)
_spec = importlib.util.spec_from_file_location(
    "induced_drift_benchmark_under_test", _MOD_PATH
)
mod = importlib.util.module_from_spec(_spec)
assert _spec and _spec.loader
# Register before exec so dataclass introspection (which resolves cls.__module__ via
# sys.modules on Python 3.10) can find the module while ModeResult is being built.
sys.modules[_spec.name] = mod
_spec.loader.exec_module(mod)

CLEAN = mod.CLEAN
REFUSE = mod.REFUSE
EXPECTED = mod.EXPECTED
ModeResult = mod.ModeResult
_ece = mod._ece
_extract_confidences = mod._extract_confidences
_mode_url = mod._mode_url
run_benchmark = mod.run_benchmark
score_results = mod.score_results


# ── pure helpers ──────────────────────────────────────────────────────────────

def test_mode_url_baseline_has_no_break_param():
    assert _mode_url("http://nexus-aegis", "none") == "http://nexus-aegis/"
    assert _mode_url("http://nexus-aegis/", "none") == "http://nexus-aegis/"


def test_mode_url_injects_break_param():
    assert _mode_url("http://nexus-aegis", "rename") == "http://nexus-aegis/?break=rename"
    assert _mode_url("http://nexus-aegis/", "regression") == "http://nexus-aegis/?break=regression"


def test_extract_confidences_pulls_top_level_and_stop_diag():
    trace = [
        {"event": "heal_applied", "confidence": 0.85},
        {"event": "noise"},
        {"event": "stop", "stop_diag": {"confidence": 0.4, "cause_label": "REAL_REGRESSION"}},
        {"event": "conf_key", "conf": "0.7"},          # string coerces
        {"event": "bad", "confidence": "not-a-number"},  # ignored, no crash
    ]
    out = _extract_confidences(trace)
    assert out == [0.85, 0.4, 0.7]


def test_extract_confidences_empty_never_fabricates():
    assert _extract_confidences([]) == []
    assert _extract_confidences(None) == []  # type: ignore[arg-type]


def test_ece_empty_is_none_not_zero():
    cal = _ece([])
    assert cal["ece"] is None and cal["n"] == 0


def test_ece_perfect_calibration_is_zero():
    # Within bucket [0.8,1.0): 5 samples at conf 1.0, all correct => mean_conf 1.0,
    # accuracy 1.0 => gap 0. Within bucket [0.0,0.2): conf 0.0, all wrong =>
    # mean_conf 0.0, accuracy 0.0 => gap 0. So ECE == 0 (truly perfect).
    samples = [(1.0, True), (1.0, True), (1.0, True),
               (0.0, False), (0.0, False)]
    cal = _ece(samples)
    assert cal["ece"] == 0.0
    assert cal["n"] == 5


def test_ece_overconfident_wrong_is_penalized():
    # high confidence but all wrong => large gap.
    samples = [(0.95, False), (0.9, False)]
    cal = _ece(samples)
    assert cal["ece"] is not None and cal["ece"] > 0.5


# ── ModeResult semantics ────────────────────────────────────────────────────

def test_false_heal_is_a_must_refuse_mode_gone_green():
    r = ModeResult(mode="regression", expected=REFUSE, terminal_state=CLEAN)
    assert r.must_refuse and r.false_heal and not r.passed and not r.correct_refuse


def test_correct_refuse_is_a_must_refuse_mode_that_refused():
    r = ModeResult(mode="regression", expected=REFUSE, terminal_state=REFUSE)
    assert r.correct_refuse and r.passed and not r.false_heal


def test_clean_mode_going_green_is_a_pass_and_never_a_false_heal():
    r = ModeResult(mode="rename", expected=CLEAN, terminal_state=CLEAN)
    assert r.passed and not r.must_refuse and not r.false_heal


def test_clean_mode_refusing_is_a_miss_not_a_false_heal():
    # A heal that gave up on a healable mode is a MISS (coverage gap), not the
    # cardinal sin — it did not green-wash.
    r = ModeResult(mode="rename", expected=CLEAN, terminal_state=REFUSE)
    assert not r.passed and not r.false_heal


# ── scoring roll-up ──────────────────────────────────────────────────────────

def test_score_results_all_correct_zero_false_heal():
    results = [
        ModeResult("none", CLEAN, CLEAN, heal_confidences=[]),
        ModeResult("rename", CLEAN, CLEAN, heal_confidences=[0.85]),
        ModeResult("custom-combo", CLEAN, CLEAN, heal_confidences=[0.82]),
        ModeResult("control-kind", CLEAN, CLEAN, heal_confidences=[0.88]),
        ModeResult("regression", REFUSE, REFUSE, heal_confidences=[0.4]),
    ]
    rep = score_results(results)
    assert rep["false_heal_rate"] == 0.0
    assert rep["false_heals"] == []
    assert rep["correct_refuse_rate"] == 1.0
    assert rep["pass_rate"] == 1.0
    assert rep["modes_scored"] == 5
    # 4 modes emitted a confidence (none had none) => 4 calibration samples.
    assert rep["calibration"]["n"] == 4


def test_score_results_flags_the_cardinal_false_heal():
    results = [
        ModeResult("rename", CLEAN, CLEAN),
        ModeResult("regression", REFUSE, CLEAN),  # green-washed regression!
    ]
    rep = score_results(results)
    assert rep["false_heal_rate"] == 1.0
    assert rep["false_heals"] == ["regression"]
    assert rep["correct_refuse_rate"] == 0.0


def test_score_results_unfinished_run_not_scored():
    # terminal_state None (poll budget exhausted) is excluded from pass_rate and
    # calibration, but does NOT silently count as a pass.
    results = [
        ModeResult("rename", CLEAN, CLEAN),
        ModeResult("control-kind", CLEAN, None, error="no terminal_state within 600s"),
    ]
    rep = score_results(results)
    assert rep["modes_run"] == 2 and rep["modes_scored"] == 1
    assert rep["pass_rate"] == 1.0  # only the scored one


def test_score_results_no_refuse_modes_yields_none_rates():
    rep = score_results([ModeResult("rename", CLEAN, CLEAN)])
    assert rep["false_heal_rate"] is None
    assert rep["correct_refuse_rate"] is None


# ── end-to-end orchestration with a faked HTTP client ─────────────────────────

class _FakeResp:
    def __init__(self, payload: dict):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _FakeClient:
    """Stand-in for httpx.AsyncClient: trigger returns a per-mode run_id, and the
    first poll for that run_id returns its scripted terminal job. Encodes the mode
    in the run_id so poll responses are deterministic and order-independent."""

    def __init__(self, scripted: dict[str, dict]):
        # scripted: mode -> job dict to return on poll (terminal_state etc.)
        self._scripted = scripted
        self.posts: list[str] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url: str, json: dict | None = None):
        self.posts.append(url)
        # url: .../test-factory/{artifact}/auto-heal/run-config ; the break mode is
        # in the request body base_url.
        base_url = (json or {}).get("base_url", "")
        if "break=" in base_url:
            mode = base_url.split("break=", 1)[1]
        else:
            mode = "none"
        return _FakeResp({"run_id": f"run-{mode}"})

    async def get(self, url: str):
        # url: .../playwright/run/run-{mode}
        run_id = url.rsplit("/", 1)[-1]
        mode = run_id[len("run-"):]
        return _FakeResp(self._scripted.get(mode, {"terminal_state": "error"}))


def _patch_client(fake: _FakeClient):
    def _factory(*a, **k):
        return fake

    # Replace AsyncClient constructor used inside run_benchmark (same module object
    # loaded by file path above).
    mod.httpx.AsyncClient = _factory  # type: ignore[assignment]
    # No real sleeping in the poll loop.
    orig_sleep = mod.asyncio.sleep

    async def _no_sleep(_s):
        return None

    mod.asyncio.sleep = _no_sleep  # type: ignore[assignment]
    return orig_sleep


def test_run_benchmark_happy_path_zero_false_heal():
    scripted = {
        "none": {"terminal_state": CLEAN, "heal_trace": [], "stop_reason": ""},
        "rename": {"terminal_state": CLEAN, "heal_trace": [{"confidence": 0.85}]},
        "custom-combo": {"terminal_state": CLEAN, "heal_trace": [{"confidence": 0.82}]},
        "control-kind": {"terminal_state": CLEAN, "heal_trace": [{"confidence": 0.88}]},
        "regression": {"terminal_state": REFUSE,
                       "heal_trace": [{"stop_diag": {"confidence": 0.4}}],
                       "stop_reason": "REAL_REGRESSION — needs a human"},
    }
    fake = _FakeClient(scripted)
    orig_sleep = _patch_client(fake)
    try:
        rep = asyncio.new_event_loop().run_until_complete(
            run_benchmark(api_base="http://localhost:8091", artifact_id="aegis-1",
                          token="jwt", aegis_base="http://nexus-aegis",
                          poll_interval_s=0.0, poll_max_s=1.0)
        )
    finally:
        mod.asyncio.sleep = orig_sleep  # type: ignore[assignment]
    assert rep["false_heal_rate"] == 0.0
    assert rep["false_heals"] == []
    assert rep["correct_refuse_rate"] == 1.0
    assert rep["pass_rate"] == 1.0
    assert rep["artifact_id"] == "aegis-1"
    # Every mode was triggered exactly once.
    assert len(fake.posts) == len(EXPECTED)


def test_run_benchmark_detects_a_green_washed_regression():
    scripted = {m: {"terminal_state": CLEAN, "heal_trace": []} for m in EXPECTED}
    # regression WRONGLY heals green — the cardinal sin the benchmark must catch.
    fake = _FakeClient(scripted)
    orig_sleep = _patch_client(fake)
    try:
        rep = asyncio.new_event_loop().run_until_complete(
            run_benchmark(api_base="http://x", artifact_id="a", token="t",
                          aegis_base="http://nexus-aegis",
                          poll_interval_s=0.0, poll_max_s=1.0)
        )
    finally:
        mod.asyncio.sleep = orig_sleep  # type: ignore[assignment]
    assert rep["false_heal_rate"] == 1.0
    assert rep["false_heals"] == ["regression"]


def test_run_benchmark_rejects_unknown_mode():
    raised = False
    try:
        asyncio.new_event_loop().run_until_complete(
            run_benchmark(api_base="http://x", artifact_id="a", token="t",
                          aegis_base="http://y", modes=("definitely-not-a-mode",))
        )
    except ValueError:
        raised = True
    assert raised


# ── allow direct execution without pytest ─────────────────────────────────────

if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    failures = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"FAIL {fn.__name__}: {exc!r}")
    print(f"\n{len(fns) - failures}/{len(fns)} passed")
    raise SystemExit(1 if failures else 0)
