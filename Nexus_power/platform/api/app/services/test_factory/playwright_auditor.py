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
V_AMBIGUOUS = "ambiguous_locator"
V_URL_TEXT = "url_as_text_oracle"

# Interactive verbs whose step compiles to a name-based locator (so a repeated
# accessible name on the same page makes the locator ambiguous).
_LOCATOR_VERBS = {"click", "press", "tap", "select", "check", "choose", "toggle"}

DECISION_CERTIFIED = "certified"
DECISION_REPAIR = "repair"
DECISION_DEFECT = "defect"

_FILL_VERBS = {"type", "fill", "input", "select"}
_SLEEP_RX = re.compile(r"waitForTimeout\s*\(")
_GETBYTEXT_RX = re.compile(r"getByText\(\s*['\"/]")

# URL-as-text oracle (F3, run 7c89de7e step 7): an EXPECT whose getByText pattern
# is a bare URL scheme/www token — expect(getByText(/https/i)) — asserts text NO
# page renders (a page does not display its own URL as text). It can only-ever-
# fail-RED, so it is a spec DEFECT, never an app signal. Scoped to expect(…) so a
# legitimate ACTION on a URL-labelled link (click getByText('www.example.com'))
# is never flagged. The QUOTED-full-URL form is warning-only: a docs-style page
# genuinely can render a URL as visible text.
_URL_TEXT_ORACLE_RX = re.compile(
    r"expect\([^\n]*?getByText\(\s*/\s*(?:https?|www)\b", re.IGNORECASE)
_URL_TEXT_QUOTED_RX = re.compile(
    r"expect\([^\n]*?getByText\(\s*['\"](?:https?://|www\.)", re.IGNORECASE)


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
        "anchor": (obs.get("anchor") or "").strip(),
        "ambiguous_unresolved": bool(obs.get("ambiguous_unresolved")),
    }


def _ambiguous_labels(evidence: Any) -> set:
    """Normalized accessible names the recording shows on 2+ interactive controls of
    the SAME page — the visible name alone can't uniquely locate them (the N-identical-
    controls case, e.g. six 'Add to cart' buttons). Mirrors
    confidence.compute_ambiguous_labels; grounded purely in the recording's actions.
    Groups by page_visit_id when present, else treats all evidence as one page."""
    per_page: dict = {}
    for a in (evidence or []):
        verb = (a.get("verb") if isinstance(a, dict) else getattr(a, "verb", "")) or ""
        if verb.strip().lower() not in _LOCATOR_VERBS:
            continue
        label = (a.get("target_label") if isinstance(a, dict) else getattr(a, "target_label", "")) or ""
        key = _norm(label)
        if not key:
            continue
        page = (a.get("page_visit_id") if isinstance(a, dict) else getattr(a, "page_visit_id", "")) or "_"
        bucket = per_page.setdefault(page, {})
        bucket[key] = bucket.get(key, 0) + 1
    out: set = set()
    for bucket in per_page.values():
        for key, count in bucket.items():
            if count > 1:
                out.add(key)
    return out


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
    # dead data file: `require('...nexus.data.json')` loaded into D but never used.
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
    # Locator UNIQUENESS (the saucedemo blind spot): a control whose accessible name is
    # REPEATED on its page, emitted with NO disambiguating anchor, compiles to a strict-
    # mode-ambiguous locator — it matches N elements (RED), or worse silently binds the
    # wrong one. Evidence-grounded + conservative: fires only when the RECORDING itself
    # shows the same name on 2+ controls of one page, and the step has no anchor scope.
    # WARNING-ONLY (per the warning-first refinement): it surfaces a finding + per-step
    # verdict + the gate's ambiguous_locators count, but does NOT deduct from the score,
    # so a benign false positive can't demote a good script until this new check's
    # false-positive rate is measured. Flip to a deduction once proven.
    ambiguous = _ambiguous_labels(evidence) if evidence is not None else set()
    for v in views:
        if (v["verb"] in _LOCATOR_VERBS and not v["anchor"]
                and (v["ambiguous_unresolved"] or _norm(v["label"]) in ambiguous)):
            findings.append(
                f"Ambiguous locator (warning): '{v['label']}' matches multiple controls on the page "
                "and has no disambiguating anchor — scope it to its row/card/section or it binds the "
                "wrong one.")
            per_step.append({"step_number": v["step_number"], "verdict": V_AMBIGUOUS,
                             "detail": f"'{v['label']}' is repeated on the page with no anchor — "
                                       "which control is targeted is unresolved."})
    d2 = _clamp(d2)

    # ── D4 — Navigation correctness (the causality axis) ─────────────────────
    d4 = 10
    impossible_steps: list[dict] = []
    for v in views:
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
    # URL-as-text oracle (F3): a bare scheme/www token asserted as visible page
    # text is an always-RED spec defect — tank the axis exactly like an
    # impossible transition so the block-gate refuses to ship it. Generic:
    # keys on URL *shape* in the compiled spec, no host/domain vocabulary.
    url_text_hits = [m.group(0) for m in _URL_TEXT_ORACLE_RX.finditer(spec_text)]
    if url_text_hits:
        d5 = min(d5, 1)
        findings.append(
            f"{len(url_text_hits)} text oracle(s) assert a URL fragment as visible page "
            "text (e.g. getByText(/https/)) — no page renders its own URL; the "
            "navigation oracle is toHaveURL. Always-RED spec defect (product-side), "
            "never an application failure.")
        per_step.append({"step_number": None, "verdict": V_URL_TEXT,
                         "detail": "Compiled spec asserts URL text visibility: "
                                   + "; ".join(h.strip()[:80] for h in url_text_hits[:3])})
    # Quoted-full-URL text oracle: warning-only (a docs-style page CAN render a
    # URL as literal text) — surfaced so a reviewer decides, never auto-blocked.
    quoted_hits = len(_URL_TEXT_QUOTED_RX.findall(spec_text))
    if quoted_hits:
        findings.append(
            f"{quoted_hits} text oracle(s) assert a full URL string as page text — "
            "verify the page really renders that URL as text; otherwise use toHaveURL.")
    d5 = _clamp(d5)

    # ── D3 — Grounded replay (evidence fidelity) ─────────────────────────────
    d3 = 10
    fill_steps = [v for v in views if v["verb"] in _FILL_VERBS and v["value"]]
    typed = _evidence_typed_values(evidence) if evidence is not None else []
    if typed:
        represented = {_norm(val) for _, val in [(v["label"], v["value"]) for v in fill_steps]}
        missing = [(lbl, val) for (lbl, val) in typed if _norm(val) not in represented]
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

    gaps = sum(1 for v in views if v["provenance"] == "inferred")
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


def gate(report: dict, *, blocking: bool = False) -> dict:
    """Turn an audit report into a GATE verdict for the generate/compile path.

    ``passed`` ALWAYS tells the truth: it is ``not would_block`` regardless of mode, so
    an impossible-transition spec reports ``passed=False`` even in warning-only mode (no
    misleading green). ``enforced`` says whether this gate actually BLOCKS shipping:
    with ``enforced=False`` (the default, WARNING-ONLY) the caller surfaces the warnings
    but ships anyway and measures how often a blocking gate WOULD fire; with
    ``enforced=True`` the caller must refuse a script whose ``passed`` is False.

    The hard never-green-wash block conditions are an impossible navigation assertion or
    navigation axis = 0. The locator-uniqueness finding is surfaced (``ambiguous_locators``
    + warnings) but is NOT a block reason yet (new check, warning-first).

    Pure + deterministic; never green-wash — it can only warn or block, never certify a
    bad script green."""
    findings = list(report.get("findings", []) or [])
    per_step = report.get("per_step", []) or []
    dims = report.get("dimension_scores", {}) or {}
    overall = report.get("overall_score", report.get("overall", 10))

    impossible = [p for p in per_step if p.get("verdict") == V_IMPOSSIBLE]
    ambiguous = [p for p in per_step if p.get("verdict") == V_AMBIGUOUS]
    url_text = [p for p in per_step if p.get("verdict") == V_URL_TEXT]
    block_reasons: list = []
    if impossible:
        block_reasons.append(f"{len(impossible)} impossible navigation assertion(s)")
    if dims.get("navigation_correctness", 10) == 0:
        block_reasons.append("navigation axis = 0 (impossible transition)")
    # F3: an always-RED URL-as-text oracle is the same severity class as an
    # impossible transition — a spec that CANNOT pass must never ship (it would
    # report an application failure the application did not cause).
    if url_text:
        block_reasons.append(
            f"{len(url_text)} URL-as-text oracle(s) (always-RED spec defect)")
    would_block = bool(block_reasons)
    return {
        "passed": not would_block,        # honest verdict, independent of enforcement
        "enforced": blocking,             # whether this gate actually blocks shipping
        "would_block": would_block,
        "block_reasons": block_reasons,
        "warnings": findings,
        "overall_score": overall,
        "ambiguous_locators": len(ambiguous),
        "url_text_oracles": len(url_text),
        "decision": report.get("decision"),
    }


# ═══════════════════════════════════════════════════════════════════════════
#  API-POLICY LINT — the validation four reports have always ADVERTISED
# ═══════════════════════════════════════════════════════════════════════════
# WHY THIS EXISTS (M0.4 / T-GT-07). Four call sites called ``lint_spec`` inside a
# bare ``except: lint = []``. The function did not exist, so every call raised
# AttributeError, every report carried ``"lint": []`` / ``"lint_errors": 0``, and
# the rubric line read "score_spec + API-policy lint". An empty finding list is
# INDISTINGUISHABLE from a clean asset — so the certificate claimed a policy
# audit that had never run, on every asset, since the field was added. The
# redteam benchmark had even written the contract down
# (``expect_lint_errors_min: 3`` on a sleep + page.click + ElementHandle spec)
# and passed anyway, because the same except-swallow zeroed the count.
#
# The rule of this milestone is that a report never claims work that did not
# execute. So the function is now real, it is PURE (regex over a string, no I/O,
# no imports beyond ``re``), and it CANNOT raise — the call sites therefore drop
# their except-swallow and an empty list finally means "ran, found nothing".
#
# SEVERITY CALIBRATION. ``error`` is reserved for API usage that is wrong under
# any circumstance and that the compiler provably never emits — so a
# COMPILER-GENERATED spec scores zero lint errors and its risk score is unchanged
# by this change. Deliberate compiler idioms that merely deserve a reader's
# attention (bounded ``networkidle``, the visual coordinate-click fallback,
# tolerant ``.catch(() => {})`` oracles) are ``warning``: surfaced, never scored.
LINT_RULES_VERSION = "api-policy-lint-v1"

# Page-level selector actions. Deprecated in favour of locators: they re-resolve
# the selector with no strictness check, so they silently act on the FIRST of N
# matches instead of failing on the ambiguity.
_LINT_PAGE_ACTIONS = ("click", "dblclick", "fill", "type", "press", "check",
                      "uncheck", "hover", "tap", "focus", "selectOption",
                      "setInputFiles", "dragAndDrop")
_LINT_PAGE_ACTION_RX = re.compile(
    r"\bpage\.(" + "|".join(_LINT_PAGE_ACTIONS) + r")\s*\(")
# ElementHandles detach from the DOM the moment the framework re-renders; the
# handle then points at a node that is no longer on the page.
_LINT_HANDLE_RX = re.compile(
    r"(\bpage\.\$\$?\s*\(|\bpage\.\$\$?eval\s*\(|\bElementHandle\b"
    r"|\.elementHandle\s*\(|\bwaitForSelector\s*\()")
_LINT_SLEEP_RX = re.compile(r"\bwaitForTimeout\s*\(")
# Retrying (async) matchers must be awaited or the assertion never runs — the
# test goes green on an unresolved promise. Sync matchers (toBeTruthy on a
# boolean the compiler computes itself) are legitimately un-awaited.
_LINT_ASYNC_MATCHERS = ("toHaveURL", "toHaveValue", "toBeVisible", "toBeHidden",
                        "toBeChecked", "toHaveText", "toContainText",
                        "toHaveCount", "toBeEnabled", "toBeDisabled",
                        "toBeAttached", "toHaveAttribute", "toHaveClass",
                        "toBeEditable", "toBeFocused", "toPass")
_LINT_FLOATING_EXPECT_RX = re.compile(
    r"(?<!await )(?<!return )\bexpect\s*\([^;]*?\.(" +
    "|".join(_LINT_ASYNC_MATCHERS) + r")\s*\(")
_LINT_NETWORKIDLE_RX = re.compile(r"waitForLoadState\s*\(\s*['\"]networkidle['\"]")
_LINT_TIMEOUT_OPT_RX = re.compile(r"\btimeout\s*:")
_LINT_MOUSE_RX = re.compile(r"\bpage\.mouse\.(click|dblclick|move)\s*\(")
_LINT_FORCE_RX = re.compile(r"\bforce\s*:\s*true\b")
_LINT_SWALLOWED_EXPECT_RX = re.compile(r"\bexpect\s*\(.*\)\s*\.[^;]*\.catch\s*\(")
_LINT_SECRET_RX = re.compile(
    r"\b(password|passwd|secret|api[_-]?key|token|bearer)\b\s*[:=]\s*['\"][^'\"]{6,}['\"]",
    re.IGNORECASE)
# A literal placeholder credential is scaffolding, not a leaked secret.
_LINT_SECRET_BENIGN_RX = re.compile(
    r"(process\.env|\$\{|__nx|<[^>]+>|\bxxx+\b|example|changeme|placeholder|redacted)",
    re.IGNORECASE)
_LINT_LINE_COMMENT_RX = re.compile(r"//.*$")


def _lint_code_only(line: str) -> str:
    """Strip a trailing line comment so prose never trips a code rule.

    Conservative on purpose: a ``//`` inside a string literal (``'http://x'``)
    must not truncate the line, so the strip is skipped when the ``//`` is
    preceded by a colon (the only form that occurs in a URL) or sits inside an
    obvious quote pair."""
    m = re.search(r"(?<!:)//", line)
    if not m:
        return line
    head = line[: m.start()]
    if head.count("'") % 2 or head.count('"') % 2 or head.count("`") % 2:
        return line  # the // is inside a string literal
    return head


def lint_spec(spec_text: Any) -> list[dict]:
    """Deterministic API-policy lint of a Playwright spec.

    Returns a list of ``{rule, severity, line, message}`` — ``severity`` is
    ``error`` (wrong under any circumstance; feeds the risk model and the
    remediation channel) or ``warning`` (worth a reader's attention; never
    scored). Pure and total: no I/O, and it never raises, so an empty list
    means the lint RAN and found nothing — never that it failed to run."""
    findings: list[dict] = []
    try:
        text = spec_text if isinstance(spec_text, str) else str(spec_text or "")
        if not text.strip():
            return []

        # One finding per (rule, line): `const h: ElementHandle = await page.$()`
        # violates the handle rule twice on one line, and reporting it twice
        # inflates lint_errors — which feeds the risk model — without adding a
        # single fact a reader can act on.
        seen: set[tuple[str, int]] = set()

        def add(rule: str, severity: str, line_no: int, message: str) -> None:
            if (rule, line_no) in seen:
                return
            seen.add((rule, line_no))
            findings.append({"rule": rule, "severity": severity,
                             "line": line_no, "message": message})

        for i, raw in enumerate(text.splitlines(), start=1):
            line = _lint_code_only(raw)
            if not line.strip():
                continue

            # ── errors ──────────────────────────────────────────────────────
            for _m in _LINT_SLEEP_RX.finditer(line):
                add("no-arbitrary-sleep", "error", i,
                    "waitForTimeout() sleeps for a fixed duration — it is either "
                    "too short (flake) or wasted wall-clock. Wait on a condition.")
            for m in _LINT_PAGE_ACTION_RX.finditer(line):
                add("no-page-selector-action", "error", i,
                    f"page.{m.group(1)}() re-resolves a raw selector with no "
                    "strictness check — it silently acts on the first of N "
                    f"matches. Use a locator: page.getByRole(...).{m.group(1)}().")
            for _m in _LINT_HANDLE_RX.finditer(line):
                add("no-element-handle", "error", i,
                    "ElementHandle / page.$ / waitForSelector returns a handle "
                    "that detaches on the next re-render and then points at a "
                    "node no longer in the DOM. Use a locator.")
            for m in _LINT_FLOATING_EXPECT_RX.finditer(line):
                add("no-floating-expect", "error", i,
                    f"expect(...).{m.group(1)}() is a RETRYING matcher and is not "
                    "awaited — the assertion never runs and the step goes green "
                    "on an unresolved promise.")

            # ── warnings ────────────────────────────────────────────────────
            if _LINT_NETWORKIDLE_RX.search(line) and not _LINT_TIMEOUT_OPT_RX.search(line):
                add("unbounded-networkidle", "warning", i,
                    "waitForLoadState('networkidle') with no timeout hangs on any "
                    "page that keeps a socket open. Bound it.")
            for m in _LINT_MOUSE_RX.finditer(line):
                add("coordinate-interaction", "warning", i,
                    f"page.mouse.{m.group(1)}() targets a pixel, not an element — "
                    "it breaks on any re-layout. Deliberate as a visual fallback; "
                    "flagged so a reader knows this step is not element-bound.")
            if _LINT_FORCE_RX.search(line):
                add("force-interaction", "warning", i,
                    "{ force: true } skips actionability checks — the click is "
                    "recorded even if the control was covered or disabled.")
            if _LINT_SWALLOWED_EXPECT_RX.search(line):
                add("swallowed-assertion", "warning", i,
                    "an expect() whose rejection is .catch()-ed cannot fail — it "
                    "is a tolerant oracle, not a proof. Intentional in compiler "
                    "output; never count it as evidence.")
            if _LINT_SECRET_RX.search(line) and not _LINT_SECRET_BENIGN_RX.search(line):
                add("hardcoded-credential", "warning", i,
                    "a credential literal in a spec leaks into every artifact "
                    "that stores the script. Read it from the environment.")
    except Exception as exc:  # pragma: no cover — a lint must never break delivery
        return [{"rule": "lint_internal_error", "severity": "warning", "line": 0,
                 "message": f"api-policy lint aborted: {str(exc)[:160]}"}]
    return findings


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
