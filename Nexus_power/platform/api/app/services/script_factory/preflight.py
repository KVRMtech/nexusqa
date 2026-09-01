"""Live Preflight — a DETERMINISTIC Playwright probe that grounds a compiled test
against the LIVE app BEFORE a full run. No LLM, no test authoring: it reuses the
frozen compiler's resilient locator ladder (read-only imports) and only *checks
reality* — for each step it reports how many live elements the locator resolves
to (0 = broken/renamed, 1 = good, >1 = ambiguous), whether a <select>'s recorded
option exists, and whether each recorded URL is reachable. It is the proof that a
test will run — turning Audit from "faithful to the recording" into "verified
against the live app". Additive: it imports the compiler's helpers but never
modifies the frozen compiler, and runs through the existing nexus-runner.
"""
from __future__ import annotations

import json
from typing import Iterable

from .compiler import _observed, _ladder, _refine_kind, _value_oracle
from .locators import js_str, url_path, js_regex_literal

_MARK = "NEXUS_PREFLIGHT "


# ── Minimal, self-contained probe config (NOT the frozen spec config) ──────────
_PREFLIGHT_CONFIG = """\
import { defineConfig } from '@playwright/test';
import * as fs from 'fs';
// Reuse the artifact's saved login session when the server provides it (written as
// nexus.auth.json, decrypted from the captured auth profile). Absent -> an
// unauthenticated probe, exactly as before. The path is resolved at runtime from
// the file the server writes; no app, URL, or credential is ever hardcoded.
const _authState = fs.existsSync('nexus.auth.json') ? 'nexus.auth.json' : undefined;
export default defineConfig({
  testDir: './tests',
  timeout: 60000,
  retries: 0,
  reporter: 'list',
  use: { __BASEURL__ storageState: _authState, headless: true, actionTimeout: 5000, navigationTimeout: 12000 },
});
"""


def _config(base_url: str) -> str:
    bu = (base_url or "").strip()
    base = f"baseURL: '{js_str(bu)}'," if bu else ""
    return _PREFLIGHT_CONFIG.replace("__BASEURL__", base)


def _probe_locator(observed: dict, field_meta: dict) -> tuple[str, str]:
    """(locator_js, kind) for the count probe — mirrors the compiler's kind
    branching but emits a LOCATOR only (no action, no assertion)."""
    verb = (observed.get("verb") or "").strip().lower()
    if verb == "click":
        kind = "link" if (observed.get("kind") or "").strip().lower() == "link" else "button"
        return _ladder(observed, kind), kind
    kind = _refine_kind(observed, field_meta)
    if kind in ("radio", "checkbox"):
        name = js_str(observed.get("value") or observed.get("label") or "")
        return f"page.getByRole('{kind}', {{ name: '{name}' }})", kind
    if kind == "select":
        return _ladder(observed, "select"), "select"
    if kind == "toggle":
        return _ladder(observed, "toggle"), "toggle"
    if kind == "date":
        return _ladder(observed, "date"), "date"
    return _ladder(observed, "text"), "text"


def _soft_advance(observed: dict, kind: str) -> str:
    """A best-effort action to advance to the next page (so later-page locators
    are reachable). Always .catch()'d — preflight never aborts on a step."""
    verb = (observed.get("verb") or "").strip().lower()
    value = observed.get("value", "") or ""
    ve = f"'{js_str(value)}'"
    if verb == "navigate":
        return ""  # navigation handled separately
    if verb == "click":
        return "await loc.first().click({ timeout: 3000 }).catch(() => {});"
    if kind == "select":
        return f"await loc.first().selectOption({ve}, {{ timeout: 3000 }}).catch(() => {{}});"
    if kind in ("radio", "checkbox"):
        return "await loc.first().check({ timeout: 3000 }).catch(() => {});"
    if kind == "toggle":
        return "await loc.first().click({ timeout: 3000 }).catch(() => {});"
    # text / date
    return f"await loc.first().fill({ve}, {{ timeout: 3000 }}).catch(() => {{}});"


def _probe_name(observed: dict, kind: str) -> str:
    """The accessible name to look for in the live DOM when the compiled locator
    misses — value for option-like controls (radio/checkbox/toggle), label otherwise."""
    if kind in ("radio", "checkbox", "toggle"):
        return observed.get("value") or observed.get("label") or ""
    return observed.get("label") or observed.get("value") or ""


def _kind_role(kind: str) -> str:
    """The ARIA role the recorded kind should resolve to — lets the DOM probe tell a
    true control-KIND mismatch (text recorded, combobox live) from a same-kind miss."""
    return {
        "text": "textbox", "date": "textbox", "select": "combobox",
        "radio": "radio", "checkbox": "checkbox", "toggle": "checkbox",
        "button": "button", "link": "link",
    }.get(kind, "textbox")


def _expected_path(url: str) -> str:
    """Discriminating page identity = pathname + hash route (top-level query stripped), so
    hash routers keep their route and a query never collapses the path to '/'. Host-less safe
    (delegates the scheme/host strip to the frozen url_path)."""
    if not url:
        return ""
    base = url_path(url) or ""
    hb = base.find("#")
    path_only = base[:hb] if hb >= 0 else base
    frag = ""
    rhi = url.find("#")
    if rhi >= 0:
        frag = url[rhi:]
        fq = frag.find("?")
        if fq >= 0:
            frag = frag[:fq]
    return (path_only or "/") + (frag or "")


def _page_gate_js(url_expr: str, path: str) -> str:
    """JS boolean expr: is `url_expr` on the recorded page? SEGMENT match on pathname+hash,
    case-insensitive + trailing-slash-normalized (mirrors the frozen compiler's js_regex_literal
    URL oracle) — NEVER a raw substring. '/' uses an exact-root match (a segment regex can't
    discriminate '/'); '' is non-discriminating so cannot gate (returns true)."""
    p = path or ""
    if p == "":
        return "true"
    if p.rstrip("/") == "":
        return ("(() => { try { const u = new URL(" + url_expr + "); "
                "return (u.pathname.replace(/\\/+$/, '') || '/') === '/'; } catch (e) { return false; } })()")
    return "_seg(" + js_regex_literal(p.lower().rstrip("/")) + ", " + url_expr + ")"


def build_preflight_spec(tc, field_meta: dict | None = None, *, base_url: str = "") -> str:
    field_meta = field_meta or {}
    steps = list(getattr(tc, "steps", None) or [])
    name = (getattr(tc, "name", None) or "preflight").strip()

    out: list[str] = [
        "// NEXUS LIVE PREFLIGHT — deterministic, app-agnostic locator-resolution probe (no LLM).",
        "// Classifies each COMPILED locator vs the LIVE app into ONE honest status. Every probe",
        "// beyond the compiled locator may only DOWNGRADE a non-resolved step — 'resolved' requires",
        "// exactly one VISIBLE + ACTIONABLE match, and mirrors what the frozen compiler actually does",
        "// (strict mode = no .first() except toggle, segment URL oracle, option/value oracle,",
        "// isEnabled actionability). Nothing here can move a step TO resolved → no green-wash.",
        "import { test } from '@playwright/test';",
        "",
        f"test({json.dumps('preflight: ' + name)}, async ({{ page }}) => {{",
        "  const R = (o: any) => console.log(" + json.dumps(_MARK) + " + JSON.stringify(o));",
        # Page-membership: SEGMENT match on pathname+hash (mirrors the compiler's js_regex_literal
        # URL oracle), case-insensitive + trailing-slash-normalized. Never a raw substring.
        "  const _seg = (re: RegExp, url: string) => { try { const u = new URL(url); let p = (u.pathname + u.hash).toLowerCase(); if (p.length > 1) p = p.replace(/\\/+$/, ''); return re.test(p); } catch (e) { return false; } };",
        # Generic, kind-aware, EXACT-match accessible-name probe. Used ONLY to downgrade a
        # non-resolved step: same-kind-but-hidden -> hidden; different-kind same name -> kind-mismatch;
        # else absent. Mirrors Playwright's accessible-name sources (incl. aria-labelledby/title/alt).
        "  const __present = (nm: string, want: string) => page.evaluate(([name, wantRole]: [string, string]) => {",
        "    const norm = (s: string) => (s || '').replace(/\\s+/g, ' ').trim().toLowerCase();",
        "    const t = norm(name); if (!t) return { hiddenPresent: false, otherRole: null as any };",
        "    const vis = (el: Element) => { try { const r = (el as HTMLElement).getClientRects(); const st = getComputedStyle(el); return r.length > 0 && st.visibility !== 'hidden' && st.display !== 'none'; } catch (e) { return false; } };",
        "    const kindOf = (el: Element) => { const role = (el.getAttribute('role') || '').toLowerCase(); if (role) return role; const tag = el.tagName.toLowerCase(); const ty = (el.getAttribute('type') || '').toLowerCase(); if (tag === 'select') return 'combobox'; if (tag === 'textarea') return 'textbox'; if (tag === 'a') return 'link'; if (tag === 'button') return 'button'; if (tag === 'input') { if (ty === 'checkbox') return 'checkbox'; if (ty === 'radio') return 'radio'; if (ty === 'button' || ty === 'submit' || ty === 'reset') return 'button'; return 'textbox'; } return tag; };",
        "    const accName = (el: Element) => { let n = el.getAttribute('aria-label') || ''; if (!n) { const lb = el.getAttribute('aria-labelledby'); if (lb) { n = lb.split(/\\s+/).map((id) => { const r = document.getElementById(id); return r ? (r.textContent || '') : ''; }).join(' '); } } if (!n) n = el.getAttribute('placeholder') || ''; if (!n) n = el.getAttribute('title') || ''; if (!n) n = el.getAttribute('alt') || ''; if (!n && (el as HTMLElement).id) { try { const l = document.querySelector('label[for=\"' + ((window as any).CSS && CSS.escape ? CSS.escape((el as HTMLElement).id) : (el as HTMLElement).id) + '\"]'); if (l) n = l.textContent || ''; } catch (e) {} } if (!n) { const w = el.closest('label'); if (w) n = w.textContent || ''; } return n; };",
        "    const sel = 'input,select,textarea,button,[role],a[href],[contenteditable=\"true\"]';",
        "    let hiddenPresent = false; let otherRole: any = null;",
        "    for (const el of Array.from(document.querySelectorAll(sel))) {",
        "      const nm2 = norm(accName(el)); const txt = norm(el.textContent || '');",
        # SUBSTRING presence mirrors getByLabel/getByRole default name matching (punctuation/
        # whitespace-tolerant). EXACT is required only to call a cross-KIND mismatch, so a short
        # name can never mis-steer heal onto an unrelated different-kind element (M6 + M7).
        "      const sub = nm2.includes(t) || txt.includes(t); if (!sub) continue;",
        "      const exact = nm2 === t || txt === t;",
        "      const k = kindOf(el);",
        "      if (k === wantRole) { if (!vis(el)) hiddenPresent = true; }",  # same kind, hidden -> reveal needed
        "      else if (!otherRole && exact) { otherRole = k; }",            # different kind (exact) -> kind-mismatch
        "      if (hiddenPresent && otherRole) break;",                       # N7: stop once both known
        "    }",
        "    return { hiddenPresent, otherRole };",
        "  }, [nm, want]);",
        "  let total = 0, resolved = 0, ambiguous = 0, strictAmbiguous = 0, valueMismatch = 0,",
        "      disabledN = 0, blocked = 0, hidden = 0, kindMismatch = 0, absent = 0;",
    ]

    # Track the page we EXPECT to be on (set by each recorded navigate). A control on a page we
    # never reached is reported 'blocked' (indeterminate), NOT 'broken/renamed'.
    expected_path = ""
    for step in steps:
        n = getattr(step, "step_number", None)
        observed = _observed(step)
        verb = (observed.get("verb") or "").strip().lower()
        action = (getattr(step, "action", "") or "").strip()

        if verb == "navigate":
            url = observed.get("url", "") or ""
            ep = _expected_path(url)
            target = url_path(url) if base_url else url
            if action.startswith("Open ") and target:
                out.append(f"  await page.goto({json.dumps(target)}, {{ waitUntil: 'domcontentloaded' }}).catch(() => {{}});")
                if ep:
                    expected_path = ep
            elif url:
                # recorded page transition: report whether we got there (segment match), set expected.
                out.append("  {")
                out.append(f"    const ok = {_page_gate_js('page.url()', ep)};")
                out.append(f"    R({{ step: {json.dumps(n)}, kind: 'nav', target: {json.dumps(ep)}, reached: ok }});")
                out.append("  }")
                expected_path = ep
            continue

        loc_js, kind = _probe_locator(observed, field_meta)
        label = observed.get("label", "") or ""
        probe_name = _probe_name(observed, kind)
        want_role = _kind_role(kind)
        strict = kind != "toggle"   # compiler drives every kind strict (no .first()) except toggle
        is_fill = kind in ("text", "date")
        tok = _value_oracle(observed.get("value", "") or "") if kind == "select" else ""

        out.append("  {")
        out.append(f"    const loc = {loc_js};")
        out.append(f"    const onPage = {_page_gate_js('page.url()', expected_path)};")
        out.append(f"    const STRICT = {'true' if strict else 'false'};")
        out.append("    let dom = -1; let vis = 0; let status = 'absent'; let foundRole: any = null;")
        out.append("    let optOk: any = null; let actionable: any = null;")
        out.append("    if (!onPage) { status = 'blocked'; }")
        out.append("    else {")
        out.append("      try { dom = await loc.count(); } catch (e) {}")
        # N7: cap the visibility scan — only the 0/1/>1 boundary matters (and dom>200 is never 'resolved').
        out.append("      if (dom > 0) { const _N = Math.min(dom, 200); for (let i = 0; i < _N && vis < 2; i++) { try { if (await loc.nth(i).isVisible()) vis++; } catch (e) {} } }")
        # M5: strict kinds with >1 DOM match are a strict-mode RED in the compiled run, regardless of visibility.
        out.append("      if (STRICT && dom > 1) { status = 'strict-ambiguous'; }")
        out.append("      else if (vis === 1) {")
        out.append("        try { actionable = await loc.first().isEnabled(); } catch (e) { actionable = null; }")
        if is_fill:
            out.append("        if (actionable !== false) { try { actionable = await loc.first().isEditable(); } catch (e) {} }")
        if tok:
            out.append(f"        try {{ optOk = (await loc.first().locator('option', {{ hasText: /{tok}/i }}).count()) > 0; }} catch (e) {{}}")
            # M4 disabled/read-only then M3 stale option — both are compiled-run REDs; only an actionable,
            # value-present control is 'resolved'.
            out.append("        if (actionable === false) { status = 'disabled'; } else if (optOk === false) { status = 'value-mismatch'; } else { status = 'resolved'; }")
        else:
            out.append("        if (actionable === false) { status = 'disabled'; } else { status = 'resolved'; }")
        out.append("      }")
        out.append("      else if (vis > 1) { status = 'ambiguous'; }")
        out.append("      else {")
        # M8: guard the evaluate — a navigation race ('Execution context destroyed') becomes 'blocked'
        # (indeterminate), never a misclassification, and the summary line always emits.
        out.append(f"        let pr: any = {{ hiddenPresent: false, otherRole: null }};")
        out.append(f"        try {{ pr = await __present({json.dumps(probe_name)}, {json.dumps(want_role)}); }} catch (e) {{ status = 'blocked'; }}")
        out.append("        if (status !== 'blocked') { if (dom > 0 || pr.hiddenPresent) { status = 'hidden'; } else if (pr.otherRole) { status = 'kind-mismatch'; foundRole = pr.otherRole; } else { status = 'absent'; } }")
        out.append("      }")
        out.append("    }")
        out.append("    total++;")
        out.append("    if (status === 'resolved') resolved++; else if (status === 'ambiguous') ambiguous++; else if (status === 'strict-ambiguous') strictAmbiguous++; else if (status === 'value-mismatch') valueMismatch++; else if (status === 'disabled') disabledN++; else if (status === 'blocked') blocked++; else if (status === 'hidden') hidden++; else if (status === 'kind-mismatch') kindMismatch++; else absent++;")
        out.append(f"    R({{ step: {json.dumps(n)}, label: {json.dumps(label)}, kind: {json.dumps(kind)}, count: vis, domCount: dom, onPage, status, foundRole, optionPresent: optOk, actionable }});")
        adv = _soft_advance(observed, kind)
        if adv:
            # Drive the recorded action ONLY when the run could: a resolved control, or a toggle
            # (which the compiler itself drives with .first()). Never .first()-advance a strict kind
            # on an ambiguous/absent match — the real run can't, so neither do we. Settle after, so a
            # navigation triggered here doesn't race the next step's probe (pairs with M8's guard).
            cond = "status === 'resolved'" + ("" if strict else " || vis >= 1")
            out.append(f"    if ({cond}) {{ {adv} await page.waitForLoadState('domcontentloaded', {{ timeout: 5000 }}).catch(() => {{}}); }}")
        out.append("  }")

    out.append("  R({ summary: true, total, resolved, ambiguous, strictAmbiguous, valueMismatch, disabled: disabledN, blocked, hidden, kindMismatch, absent });")
    out.append("});")
    out.append("")
    return "\n".join(out)


def build_preflight_files(tc, field_meta: dict | None = None, *, base_url: str = "",
                          storage_state: str | None = None) -> dict:
    files = {
        "tests/preflight.spec.ts": build_preflight_spec(tc, field_meta, base_url=base_url),
        "playwright.config.ts": _config(base_url),
    }
    # Reuse the artifact's captured login session (already decrypted upstream) so the
    # probe sees authenticated pages — identical mechanism to a real run's
    # nexus.auth.json, which the config above auto-detects. Absent -> unauthenticated.
    if storage_state and storage_state.strip():
        files["nexus.auth.json"] = storage_state
    return files


def _step_status(s: dict) -> str:
    """The honest status of a control step. Prefer the engine-emitted `status`; fall
    back to count-based inference for any older/partial line (keeps the parser robust)."""
    st = s.get("status")
    if st:
        return st
    c = s.get("count")
    if c == 1:
        return "resolved"
    if isinstance(c, int) and c > 1:
        return "ambiguous"
    return "absent"


def parse_preflight_output(output: str) -> dict:
    """Extract the NEXUS_PREFLIGHT JSON lines from runner stdout into an honest report.

    Buckets (mutually exclusive, per control step):
      resolved        — exactly one VISIBLE + ACTIONABLE match of the compiled locator.
      ambiguous       — >1 visible match for a kind the run drives leniently (toggle).
      strict_ambiguous— >1 DOM match for a kind the run drives strictly (no .first()):
                        a guaranteed Playwright strict-mode RED → tighten locator.
      value_mismatch  — the <select>'s recorded option/value no longer exists; the
                        locator resolves but the run's value oracle goes RED.
      disabled        — exactly one match but disabled/read-only: not actionable.
      hidden          — a same-kind control exists but is not visible (behind a
                        collapsed/conditional section) → needs a reveal step.
      kind_mismatch   — a control with this name exists under a DIFFERENT kind/role
                        (e.g. recorded `text`, live is a combobox) → heal / Add-select.
      absent          — genuinely not present (truly broken / renamed) → re-anchor.
      blocked         — the step's page was never reached / a navigation race made it
                        INDETERMINATE. Not a locator failure.
    `broken` (back-compat) = the definite compiled-run REDs: absent + kind_mismatch +
    strict_ambiguous + value_mismatch. `hidden`, `disabled`, and `blocked` are NOT
    counted as broken — that would falsely indict reachable/transient controls. Nothing
    is ever counted as resolved that the compiled run would not actually pass."""
    steps: list[dict] = []
    summary: dict = {}
    for line in (output or "").splitlines():
        i = line.find(_MARK)
        if i < 0:
            continue
        try:
            obj = json.loads(line[i + len(_MARK):].strip())
        except Exception:
            continue
        if obj.get("summary"):
            summary = obj
        else:
            steps.append(obj)

    control = [s for s in steps if s.get("kind") != "nav" and ("status" in s or "count" in s)]
    keys = ("resolved", "ambiguous", "strict_ambiguous", "value_mismatch", "disabled",
            "hidden", "kind_mismatch", "absent", "blocked")
    by: dict[str, list[dict]] = {k: [] for k in keys}
    _norm = {"strict-ambiguous": "strict_ambiguous", "value-mismatch": "value_mismatch",
             "kind-mismatch": "kind_mismatch"}
    for s in control:
        st = _step_status(s)
        by.get(_norm.get(st, st), by["absent"]).append(s)

    probed = len(control)
    resolved = len(by["resolved"])
    # Resolvability is measured over steps we could actually reach (excludes 'blocked').
    reachable = probed - len(by["blocked"])
    return {
        "steps": steps,
        "summary": summary,
        "probed": probed,
        "reachable": reachable,
        "resolved": resolved,
        "resolve_pct": round(100.0 * resolved / probed) if probed else 0,
        "resolve_pct_reachable": round(100.0 * resolved / reachable) if reachable else 0,
        "resolved_steps": by["resolved"],
        "ambiguous": by["ambiguous"],
        "strict_ambiguous": by["strict_ambiguous"],
        "value_mismatch": by["value_mismatch"],
        "disabled": by["disabled"],
        "hidden": by["hidden"],
        "kind_mismatch": by["kind_mismatch"],
        "absent": by["absent"],
        "blocked": by["blocked"],
        # back-compat: the definite compiled-run REDs (NOT hidden / disabled / blocked).
        "broken": by["absent"] + by["kind_mismatch"] + by["strict_ambiguous"] + by["value_mismatch"],
        "nav": [s for s in steps if s.get("kind") == "nav"],
    }
