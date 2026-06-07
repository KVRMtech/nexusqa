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


def _label_locator(observed: dict) -> str:
    label = js_str(observed.get("label", ""))
    anchor = js_str(observed.get("anchor", ""))
    el = f"getByLabel('{label}')"
    if anchor:
        return f"page.getByRole('row', {{ name: '{anchor}' }}).{el}"
    return f"page.{el}"


def _role_locator(observed: dict, role: str) -> str:
    label = js_str(observed.get("label", ""))
    anchor = js_str(observed.get("anchor", ""))
    el = f"getByRole('{role}', {{ name: '{label}' }})"
    if anchor:
        return f"page.getByRole('row', {{ name: '{anchor}' }}).{el}"
    return f"page.{el}"


# ─── Per-step action emission ─────────────────────────────────────────────────


def _action_lines(step, field_meta: dict) -> list[str]:
    """Kind-aware Playwright lines for one (observed) step."""
    observed = _observed(step)
    verb = (observed.get("verb") or "").strip().lower()
    value = observed.get("value", "") or ""
    url = observed.get("url", "") or ""
    label = observed.get("label", "") or ""
    action = (getattr(step, "action", "") or "").strip()
    after = (observed.get("after") or "").strip()
    out: list[str] = []

    if verb == "navigate":
        if action.startswith("Open ") and url:
            out.append(f"await page.goto('{js_str(url)}');")
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
        kind = _refine_kind(observed, field_meta)
        note = _a11y_note(observed)
        if note:
            out.append(note)
        if kind == "select":
            out.append(f"await {_label_locator(observed)}.selectOption('{js_str(value)}');")
        elif kind == "date":
            out.append(f"await {_label_locator(observed)}.fill('{js_str(value)}');")
            out.append(
                f"// review: '{js_str(label)}' looks like a date control — confirm "
                "the picker accepts this value/format."
            )
        else:  # text (incl. possible autocomplete)
            loc = _label_locator(observed)
            out.append(f"const field = {loc};")
            out.append(f"await field.fill('{js_str(value)}');")
            tok = _assert_token(value)
            if tok:
                # Tolerant: a committed/normalized value (e.g. an autocomplete
                # rewriting "Austin AUS" -> "Austin, TX (AUS)") still passes; a
                # blank/failed entry still fails. Never an exact-keystroke mirror.
                out.append(
                    f"await expect(field).toHaveValue(/{tok}/i); "
                    "// tolerant: survives normalization/autocomplete"
                )
    elif verb == "select":
        kind = _refine_kind(observed, field_meta)
        if kind in ("radio", "checkbox"):
            # Control type CONFIRMED by captured signals -> assert the resulting
            # state so a no-op click cannot pass green.
            name = js_str(value or label)
            loc = f"page.getByRole('{kind}', {{ name: '{name}' }})"
            out.append(f"await {loc}.check();")
            out.append(f"await expect({loc}).toBeChecked();")
        elif kind == "toggle":
            name = js_str(label)
            # Role UNCONFIRMED -> role-tolerant locator (radio name OR visible
            # text); no state assertion we cannot safely make.
            out.append(
                f"await page.getByRole('radio', {{ name: '{name}' }})"
                f".or(page.getByText('{name}', {{ exact: true }})).first().click();"
            )
        else:
            out.append(f"await {_label_locator(observed)}.selectOption('{js_str(value)}');")
    elif verb == "click":
        role = "link" if (observed.get("kind") or "").strip().lower() == "link" else "button"
        out.append(f"await {_role_locator(observed, role)}.click();")
    else:
        out.append(f"// (no executable action derived) {action}")

    if after:
        out.append(f"// observed outcome: {after}")
    return out


# ─── Case + project compilation ───────────────────────────────────────────────


def compile_case(tc, field_meta: dict | None = None) -> str:
    """Compile one ProductionTestCase to a runnable Playwright .spec.ts (string)."""
    field_meta = field_meta or {}
    name = (getattr(tc, "name", None) or "Generated test").strip()
    description = (getattr(tc, "description", None) or "").strip()
    steps = list(getattr(tc, "steps", None) or [])
    high = sum(1 for s in steps if _confidence(s) == "high")
    review = sum(1 for s in steps if _confidence(s) == "review")
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
        "// Edit freely — you own this code. UNPROVEN steps are skipped honestly, not faked.",
    ]
    if description:
        out.append(f"// {description}")
    out.append(f"// Confidence: {high} solid step(s), {review} need review.")
    if weak_a11y:
        out.append(f"// a11y: {weak_a11y} control(s) had no observed accessible name "
                   "(flagged inline) — improving the app's labels makes these reliable.")
    out.append("")
    out.append("import { test, expect } from '@playwright/test';")
    out.append("")
    out.append(f"test('{js_str(name)}', async ({{ page }}) => {{")

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
        for line in _action_lines(step, field_meta):
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
  reporter: [['list'], ['html', { open: 'never' }]],
  use: {
    trace: 'on-first-retry',
    screenshot: 'only-on-failure',
  },
  projects: [
    { name: 'chromium', use: { ...devices['Desktop Chrome'] } },
  ],
});
"""

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


def compile_project(cases: Iterable, field_meta: dict | None = None) -> dict[str, str]:
    """Compile active cases into a runnable Playwright project (path -> content)."""
    cases = list(cases)
    field_meta = field_meta or {}
    files: dict[str, str] = {}
    used: dict[str, int] = {}
    high = review = 0
    for tc in cases:
        steps = list(getattr(tc, "steps", None) or [])
        high += sum(1 for s in steps if _confidence(s) == "high")
        review += sum(1 for s in steps if _confidence(s) == "review")
        kind = _slug(getattr(tc, "type", "") or "functional", "functional")
        base = _slug(getattr(tc, "name", "") or "test", "test")
        key = f"tests/{kind}/{base}"
        if key in used:
            used[key] += 1
            path = f"{key}-{used[key]}.spec.ts"
        else:
            used[key] = 0
            path = f"{key}.spec.ts"
        files[path] = compile_case(tc, field_meta)

    files["package.json"] = _PACKAGE_JSON
    files["playwright.config.ts"] = _PLAYWRIGHT_CONFIG
    files["tsconfig.json"] = _TSCONFIG
    files["README.md"] = _readme(len(cases), high, review)
    return files
