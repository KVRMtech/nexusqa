"""Auto-authored defect report (Auto-Heal V0 — the wedge no competitor ships).

When the verdict is a REAL APPLICATION BUG (the recorded outcome was contradicted, or a
server/network error fired in the failing step's window) we DO NOT heal. We author a
structured, independently-REPLAYABLE bug report as the DIRECT OUTPUT of that verdict:
  * the recorded grounded steps 1..N as exact reproduction (verb + value + url),
  * the precise failing step + locator + action + value,
  * EXPECTED vs ACTUAL, both grounded (recorded outcome vs contradiction / network error),
  * an evidence bundle (baseline/actual screenshots, network excerpt, trace, Part-11 ref).

Rivals heal-and-continue, or hand a human an evidence dump; none auto-author a replayable
repro tied to a bug-vs-test verdict. That is exactly this artifact.

Pure + stdlib-only (duck-typed test case / steps; each step exposes ``.observed`` dict +
``.step_number`` + ``.action`` + ``.selector`` + ``.expected``/``.expected_result``). Never
claims BUSINESS severity — only FLOW-mechanical impact (does it block the rest of the
recorded flow?). The caller (the auto-heal loop) attaches the result to the run job and the
markdown drives the existing "Copy defect" / "Download .md" surface.
"""
from __future__ import annotations


def _obs(step) -> dict:
    return getattr(step, "observed", None) or {}


def _steps(tc) -> list:
    return [s for s in (getattr(tc, "steps", None) or []) if getattr(s, "step_number", None) is not None]


def _step_at(tc, n):
    for s in _steps(tc):
        if getattr(s, "step_number", None) == n:
            return s
    return None


def _action_of(step) -> str:
    if step is None:
        return "(action)"
    return (getattr(step, "action", "") or "").strip() or _obs(step).get("verb", "") or "(action)"


def _expected_of(step) -> str:
    if step is None:
        return ""
    o = _obs(step)
    nav = ("navigates to " + o["next_url"]) if o.get("next_url") else ""
    return (getattr(step, "expected_result", "") or getattr(step, "expected", "")
            or o.get("after", "") or nav).strip()


def _short(s: str, n: int = 90) -> str:
    s = (s or "").strip().replace("\n", " ")
    return s[:n] + ("…" if len(s) > n else "")


def _app_from_url(url: str) -> str:
    u = (url or "").strip()
    if not u:
        return ""
    u = u.split("://", 1)[-1].split("/", 1)[0]
    return u or ""


def build_defect(*, tc, failing_step_number, diag=None, network=None, error_message="",
                 base_url="", scenario_id="", app_name="", baseline_screenshot=None,
                 actual_screenshot=None, trace_ref=None, part11_ref=None) -> dict:
    """Assemble a grounded, replayable DefectReport for a REAL regression. Pure + deterministic."""
    diag = diag or {}
    fstep = _step_at(tc, failing_step_number)
    fobs = _obs(fstep)
    app = (app_name or "").strip() or _app_from_url(base_url) or "The application"
    name = getattr(tc, "name", "") or scenario_id or "E2E flow"
    expected = _expected_of(fstep)

    # Severity = FLOW-mechanical ONLY (blocks the rest of the recorded flow?), never business.
    downstream = [s for s in _steps(tc) if (getattr(s, "step_number", 0) or 0) > (failing_step_number or 0)]
    severity = "critical" if downstream else "major"
    severity_basis = ("flow-mechanical: blocks the remaining recorded steps"
                      if downstream else "flow-mechanical: final recorded step")

    # Repro = recorded grounded steps 1..N verbatim, the failing one flagged.
    repro = []
    for s in _steps(tc):
        n = getattr(s, "step_number", None)
        if n is None or n > failing_step_number:
            continue
        o = _obs(s)
        repro.append({
            "step": n, "action": _action_of(s), "value": o.get("value", "") or "",
            "url": o.get("url", "") or "", "is_failing": n == failing_step_number,
        })

    actual_bits = []
    if diag.get("cause") == "REAL_REGRESSION":
        actual_bits.append("the recorded outcome was contradicted — the flow did not reach the recorded result")
    if network:
        actual_bits.append(network.get("detail", "") or network.get("kind", ""))
    if not actual_bits:
        actual_bits.append("the recorded outcome was not observed at this step")

    title = (f"[Product Bug] {app} — \"{_short(expected) or name}\" did not occur after "
             f"step {failing_step_number} ({_action_of(fstep)})")

    return {
        "classification": "REAL_REGRESSION",
        "title": title,
        "severity": severity,
        "severity_basis": severity_basis,
        "confidence": float(diag.get("confidence") or 0.0),
        "precise_failure": {
            "scenario_id": scenario_id,
            "step_number": failing_step_number,
            "action": _action_of(fstep),
            "verb": fobs.get("verb", ""),
            "control_label": fobs.get("label", ""),
            "control_kind": fobs.get("kind", ""),
            "value": fobs.get("value", ""),
            "locator": (getattr(fstep, "selector", "") or "") if fstep is not None else "",
        },
        "repro_steps": repro,
        "expected": expected or "(recorded baseline outcome)",
        "actual": "; ".join([b for b in actual_bits if b]),
        "error_message": (error_message or diag.get("error_message") or "")[:1200],
        "network": network or None,
        "evidence": {
            "baseline_screenshot": baseline_screenshot,
            "actual_screenshot": actual_screenshot,
            "trace_ref": trace_ref,
            "part11_ref": part11_ref,
        },
        "recommended_action": (diag.get("recommended_action") or "").strip() or
            ("Do NOT heal — this is a real application regression. Confirm against the "
             "recorded baseline, then route to engineering."),
    }


def defect_to_markdown(d: dict) -> str:
    """Render a DefectReport to filing-ready markdown (paste into Jira / Linear / Xray)."""
    d = d or {}
    pf = d.get("precise_failure", {}) or {}
    ev = d.get("evidence", {}) or {}
    net = d.get("network") or {}
    out = []
    out.append(f"# {d.get('title', '[Product Bug]')}")
    out.append("")
    out.append(f"**Classification:** {d.get('classification', 'REAL_REGRESSION')}  ·  "
               f"**Severity:** {d.get('severity', 'major')} ({d.get('severity_basis', '')})  ·  "
               f"**Confidence:** {round(float(d.get('confidence', 0)) * 100)}%")
    out.append(f"**Failed at:** step {pf.get('step_number', '—')} — {pf.get('action', '')}")
    out.append("**Environment:** nexus-runner")
    out.append("")
    out.append("## Steps to reproduce")
    for r in d.get("repro_steps", []):
        mark = "  ⟵ **FAILS HERE**" if r.get("is_failing") else ""
        val = f" → `{r['value']}`" if r.get("value") else ""
        url = f"  _({r['url']})_" if r.get("url") else ""
        out.append(f"{r.get('step')}. {r.get('action', '')}{val}{url}{mark}")
    out.append("")
    out.append("## Precise failure point")
    out.append(f"- **Step:** {pf.get('step_number', '—')} — {pf.get('action', '')}")
    if pf.get("control_label"):
        out.append(f"- **Control:** {pf.get('control_label')} ({pf.get('control_kind') or 'control'})")
    if pf.get("locator"):
        out.append(f"- **Locator:** `{pf.get('locator')}`")
    if pf.get("value"):
        out.append(f"- **Value:** `{pf.get('value')}`")
    out.append("")
    out.append("## Expected vs Actual")
    out.append(f"- **Expected:** {d.get('expected', '')}")
    out.append(f"- **Actual:** {d.get('actual', '')}")
    out.append("")
    if net:
        out.append("## Network / API")
        out.append(f"- `{net.get('detail') or net.get('kind', '')}`")
        out.append("")
    if d.get("error_message"):
        out.append("## Error")
        out.append("```")
        out.append(d["error_message"])
        out.append("```")
        out.append("")
    out.append("## Evidence")
    out.append(f"- Known-good baseline: {ev.get('baseline_screenshot') or '(none)'}")
    out.append(f"- This run: {ev.get('actual_screenshot') or '(awaiting capture)'}")
    out.append(f"- Trace: {ev.get('trace_ref') or '(see run)'}")
    if ev.get("part11_ref"):
        out.append(f"- Audit (Part-11): `{ev['part11_ref']}`")
    out.append("")
    out.append("_Auto-authored by Nexus from the grounded recording + the run verdict. "
               "We REFUSED to heal this — it is a real regression, not a selector issue._")
    return "\n".join(out)
