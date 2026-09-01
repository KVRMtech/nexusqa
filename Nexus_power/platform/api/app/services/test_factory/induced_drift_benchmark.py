"""TIER-5.1 — INDUCED-DRIFT BENCHMARK (standalone, $0, deterministic; MEASURES,
never changes the heal).

This is the canonical home of the induced-drift harness under ``test_factory/``.
It re-exports nothing magical: it is the single, self-contained implementation that
drives the REAL Auto-Heal endpoint contract once per Aegis ``?break=`` mode and scores
the outcomes. It MEASURES the shipping heal; it never imports, calls, or mutates any
heal-state code, so it can never green-wash a result by construction.

The Aegis proving ground renders the SAME workflow under a ``?break=`` query param,
and for each break mode the CORRECT Auto-Heal outcome is KNOWN a priori:

    rename        selector drifted (label text changed) -> re-anchor heals
                  -> terminal_state == clean_run_v1
    custom-combo  native <select> became a custom ARIA combobox -> interaction
                  re-synthesis heals  -> clean_run_v1
    control-kind  recipe is .fill on a <select> -> .fill->.selectOption heals
                  -> clean_run_v1
    regression    the business OUTCOME is genuinely broken (a missing
                  confirmation reference) -> Auto-Heal MUST REFUSE
                  -> terminal_state == needs_human   (a heal that goes green here
                     is the CARDINAL SIN — a FALSE HEAL)
    none          baseline, nothing broken -> clean_run_v1 (control)

This harness drives the REAL endpoint contract (it does not re-implement the heal):

    trigger :  POST /api/v1/test-factory/{artifact_id}/auto-heal/run-config
               body = RunConfigRequest, with base_url pointed at
                      {aegis_base}/?break={mode}
               -> { run_id, live_url, ... }
    poll    :  GET  /api/v1/test-factory/{artifact_id}/playwright/run/{run_id}
               -> { terminal_state, heal_trace, stop_reason, ... }
               terminal_state in {clean_run_v1, needs_human, error, None(=running)}

It then compares each mode's observed terminal_state to the known-correct outcome
and reports:

  * per-mode pass/fail,
  * FALSE-HEAL RATE  = (# of MUST-REFUSE modes that instead went clean_run_v1)
                       / (# of MUST-REFUSE modes run)   — the cardinal metric,
  * CORRECT-REFUSE RATE = (# of MUST-REFUSE modes that correctly refused)
                          / (# of MUST-REFUSE modes run),
  * a simple ECE-style CALIBRATION over the heal "confidence" the loop emitted
    (from heal_trace) bucketed against whether the heal was actually correct.

Pure orchestration over HTTP + arithmetic. No DB, no LLM, no heal-state mutation.
Runnable later (the VM is stopped now) via
``python -m scripts.run_induced_drift_benchmark`` (see that script).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import httpx

# ── Known-correct outcomes per break mode ────────────────────────────────────
# clean_run_v1  => Auto-Heal SHOULD heal to green.
# needs_human   => Auto-Heal MUST refuse (a green here is a FALSE HEAL).
CLEAN = "clean_run_v1"
REFUSE = "needs_human"

# The Aegis break modes with a KNOWN-correct heal outcome. (Other Aegis modes —
# slow/late/shadow/iframe/hashed — are timing/locator-robustness cases without a
# single canonical heal verdict, so they are excluded from the false-heal metric.)
EXPECTED: dict[str, str] = {
    "none": CLEAN,            # baseline control — nothing broken, must run green
    "rename": CLEAN,          # re-anchor heals
    "custom-combo": CLEAN,    # interaction re-synthesis heals
    "control-kind": CLEAN,    # .fill -> .selectOption heals
    "regression": REFUSE,     # MUST refuse — the cardinal false-heal test
}

# Default modes to run (override per call).
DEFAULT_MODES = tuple(EXPECTED.keys())


@dataclass
class ModeResult:
    mode: str
    expected: str
    terminal_state: str | None
    run_id: str = ""
    stop_reason: str = ""
    heal_confidences: list[float] = field(default_factory=list)
    error: str = ""

    @property
    def must_refuse(self) -> bool:
        return self.expected == REFUSE

    @property
    def passed(self) -> bool:
        """Correct iff the observed terminal_state matches the known-correct one."""
        return self.terminal_state == self.expected

    @property
    def false_heal(self) -> bool:
        """The cardinal sin: a MUST-REFUSE mode that instead went green."""
        return self.must_refuse and self.terminal_state == CLEAN

    @property
    def correct_refuse(self) -> bool:
        return self.must_refuse and self.terminal_state == REFUSE


def _mode_url(aegis_base: str, mode: str) -> str:
    base = (aegis_base or "").rstrip("/")
    if mode == "none":
        return base + "/"
    return f"{base}/?break={mode}"


def _extract_confidences(heal_trace: list[dict]) -> list[float]:
    """Pull any per-step heal 'confidence' the loop recorded into the trace. The
    loop traces heal_applied / stop_* events; a confidence (when present, e.g. on a
    stop_diag or the heal_policy event) is bucketed for calibration. Absent => empty
    (calibration just has fewer samples; it never fabricates a confidence)."""
    out: list[float] = []
    for ev in heal_trace or []:
        if not isinstance(ev, dict):
            continue
        for key in ("confidence", "conf"):
            if key in ev:
                try:
                    out.append(float(ev[key]))
                except (TypeError, ValueError):
                    pass
        diag = ev.get("stop_diag") if isinstance(ev.get("stop_diag"), dict) else None
        if diag and "confidence" in diag:
            try:
                out.append(float(diag["confidence"]))
            except (TypeError, ValueError):
                pass
    return out


async def _run_one_mode(
    client: httpx.AsyncClient,
    *,
    api_base: str,
    artifact_id: str,
    mode: str,
    aegis_base: str,
    poll_interval_s: float,
    poll_max_s: float,
) -> ModeResult:
    expected = EXPECTED[mode]
    res = ModeResult(mode=mode, expected=expected, terminal_state=None)
    trigger_url = f"{api_base}/api/v1/test-factory/{artifact_id}/auto-heal/run-config"
    body = {
        "base_url": _mode_url(aegis_base, mode),
        # Deterministic, single-display HEADED prove path = exactly the demo path.
        # (The benchmark MEASURES the shipping behaviour; it does not opt into the
        # headless/SLA/flake knobs so what it scores is what the demo does.)
        "browsers": ["chromium"],
    }
    try:
        r = await client.post(trigger_url, json=body)
        r.raise_for_status()
        run_id = (r.json() or {}).get("run_id") or ""
    except Exception as exc:
        res.error = f"trigger failed: {exc}"
        return res
    res.run_id = run_id
    if not run_id:
        res.error = "trigger returned no run_id"
        return res

    poll_url = f"{api_base}/api/v1/test-factory/{artifact_id}/playwright/run/{run_id}"
    elapsed = 0.0
    while elapsed < poll_max_s:
        await asyncio.sleep(poll_interval_s)
        elapsed += poll_interval_s
        try:
            pr = await client.get(poll_url)
            pr.raise_for_status()
            job = pr.json() or {}
        except Exception:
            continue  # transient — keep polling within the budget
        ts = job.get("terminal_state")
        if ts:  # terminal: clean_run_v1 | needs_human | error
            res.terminal_state = ts
            res.stop_reason = job.get("stop_reason", "") or ""
            res.heal_confidences = _extract_confidences(job.get("heal_trace") or [])
            return res
    res.error = f"no terminal_state within {poll_max_s:.0f}s (still running / runner busy)"
    return res


def _ece(samples: list[tuple[float, bool]], *, n_buckets: int = 5) -> dict:
    """Expected Calibration Error over (confidence, was_correct) samples.

    Buckets confidences into n equal-width bins on [0,1]; ECE = sum_b
    (|bin|/N) * |mean_confidence_b - accuracy_b|. Lower is better-calibrated. Returns
    {ece, buckets:[{lo,hi,n,mean_conf,accuracy}]}. Empty samples => ece=None."""
    if not samples:
        return {"ece": None, "buckets": [], "n": 0}
    buckets = []
    n = len(samples)
    ece = 0.0
    width = 1.0 / n_buckets
    for b in range(n_buckets):
        lo, hi = b * width, (b + 1) * width
        in_b = [(c, ok) for (c, ok) in samples
                if (c >= lo and (c < hi or (b == n_buckets - 1 and c <= hi)))]
        if not in_b:
            buckets.append({"lo": round(lo, 2), "hi": round(hi, 2), "n": 0,
                            "mean_conf": None, "accuracy": None})
            continue
        mean_conf = sum(c for c, _ in in_b) / len(in_b)
        acc = sum(1 for _, ok in in_b if ok) / len(in_b)
        ece += (len(in_b) / n) * abs(mean_conf - acc)
        buckets.append({"lo": round(lo, 2), "hi": round(hi, 2), "n": len(in_b),
                        "mean_conf": round(mean_conf, 3), "accuracy": round(acc, 3)})
    return {"ece": round(ece, 4), "buckets": buckets, "n": n}


def score_results(results: list[ModeResult]) -> dict:
    """Aggregate per-mode results into the benchmark report — the false-heal rate
    (cardinal), correct-refuse rate, overall pass rate, and an ECE-style calibration."""
    must_refuse = [r for r in results if r.must_refuse]
    false_heals = [r for r in must_refuse if r.false_heal]
    correct_refuses = [r for r in must_refuse if r.correct_refuse]
    scored = [r for r in results if r.terminal_state is not None]
    passed = [r for r in scored if r.passed]

    # Calibration samples: each mode contributes (max heal confidence the loop
    # emitted, whether the mode's outcome was CORRECT). A higher confidence on a
    # correct outcome and a lower confidence on a wrong one => well-calibrated.
    cal_samples: list[tuple[float, bool]] = []
    for r in scored:
        if r.heal_confidences:
            cal_samples.append((max(r.heal_confidences), bool(r.passed)))

    return {
        "modes_run": len(results),
        "modes_scored": len(scored),
        "pass_rate": (len(passed) / len(scored)) if scored else None,
        # CARDINAL: of the must-refuse modes, how many wrongly went green.
        "false_heal_rate": (len(false_heals) / len(must_refuse)) if must_refuse else None,
        "false_heals": [r.mode for r in false_heals],
        "correct_refuse_rate": (len(correct_refuses) / len(must_refuse)) if must_refuse else None,
        "calibration": _ece(cal_samples),
        "per_mode": [
            {
                "mode": r.mode, "expected": r.expected,
                "terminal_state": r.terminal_state, "passed": r.passed,
                "false_heal": r.false_heal, "stop_reason": r.stop_reason,
                "heal_confidences": r.heal_confidences, "run_id": r.run_id,
                "error": r.error,
            }
            for r in results
        ],
    }


async def run_benchmark(
    *,
    api_base: str,
    artifact_id: str,
    token: str,
    aegis_base: str,
    modes: tuple[str, ...] = DEFAULT_MODES,
    poll_interval_s: float = 3.0,
    poll_max_s: float = 600.0,
) -> dict:
    """Drive Auto-Heal once per break mode (SERIALLY — the runner serves ONE live
    display at a time) and score the outcomes.

    api_base    : platform-api origin, e.g. http://localhost:8091
    artifact_id : an artifact whose generated suite exercises the Aegis workflow
                  (generate it once against Aegis before running the benchmark).
    token       : a valid JWT (the heal endpoints are RBAC-gated to admin|manager).
    aegis_base  : the Aegis app origin REACHABLE BY THE RUNNER, e.g.
                  http://nexus-aegis (the runner injects ?break per mode).
    """
    unknown = [m for m in modes if m not in EXPECTED]
    if unknown:
        raise ValueError(f"unknown break mode(s) with no known-correct outcome: {unknown}")

    headers = {"Authorization": f"Bearer {token}"} if token else {}
    timeout = httpx.Timeout(connect=10.0, read=60.0, write=60.0, pool=10.0)
    results: list[ModeResult] = []
    async with httpx.AsyncClient(headers=headers, timeout=timeout) as client:
        for mode in modes:
            results.append(await _run_one_mode(
                client, api_base=api_base, artifact_id=artifact_id, mode=mode,
                aegis_base=aegis_base, poll_interval_s=poll_interval_s,
                poll_max_s=poll_max_s,
            ))
    report = score_results(results)
    report["artifact_id"] = artifact_id
    report["aegis_base"] = aegis_base
    return report


__all__ = [
    "EXPECTED", "DEFAULT_MODES", "CLEAN", "REFUSE",
    "ModeResult", "run_benchmark", "score_results",
    "_mode_url", "_extract_confidences", "_ece",
]
