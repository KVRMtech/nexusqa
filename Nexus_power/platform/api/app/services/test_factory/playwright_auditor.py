"""Agentic Playwright Auditor — score a generated .spec.ts for PHYSICAL
POSSIBILITY + GROUNDING, the way an independent auditor would, and never green-wash.

Two layers, deterministic-first:

  1. ``score_spec()`` — the DETERMINISTIC rubric (D1-D5). It is the source of truth
     for the number: $0, no LLM, flake-free, the CI gate AND the fail-open fallback.
     The LLM never scores itself. It operationalises the five auditor dimensions:
        D1 playwright_apis      — first-class APIs, no sleeps, no dead scaffolding
        D2 locator_quality      — accessible names, no getByText-on-a-value
        D3 grounded_replay      — every recorded typed value is represented as a fill
        D4 navigation_correctness — a toHaveURL is asserted ONLY for a click the
                                    recording proves navigated (the impossible-
                                    transition axis; any ungrounded toHaveURL ⇒ 0)
        D5 assertion_correctness  — no assertion can only-ever-fail-RED, no same-page
                                    green-wash, the "solid step" count is honest
     overall is MIN-gated: any dimension's hard-deduction sinks the whole score
     (a single impossible assertion must tank it — the saucedemo failure mode).

  2. ``audit()`` — the LLM layer (mirrors ``agentic_heal.propose``): per-step
     verdicts + repair directives. Every claim is grounded-validated — an
     ``evidence_quote`` must be a VERBATIM substring of the recording evidence or
     the finding is demoted to ``mark_unproven`` / ``file_defect``. The agent NEVER
     writes Playwright and can NEVER turn a red/unproven step green. On any LLM
     failure it falls back to the deterministic verdict — never crash, never
     auto-certify.

The repaired spec is always COMPILER-emitted (the applier re-runs ``compile_case``
through existing additive channels); no LLM string is ever spliced into a .spec.ts.
"""
from __future__ import annotations

import re
from typing import Any

# ── Dimensions + verdict vocabulary ──────────────────────────────────────────
DIMENSIONS = (
    "playwright_apis",
    "locator_quality",
    "grounded_replay",
    "navigation_correctness",
    "assertion_correctness",
)
# Per-step verdicts (deterministic + LLM share this vocabulary).
V_OK = "grounded_ok"
V_IMPOSSIBLE = "impossible_transition"
V_UNGROUNDED = "ungrounded_assertion"
V_MISSING_PREREQ = "missing_prerequisite"
V_DEAD = "dead_or_unused"
V_DATA = "data_not_replayed"
V_INFO = "documented_exemption"

DECISION_CERTIFIED = "certified"
DECISION_REPAIR = "repair"
DECISION_DEFECT = "defect"

_FILL_VERBS = {"type", "fill", "input", "select"}
_SLEEP_RX = re.compile(r"waitForTimeout\s*\(")
_GETBYTEXT_RX = re.compile(r"getByText\(\s*['\"/]")


def _clamp(n: int) -> int:
    return max(0, min(10, n))


def _sv(step: Any) -> dict:
    """Normalise a step (ProductionTestStep OR dict) to a flat view."""
    if isinstance(step, dict):
        obs = step.get("observed") or {}
        prov = step.get("provenance") or ""
        action = step.get("action") or ""
        expected = step.get("expected_result") or step.get("expected") or ""
        num = step.get("step_number")
    else:
        obs = getattr(step, "observed", None) or {}
        prov = getattr(step, "provenance", "") or ""
        action = getattr(step, "action", "") or ""
        expected = getattr(step, "expected_result", "") or getattr(step, "expected", "") or ""
        num = getattr(step, "step_number", None)
    return {
        "step_number": num,
        "action": action,
        "expected": expected,
        "provenance": (prov or "").strip().lower(),
        "verb": (obs.get("verb") or "").strip().lower(),
        "label": obs.get("label") or "",
        "value": obs.get("value"),
        "url": obs.get("url") or "",
        "next_url": obs.get("next_url") or "",
        "navigation_grounded": bool(obs.get("navigation_grounded")),
        "after": obs.get("after") or "",
        "kind": obs.get("kind") or "",
    }


def _evidence_typed_values(evidence: Any) -> list[tuple[str, str]]:
    """(label, value) the recording shows the user TYPED — from page_actions."""
    out: list[tuple[str, str]] = []
    for a in (evidence or []):
        verb = (a.get("verb") if isinstance(a, dict) else getattr(a, "verb", "")) or ""
        if verb.strip().lower() not in _FILL_VERBS:
            continue
        label = (a.get("target_label") if isinstance(a, dict) else getattr(a, "target_label", "")) or ""
        value = (a.get("value") if isinstance(a, dict) else getattr(a, "value", None))
        if label and value not in (None, ""):
            out.append((str(label).strip(), str(value).strip()))
    return out


def _emits_to_have_url(v: dict) -> bool:
    """Would this step compile to a toHaveURL assertion? (mirrors the compiler:
    a click carrying next_url, or a non-entry navigate-verify that is NOT inferred.)"""
    if v["next_url"]:
        return True
    if v["verb"] == "navigate" and not v["action"].lower().startswith("open"):
        return v["provenance"] != "inferred"
    return False


def _to_have_url_grounded(v: dict) -> bool:
    """Is that toHaveURL grounded? next_url needs navigation_grounded; a
    demonstrated navigate-verify was emitted only when the prior boundary was proven."""
    if v["next_url"]:
        return v["navigation_grounded"]
    return v["provenance"] == "demonstrated"


_AUTH_FIELD_TOKENS = ("username", "user name", "user id", "email", "password", "login")


def _skip_guarded_steps(spec_text: str) -> set:
    """Step numbers the DELIVERED spec skip-guards (test.skip UNPROVEN). Those
    steps assert nothing at runtime, so they can never be 'impossible' — they
    are honest coverage gaps. Parsed from the artifact itself, so the audit
    always reflects what actually ships."""
    return {int(n) for n in re.findall(r"UNPROVEN:\s*step\s+(\d+)", spec_text or "")}


def score_spec(spec_text: str, steps: list, evidence: Any = None) -> dict:
    """Deterministic 5-dimension audit of a compiled spec + its grounded steps.

    Returns ``{dimension_scores, overall, per_step, decision, findings, gaps}``.
    The number is computed here (never self-reported by an LLM)."""
    spec_text = spec_text or ""
    views = [_sv(s) for s in (steps or [])]
    findings: list[str] = []
    per_step: list[dict] = []

    # ── D1 — Uses Playwright APIs (static) ───────────────────────────────────
    d1 = 10
    sleeps = len(_SLEEP_RX.findall(spec_text))
    d1 -= 2 * sleeps
    if sleeps:
        findings.append(f"{sleeps} raw waitForTimeout() sleep(s) — use a condition-based wait.")
    # dead data file: `require('...vkpower.data.json')` loaded into D but never used.
    dead_data = False
    if "require(" in spec_text and re.search(r"\.data\.json", spec_text):
        body_uses = len(re.findall(r"\bD\s*\[", spec_text)) + len(re.findall(r"\bD\.", spec_text))
        if body_uses == 0:
            dead_data = True
            d1 -= 2
            findings.append("Data file is required but never used (dead scaffolding).")
    # declared-but-unused helpers (e.g. __nxTok defined, never called).
    for m in re.finditer(r"function\s+(__\w+)\s*\(", spec_text):
        name = m.group(1)
        if len(re.findall(re.escape(name) + r"\s*\(", spec_text)) <= 1:  # only the definition
            d1 -= 1
            findings.append(f"Helper {name}() is declared but never used (dead scaffolding).")
    d1 = _clamp(d1)

    # ── D2 — Locator quality (static) ────────────────────────────────────────
    d2 = 10
    fill_values = {str(v["value"]).strip() for v in views if v["verb"] in _FILL_VERBS and v["value"]}
    # getByText asserting on a FIELD VALUE (must use toHaveValue) → -3 each.
    for m in _GETBYTEXT_RX.finditer(spec_text):
        seg = spec_text[m.start(): m.start() + 120]
        if any(val and val in seg for val in fill_values):
            d2 -= 3
            findings.append("A field value is asserted via getByText — use toHaveValue on the field.")
    d2 = _clamp(d2)

    # ── D4 — Navigation correctness (the causality axis) ─────────────────────
    d4 = 10
    _guarded = _skip_guarded_steps(spec_text)
    impossible_steps: list[dict] = []
    for v in views:
        if v["step_number"] in _guarded:
            continue  # skip-guarded in the delivered spec — asserts nothing
        if _emits_to_have_url(v) and not _to_have_url_grounded(v):
            impossible_steps.append(v)
            per_step.append({
                "step_number": v["step_number"], "verdict": V_IMPOSSIBLE,
                "detail": f"'{v['action']}' asserts navigation to {v['next_url'] or v['url']} but the "
                          "recording does not show this action caused it.",
            })
    if impossible_steps:
        d4 = 0  # a single impossible toHaveURL is the spine failure — tank the axis
        findings.append(
            f"{len(impossible_steps)} navigation assertion(s) attribute a page change to a click "
            "the recording does not show navigating (impossible transition).")
    d4 = _clamp(d4)

    # ── D5 — Assertion correctness ───────────────────────────────────────────
    d5 = 10
    if impossible_steps:
        d5 = min(d5, 1)  # an always-RED assertion
    # same-page green-wash: a toHaveURL whose asserted path == the step's own page.
    for v in views:
        if v["next_url"] and v["url"] and _path(v["next_url"]) and _path(v["next_url"]) == _path(v["url"]):
            d5 = min(d5, 3)
            findings.append("A toHaveURL asserts the SAME page the step is already on (same-page green-wash).")
    # honest "solid" count: a step asserting an ungrounded nav must not be high-confidence.
    if impossible_steps:
        d5 = min(d5, 3)
    d5 = _clamp(d5)

    # ── D3 — Grounded replay (evidence fidelity) ─────────────────────────────
    d3 = 10
    fill_steps = [v for v in views if v["verb"] in _FILL_VERBS and v["value"]]
    typed = _evidence_typed_values(evidence) if evidence is not None else []
    if typed:
        represented = {_norm(val) for _, val in [(v["label"], v["value"]) for v in fill_steps]}
        # A radio/segmented CHOICE is replayed by selecting its LABEL — the
        # option label IS the demonstrated value (Select 'Self-employed').
        # Count choice-step labels as replayed values so a demonstrated
        # selection is never scored as a dropped fill.
        for _vc in views:
            if _vc["verb"] in ("select", "check", "toggle", "click") and _vc.get("label"):
                represented.add(_norm(_vc["label"]))
        missing = [(lbl, val) for (lbl, val) in typed if _norm(val) not in represented]
        # Documented login-prologue skip: URL-anchored tests do not replay the
        # login (the generated auth precondition says so). An auth-field value
        # with NO case step at all is that documented skip — an honest gap,
        # never a grounded-replay deduction. Any non-auth value stays a finding.
        _step_labels = {_norm(v.get("label") or "") for v in views}
        # Labels typed into BROWSER CHROME (the address bar) are navigation,
        # not form data — the case replays them as its Open/goto step.
        _chrome_tokens = ("address bar", "addressbar", "url bar", "urlbar", "omnibox")
        # A typed value SUPERSEDED by a later value on the same label that IS
        # replayed (user typed 1500, then 150000) is state history, not a drop.
        def _digits_key(x):
            # formatting-tolerant compare: '1500' == '$1,500'; '150000' == '$150,000'
            return re.sub(r"[^0-9a-z]", "", _norm(x))
        _represented_keys = {_digits_key(r) for r in represented}
        # ORDER-INDEPENDENT sibling map: all demonstrated values per field.
        # A missing value is SUPERSEDED when any SIBLING value of the same
        # field IS replayed — the field is exercised with a demonstrated
        # value; the missing one is transient state history (typed 1500,
        # corrected to 150,000). Order of evidence rows must not matter.
        _label_values: dict = {}
        for _lbl2, _val2 in typed:
            _label_values.setdefault(_norm(_lbl2), set()).add(_norm(_val2))
        # Replayed fills BY FIELD: when re-derivation kept only the typed DRAFT
        # (user typed 1500, corrected to 150,000 — the final value survives in
        # the form snapshot and IS what the case fills), the draft's superseder
        # is the replayed fill itself. Guarded tightly: the replayed digits
        # must EXTEND the draft's digits — an unrelated mismatch (typed John,
        # replayed Venkata) remains a real finding.
        _replayed_by_label: dict = {}
        for _v3 in fill_steps:
            _replayed_by_label.setdefault(
                _norm(_v3.get("label") or ""), set()).add(_norm(_v3.get("value") or ""))
        _prologue, _dropped = [], []
        for lbl, val in missing:
            _nl = _norm(lbl)
            if any(tok in _nl for tok in _AUTH_FIELD_TOKENS) and _nl not in _step_labels:
                _prologue.append((lbl, val, "login prologue — deliberately not replayed "
                                            "(apply an Authentication profile)"))
            elif any(tok in _nl for tok in _chrome_tokens):
                _prologue.append((lbl, val, "browser-chrome navigation typing — replayed "
                                            "as the entry Open step, not a form fill"))
            elif any(
                _sib != _norm(val) and _digits_key(_sib) in _represented_keys
                for _sib in _label_values.get(_nl, ())
            ) or any(
                _digits_key(val) and _digits_key(_rv) != _digits_key(val)
                and _digits_key(_rv).startswith(_digits_key(val))
                for _rv in _replayed_by_label.get(_nl, ())
            ):
                _prologue.append((lbl, val, "superseded by the final demonstrated value "
                                            "for this field (state history, not a drop)"))
            else:
                _dropped.append((lbl, val))
        if _prologue:
            for lbl, val, _why in _prologue:
                per_step.append({"step_number": None, "verdict": V_INFO,
                                 "detail": f"'{lbl}': {_why}; counted as a gap, not a defect."})
        missing = _dropped
        if missing:
            d3 -= min(8, 4 * len(missing))
            for lbl, val in missing:
                findings.append(f"Recorded value '{val}' for '{lbl}' is never filled (dropped from replay).")
                per_step.append({"step_number": None, "verdict": V_DATA,
                                 "detail": f"Typed '{val}' in '{lbl}' was not replayed."})
    elif dead_data and not fill_steps:
        d3 = min(d3, 4)  # data file dead AND no fills → typed values were dropped
    d3 = _clamp(d3)

    # ── per-step OK verdicts for the steps that aren't already flagged ────────
    flagged = {id(v) for v in impossible_steps}
    for v in views:
        if id(v) in flagged:
            continue
        if v["step_number"] in _guarded and v["provenance"] != "inferred":
            per_step.append({"step_number": v["step_number"], "verdict": V_MISSING_PREREQ,
                             "detail": f"'{v['action']}' — skip-guarded UNPROVEN in the delivered "
                                       "spec (asserts nothing; honest coverage gap)."})
            continue
        if v["provenance"] == "inferred":
            per_step.append({"step_number": v["step_number"], "verdict": V_MISSING_PREREQ,
                             "detail": f"'{v['action']}' — transition not captured; honestly UNPROVEN (not asserted)."})
        else:
            per_step.append({"step_number": v["step_number"], "verdict": V_OK,
                             "detail": f"'{v['action']}' is grounded in the recording."})

    dims = {
        "playwright_apis": d1, "locator_quality": d2, "grounded_replay": d3,
        "navigation_correctness": d4, "assertion_correctness": d5,
    }
    base = round(sum(dims.values()) / len(dims))
    overall = min(base, min(dims.values()))  # MIN-gated: a low axis caps the whole

    gaps = sum(
        1 for v in views
        if v["provenance"] == "inferred" or v["step_number"] in _guarded
    )
    if impossible_steps or min(dims.values()) < 5:
        decision = DECISION_REPAIR
    elif overall >= 9:
        decision = DECISION_CERTIFIED
    else:
        decision = DECISION_REPAIR

    return {
        "dimension_scores": dims,
        "overall_score": overall,
        "per_step": sorted(per_step, key=lambda p: (p["step_number"] is None, p["step_number"] or 0)),
        "decision": decision,
        "findings": findings,
        "gaps": gaps,  # honest UNPROVEN transitions (uncaptured recording, not a defect)
        "source": "deterministic",
    }


def _path(u: str) -> str:
    u = (u or "").strip()
    m = re.search(r"https?://[^/]+(/[^?#]*)", u)
    if m:
        return m.group(1).rstrip("/")
    if u.startswith("/"):
        return u.split("?")[0].split("#")[0].rstrip("/")
    return ""


def _norm(s: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(s or "").lower())


# ═══════════════════════════════════════════════════════════════════════════
#  LLM layer — per-step reasoning + repair directives (mirrors agentic_heal)
# ═══════════════════════════════════════════════════════════════════════════

# recommended actions the LLM may propose; only these route to the deterministic
# applier. Anything that would KEEP/ADD a green assertion is grounded-validated.
_REC_GROUNDED = {"add_fill_step", "add_precondition"}  # require verbatim evidence
_REC_SAFE = {"keep", "drop_assertion", "mark_unproven", "file_defect"}


def audit_tool():
    from ..llm.types import ToolDefinition
    return ToolDefinition(
        name="record_audit",
        description=(
            "Audit a generated Playwright test for physical possibility and grounding. For each "
            "step, decide whether its action→assertion is achievable on a faithful replay AND "
            "grounded in the recording evidence. You may NEVER author Playwright and NEVER turn a "
            "red/unproven step green: you may keep a grounded step, drop/mark-unproven an "
            "ungrounded assertion, add a fill/precondition that appears VERBATIM in the evidence, "
            "or file a defect. Every evidence_quote MUST be a verbatim substring of the supplied "
            "evidence, or set it to ''."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "per_step": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "step_number": {"type": "integer"},
                            "verdict": {"type": "string", "enum": [
                                V_OK, V_IMPOSSIBLE, V_UNGROUNDED, V_MISSING_PREREQ, V_DEAD, V_DATA]},
                            "evidence_quote": {"type": "string"},
                            "recommended": {"type": "string", "enum": [
                                "keep", "drop_assertion", "mark_unproven",
                                "add_precondition", "add_fill_step", "file_defect"]},
                        },
                        "required": ["step_number", "verdict", "evidence_quote", "recommended"],
                    },
                },
            },
            "required": ["per_step"],
        },
    )


def build_audit_prompt(*, spec_text: str, evidence_text: str, steps: list) -> tuple[str, str]:
    system = (
        "You are an independent Playwright test auditor. You are given a generated .spec.ts, the "
        "RECORDING EVIDENCE it was built from (the ONLY ground truth), and the step list. Audit "
        "each step like a strict reviewer: is the action→assertion physically possible on a real "
        "replay, and is it grounded in the evidence?\n\n"
        "HARD rules (never green-wash):\n"
        "- A click that does not navigate must NOT assert a URL change. Flag it impossible_transition "
        "and recommend drop_assertion.\n"
        "- A page boundary with no captured causal action is missing_prerequisite → mark_unproven "
        "(do NOT invent the missing steps).\n"
        "- You may only recommend add_fill_step / add_precondition when the value/precondition "
        "appears VERBATIM in the evidence; put that substring in evidence_quote. Otherwise set "
        "evidence_quote='' and recommend mark_unproven or file_defect.\n"
        "- Never recommend keeping an assertion the evidence does not support.\n"
        "Call record_audit exactly once."
    )
    prompt = (
        "RECORDING EVIDENCE (ground truth):\n" + (evidence_text or "(none)") + "\n\n"
        "GENERATED SPEC:\n" + (spec_text or "(none)")[:6000] + "\n\n"
        "STEPS:\n" + "\n".join(
            f"  step {v['step_number']}: {v['action']} "
            f"[provenance={v['provenance']}, next_url={v['next_url'] or '-'}, "
            f"grounded={v['navigation_grounded']}]"
            for v in [_sv(s) for s in (steps or [])]
        )
    )
    return system, prompt


def validate_audit(raw_per_step: list, *, evidence_text: str) -> list[dict]:
    """Grounded-validate the LLM's per-step findings. A finding that would KEEP/ADD a
    passing assertion is accepted ONLY if its evidence_quote is a verbatim substring of
    the evidence; otherwise it is demoted to mark_unproven (never a fabricated green)."""
    ev = (evidence_text or "")
    ev_norm = re.sub(r"\s+", " ", ev).strip().lower()
    out: list[dict] = []
    for f in (raw_per_step or []):
        if not isinstance(f, dict):
            continue
        rec = (f.get("recommended") or "").strip().lower()
        quote = (f.get("evidence_quote") or "").strip()
        verdict = (f.get("verdict") or "").strip().lower()
        grounded = bool(quote) and re.sub(r"\s+", " ", quote).strip().lower() in ev_norm
        if rec in _REC_GROUNDED and not grounded:
            # ungrounded "fix to green" → demote to honest UNPROVEN
            rec = "mark_unproven"
            verdict = verdict or V_MISSING_PREREQ
        out.append({
            "step_number": f.get("step_number"),
            "verdict": verdict or V_OK,
            "recommended": rec if (rec in _REC_GROUNDED or rec in _REC_SAFE) else "mark_unproven",
            "evidence_quote": quote if grounded else "",
            "grounded": grounded,
        })
    return out


async def audit(*, spec_text: str, evidence_text: str, steps: list, evidence: Any = None,
                router=None, tier_name: str = "tier_premium", timeout_s: float = 45.0) -> dict:
    """Full audit: the deterministic score (authoritative number) enriched with the
    LLM's grounded per-step reasoning. Fail-open — on any LLM failure the deterministic
    verdict stands unchanged; never crash, never auto-certify."""
    base = score_spec(spec_text, steps, evidence=evidence)
    try:
        from ..llm.router import build_router
        from ..llm.types import CompletionRequest, FinishReason
        r = router or build_router()
        system, prompt = build_audit_prompt(spec_text=spec_text, evidence_text=evidence_text, steps=steps)
        tool = audit_tool()
        req = CompletionRequest(
            system=system, prompt=prompt, max_tokens=2000, temperature=0.0,
            request_timeout_s=timeout_s, tools=(tool,), tool_choice=tool.name,
            metadata={"task": "playwright_audit"},
        )
        resp = await r.complete_via_tier(tier_name=tier_name, request=req)
        if resp.finish_reason == FinishReason.ERROR or not resp.tool_calls:
            base["llm"] = {"ok": False, "error": (resp.error_detail or "no tool call from auditor")}
            return base
        raw = (resp.tool_calls[0].arguments or {}).get("per_step") or []
        base["llm"] = {
            "ok": True,
            "model": getattr(resp, "model", ""),
            "per_step": validate_audit(raw, evidence_text=evidence_text),
        }
        return base
    except Exception as e:  # never crash, never auto-certify
        base["llm"] = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        return base


def gate(spec_text: str, steps: list, evidence: Any = None, *, enforce: bool = False) -> dict:
    """Deterministic auditor GATE over a compiled spec (Phase-0 wiring).

    Runs the $0 ``score_spec`` and returns a gate verdict. ``passed`` is the HONEST
    verdict (CERTIFIED and overall >= 8), computed here — never self-reported by an LLM.
    ``enforce`` toggles BLOCKING: when True and the audit is not certified, ``would_block``
    is True so a CI / write path can refuse the script; when False (default — warning-first,
    per the never-green-wash policy) the caller surfaces ``findings`` without blocking. A
    green run that still carries an impossible-transition or a dropped recorded value is
    caught HERE, so a structural green-wash cannot slip through a passing run unnoticed."""
    audit = score_spec(spec_text, steps, evidence)
    passed = (audit.get("decision") == DECISION_CERTIFIED) and (audit.get("overall_score", 0) >= 8)
    return {
        "passed": bool(passed),
        "enforce": bool(enforce),
        "would_block": bool(enforce and not passed),
        "decision": audit.get("decision"),
        "overall_score": audit.get("overall_score"),
        "findings": audit.get("findings", []),
        "gaps": audit.get("gaps", 0),
        "audit": audit,
    }


__all__ = ["score_spec", "gate", "audit", "audit_tool", "build_audit_prompt", "validate_audit",
           "DECISION_CERTIFIED", "DECISION_REPAIR", "DECISION_DEFECT"]


# ── Anti-flake API-policy linter (P6) ────────────────────────────────────────
# Versioned, data-driven policy: blessed APIs live in the compiler; these are
# the FORBIDDEN patterns that mint flakiness. Pure regex over emitted code —
# generic across apps; severity 'error' should gate, 'advisory' informs.
_API_POLICY: "list[tuple[str, str, str]]" = [
    (r"page\.click\(", "error",
     "page.click(selector) bypasses locator auto-wait — use locator.click()"),
    (r"page\.type\(", "error",
     "page.type is deprecated — use locator.fill() / pressSequentially()"),
    (r"waitForTimeout\(", "error",
     "fixed sleep — replace with a condition wait (zero-sleep policy)"),
    (r"\$eval\(", "error",
     "$eval bypasses actionability checks — use locator APIs"),
    (r"ElementHandle", "error",
     "ElementHandle pins a node and goes stale — locators re-resolve"),
    (r"\.nth\(", "advisory",
     "index-based selection is order-fragile — prefer semantic scoping"),
    (r"networkidle", "advisory",
     "networkidle is unreliable on SPAs with polling — prefer explicit "
     "response/element conditions"),
]


def lint_spec(spec_text: str) -> list:
    """API-policy lint findings for one emitted spec. Returns
    [{rule, severity, line, snippet, why}] — deterministic, $0."""
    out = []
    lines = (spec_text or "").splitlines()
    for pattern, severity, why in _API_POLICY:
        rx = re.compile(pattern)
        for i, ln in enumerate(lines, 1):
            if rx.search(ln):
                out.append({
                    "rule": pattern,
                    "severity": severity,
                    "line": i,
                    "snippet": ln.strip()[:140],
                    "why": why,
                })
    return out
