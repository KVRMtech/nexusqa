"""Deterministic ProductionTestCase -> Playwright TypeScript compiler.

Phase 2 (runnable): kind-aware actions, load-bearing UNPROVEN skips,
consent-as-setup, anchored URL assertions, test.step grouping + evidence
comments. Pure string compilation — no LLM, no I/O, no Date/random — so the same
case always produces byte-identical code.

Kind refinement is DETERMINISTIC, from data already captured
(form_snapshot_signals: control type + options; plus the value's own pattern).
No new vision pass, no cost, and it never modifies the frozen capture pipeline.
"""

from __future__ import annotations

import json
import re
from typing import Iterable
from urllib.parse import urlparse

from .locators import js_regex_literal, js_str, url_path

_SLUG_RX = re.compile(r"[^a-z0-9]+")
_CONSENT_RX = re.compile(r"cookie|consent|accept all|accept cookies", re.IGNORECASE)
_DATE_RX = re.compile(
    r"\b(\d{4}-\d{1,2}-\d{1,2}|\d{1,2}[/.\-]\d{1,2}"
    r"|jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\b",
    re.IGNORECASE,
)
# First alphabetic token of a value — used for a TOLERANT value assertion that
# survives normalization/autocomplete ("Austin AUS" -> "Austin, TX (AUS)").
_TOKEN_RX = re.compile(r"[A-Za-z]{2,}")


def _assert_token(value: str) -> str:
    m = _TOKEN_RX.search(value or "")
    return m.group(0) if m else ""


# Longest significant digit-run — a TOLERANT numeric oracle so amounts, times,
# phone fragments and dates (e.g. a 4-digit year) get a real value assertion
# instead of a no-op fill silently passing green.
_DIGIT_RUN_RX = re.compile(r"\d{2,}")


def _value_oracle(value: str) -> str:
    """Regex-safe token for a tolerant ``toHaveValue(/.../i)`` oracle, or '' when
    no safe token exists. Prefers the first alphabetic token (survives
    autocomplete normalization); falls back to the longest digit run so numeric,
    symbolic and date values are still verified rather than passing green on a
    no-op fill. Returns '' for single-char / pure-symbol values — the caller
    then asserts the field is simply non-empty."""
    m = _TOKEN_RX.search(value or "")
    if m:
        return re.escape(m.group(0))
    digits = _DIGIT_RUN_RX.findall(value or "")
    if digits:
        return re.escape(max(digits, key=len))
    return ""


# ─── Parametrization helpers (opt-in; defaults stay the observed values) ───────


def _origin(url: str) -> str:
    """Scheme+host of a recorded URL — the default base for env-portable runs."""
    try:
        p = urlparse(url or "")
        if p.scheme and p.netloc:
            return f"{p.scheme}://{p.netloc}"
    except Exception:
        pass
    return ""


def _rel_path(url: str) -> str:
    """Path(+query) of a recorded URL, so navigation resolves against use.baseURL."""
    try:
        p = urlparse(url or "")
        path = p.path or "/"
        if p.query:
            path += "?" + p.query
        return path or "/"
    except Exception:
        return url or "/"


def _data_key(label: str) -> str:
    """Stable JSON key for an overridable field, derived from its visible label."""
    return _slug(label, "")


def _val_expr(value: str, label: str, parametrize: bool) -> str:
    """A JS expression for a data value: a literal normally, or an override-aware
    `(D['key'] ?? 'observed')` when parametrized — the observed value is always
    the default, so a plain run is unchanged."""
    lit = f"'{js_str(value)}'"
    if not parametrize:
        return lit
    key = _data_key(label)
    if not key:
        return lit
    return f"(D['{js_str(key)}'] ?? {lit})"


def _a11y_note(observed: dict) -> str | None:
    """Flag a control with no observed accessible name (a11y-weakness surfacing).

    We never silently drop to a brittle locator: when the app gives us no
    role/name to anchor on, we say so."""
    if not (observed.get("label") or "").strip():
        return ("// a11y-weakness: no accessible name observed for this control — "
                "locator is a best-effort fallback; add a label/aria-label for "
                "reliable automation.")
    return None


def _norm(text: str) -> str:
    return " ".join((text or "").lower().split())


def _slug(text: str, fallback: str = "test") -> str:
    s = _SLUG_RX.sub("-", (text or "").lower()).strip("-")
    return s or fallback


def _confidence(step) -> str:
    return (getattr(step, "confidence", "") or "").strip().lower()


def _provenance(step) -> str:
    return (getattr(step, "provenance", "") or "").strip().lower()


def _observed(step) -> dict:
    return getattr(step, "observed", None) or {}


def _is_consent(step) -> bool:
    o = _observed(step)
    if (o.get("verb") or "").strip().lower() != "click":
        return False
    return bool(_CONSENT_RX.search(o.get("label", "") or ""))


# ─── Deterministic kind refinement (from already-captured signals) ────────────


def build_field_meta(visits: Iterable) -> dict[str, dict]:
    """Map normalized label -> {control, options, required} from the captured
    form_snapshot_signals. Read-only over existing data; no LLM."""
    meta: dict[str, dict] = {}
    for v in visits:
        signals = getattr(v, "form_snapshot_signals", None) or {}
        for label, sig in signals.items():
            if not isinstance(sig, dict):
                continue
            key = _norm(label)
            if not key:
                continue
            opts = [str(o).strip() for o in (sig.get("options") or []) if str(o).strip()]
            meta[key] = {
                "control": (sig.get("type") or "").strip().lower(),
                "options": opts,
                "required": bool(sig.get("required")),
            }
    return meta


def _refine_kind(observed: dict, field_meta: dict) -> str:
    """text | select | date | radio | checkbox | toggle — deterministic."""
    base = (observed.get("kind") or "").strip().lower()
    if base == "toggle":
        return "toggle"
    fm = field_meta.get(_norm(observed.get("label", ""))) or {}
    control = fm.get("control", "")
    options = fm.get("options") or []
    if control == "checkbox":
        return "checkbox"
    if control in ("radio", "segmented"):
        return "radio"
    if control in ("select", "dropdown", "combobox") or len(options) >= 2:
        return "select"
    if _DATE_RX.search(observed.get("value", "") or ""):
        return "date"
    return "text"


# ─── Anchor scoping (generalized beyond table rows) ───────────────────────────
# A repeated control ("Select" in many rows/cards) is disambiguated by scoping
# its locator to the nearest landmark. The landmark's ARIA role is derived from
# the OBSERVED anchor_kind (captured upstream); we default to 'row' so existing
# table-anchored captures compile byte-for-byte unchanged.
_ANCHOR_ROLE = {
    "row": "row", "table-row": "row", "tablerow": "row", "tr": "row",
    "listitem": "listitem", "list-item": "listitem", "li": "listitem",
    "list": "list",
    "article": "article", "card": "article",
    "gridcell": "gridcell", "grid-cell": "gridcell", "cell": "cell",
    "region": "region", "section": "region", "group": "group",
    "listbox": "listbox", "option": "option", "menuitem": "menuitem",
    "tabpanel": "tabpanel", "dialog": "dialog",
}


def _anchor_scope(observed: dict) -> str:
    """Locator scope for a repeated control. ``'page'`` when there's no anchor;
    otherwise ``getByRole(<role-from-anchor_kind>, { name: anchor })`` — the role
    comes from the observed anchor_kind (default ``'row'`` for backward compat),
    so card / list / grid / region / dialog layouts disambiguate, not just
    tables. Every rung still targets the SAME accessible name, so this can never
    silently bind to a different control."""
    anchor = js_str(observed.get("anchor", ""))
    if not anchor:
        return "page"
    kind = _norm(observed.get("anchor_kind", "")).replace(" ", "-")
    role = _ANCHOR_ROLE.get(kind, "row")
    return f"page.getByRole('{role}', {{ name: '{anchor}' }})"


def _label_locator(observed: dict) -> str:
    label = js_str(observed.get("label", ""))
    el = f"getByLabel('{label}')"
    scope = _anchor_scope(observed)
    return f"{scope}.{el}" if scope != "page" else f"page.{el}"


def _role_locator(observed: dict, role: str) -> str:
    label = js_str(observed.get("label", ""))
    el = f"getByRole('{role}', {{ name: '{label}' }})"
    scope = _anchor_scope(observed)
    return f"{scope}.{el}" if scope != "page" else f"page.{el}"


def _ladder(observed: dict, kind: str) -> str:
    """A resilient locator: a .or() chain of complementary USER-FACING strategies,
    all keyed to the SAME accessible name. Survives a UI refactor that breaks one
    strategy, but can never silently bind to a semantically-different element
    (every rung targets the same name). If no rung matches, the action throws —
    it fails toward RED, never a silent heal-to-green. The step's own observed
    assertion is the independent oracle that catches a wrong bind."""
    label = js_str(observed.get("label", ""))
    scope = _anchor_scope(observed)
    if kind in ("text", "date"):
        rungs = [f"getByLabel('{label}')", f"getByRole('textbox', {{ name: '{label}' }})"]
    elif kind == "select":
        rungs = [f"getByLabel('{label}')", f"getByRole('combobox', {{ name: '{label}' }})"]
    elif kind == "link":
        rungs = [f"getByRole('link', {{ name: '{label}' }})", f"getByText('{label}', {{ exact: true }})"]
    elif kind == "toggle":
        rungs = [f"getByRole('radio', {{ name: '{label}' }})", f"getByText('{label}', {{ exact: true }})"]
    else:  # button
        rungs = [f"getByRole('button', {{ name: '{label}' }})", f"getByText('{label}', {{ exact: true }})"]
    chain = f"{scope}.{rungs[0]}"
    for r in rungs[1:]:
        chain = f"{chain}.or({scope}.{r})"
    return chain


# ─── Per-step action emission ─────────────────────────────────────────────────


_OUTCOME_REGION_RX = re.compile(
    r"\b(results?|listings?|table|confirmation|summary|dashboard|success|details?)\b",
    re.IGNORECASE,
)


def _assertion_from_expected_result(observed: dict) -> list[str]:
    """Compile GROUNDED, tolerant assertions from a step's observed outcome.

    Closes the two oracles the compiler previously left as comments:
      * NAVIGATION — a SUBMIT/click step carries the RECORDED next-page URL in
        ``observed['next_url']`` (threaded by the generator); assert the PATH was
        reached. Path-only + regex-contains → tolerant of host/query, never a
        full-URL mirror, and grounded in the recorded next page (not LLM prose).
      * OUTCOME REGION — when the observed ``after`` names a results/summary/etc.
        region, assert that region is visible.
    Reads ONLY recorded fields (next_url / after), so a step with no grounded
    outcome emits NOTHING — never a fake green, never a brittle red."""
    out: list[str] = []
    next_url = (observed.get("next_url") or "").strip()
    if next_url:
        path = url_path(next_url)
        if path and path != "/":
            out.append(
                f"await expect(page).toHaveURL({js_regex_literal(path)}, "
                "{ timeout: 30000 }); // grounded: navigated to the recorded next page"
            )
    after = (observed.get("after") or "").strip()
    if after:
        m = _OUTCOME_REGION_RX.search(after)
        if m:
            out.append(
                f"await expect(page.getByText(/{m.group(1).lower()}/i).first())"
                ".toBeVisible(); // grounded: observed outcome region is shown"
            )
    return out


def _action_lines(step, field_meta: dict, parametrize: bool = False,
                  reanchor: dict | None = None) -> list[str]:
    """Kind-aware Playwright lines for one (observed) step.

    When `parametrize` is set, navigation targets a path resolved against
    use.baseURL and data values become `D['key'] ?? 'observed'` — defaults stay
    the observed values, so a plain run is unchanged.

    `reanchor` (TrueFix selector re-anchor) is an optional
    `{name, kind?}` override: when present, this step is re-bound to the renamed
    control's new accessible `name` (the resilient ladder + the step's own
    visibility oracle then key off the new name). Default None → byte-identical."""
    observed = _observed(step)
    if reanchor and reanchor.get("name"):
        observed = {**observed, "label": reanchor["name"]}
        if reanchor.get("kind"):
            observed["kind"] = reanchor["kind"]
    verb = (observed.get("verb") or "").strip().lower()
    value = observed.get("value", "") or ""
    url = observed.get("url", "") or ""
    label = observed.get("label", "") or ""
    action = (getattr(step, "action", "") or "").strip()
    after = (observed.get("after") or "").strip()
    out: list[str] = []

    if verb == "navigate":
        if action.startswith("Open ") and url:
            target = _rel_path(url) if parametrize else url
            out.append(f"await page.goto('{js_str(target)}');")
        else:
            path = url_path(url)
            if path:
                out.append(
                    f"await expect(page).toHaveURL({js_regex_literal(path)}, "
                    "{ timeout: 30000 });"
                )
            else:
                out.append(f"// observed navigation: {action}")
    elif verb == "assert_required":
        for fld in [f.strip() for f in str(label).split(",") if f.strip()]:
            out.append(
                f"await expect(page.getByLabel('{js_str(fld)}')).toBeVisible(); "
                "// field present"
            )
    elif verb == "type":
        _vc = observed.get("value_conflict")
        if isinstance(_vc, dict):
            out.append(
                "// value-conflict: the recording typed '" + js_str(str(_vc.get("typed", "")))
                + "' but the snapshot captured '" + js_str(str(_vc.get("committed", "")))
                + "' — using the snapshot; confirm the intended value."
            )
        kind = _refine_kind(observed, field_meta)
        note = _a11y_note(observed)
        if note:
            out.append(note)
        if kind == "select":
            _sel = _ladder(observed, 'select')
            out.append(f"await {_sel}.selectOption({_val_expr(value, label, parametrize)});")
            if parametrize:
                # Value may be overridden at run time — assert the chooser committed
                # SOMETHING (the selection took) rather than mirroring a fixed token.
                out.append(f"await expect({_sel}).not.toHaveValue(''); // tolerant: grounded select oracle (data-driven)")
            else:
                _seltok = _value_oracle(value)
                if _seltok:
                    # Tolerant: a no-op selectOption (silent accept of nothing) now FAILS
                    # the step instead of passing green — the heal's own oracle.
                    out.append(f"await expect({_sel}).toHaveValue(/{_seltok}/i); // tolerant: grounded select oracle")
        elif kind == "date":
            out.append(f"const dateField = {_ladder(observed, 'date')};")
            out.append(f"await dateField.fill({_val_expr(value, label, parametrize)});")
            out.append(
                f"// review: '{js_str(label)}' looks like a date control — confirm "
                "the picker accepts this value/format."
            )
            if parametrize:
                out.append("await expect(dateField).not.toHaveValue(''); // tolerant: a date value was committed (data-driven)")
            else:
                _dtok = _value_oracle(value)
                if _dtok:
                    # Tolerant: assert a stable token from the date (e.g. the year)
                    # committed — a no-op fill of a date control FAILS, not greens.
                    out.append(f"await expect(dateField).toHaveValue(/{_dtok}/i); // tolerant: grounded date oracle")
                elif (value or "").strip():
                    out.append("await expect(dateField).not.toHaveValue(''); // tolerant: a date value was committed")
        else:  # text (incl. possible autocomplete)
            out.append(f"const field = {_ladder(observed, 'text')};")
            out.append(f"await field.fill({_val_expr(value, label, parametrize)});")
            if parametrize:
                # Value may be overridden at run time — assert the field committed
                # *something* (the fill worked) rather than mirroring a fixed token.
                out.append("await expect(field).not.toHaveValue(''); // tolerant: data-driven")
            else:
                tok = _value_oracle(value)
                if tok:
                    # Tolerant: a committed/normalized value (e.g. an autocomplete
                    # rewriting "Austin AUS" -> "Austin, TX (AUS)", or a numeric
                    # amount/time/year) still passes; a blank/failed entry fails.
                    # Never an exact-keystroke mirror.
                    out.append(
                        f"await expect(field).toHaveValue(/{tok}/i); "
                        "// tolerant: survives normalization/autocomplete"
                    )
                elif (value or "").strip():
                    # Symbol-only / single-char value — no safe token, but a no-op
                    # fill must still FAIL rather than pass green.
                    out.append(
                        "await expect(field).not.toHaveValue(''); "
                        "// tolerant: a value was committed"
                    )
    elif verb == "select":
        kind = _refine_kind(observed, field_meta)
        if kind in ("radio", "checkbox"):
            # Control type CONFIRMED by captured signals -> single role locator +
            # state assertion so a no-op click cannot pass green.
            name = js_str(value or label)
            loc = f"page.getByRole('{kind}', {{ name: '{name}' }})"
            out.append(f"await {loc}.check();")
            out.append(f"await expect({loc}).toBeChecked();")
        elif kind == "toggle":
            # Role UNCONFIRMED -> resilient ladder (radio name OR visible text);
            # no state assertion we cannot safely make.
            out.append(f"await {_ladder(observed, 'toggle')}.first().click();")
        else:
            _sel = _ladder(observed, 'select')
            out.append(f"await {_sel}.selectOption({_val_expr(value, label, parametrize)});")
            if parametrize:
                # Value may be overridden at run time — assert the chooser committed
                # SOMETHING (the selection took) rather than mirroring a fixed token.
                out.append(f"await expect({_sel}).not.toHaveValue(''); // tolerant: grounded select oracle (data-driven)")
            else:
                _seltok = _value_oracle(value)
                if _seltok:
                    # Tolerant: a no-op selectOption (silent accept of nothing) now FAILS
                    # the step instead of passing green — the heal's own oracle.
                    out.append(f"await expect({_sel}).toHaveValue(/{_seltok}/i); // tolerant: grounded select oracle")
    elif verb == "click":
        kind = "link" if (observed.get("kind") or "").strip().lower() == "link" else "button"
        out.append(f"await {_ladder(observed, kind)}.click();")
    else:
        out.append(f"// (no executable action derived) {action}")

    # Compile the step's Expected Result into real, grounded assertions
    # (navigation to the recorded next page + observed outcome region).
    out.extend(_assertion_from_expected_result(observed))
    if after:
        out.append(f"// observed outcome: {after}")
    return out


# ─── Case + project compilation ───────────────────────────────────────────────


# TrueFix re-anchor capture (P-B): a COMPILE-TIME-gated afterEach that, only on a
# heal-capture re-run (NEXUS_HEAL_CAPTURE=1) AND only when the test FAILED,
# snapshots the live page's accessibility tree and posts it to NEXUS_HEAL_ENDPOINT
# so the resolver can find a renamed control. Off by default → the user's owned
# spec is unchanged; best-effort → never alters the run's pass/fail result.
_HEAL_CAPTURE_AFTEREACH = """\
test.afterEach(async ({ page }, testInfo) => {
  if (process.env.NEXUS_HEAL_CAPTURE !== '1') return;
  if (testInfo.status === testInfo.expectedStatus) return; // only on failure
  const ep = process.env.NEXUS_HEAL_ENDPOINT;
  if (!ep) return;
  try {
    const aria = await page.accessibility.snapshot({ interestingOnly: false });
    await fetch(ep, {
      method: 'POST',
      headers: {
        'content-type': 'application/json',
        ...(process.env.NEXUS_TOKEN ? { authorization: 'Bearer ' + process.env.NEXUS_TOKEN } : {}),
      },
      body: JSON.stringify({
        artifact_id: process.env.NEXUS_ARTIFACT_ID || '',
        run_id: process.env.NEXUS_RUN_ID || '',
        scenario_id: '__TEST_ID__',
        status: testInfo.status,
        aria,
      }),
    });
  } catch { /* capture is best-effort; never fail the run */ }
});"""


def compile_case(tc, field_meta: dict | None = None, *, parametrize: bool = False,
                 reanchors: dict | None = None, heal_capture: bool = False) -> str:
    """Compile one ProductionTestCase to a runnable Playwright .spec.ts (string).

    When `parametrize` is set, the spec reads optional env/data overrides
    (use.baseURL + nexus.data.json) with the observed values as defaults — so it
    runs against any environment with any data, identically by default.

    `reanchors` (TrueFix selector re-anchor) is an optional
    `{step_number: {name, kind?}}` map; a step with an entry is re-bound to the
    renamed control's new accessible name. Default None → byte-identical.

    `heal_capture` appends a gated afterEach that snapshots the failure-state a11y
    tree for the re-anchor resolver (only on a NEXUS_HEAL_CAPTURE=1 re-run that
    fails). Default False → byte-identical (the user's owned spec is untouched)."""
    field_meta = field_meta or {}
    name = (getattr(tc, "name", None) or "Generated test").strip()
    description = (getattr(tc, "description", None) or "").strip()
    steps = list(getattr(tc, "steps", None) or [])
    high = sum(1 for s in steps if _confidence(s) == "high")
    review = sum(1 for s in steps if _confidence(s) in ("review", "confirm"))
    weak_a11y = sum(
        1 for s in steps
        if (_observed(s).get("verb") or "").strip().lower() in ("type", "select", "click")
        and not (_observed(s).get("label") or "").strip()
    )

    # Consent/cookie clicks are handled as defensive SETUP after navigation, not
    # as a provenance-late mid-flow step that the overlay would have blocked.
    consent_present = any(_is_consent(s) for s in steps)
    flow = [s for s in steps if not _is_consent(s)]

    out: list[str] = [
        "// GENERATED by Nexus Script Factory — deterministic, grounded in a real recording.",
        "// Locators/actions/assertions derive from OBSERVED evidence (kind-aware). No LLM.",
        "// Resilient locators: each is a .or() ladder of user-facing strategies keyed to",
        "// the same accessible name — survives a UI refactor, fails toward RED (never a",
        "// silent wrong-bind). Edit freely — you own this code; UNPROVEN steps are skipped.",
    ]
    if description:
        out.append(f"// {description}")
    _expected_outcome = (getattr(tc, "expected_outcome", "") or "").strip()
    if _expected_outcome:
        out.append(f"// Expected outcome: {_expected_outcome}")
    out.append(f"// Confidence: {high} solid step(s), {review} need review.")
    if weak_a11y:
        out.append(f"// a11y: {weak_a11y} control(s) had no observed accessible name "
                   "(flagged inline) — improving the app's labels makes these reliable.")
    out.append("")
    out.append("import { test, expect } from '@playwright/test';")
    out.append("")
    test_id = js_str(getattr(tc, "test_id", "") or "")
    if test_id:
        # Carry the test-case id so the Nexus reporter can map a run's failure
        # back to this test's capture-time baseline (grounded triage).
        out.append(
            f"test('{js_str(name)}', "
            f"{{ annotation: [{{ type: 'nexus-test-id', description: '{test_id}' }}] }}, "
            "async ({ page }) => {"
        )
    else:
        out.append(f"test('{js_str(name)}', async ({{ page }}) => {{")

    if parametrize:
        # Optional run-time data overrides; defaults are the observed values, so a
        # plain run is unchanged. Each spec reads ITS OWN test's data slot merged
        # over shared _global defaults (precedence: per-test > global > observed).
        # Base URL comes from use.baseURL (playwright.config).
        tid_js = js_str(getattr(tc, "test_id", "") or "")
        out.append(
            "  const D = (() => { try { const __a = require('../../nexus.data.json'); "
            "return Object.assign({}, __a['_global'] || {}, __a['" + tid_js + "'] || {}); } "
            "catch { return {}; } })();"
        )

    consent_emitted = False
    for step in flow:
        n = getattr(step, "step_number", None)
        action = (getattr(step, "action", "") or "").strip()
        observed = _observed(step)
        verb = (observed.get("verb") or "").strip().lower()

        # Load-bearing honesty: un-observed / review steps STOP the test with an
        # UNPROVEN skip — so no downstream assertion runs across the gap and
        # false-reds. (Never a fake green, never a silent jump.)
        if _provenance(step) == "inferred" or _confidence(step) == "review":
            out.append(f"  // step {n} — {action}")
            reason = f"UNPROVEN: step {n} not directly observed — {action}"
            out.append(f"  test.skip(true, {json.dumps(reason)});")
            continue

        out.append(f"  await test.step({json.dumps(f'step {n}: {action}')}, async () => {{")
        out.append(f"    // evidence: provenance={_provenance(step) or 'n/a'}, "
                   f"confidence={_confidence(step) or 'n/a'}")
        for line in _action_lines(step, field_meta, parametrize,
                                  reanchor=(reanchors or {}).get(n)):
            out.append(f"    {line}")
        out.append("  });")

        if (consent_present and not consent_emitted
                and verb == "navigate" and action.startswith("Open ")):
            consent_emitted = True
            out.append("  // dismiss a consent/cookie overlay if present (defensive setup)")
            out.append("  const __consent = page.getByRole('button', "
                       "{ name: /accept|agree|allow|got it/i });")
            out.append("  if (await __consent.isVisible().catch(() => false)) "
                       "await __consent.click();")

    out.append("});")
    if heal_capture:
        out.append("")
        out.append(_HEAL_CAPTURE_AFTEREACH.replace("__TEST_ID__", test_id))
    out.append("")
    return "\n".join(out)


_PACKAGE_JSON = """\
{
  "name": "nexus-generated-e2e",
  "private": true,
  "version": "1.0.0",
  "scripts": {
    "test": "playwright test",
    "report": "playwright show-report"
  },
  "devDependencies": {
    "@playwright/test": "^1.48.0",
    "typescript": "^5.5.0"
  }
}
"""

_PLAYWRIGHT_CONFIG = """\
import { defineConfig, devices } from '@playwright/test';

export default defineConfig({
  testDir: './tests',
  fullyParallel: true,
  forbidOnly: true,
  retries: 0,
  // Per-test ceiling so one hanging spec fails alone instead of letting the
  // outer run timeout SIGKILL (and red-flag) the whole batch.
  timeout: 60_000,
  expect: { timeout: 15_000 },
  reporter: [['list'], ['html', { open: 'never' }], ['junit', { outputFile: 'results/junit.xml' }], ['./nexus-reporter.ts']],
  use: {
    trace: 'on-first-retry',
    screenshot: 'only-on-failure',
    // Reuse a captured authenticated session when Nexus injects one (auth profile
    // → nexus.auth.json in the run dir). Self-detecting, so a downloaded bundle
    // (no auth file) is unaffected; a normal unauthenticated run is unchanged.
    ...((() => { try { return require('fs').existsSync('./nexus.auth.json') ? { storageState: './nexus.auth.json' } : {}; } catch { return {}; } })()),
  },
  projects: [
    { name: 'chromium', use: { ...devices['Desktop Chrome'] } },
  ],
});
"""

# Playwright device profile per selectable browser.
_DEVICE = {
    "chromium": "Desktop Chrome",
    "firefox": "Desktop Firefox",
    "webkit": "Desktop Safari",
}

# Parametrized config template — placeholders filled by _playwright_config_param.
# Base URL precedence: env NEXUS_BASE_URL > nexus.config.json > recorded default,
# so the SAME suite runs against dev / staging / prod with no code edits.
_CONFIG_PARAM_TEMPLATE = """\
import { defineConfig, devices } from '@playwright/test';
import * as fs from 'fs';

function nexusBaseURL(): string | undefined {
  if (process.env.NEXUS_BASE_URL) return process.env.NEXUS_BASE_URL;
  try {
    const cfg = JSON.parse(fs.readFileSync('./nexus.config.json', 'utf-8'));
    if (cfg && cfg.baseURL) return cfg.baseURL;
  } catch { /* no config file — fall through to undefined */ }
  return undefined;
}

export default defineConfig({
  testDir: './tests',
  fullyParallel: true,
  forbidOnly: true,
  retries: __RETRIES__,
  // Per-test ceiling so one hanging spec fails alone instead of letting the
  // outer run timeout SIGKILL (and red-flag) the whole batch.
  timeout: 60_000,
  expect: { timeout: 15_000 },
__WORKERS__  reporter: [['list'], ['html', { open: 'never' }], ['junit', { outputFile: 'results/junit.xml' }], ['./nexus-reporter.ts']],
  use: {
    baseURL: nexusBaseURL(),
    headless: __HEADLESS__,
    trace: 'on-first-retry',
    screenshot: 'only-on-failure',
    // Reuse a captured authenticated session when Nexus injects one (auth profile
    // → nexus.auth.json in the run dir). Self-detecting, so a downloaded bundle
    // (no auth file) is unaffected; a normal unauthenticated run is unchanged.
    ...(fs.existsSync('./nexus.auth.json') ? { storageState: './nexus.auth.json' } : {}),
    // Container runners set NEXUS_LAUNCH_ARGS (e.g. --no-sandbox); empty locally.
    launchOptions: { args: (process.env.NEXUS_LAUNCH_ARGS || '').split(' ').filter(Boolean) },
  },
  projects: [
__PROJECTS__
  ],
});
"""


def _playwright_config_param(projects=None, headed: bool = False,
                             workers=None, retries: int = 0) -> str:
    """Build the parametrized playwright.config.ts with the chosen browser
    projects, headed/headless mode, worker count and retries. Unknown browsers
    are dropped; an empty selection falls back to chromium."""
    sel = [p for p in (projects or []) if p in _DEVICE] or ["chromium"]
    proj_lines = ",\n".join(
        f"    {{ name: '{p}', use: {{ ...devices['{_DEVICE[p]}'] }} }}" for p in sel
    )
    workers_line = f"  workers: {int(workers)},\n" if workers else ""
    return (
        _CONFIG_PARAM_TEMPLATE
        .replace("__RETRIES__", str(int(retries or 0)))
        .replace("__WORKERS__", workers_line)
        .replace("__HEADLESS__", "false" if headed else "true")
        .replace("__PROJECTS__", proj_lines)
    )

_TSCONFIG = """\
{
  "compilerOptions": {
    "target": "ES2021",
    "module": "CommonJS",
    "moduleResolution": "Node",
    "strict": true,
    "esModuleInterop": true,
    "skipLibCheck": true,
    "types": ["node"]
  }
}
"""


_NEXUS_REPORTER_TS = """\
// Nexus reporter — ships this run's results back to your Nexus platform so the
// Grounded Triage view can show baseline-vs-actual + a verdict for each failure.
// 100% yours: edit or delete freely. It is a NO-OP unless NEXUS_ENDPOINT,
// NEXUS_TOKEN and NEXUS_ARTIFACT_ID are set in the environment, so normal local
// runs are unaffected. Requires Node 18+ (global fetch).
import type { Reporter, FullResult, FullConfig, Suite, TestCase, TestResult } from '@playwright/test/reporter';
import * as fs from 'fs';

const ENDPOINT = process.env.NEXUS_ENDPOINT || '';
const TOKEN = process.env.NEXUS_TOKEN || '';
const ARTIFACT_ID = process.env.NEXUS_ARTIFACT_ID || '';

type StepRecord = {
  test_name: string;
  scenario_id: string;
  step_number: number;
  status: string;
  duration_ms: number;
  error_message: string;
  screenshot_url?: string;
};

type PendingShot = {
  idx: number;
  scenarioId: string;
  stepNumber: number;
  path?: string;
  body?: Buffer;
  contentType: string;
};

function mapRunStatus(s: string): string {
  if (s === 'passed') return 'passed';
  if (s === 'timedOut') return 'timed_out';
  if (s === 'skipped') return 'skipped';
  if (s === 'interrupted') return 'broken';
  return 'failed';
}

export default class NexusReporter implements Reporter {
  private steps: StepRecord[] = [];
  private pendingShots: PendingShot[] = [];
  private startedAt = new Date(0).toISOString();
  private done = 0;
  private total = 0;

  onBegin(_config: FullConfig, suite: Suite): void {
    this.startedAt = new Date().toISOString();
    try { this.total = suite.allTests().length; } catch { this.total = 0; }
  }

  onTestEnd(test: TestCase, result: TestResult): void {
    const ann = test.annotations.find((a) => a.type === 'nexus-test-id');
    const scenarioId = (ann?.description || '').slice(0, 64);
    const skipped = result.status === 'skipped';
    const observed = result.steps.filter((s) => /^step \\d+:/.test(s.title));
    const startIdx = this.steps.length;
    if (observed.length === 0) {
      this.steps.push({
        test_name: test.title,
        scenario_id: scenarioId,
        step_number: 0,
        status: mapRunStatus(result.status),
        duration_ms: Math.round(result.duration),
        error_message: (result.error?.message || '').slice(0, 8000),
      });
    } else {
      for (const s of observed) {
        const m = s.title.match(/^step (\\d+):/);
        this.steps.push({
          test_name: test.title,
          scenario_id: scenarioId,
          step_number: m ? parseInt(m[1], 10) : 0,
          status: skipped ? 'skipped' : s.error ? 'failed' : 'passed',
          duration_ms: Math.round(s.duration),
          error_message: (s.error?.message || '').slice(0, 8000),
        });
      }
    }
    // Associate Playwright's only-on-failure screenshot with the failing step
    // record; uploaded at onEnd. Best-effort — never affects the run result.
    if (result.status === 'failed' || result.status === 'timedOut') {
      const shot = result.attachments.find(
        (a) => (a.name === 'screenshot' || (a.contentType || '').startsWith('image/')) && (a.path || a.body),
      );
      if (shot) {
        let idx = -1;
        for (let i = this.steps.length - 1; i >= startIdx; i--) {
          const stt = this.steps[i].status;
          if (stt === 'failed' || stt === 'broken' || stt === 'timed_out') { idx = i; break; }
        }
        if (idx < 0) idx = this.steps.length - 1;
        if (idx >= startIdx) {
          this.pendingShots.push({
            idx,
            scenarioId,
            stepNumber: this.steps[idx].step_number,
            path: shot.path,
            body: shot.body as Buffer | undefined,
            contentType: shot.contentType || 'image/png',
          });
        }
      }
    }
    this.done += 1;
    this.postProgress(mapRunStatus(result.status));
  }

  private postProgress(lastStatus: string): void {
    const runId = process.env.NEXUS_RUN_ID || '';
    if (!ENDPOINT || !TOKEN || !ARTIFACT_ID || !runId) return;
    void fetch(`${ENDPOINT.replace(/\\/$/, '')}/api/v1/test-runs/progress`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${TOKEN}` },
      body: JSON.stringify({ run_id: runId, artifact_id: ARTIFACT_ID, done: this.done, total: this.total, last_status: lastStatus }),
    }).catch(() => { /* best-effort progress; never blocks the run */ });
  }

  async onEnd(result: FullResult): Promise<void> {
    if (!ENDPOINT || !TOKEN || !ARTIFACT_ID) {
      console.warn('[nexus-reporter] NEXUS_ENDPOINT/TOKEN/ARTIFACT_ID not set — results not uploaded.');
      return;
    }
    const base = ENDPOINT.replace(/\\/$/, '');
    const runId = process.env.NEXUS_RUN_ID || process.env.GITHUB_RUN_ID || '';

    // Upload each failure-state screenshot and attach its serve URL to the
    // matching step. Best-effort: a failed upload never blocks or fails the run;
    // pre-migration the endpoint returns 503 and we simply skip (screenshot_url
    // stays empty, so the UI shows 'awaiting capture' exactly as before).
    for (const ps of this.pendingShots) {
      try {
        const buf: Buffer | undefined = ps.body || (ps.path ? fs.readFileSync(ps.path) : undefined);
        if (!buf || !buf.length) continue;
        const fd = new FormData();
        fd.append('run_id', runId);
        fd.append('artifact_id', ARTIFACT_ID);
        fd.append('scenario_id', ps.scenarioId);
        fd.append('step_number', String(ps.stepNumber));
        fd.append('file', new Blob([buf], { type: ps.contentType }), 'actual.png');
        const sr = await fetch(`${base}/api/v1/test-runs/screenshot`, {
          method: 'POST',
          headers: { Authorization: `Bearer ${TOKEN}` },
          body: fd as any,
        });
        if (sr.ok) {
          const j: any = await sr.json().catch(() => null);
          if (j && j.url && this.steps[ps.idx]) this.steps[ps.idx].screenshot_url = j.url;
        }
      } catch { /* best-effort screenshot upload */ }
    }

    const body = {
      artifact_id: ARTIFACT_ID,
      ci_run_id: runId,
      ci_commit_sha: process.env.GITHUB_SHA || process.env.CI_COMMIT_SHA || '',
      ci_pipeline_url: process.env.NEXUS_PIPELINE_URL || '',
      environment: process.env.NEXUS_ENV || 'ci',
      status: mapRunStatus(result.status),
      started_at: this.startedAt,
      completed_at: new Date().toISOString(),
      steps: this.steps,
    };
    try {
      const resp = await fetch(`${base}/api/v1/test-runs/ingest`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${TOKEN}` },
        body: JSON.stringify(body),
      });
      console.log(`[nexus-reporter] uploaded ${this.steps.length} step result(s) -> HTTP ${resp.status}`);
    } catch (e) {
      console.warn('[nexus-reporter] upload failed:', (e as Error).message);
    }
  }
}
"""

_ENV_EXAMPLE = """\
# Upload run results to Nexus for the Grounded Triage view (baseline-vs-actual +
# a verdict per failure). Leave unset to run normally with no upload.
NEXUS_ENDPOINT=https://your-nexus-host
NEXUS_TOKEN=your-api-jwt
NEXUS_ARTIFACT_ID=your-artifact-id
"""


def _readme(case_count: int, high: int, review: int) -> str:
    return (
        "# Nexus-generated Playwright suite\n\n"
        "Deterministically generated from a real, recorded session — grounded in "
        "observed evidence (kind-aware locators/actions + outcome), no LLM.\n\n"
        f"- Test specs: {case_count}\n"
        f"- Solid steps: {high}  ·  Steps needing review: {review}\n\n"
        "## Run\n\n"
        "```bash\nnpm install\nnpx playwright install --with-deps\nnpm test\n```\n\n"
        "You own this code — edit it like any hand-written Playwright suite. Steps "
        "skipped with `UNPROVEN` were not directly observed in the recording; "
        "implement the gating action before relying on the rest of that test.\n"
    )


def _recorded_origin(cases: Iterable) -> str:
    """First navigated origin across the suite — the default base for runs."""
    for tc in cases:
        for s in (getattr(tc, "steps", None) or []):
            o = _observed(s)
            if (o.get("verb") or "").strip().lower() == "navigate":
                origin = _origin(o.get("url", "") or "")
                if origin:
                    return origin
    return ""


def compile_project(
    cases: Iterable,
    field_meta: dict | None = None,
    *,
    parametrize: bool = False,
    base_url_default: str = "",
    projects=None,
    headed: bool = False,
    workers=None,
    retries: int = 0,
) -> dict[str, str]:
    """Compile active cases into a runnable Playwright project (path -> content).

    parametrize=True emits an env/data-driven project: navigation resolves
    against use.baseURL and data values read nexus.data.json (observed defaults).
    A default nexus.config.json (baseURL = base_url_default or the recorded
    origin) is included so the parametrized bundle runs standalone."""
    cases = list(cases)
    field_meta = field_meta or {}
    files: dict[str, str] = {}
    used: dict[str, int] = {}
    high = review = 0
    for tc in cases:
        steps = list(getattr(tc, "steps", None) or [])
        high += sum(1 for s in steps if _confidence(s) == "high")
        review += sum(1 for s in steps if _confidence(s) in ("review", "confirm"))
        kind = _slug(getattr(tc, "type", "") or "functional", "functional")
        base = _slug(getattr(tc, "name", "") or "test", "test")
        key = f"tests/{kind}/{base}"
        if key in used:
            used[key] += 1
            path = f"{key}-{used[key]}.spec.ts"
        else:
            used[key] = 0
            path = f"{key}.spec.ts"
        files[path] = compile_case(tc, field_meta, parametrize=parametrize)

    files["package.json"] = _PACKAGE_JSON
    files["playwright.config.ts"] = (
        _playwright_config_param(projects, headed, workers, retries)
        if parametrize else _PLAYWRIGHT_CONFIG
    )
    files["tsconfig.json"] = _TSCONFIG
    files["nexus-reporter.ts"] = _NEXUS_REPORTER_TS
    files[".env.example"] = _ENV_EXAMPLE
    files["README.md"] = _readme(len(cases), high, review)
    if parametrize:
        base = (base_url_default or _recorded_origin(cases) or "").strip()
        files["nexus.config.json"] = json.dumps({"baseURL": base}, indent=2) + "\n"
    return files


# ─── CI/CD pipeline emitters (deterministic; run the bundled suite + report) ───
# `npx playwright test` runs every project (browser) baked into playwright.config,
# so no per-browser matrix is needed here. NEXUS_* are CI secrets.

_GITHUB_ACTIONS_YML = """\
name: Nexus E2E
on: [push, workflow_dispatch]
jobs:
  e2e:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-node@v4
        with:
          node-version: 20
      - run: npm install
      - run: npx playwright install --with-deps
      - run: npx playwright test
        env:
          NEXUS_ENDPOINT: ${{ secrets.NEXUS_ENDPOINT }}
          NEXUS_TOKEN: ${{ secrets.NEXUS_TOKEN }}
          NEXUS_ARTIFACT_ID: __ARTIFACT_ID__
      - if: always()
        uses: actions/upload-artifact@v4
        with:
          name: playwright-report
          path: playwright-report/
"""

_GITLAB_CI_YML = """\
stages: [e2e]
e2e:
  stage: e2e
  image: mcr.microsoft.com/playwright:v1.48.0-jammy
  variables:
    NEXUS_ARTIFACT_ID: "__ARTIFACT_ID__"
  before_script:
    - npm install
    - npx playwright install --with-deps
  script:
    - npx playwright test
  artifacts:
    when: always
    paths: [playwright-report/]
    expire_in: 1 week
  # Set NEXUS_ENDPOINT + NEXUS_TOKEN as masked GitLab CI/CD variables.
"""

_JENKINSFILE = """\
pipeline {
  agent any
  environment {
    NEXUS_ENDPOINT = credentials('nexus-endpoint')
    NEXUS_TOKEN = credentials('nexus-token')
    NEXUS_ARTIFACT_ID = '__ARTIFACT_ID__'
  }
  stages {
    stage('Install') { steps { sh 'npm install && npx playwright install --with-deps' } }
    stage('Test')    { steps { sh 'npx playwright test' } }
  }
  post {
    always { archiveArtifacts artifacts: 'playwright-report/**', allowEmptyArchive: true }
  }
}
"""


def _ci_readme(aid: str) -> str:
    return (
        "# Nexus — CI/CD\n\n"
        "Ready-to-commit GitHub Actions, GitLab CI and Jenkins pipelines that run this "
        "suite on every push and report failures to the Nexus Grounded Triage board.\n\n"
        "## Required secrets (set in your CI provider)\n\n"
        "- `NEXUS_ENDPOINT` — your Nexus host (e.g. https://nexus.yourco.com)\n"
        "- `NEXUS_TOKEN` — an API JWT\n"
        f"- `NEXUS_ARTIFACT_ID` — already baked in ({aid})\n\n"
        "GitHub: Settings → Secrets and variables → Actions. "
        "GitLab: Settings → CI/CD → Variables (masked). "
        "Jenkins: Credentials → `nexus-endpoint`, `nexus-token`.\n"
    )


def ci_workflow_files(artifact_id: str) -> dict:
    """Deterministic CI pipeline files that run the bundled suite and report back
    via the NEXUS_* reporter env. Pure string templates — zero backend deps."""
    aid = (artifact_id or "")[:64]
    return {
        ".github/workflows/nexus-e2e.yml": _GITHUB_ACTIONS_YML.replace("__ARTIFACT_ID__", aid),
        ".gitlab-ci.yml": _GITLAB_CI_YML.replace("__ARTIFACT_ID__", aid),
        "Jenkinsfile": _JENKINSFILE.replace("__ARTIFACT_ID__", aid),
        "CI.md": _ci_readme(aid),
    }


# Human labels for the test categories — mirror the UI's SECTIONS so the
# Execution view groups identically.
_CATEGORY_LABELS = {
    "functional": "Demonstrated",
    "combination": "Suggested combinations",
    "negative": "Negative",
    "boundary": "Boundary",
    "error_state": "Error-state",
}


def _step_stats(steps: list) -> dict:
    """Per-script step counts — total / solid / needs-review / skipped. The
    `skipped` rule mirrors compile_case's load-bearing UNPROVEN skip exactly."""
    return {
        "total": len(steps),
        "solid": sum(1 for s in steps if _confidence(s) == "high"),
        "review": sum(1 for s in steps if _confidence(s) in ("review", "confirm")),
        "skipped": sum(
            1 for s in steps
            if _provenance(s) == "inferred" or _confidence(s) == "review"
        ),
    }


def _data_fields(steps: list, field_meta: dict) -> list[dict]:
    """Overridable data values observed in this script — exactly the values the
    parametrized compile wraps (typed values + real <select>s), keyed by label
    slug (first occurrence wins). Drives the Run Console's data editor. Radio /
    checkbox / toggle choices are omitted (left literal by the compiler)."""
    seen: set = set()
    fields: list[dict] = []
    for s in steps:
        o = _observed(s)
        verb = (o.get("verb") or "").strip().lower()
        if verb == "type":
            overridable = True
        elif verb == "select":
            overridable = _refine_kind(o, field_meta) == "select"
        else:
            overridable = False
        if not overridable:
            continue
        label = (o.get("label") or "").strip()
        key = _data_key(label)
        if not key or key in seen:
            continue
        seen.add(key)
        fields.append({
            "key": key,
            "label": label,
            "default": o.get("value", "") or "",
            "kind": (o.get("kind") or "text").strip().lower() or "text",
        })
    return fields


def compile_manifest(cases: Iterable, field_meta: dict | None = None) -> dict:
    """Structured listing of the compiled suite for the Execution UI.

    Returns each script's SOURCE + spec path + category + per-step stats, plus
    the supporting project files and run commands. Identical compilation to
    compile_project (same paths, byte-identical code) — just returned as data
    instead of a zip. Deterministic, ZERO LLM. Empty `scripts` when no cases.
    """
    cases = list(cases)
    field_meta = field_meta or {}
    used: dict[str, int] = {}
    scripts: list[dict] = []
    high = review = 0
    for tc in cases:
        steps = list(getattr(tc, "steps", None) or [])
        high += sum(1 for s in steps if _confidence(s) == "high")
        review += sum(1 for s in steps if _confidence(s) in ("review", "confirm"))
        # Path keying — kept IDENTICAL to compile_project (single source of truth
        # for the layout the downloaded zip uses).
        kind = _slug(getattr(tc, "type", "") or "functional", "functional")
        base = _slug(getattr(tc, "name", "") or "test", "test")
        key = f"tests/{kind}/{base}"
        if key in used:
            used[key] += 1
            path = f"{key}-{used[key]}.spec.ts"
        else:
            used[key] = 0
            path = f"{key}.spec.ts"
        code = compile_case(tc, field_meta)
        cat = (getattr(tc, "type", "") or "functional").strip().lower() or "functional"
        scripts.append({
            "test_id": getattr(tc, "test_id", "") or "",
            "name": (getattr(tc, "name", None) or "Generated test").strip(),
            "description": (getattr(tc, "description", None) or "").strip(),
            "category": cat,
            "category_label": _CATEGORY_LABELS.get(cat, cat.replace("_", " ").title()),
            "priority": (getattr(tc, "priority", "") or "").strip(),
            "path": path,
            "code": code,
            "lines": code.count("\n") + 1,
            "stats": _step_stats(steps),
            "data_fields": _data_fields(steps, field_meta),
            "base_url": _recorded_origin([tc]),
        })

    project_files = [
        {"path": "playwright.config.ts", "code": _PLAYWRIGHT_CONFIG},
        {"path": "package.json", "code": _PACKAGE_JSON},
        {"path": "tsconfig.json", "code": _TSCONFIG},
        {"path": "nexus-reporter.ts", "code": _NEXUS_REPORTER_TS},
        {"path": ".env.example", "code": _ENV_EXAMPLE},
        {"path": "README.md", "code": _readme(len(cases), high, review)},
    ]
    return {
        "scripts": scripts,
        "project_files": project_files,
        "recorded_base_url": _recorded_origin(cases),
        "totals": {
            "scripts": len(cases),
            "solid_steps": high,
            "review_steps": review,
        },
    }
