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
import os
import re
from typing import Iterable
from urllib.parse import urlparse

from .locators import js_regex_literal, js_str, url_path
# Control-KIND / interaction heal recipes live OUTSIDE this frozen file; the
# `interactions` channel below early-returns into them. Cycle-safe: the resolver
# only imports compiler helpers lazily (inside its emitters).
from .interaction_resolver import INTERACTION_RECIPES

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
    if observed.get("kind_locked") and base:
        # a re-anchor bound this step to the REAL live control and named its kind —
        # live evidence outranks every accumulated override below.
        return base
    fm = field_meta.get(_norm(observed.get("label", ""))) or {}
    control = fm.get("control", "")
    options = fm.get("options") or []
    # An EXPLICIT chooser override (auto-heal's select_content_fallback, or a human edit
    # that re-points the control) is AUTHORITATIVE and MUST beat a mis-captured
    # observed.kind=="toggle". Without this, a <select> wrongly recorded as a toggle could
    # NEVER be corrected — the toggle short-circuit below would discard the override before
    # it is ever read. Byte-identical for every non-overridden case: a real toggle has no
    # 'select' control in field_meta, so it still falls through to the toggle return below.
    if control in ("select", "dropdown", "combobox"):
        return "select"
    if base == "toggle":
        return "toggle"
    if control == "checkbox":
        return "checkbox"
    if control in ("radio", "segmented"):
        return "radio"
    # A boolean value on a NON-select control is a checkbox/toggle, never a
    # <select> -- keeps a true/false field off the selectOption() path even when
    # its captured options are ["true","false"] (options>=2 would mis-route it).
    _bv = (observed.get("value", "") or "").strip().lower()
    if _bv in ("true", "false", "yes", "no", "on", "off") and control not in ("select", "dropdown", "combobox"):
        return "checkbox"
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

# The control's ARIA role for the over-qualified ``block`` scope's ``has`` filter,
# derived from the recorded control kind. Defaults to 'button' (the common case).
_CONTROL_ROLE = {
    "button": "button", "link": "link", "text": "textbox", "date": "textbox",
    "select": "combobox", "toggle": "checkbox", "checkbox": "checkbox", "radio": "radio",
}

# Generic block containers for the ``block`` anchor scope — common semantic + ARIA +
# class-pattern wrappers for a card/list/row/grid cell. No app-specific selector: the
# double filter (hasText + has-control) + ``.last()`` (innermost matching ancestor)
# resolves to the one item that holds BOTH the block text and the control.
_BLOCK_CONTAINERS = (
    "li, tr, article, section, aside, fieldset, "
    "[role=\"row\"], [role=\"listitem\"], [role=\"article\"], [role=\"gridcell\"], "
    "[role=\"group\"], [role=\"region\"], [role=\"option\"], "
    "[class*=\"item\"], [class*=\"card\"], [class*=\"cell\"], [class*=\"row\"], "
    "[class*=\"tile\"], [class*=\"product\"], div"
)


def _anchor_scope(observed: dict) -> str:
    """Locator scope for a repeated control. ``'page'`` when there's no anchor;
    otherwise ``getByRole(<role-from-anchor_kind>, { name: anchor })`` — the role
    comes from the observed anchor_kind (default ``'row'`` for backward compat),
    so card / list / grid / region / dialog layouts disambiguate, not just
    tables. Every rung still targets the SAME accessible name, so this can never
    silently bind to a different control."""
    frame_sel = observed.get("frame_selector") or ""
    if frame_sel:
        # P4 iframe: a frame-scoped re-anchor binds INSIDE the owning iframe.
        # frameLocator exposes the SAME getByRole/getByLabel/... ladder, so this
        # composes with the whole resilient locator chain + the step's own oracle.
        return f"page.frameLocator('{js_str(frame_sel)}')"
    anchor = js_str(observed.get("anchor", ""))
    if not anchor:
        return "page"
    kind = _norm(observed.get("anchor_kind", "")).replace(" ", "-")
    if kind == "block":
        # P6 over-qualified scope: the block anchor is plain TEXT (a product/card name),
        # not a named ARIA landmark, so getByRole(name=anchor) wouldn't find it. Scope to
        # the nearest container holding BOTH the block text AND the control. .last() = the
        # innermost matching ancestor (outer containers open first in document order), i.e.
        # the single card/row. Generic across card/list/row/grid layouts; a wrong scope is
        # still caught by the step's own outcome oracle (fails RED, never green-wash).
        ctrl_role = _CONTROL_ROLE.get(_norm(observed.get("kind", "")), "button")
        ctrl_name = js_str(observed.get("label", ""))
        return (
            f"page.locator('{_BLOCK_CONTAINERS}')"
            f".filter({{ hasText: '{anchor}' }})"
            f".filter({{ has: page.getByRole('{ctrl_role}', {{ name: '{ctrl_name}' }}) }})"
            f".last()"
        )
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
        # P1 content-anchored FALLBACK: a <select> whose recorded VISUAL CAPTION is not its
        # DOM accessible name (e.g. SauceDemo's sort dropdown, labelled only by an icon)
        # matches neither rung above and times out. Bind by OPTION CONTENT — the <select>
        # that actually contains the recorded option. Label/role rungs stay FIRST (a real
        # accessible name always wins); this is the last resort. The step's own committed-
        # value oracle (toHaveValue / option:checked) still independently proves green, so a
        # wrong bind fails RED — never green-wash.
        _optval = js_str(observed.get("value", ""))
        if _optval:
            rungs.append(
                f"locator('select').filter({{ has: {scope}.getByRole('option', "
                f"{{ name: '{_optval}' }}) }})")
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


# Runtime token of a (possibly data-overridden) value for a tolerant, override-safe
# field oracle. Emitted into each parametrized spec; asserts the field/selected
# option HOLDS the entered value without mirroring a fixed token or a brittle exact
# string (survives input masking / option-label normalization). Falls back to a
# non-empty check when the value has no stable token.
_NXTOK_JS = r"""function __nxTok(v){const s=String(v==null?'':v);const m=s.match(/[A-Za-z]{2,}|[0-9]{2,}/);return m?new RegExp(m[0].replace(/[.*+?^${}()|[\]\\]/g,'\\$&'),'i'):/\S/;}"""

_NXSETTLE_JS = r"""async function __nxSettle(page){try{await page.waitForLoadState('domcontentloaded',{timeout:5000});}catch(e){}const sp=page.locator('[class*=spinner i],[class*=loading i],[aria-busy=\"true\"],[role=progressbar]').first();try{if(await sp.isVisible().catch(()=>false)){await sp.waitFor({state:'hidden',timeout:8000});}}catch(e){}}"""

_NXCLICK_JS = r"""async function __nxClick(loc){
  let n; try { n = await loc.count(); } catch (e) { return await loc.click(); }
  if (n <= 1) return await loc.click();
  const sigs = [];
  for (let i = 0; i < n; i++) {
    try {
      sigs.push(await loc.nth(i).evaluate(el => {
        const a = (el.closest && el.closest('a')) || el;
        return ((a.getAttribute && a.getAttribute('href')) || '') + '||' +
               ((el.getAttribute && el.getAttribute('onclick')) || '');
      }));
    } catch (e) { sigs.push('\u0000' + i); }
  }
  const disc = (sigs[0] || '').replace(/\|\|$/, '');
  if (disc.length > 0 && sigs.every(s => s === sigs[0]))
    return await loc.first().click(); // duplicate control (e.g. header+footer nav share one href) -> first is provably equivalent
  return await loc.click(); // genuinely ambiguous -> Playwright's honest strict-mode error stands (heal/human)
}"""



_ER_STOPWORDS = frozenset("""
the a an and or of to in on at is are be was were should shall will would must may can
that this these those with for from by as it its their his her your our then than
when after before page user users system field fields value values text button buttons
link links form forms display displayed displays shows shown show appear appears appearing
visible see seen successfully success correct correctly able message messages please into
out new view click clicked select selected enter entered submit submitted screen above below
""".split())


def _grounded_expectation_token(expected_result: str, grounded_text: str) -> str:
    """Pick a regex-safe token that the Expected Result names AND that appears in the
    OBSERVED OUTCOME text — so the compiled visibility oracle is grounded in real
    rendered page text (getByText-safe), not in un-observed prose or a field value
    (which lives in an <input> / closed <option> and would false-RED). Returns ''
    when nothing the Expected Result names was actually observed (caller emits an
    honest UNVERIFIED comment instead of a brittle assertion)."""
    g = (grounded_text or "").lower()
    if not g.strip():
        return ""
    for raw in re.findall(r"[A-Za-z][A-Za-z0-9]{3,}", expected_result or ""):
        w = raw.lower()
        if w in _ER_STOPWORDS:
            continue
        if w in g:
            return re.escape(raw)
    return ""


def _assertion_from_expected_result(observed: dict, expected_result: str = "",
                                    *, nav_proven: bool = True) -> list[str]:
    """Compile GROUNDED, tolerant assertions from a step's observed outcome.

    Closes the two oracles the compiler previously left as comments:
      * NAVIGATION — a SUBMIT/click step carries the RECORDED next-page URL in
        ``observed['next_url']`` (threaded by the generator); assert the PATH was
        reached. Path-only + regex-contains → tolerant of host/query, never a
        full-URL mirror, and grounded in the recorded next page (not LLM prose).
      * OUTCOME REGION — when the observed ``after`` names a results/summary/etc.
        region, assert that region is visible.
    Reads ONLY recorded fields (next_url / after), so a step with no grounded
    outcome emits NOTHING — never a fake green, never a brittle red.

    ``nav_proven`` (PROVEN-only nav oracle): when False, the recorded next_url is an
    UNPROVEN navigation (the recording did NOT show THIS action cause it — an
    inferred/review step whose next_url is often MIS-ATTRIBUTED). We keep the action
    but downgrade the URL check to a greppable soft observation rather than a HARD
    toHaveURL, so a mis-attributed navigation is not a false RED. A PROVEN nav
    (default True) stays a HARD oracle, so real regressions still fail RED — never
    green-wash. Mirrors the navigate-verb handler's existing inferred-provenance path."""
    out: list[str] = []
    next_url = (observed.get("next_url") or "").strip()
    if next_url:
        path = url_path(next_url)
        if path and path != "/":
            if nav_proven:
                out.append(
                    f"await expect(page).toHaveURL({js_regex_literal(path)}, "
                    "{ timeout: 30000 }); // grounded: navigated to the recorded next page"
                )
            else:
                out.append(
                    f"// UNPROVEN transition: the recording did not show this action caused a "
                    f"navigation to {path} — NOT hard-asserted (inferred/review nav). The action "
                    f"still ran; a PROVEN navigation would be a hard toHaveURL oracle."
                )
    # PROVEN-only outcome oracle (gated NEXUS_PROVEN_NAV_ORACLE): a getByText derived from
    # the LLM's PROSE outcome description (`after` / expected-result) is NOT a grounded oracle
    # — the prose word may not be literal page text (e.g. 'product listing' → getByText(/listing/)
    # which SauceDemo's inventory never renders). Under the policy, emit it as a NON-FAILING hint
    # (.catch) so a fabricated description never false-reds a step whose real action + navigation
    # already passed (those stay the hard oracles). Off => byte-identical (hard assertion).
    _soft_outcome = os.getenv("NEXUS_PROVEN_NAV_ORACLE") == "1"
    _oc_tail = (".catch(() => {}); // best-effort: LLM-prose outcome hint (non-failing under the "
                "proven-only policy; the action + navigation are the hard oracles)"
                if _soft_outcome else "; // grounded: observed outcome region is shown")
    after = (observed.get("after") or "").strip()
    if after:
        m = _OUTCOME_REGION_RX.search(after)
        if m:
            out.append(
                f"await expect(page.getByText(/{m.group(1).lower()}/i).first())"
                f".toBeVisible(){_oc_tail}"
            )
    # GROUNDED step Expected Result (additive): assert a token the Expected
    # Result names AND that appears in the OBSERVED OUTCOME text (real rendered
    # page text -> getByText-safe). A field value is NEVER used as the ground
    # (it lives in an <input>/closed <option>); the field's own value oracle
    # already guards that. Un-groundable prose -> honest UNVERIFIED comment.
    er = (expected_result or "").strip()
    if er:
        already = " ".join(out).lower()
        tok = _grounded_expectation_token(er, after)
        if tok and tok.lower() not in already:
            _er_tail = (".catch(() => {}); // best-effort: LLM-prose Expected-Result hint "
                        "(non-failing under the proven-only policy)"
                        if _soft_outcome
                        else "; // grounded: step Expected Result, verified against the observed outcome")
            out.append(
                f"await expect(page.getByText(/{tok}/i).first()).toBeVisible(){_er_tail}"
            )
        elif not tok and "await expect(" not in already:
            out.append(f"// UNVERIFIED expected result (no grounded oracle for this step): {er[:120]}")
    return out


def _to_iso_date(value: str) -> str:
    """Best-effort, deterministic date -> ISO (YYYY-MM-DD) for native <input
    type=date>, which rejects the displayed MM/DD/YYYY. Returns "" if unparseable
    (caller keeps the raw value). US MM/DD default; DD/MM only when day>12."""
    import re as _re
    v = (value or '').strip()
    if not v:
        return ''
    m = _re.match(r'^(\d{4})-(\d{1,2})-(\d{1,2})$', v)
    if m:
        return '%04d-%02d-%02d' % (int(m.group(1)), int(m.group(2)), int(m.group(3)))
    m = _re.match(r'^(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{4})$', v)
    if m:
        a, b, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        mm, dd = (b, a) if (a > 12 and b <= 12) else (a, b)
        if 1 <= mm <= 12 and 1 <= dd <= 31:
            return '%04d-%02d-%02d' % (y, mm, dd)
    return ''


def _emit_wizard_advance(step, max_pages: int) -> list[str]:
    """WIZARD-ADVANCE heal preamble: the recorded control is absent on this page —
    the recording reached it after advancing a wizard (a navigation the extraction
    dropped). Click the page's own PROGRESSION control (curated, non-destructive
    labels only) until the step's recorded label appears. Self-guarded: if the label
    is already present (main frame OR an iframe) NOTHING is clicked, so the channel
    is inert on a correct page. Bounded (max_pages); the step's own action + oracle
    below still decide — a wrong advance just fails RED. Never green-wash."""
    observed = _observed(step)
    label = (observed.get("label") or observed.get("value") or "").strip()
    if not label:
        return []
    lab = js_str(label)
    out: list[str] = []
    out.append("// WIZARD-ADVANCE heal: recorded control absent on this page -> advance the")
    out.append("// wizard via its own progression control until the label appears (bounded,")
    out.append("// self-guarded, non-destructive; the step's action + oracle still decide).")
    out.append(f"for (let _adv = 0; _adv < {int(max_pages)}; _adv++) {{")
    # NOTE: frameLocator is ILLEGAL inside a composite locator (Playwright throws
    # at query time) — probing it in the same .or() chain poisons the whole count
    # and the .catch(0) masks the throw as "absent" => the advance overshoots the
    # target page. Probe the main frame and the iframe SEPARATELY.
    out.append(f"  let _here = await page.getByLabel('{lab}')"
               f".or(page.getByText('{lab}', {{ exact: false }})).count().catch(() => 0);")
    out.append(f"  if (!_here) {{ _here = await page.frameLocator('iframe')"
               f".getByLabel('{lab}').count().catch(() => 0); }}")
    out.append("  if (_here) break; // control present -> no advance")
    out.append("  const _nextBtn = page.getByRole('button', "
               "{ name: /^(next|continue|proceed|save (and|&) continue)( .{0,3})?$/i }).first();")
    out.append("  if (!(await _nextBtn.count().catch(() => 0))) break; // no progression control -> stop honestly")
    out.append("  await _nextBtn.click({ timeout: 6000 });")
    out.append("  await page.waitForLoadState('domcontentloaded').catch(() => {});")
    out.append("  await page.waitForLoadState('networkidle', { timeout: 2000 }).catch(() => {}); // bounded condition-based settle — the label probe above is the real synchronizer")
    out.append("}")
    return out


def _action_lines(step, field_meta: dict, parametrize: bool = False,
                  reanchor: dict | None = None, visual: dict | None = None,
                  interaction: dict | None = None, nav_override: str = "",
                  nav_recover: bool = False,
                  autonomous_resolve: bool = False) -> list[str]:
    """Kind-aware Playwright lines for one (observed) step.

    When `parametrize` is set, navigation targets a path resolved against
    use.baseURL and data values become `D['key'] ?? 'observed'` — defaults stay
    the observed values, so a plain run is unchanged.

    `reanchor` (TrueFix selector re-anchor) is an optional
    `{name, kind?}` override: when present, this step is re-bound to the renamed
    control's new accessible `name` (the resilient ladder + the step's own
    visibility oracle then key off the new name). Default None → byte-identical."""
    observed = _observed(step)
    if visual and visual.get("x") is not None and visual.get("y") is not None:
        # P5-full: VLM-located canvas / no-DOM control — coordinate actuation (opt-in,
        # oracle-gated). The click alone proves nothing; safety is the loop's prove-green
        # gate (the WHOLE scenario must re-run green — a wrong coordinate breaks the flow
        # downstream and a later step fails RED) + the hollow-suite refusal. Never green-wash.
        return [f"await page.mouse.click({int(visual['x'])}, {int(visual['y'])}); "
                "// P5 visual: VLM-located canvas control (opt-in; gated by the prove-green re-run)"]
    if reanchor and reanchor.get("name"):
        observed = {**observed, "label": reanchor["name"]}
        if reanchor.get("kind"):
            observed["kind"] = reanchor["kind"]
            # LIVE-EVIDENCE LOCK: the re-anchor matched the REAL control on the live
            # page (similo / the agentic analyst), so its kind outranks any
            # accumulated field_meta guess (e.g. a stale select-content fallback) —
            # without this a rebound RADIO still compiles as selectOption.
            observed["kind_locked"] = True
        if reanchor.get("frame_selector"):
            observed["frame_selector"] = reanchor["frame_selector"]
        # P6 over-qualified disambiguation: the re-anchor split an over-qualified name
        # into the control (now `label`) + a disambiguating block `anchor`. Thread it so
        # `_anchor_scope` scopes the locator to the one card/row that holds both. Absent
        # => no anchor => byte-identical to a plain name re-anchor.
        if reanchor.get("anchor"):
            observed["anchor"] = reanchor["anchor"]
            observed["anchor_kind"] = reanchor.get("anchor_kind") or "block"
    verb = (observed.get("verb") or "").strip().lower()
    value = observed.get("value", "") or ""
    url = observed.get("url", "") or ""
    if nav_override:
        # ENTRY-URL NORMALIZATION heal: the recorded (OCR-derived) URL was malformed
        # (apex host / dropped suffix); the heal grounded a corrected URL in the
        # recording's OWN page_visits. Only the navigation target changes — every
        # oracle still runs, so a wrong candidate fails RED (never green-wash).
        url = str(nav_override)
    label = observed.get("label", "") or ""
    action = (getattr(step, "action", "") or "").strip()
    after = (observed.get("after") or "").strip()
    out: list[str] = []

    # CONTROL-KIND / INTERACTION re-synthesis (additive heal channel; default None
    # -> byte-identical). When a heal finds this step's control changed KIND so the
    # recorded recipe is wrong even though the element exists (native <select> ->
    # custom ARIA combobox needing open+pick, range slider, role=switch, accordion,
    # progressively-revealed field), it supplies an `interaction` recipe whose
    # choreography + OWN grounded committed-value oracle REPLACE the verb branches
    # below. Recipes live outside this frozen file (interaction_resolver); every
    # recipe is bounded (~6s) and fails RED on a wrong guess — never green-wash.
    if interaction and interaction.get("kind") in INTERACTION_RECIPES:
        out.extend(INTERACTION_RECIPES[interaction["kind"]](observed, field_meta, parametrize, interaction))
        return out

    # PROVEN-only navigation oracle (gated NEXUS_PROVEN_NAV_ORACLE; default-off →
    # nav_proven always True → byte-identical). When ON, an UNPROVEN step (inferred
    # provenance OR review/confirm confidence — the SAME predicate the load-bearing
    # UNPROVEN-skip uses) has its recorded next_url treated as an UNPROVEN navigation:
    # the action still runs, but its toHaveURL is a soft observation, not a hard oracle
    # (a mis-attributed next_url must not be a false RED). PROVEN navs stay hard.
    _nav_proven = (os.getenv("NEXUS_PROVEN_NAV_ORACLE") != "1") or not (
        _provenance(step) == "inferred" or _confidence(step) in ("review", "confirm"))

    # Boolean checkbox/toggle -- handle uniformly (regardless of whether the
    # step was emitted as type or select) so a true/false value compiles to
    # check()/uncheck() with a toBeChecked oracle, never selectOption('false')
    # (which throws on a real checkbox or mis-passes a label-vs-value assertion).
    _bval = (value or "").strip().lower()
    if (verb in ("type", "select") and _bval in ("true", "false", "yes", "no", "on", "off")
            and _refine_kind(observed, field_meta) == "checkbox"):
        _cb = (f"page.getByLabel('{js_str(label)}')"
               f".or(page.getByRole('checkbox', {{ name: '{js_str(label)}' }}))")
        _on = _bval in ("true", "yes", "on")
        out.append(f"const cb = {_cb}.first();")
        out.append(f"await cb.{'check' if _on else 'uncheck'}();")
        out.append("await expect(cb)." + ("toBeChecked();" if _on else "not.toBeChecked();"))
        out.extend(_assertion_from_expected_result(observed, nav_proven=_nav_proven))
        if after:
            out.append(f"// observed outcome: {after}")
        return out

    if verb == "navigate":
        # NAVIGATION INVARIANT (exec-ready architecture 2026-06-21): page.goto() is
        # emitted for the ENTRY page only ("Open ..."); every later page is reached by
        # REPLAYING the recorded commit click + asserting toHaveURL(path-regex). NEVER
        # emit a mid-flow goto -- it would reload and wipe in-memory-auth / wizard state
        # on a client-routed SPA. Keep this invariant.
        if action.startswith("Open ") and url:
            # A nav_override is an ABSOLUTE corrected URL (often fixing the HOST —
            # apex vs www) — resolving it against use.baseURL would re-strip the fix,
            # so it bypasses _rel_path. Plain compiles are byte-identical.
            target = _rel_path(url) if (parametrize and not nav_override) else url
            out.append(f"await page.goto('{js_str(target)}'); // entry navigation only")
            out.append("await __nxSettle(page); // bounded settle: dcl + visible loading indicator only")
        else:
            path = url_path(url)
            prov = (observed.get("provenance") or "").strip().lower()
            # Downgrade to a soft observation when the transition is UNPROVEN: either the
            # observed provenance is inferred (existing behavior, preserved byte-identical)
            # OR — under the PROVEN-only nav oracle — the STEP itself is inferred/review
            # (_nav_proven False, e.g. a "Verify navigated to X" step the recording never
            # showed was caused by the prior action). A PROVEN transition stays a HARD
            # toHaveURL so a real regression still fails RED — never green-wash.
            if prov == "inferred" or not _nav_proven:
                import re as _re
                _navpath = _re.sub(r"[^A-Za-z0-9._/\-]", "", (path or "").lstrip("/"))
                # basename of the recorded URL path (e.g. 'cart.html' -> 'cart') — a
                # JS-driven nav link (no href) is usually identified by that word in its
                # class / data-test / id / aria-label (saucedemo: <a class="shopping_cart_link"
                # data-test="shopping-cart-link">, href=null). Restricting to LINK controls
                # (<a> / role=link) avoids the action <button>s (add-to-cart etc.).
                _navbase = _re.sub(r"[^A-Za-z0-9\-]", "",
                                   _re.sub(r"\.\w+$", "", _navpath.rstrip("/")).split("/")[-1])
                if os.getenv("NEXUS_NAV_RECOVERY") == "1" and _navpath:
                    # NAV-RECOVERY (autopilot hardening): the recording REACHED this page (a
                    # recorded page-visit) but the extraction DROPPED the navigation action that
                    # caused it (a silent_drop). Best-effort: DRIVE to the recorded page via its
                    # real on-page LINK — grounded (the link's href/class/data-test/aria names the
                    # destination) and FAITHFUL (it is the control the user actually used). NO
                    # assertion here: the DOWNSTREAM step is the oracle, so a wrong/absent link
                    # simply lets the next step fail RED — never green-wash. Recovers the dropped
                    # navigation so the recorded flow proceeds instead of dead-ending.
                    _cands = [f'a[href*=\\"{_navpath}\\"]']
                    if _navbase:
                        for _at in ("data-test", "class", "id", "aria-label"):
                            _cands.append(f'a[{_at}*=\\"{_navbase}\\" i]')
                        _cands.append(f'[role=\\"link\\"][aria-label*=\\"{_navbase}\\" i]')
                    _sel = ", ".join(_cands)
                    # A dropped navigation is often a LINK (cart icon) but sometimes a
                    # primary PROGRESSION BUTTON (Finish/Continue/…). If no link matches, fall
                    # back to a GENERIC advance button (curated, non-destructive labels — never
                    # a Delete/Remove/Cancel) and still verify arrival. Faithful: the recording
                    # DID perform this click; grounded: arrival at the recorded URL is asserted
                    # by waitForURL; never green-wash: no arrival => caught, downstream is oracle.
                    out.append(
                        "try { "
                        f"if (!page.url().includes('{_navpath}')) {{ "
                        f"let __nav = page.locator(\"{_sel}\").first(); "
                        "if (await __nav.count() === 0) { __nav = page.getByRole('button', "
                        "{ name: /^\\s*(finish|continue|next|submit|done|complete|confirm|proceed"
                        "|place order|pay now|pay|checkout|review order|save and continue)( .{0,3})?\\s*$/i })"
                        ".first(); } "
                        "if (await __nav.count() > 0) { await __nav.click(); "
                        f"await page.waitForURL({js_regex_literal(path)}, {{ timeout: 15000 }}); }} "
                        "} } catch (e) { /* best-effort nav-recovery; the downstream step is the oracle */ }"
                    )
                    out.append(
                        f"// nav-recovery ({action}): drove to the recorded page {path} via its "
                        "on-page link (extraction dropped the navigation action). Downstream step "
                        "is the oracle — never green-wash."
                    )
                else:
                    # The page advanced but the action that CAUSED it was not captured.
                    # Stay honest: do NOT assert a transition we cannot prove. Flag it
                    # UNPROVEN (greppable) so a wrong page surfaces here rather than as a
                    # confusing downstream locator timeout. Enrich the recording or edit.
                    out.append(
                        f"// UNPROVEN transition ({action}): the action that advanced the "
                        "page was not captured -- not asserted (no false red/green). Verify "
                        "manually or enrich the recording."
                    )
            elif path:
                if nav_recover:
                    import re as _re
                    _navpath = _re.sub(r"[^A-Za-z0-9._/\-]", "", (path or "").lstrip("/"))
                    _navbase = _re.sub(r"[^A-Za-z0-9\-]", "",
                                       _re.sub(r"\.\w+$", "", _navpath.rstrip("/")).split("/")[-1])
                    _cands = [f'a[href*=\\"{_navpath}\\"]']
                    if _navbase:
                        for _at in ("data-test", "class", "id", "aria-label"):
                            _cands.append(f'a[{_at}*=\\"{_navbase}\\" i]')
                        _cands.append(f'[role=\\"link\\"][aria-label*=\\"{_navbase}\\" i]')
                    _sel = ", ".join(_cands)
                    # NAV-RECOVER on a PROVEN transition (heal channel, loop-applied): the
                    # recording PROVED this page is reached, but the CLICK that causes it was
                    # dropped by extraction — the app is still on the previous page. Perform
                    # the missing user action via the app's own link/progression control and
                    # keep the HARD toHaveURL below UNTOUCHED (the locked oracle): a genuinely
                    # broken navigation makes the recovery ALSO fail to arrive, and the hard
                    # assertion stays RED. Recovering an action never softens an oracle.
                    out.append(
                        "try { "
                        f"if (!page.url().includes('{_navpath}')) {{ "
                        f"let __nav = page.locator(\"{_sel}\").first(); "
                        "if (await __nav.count() === 0) { __nav = page.getByRole('button', "
                        "{ name: /^\\s*(finish|continue|next|submit|done|complete|confirm|proceed"
                        "|place order|pay now|pay|checkout|review order|save and continue)( .{0,3})?\\s*$/i })"
                        ".first(); } "
                        "if (await __nav.count() > 0) { await __nav.click(); "
                        f"await page.waitForURL({js_regex_literal(path)}, {{ timeout: 15000 }}); }} "
                        "} } catch (e) { /* recovery is best-effort; the HARD assertion below decides */ }"
                    )
                out.append(
                    f"await expect(page).toHaveURL({js_regex_literal(path)}, "
                    "{ timeout: 30000 }); // nav verified via the recorded transition"
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
            out.append(f"const sel = {_sel}.first();")
            out.append(f"await sel.selectOption({_val_expr(value, label, parametrize)});")
            if parametrize:
                out.append(f"await expect(sel.locator('option:checked')).toHaveText(__nxTok({_val_expr(value, label, parametrize)})).catch(() => {{}}); // grounded: selected option text holds the entered value (token-tolerant, data-driven)")
            else:
                _seltok = _value_oracle(value)
                if _seltok:
                    # Reconcile the oracle to the SAME dimension selectOption() matched:
                    # the recorded value is what was selected, so the <select>'s committed
                    # value attribute is what we assert. A coded option (value 'CA', text
                    # 'Canada') previously false-RED because we matched by value but asserted
                    # option TEXT; assert .toHaveValue OR option-text so either coded or
                    # display-text recordings pass, and a no-op selection still fails red.
                    out.append(
                        f"await expect(async () => {{ "
                        f"const okV = await sel.evaluate((el, re) => new RegExp(re,'i').test(el.value), {js_str(_seltok)}).catch(() => false); "
                        f"const okT = await sel.locator('option:checked').evaluate((el, re) => new RegExp(re,'i').test(el.textContent||''), {js_str(_seltok)}).catch(() => false); "
                        f"expect(okV || okT).toBeTruthy(); }}).toPass(); "
                        "// tolerant: selected-option matched by value OR text (coded-value safe)"
                    )
                else:
                    out.append("await expect(sel).not.toHaveValue(''); // tolerant: a selection was committed")
        elif kind == "date":
            out.append(f"const dateField = {_ladder(observed, 'date')}.first();")
            # Only ISO-normalize when the captured form signals CONFIRM a native
            # <input type=date> (control=='date'). The kind can also be reached by a
            # loose VALUE regex on a plain text field that accepts the recorded
            # MM/DD/YYYY; ISO-forcing THAT field commits a format the app may reject
            # while the value-token oracle still passes green. Confirmed-date only.
            _fm_control = (field_meta.get(_norm(label)) or {}).get("control", "")
            _iso = _to_iso_date(value) if _fm_control == "date" else ""
            if _iso:
                # Native <input type=date> needs ISO; a text date field takes the
                # recorded format. Try ISO, fall back to the raw value. Deterministic.
                out.append(
                    f"try {{ await dateField.fill('{_iso}'); }} "
                    f"catch {{ await dateField.fill({_val_expr(value, label, parametrize)}); }}"
                )
            else:
                out.append(f"await dateField.fill({_val_expr(value, label, parametrize)});")
            out.append(
                f"// review: '{js_str(label)}' looks like a date control — confirm "
                "the picker accepts this value/format."
            )
            if parametrize:
                out.append(f"await expect(dateField).toHaveValue(__nxTok({_val_expr(value, label, parametrize)})); // grounded: date field holds the entered value (token-tolerant, data-driven)")
            else:
                _dtok = _value_oracle(value)
                if _dtok:
                    # Tolerant: assert a stable token from the date (e.g. the year)
                    # committed — a no-op fill of a date control FAILS, not greens.
                    out.append(f"await expect(dateField).toHaveValue(/{_dtok}/i); // tolerant: grounded date oracle")
                elif (value or "").strip():
                    out.append("await expect(dateField).not.toHaveValue(''); // tolerant: a date value was committed")
        else:  # text (incl. possible autocomplete)
            out.append(f"const field = {_ladder(observed, 'text')}.first();")
            _ve = _val_expr(value, label, parametrize)
            # ADAPTIVE SET: capture can mis-classify a dependent <select> (options not
            # yet populated at capture time) as a text field. Try fill; if the LIVE
            # element is a <select> (fill throws), self-correct to selectOption by label
            # then value. Generic + grounded value; the field's own oracle still guards.
            out.append(
                f"await field.fill({_ve}, {{ timeout: 5000 }}).catch(async () => {{ "
                f"await field.selectOption({{ label: {_ve} }}, {{ timeout: 3000 }}).catch(async () => {{ "
                f"await field.selectOption({_ve}, {{ timeout: 3000 }}).catch(async () => {{ "
                f"await page.getByRole('radio', {{ name: {_ve} }}).first().check({{ timeout: 3000 }}).catch(() => "
                f"page.getByRole('checkbox', {{ name: {_ve} }}).first().check({{ timeout: 3000 }})); }}); }}); }});"
            )
            if parametrize:
                # Value may be overridden at run time — assert the field committed
                # *something* (the fill worked) rather than mirroring a fixed token.
                # #26 never-green-wash: the __nxTok(...).catch() oracle is SWALLOWED
                # (data-driven safe) — ALONE it lets a no-op/wrong fill into a
                # prepopulated or mis-classified field pass green. Add a NON-swallowed
                # floor: a value MUST be committed, and when NO data override is active
                # the field MUST hold the recorded token (a real override falls back to
                # the tolerant swallowed token).
                out.append("await expect(field).not.toHaveValue(''); // grounded: a value WAS committed (no-op fill fails red)")
                _ptok = _value_oracle(value)
                _pkey = _data_key(label)
                if _ptok and _pkey:
                    out.append(
                        f"if (D['{js_str(_pkey)}'] === undefined) "
                        f"await expect(field).toHaveValue(/{_ptok}/i); "
                        "// hard: no override active -> field MUST hold the recorded token"
                    )
                out.append(f"await expect(field).toHaveValue(__nxTok({_val_expr(value, label, parametrize)})).catch(() => {{}}); // grounded: field holds the entered value (token-tolerant, data-driven; adaptive-set safe)")
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
            # EXACT name: Playwright name-matching is substring by default, so
            # 'Employed' would also match 'Self-employed' (strict-mode ambiguity).
            # The name comes from the recording / a live rebind — exact by construction.
            loc = f"page.getByRole('{kind}', {{ name: '{name}', exact: true }})"
            out.append(f"await {loc}.check();")
            out.append(f"await expect({loc}).toBeChecked();")
        elif kind == "toggle":
            # Role UNCONFIRMED. Restrict the locator to INTERACTIVE roles only
            # (switch / checkbox / radio) keyed to the accessible name — never the
            # getByText rung, which could bind STATIC label text and a bare click()
            # then passes with no state proof. After clicking, assert a tolerant
            # checked-state (toBeChecked OR aria-checked='true') so a no-op click on a
            # real toggle fails red; if NO interactive role is present we cannot prove
            # state, so flag the step for REVIEW rather than green-wash it.
            _tname = js_str(label or value)
            _tscope = _anchor_scope(observed)
            _troot = _tscope if _tscope != "page" else "page"
            _tloc = (
                f"{_troot}.getByRole('switch', {{ name: '{_tname}' }})"
                f".or({_troot}.getByRole('checkbox', {{ name: '{_tname}' }}))"
                f".or({_troot}.getByRole('radio', {{ name: '{_tname}' }}))"
            )
            out.append(f"const toggle = {_tloc}.first();")
            out.append(
                "if (await toggle.count().then(c => c > 0).catch(() => false)) {"
            )
            out.append("  await toggle.click();")
            out.append(
                "  await expect(async () => { "
                "const checked = await toggle.evaluate(el => el.getAttribute && (el.getAttribute('aria-checked') === 'true' || ('checked' in el && el.checked))).catch(() => false); "
                "expect(checked).toBeTruthy(); }).toPass(); "
                "// tolerant post-state: a real toggle reports checked (no-op click fails red)"
            )
            out.append("} else {")
            if autonomous_resolve:
                # AUTOPILOT: test.skip() inside a test.step skips the WHOLE test
                # (wiping every already-passed step) — fail THIS step RED instead so
                # the heal loop binds it live (toggle recipe) or stops honestly.
                out.append(
                    f"  throw new Error('UNPROVEN toggle: no interactive role for "
                    f"{_tname} — autopilot binds + proves it live');"
                )
            else:
                out.append(
                    f"  // REVIEW: no interactive toggle role (switch/checkbox/radio) found for "
                    f"'{_tname}' — state cannot be proven; not asserting (no false green)."
                )
                out.append("  test.skip(true, 'UNPROVEN toggle: no interactive role to bind/verify');")
            out.append("}")
        else:
            _sel = _ladder(observed, 'select')
            out.append(f"const sel = {_sel}.first();")
            out.append(f"await sel.selectOption({_val_expr(value, label, parametrize)});")
            if parametrize:
                out.append(f"await expect(sel.locator('option:checked')).toHaveText(__nxTok({_val_expr(value, label, parametrize)})).catch(() => {{}}); // grounded: selected option text holds the entered value (token-tolerant, data-driven)")
            else:
                _seltok = _value_oracle(value)
                if _seltok:
                    # Assert the SELECTED OPTION's visible text, not the <select>'s value
                    # attribute -- coded option values (CA != 'Canada', plan IDs) would mis-fail.
                    out.append(f"await expect(sel.locator('option:checked')).toHaveText(/{_seltok}/i); // tolerant: selected-option text oracle")
                else:
                    out.append("await expect(sel).not.toHaveValue(''); // tolerant: a selection was committed")
    elif verb == "click":
        kind = "link" if (observed.get("kind") or "").strip().lower() == "link" else "button"
        out.append(f"await __nxClick({_ladder(observed, kind)});")
    else:
        out.append(f"// (no executable action derived) {action}")

    # Compile the step's Expected Result into real, grounded assertions
    # (recorded next page + observed outcome region + a grounded visibility
    # oracle from the step's Expected Result text).
    _er = (getattr(step, "expected_result", "") or getattr(step, "expected", "") or "").strip()
    out.extend(_assertion_from_expected_result(observed, _er, nav_proven=_nav_proven))
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
    // P5 visual: count <canvas> elements — a control that resolves to none of the DOM/a11y
    // signals but sits on a canvas is a pixel-drawn control with no DOM handle, so we can
    // DIAGNOSE it honestly (visual interaction needed) rather than mislabel it.
    const canvas = await page.evaluate(() => document.querySelectorAll('canvas').length).catch(() => 0);
    // Auth legibility: a 'control not found' is OFTEN an UNAUTHENTICATED run (an expired/
    // missing session redirected to a login screen) — NOT a renamed/removed control. Capture
    // GENERIC login signals (works on any UI) so the diagnosis can say 'session expired /
    // re-authenticate' instead of the misleading 'locator not found'. A password field is the
    // strongest signal; the URL and a sign-in/expired message corroborate.
    const login_signals = await page.evaluate(() => {
      const lc = (s) => ('' + (s || '')).toLowerCase();
      const url = lc(location.href);
      const txt = lc(document.body ? document.body.innerText : '').slice(0, 4000);
      return {
        password_fields: document.querySelectorAll('input[type=password]').length,
        url: location.href || '',
        url_login: /(login|sign-?in|\\bauth\\b|sso|account\\/login|session)/.test(url),
        text_login: /(sign[\\s-]?in|log[\\s-]?in|logged[\\s-]?in|must be logged|session (?:has )?expired|please (?:sign|log)|re-?authenticate|unauthori[sz]ed)/.test(txt),
      };
    }).catch(() => ({ password_fields: 0, url: '', url_login: false, text_login: false }));
    // P5-full (opt-in visual heal): a base64 screenshot + viewport so a VLM can locate a
    // canvas/no-DOM control. Best-effort; only on the gated heal-capture re-run.
    const shot = await page.screenshot({ fullPage: false }).then((b) => b.toString('base64')).catch(() => '');
    const viewport = page.viewportSize() || null;
    // P4 iframe: child-frame controls are NOT in the main-frame a11y tree and locators
    // don't pierce iframes — DOM-extract each child frame's controls + a stable iframe
    // selector, so a frame-scoped control can be re-anchored to a frameLocator. Best-
    // effort per frame; never fails the run.
    const frames = [];
    for (const fr of page.frames()) {
      if (fr === page.mainFrame()) continue;
      try {
        let selector = '';
        try {
          const fel = await fr.frameElement();
          selector = await fel.evaluate((e) => e.id ? ('iframe#' + e.id)
            : e.name ? ('iframe[name="' + e.name + '"]')
            : e.getAttribute('title') ? ('iframe[title="' + e.getAttribute('title') + '"]')
            : e.getAttribute('src') ? ('iframe[src="' + e.getAttribute('src') + '"]') : 'iframe');
        } catch {}
        const nodes = await fr.evaluate(() => {
          const out = [];
          const sel = 'input,select,textarea,button,a[href],[role]';
          for (const el of Array.from(document.querySelectorAll(sel)).slice(0, 400)) {
            const tag = el.tagName.toLowerCase();
            const role = el.getAttribute('role') || (tag === 'a' ? 'link'
              : tag === 'select' ? 'combobox' : tag === 'button' ? 'button'
              : (tag === 'input' || tag === 'textarea') ? 'textbox' : tag);
            const lbl = (el.labels && el.labels[0] && el.labels[0].textContent) || '';
            const name = ((el.getAttribute('aria-label') || lbl || el.getAttribute('name')
              || el.getAttribute('placeholder') || (el.textContent || '')) + '').trim().slice(0, 100);
            out.push({ role, name, value: ((el.value || '') + '').slice(0, 100),
                       visible_text: name, neighbor_text: '' });
          }
          return out;
        });
        if (nodes && nodes.length) frames.push({ selector, name: fr.name() || '', url: fr.url() || '', nodes });
      } catch {}
    }
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
        frames,
        canvas,
        shot,
        viewport,
        login_signals,
      }),
    });
  } catch { /* capture is best-effort; never fail the run */ }
});"""


def compile_case(tc, field_meta: dict | None = None, *, parametrize: bool = False,
                 reanchors: dict | None = None, heal_capture: bool = False,
                 stabilize: dict | None = None, visual: dict | None = None,
                 interactions: dict | None = None, nav_overrides: dict | None = None,
                 pre_advance: dict | None = None, nav_recovers: dict | None = None,
                 force_open_shadow: bool = False,
                 autonomous_resolve: bool = False, phantom_skips=None) -> str:
    """Compile one ProductionTestCase to a runnable Playwright .spec.ts (string).

    When `parametrize` is set, the spec reads optional env/data overrides
    (use.baseURL + vkpower.data.json) with the observed values as defaults — so it
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
        "// GENERATED by VKPower Script Factory — deterministic, grounded in a real recording.",
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
    # ANSWERS P1 — business-value oracle. A case may carry expected OUTCOMES
    # (attached at generate from the client's answer_key); compile them into
    # grounded assertions. DEFAULT-OFF: a case with no value_assertions leaves
    # _vo_lines empty and _vo_uses_nxnum False → the spec is byte-identical.
    _vo_lines: list[str] = []
    _vo_uses_nxnum = False
    _value_assertions = getattr(tc, "value_assertions", None)
    if _value_assertions:
        from ..test_factory.value_oracle import value_assertion_lines
        _vo_lines, _vo_uses_nxnum = value_assertion_lines(_value_assertions, field_meta=field_meta)
    out.append(f"// Confidence: {high} solid step(s), {review} need review.")
    if weak_a11y:
        out.append(f"// a11y: {weak_a11y} control(s) had no observed accessible name "
                   "(flagged inline) — improving the app's labels makes these reliable.")
    out.append("")
    out.append("import { test, expect } from '@playwright/test';")
    # __nxSettle is CALLED after every goto in all modes — define it at
    # module scope unconditionally (a call without a definition is a runtime
    # ReferenceError the parse-only checks cannot catch).
    out.append(_NXSETTLE_JS)
    out.append(_NXCLICK_JS)
    if _vo_uses_nxnum:  # ANSWERS P1 — numeric value-oracle comparator (gated)
        from ..test_factory.value_oracle import NXNUM_JS
        out.append(NXNUM_JS)
    out.append("")
    test_id = js_str(getattr(tc, "test_id", "") or "")
    if test_id:
        # Carry the test-case id so the VKPower reporter can map a run's failure
        # back to this test's capture-time baseline (grounded triage).
        out.append(
            f"test('{js_str(name)}', "
            f"{{ annotation: [{{ type: 'nexus-test-id', description: '{test_id}' }}] }}, "
            "async ({ page }) => {"
        )
    else:
        out.append(f"test('{js_str(name)}', async ({{ page }}) => {{")

    # Data plumbing is emitted ONLY when the case actually has data-driven
    # fields (a type/select step with a value). A browse-only flow carrying an
    # unused D loader + __nxTok is dead scaffolding — the auditor rightly
    # deducts for it (the parabank 4/10 vs gate-10 inconsistency).
    _has_data_fields = any(
        (str(( (s.get("observed") if isinstance(s, dict) else getattr(s, "observed", None)) or {}).get("verb") or "").lower() in ("type", "select", "fill"))
        and str(((s.get("observed") if isinstance(s, dict) else getattr(s, "observed", None)) or {}).get("value") or "").strip()
        for s in list(getattr(tc, "steps", []) or [])
    )
    parametrize = parametrize and _has_data_fields
    if parametrize:
        # Optional run-time data overrides; defaults are the observed values, so a
        # plain run is unchanged. Each spec reads ITS OWN test's data slot merged
        # over shared _global defaults (precedence: per-test > global > observed).
        # Base URL comes from use.baseURL (playwright.config).
        tid_js = js_str(getattr(tc, "test_id", "") or "")
        out.append(
            "  const D = (() => { try { const __a = require('../../vkpower.data.json'); "
            "return Object.assign({}, __a['_global'] || {}, __a['" + tid_js + "'] || {}); } "
            "catch { return {}; } })();"
        )
        out.append("  " + _NXTOK_JS)

    # ANY-UI heal (closed shadow DOM -> open): when a heal determined a control sits
    # in a CLOSED shadow root, force every root to 'open' BEFORE the app boots
    # (addInitScript runs before the entry goto + persists across SPA navigations),
    # so the normal open-shadow locator path can reach it. Default-off -> byte-identical.
    if force_open_shadow:
        from .any_ui_resolver import emit_open_shadow_preamble  # lazy: avoid import cycle
        for _osln in emit_open_shadow_preamble():
            out.append("  " + _osln)

    consent_emitted = False
    _phantom = phantom_skips or {}
    for step in flow:
        n = getattr(step, "step_number", None)
        action = (getattr(step, "action", "") or "").strip()
        observed = _observed(step)
        verb = (observed.get("verb") or "").strip().lower()

        # PHANTOM-SKIP (heal-loop directive): this step's control was proven ABSENT and the
        # step is an EXACT duplicate of an earlier PASSED step — a fabricated/misplaced
        # generation artifact. Emit a no-op test.step (so the recorded flow CONTINUES to the
        # next step) that asserts NOTHING. NOT test.skip (that aborts the whole test). Never
        # green-wash: we recognize a proven duplicate; we never claim the phantom's action ran.
        # Empty directive => byte-identical (this branch is never taken).
        if n in _phantom:
            out.append(f"  await test.step({json.dumps(f'step {n}: {action} (PHANTOM-SKIP)')}, async () => {{")
            out.append("    // PHANTOM-SKIP: exact duplicate of an earlier PASSED step and its "
                       "control is absent on this page — a fabricated/misplaced generation "
                       "artifact; not executed and nothing asserted (never green-wash).")
            out.append("  });")
            continue

        # Load-bearing honesty: un-observed / review steps STOP the test with an
        # UNPROVEN skip — so no downstream assertion runs across the gap and
        # false-reds. (Never a fake green, never a silent jump.) EXCEPTION = AUTOPILOT:
        # when autonomous_resolve is on, an UNPROVEN step does NOT skip — it EXECUTES its
        # recorded action + the SAME grounded orthogonal oracle a proven step gets (emitted
        # by the normal path below), so the autonomous loop can DRIVE + PROVE it, or have
        # the grounded heal/agentic layer resolve it, or escalate. NEVER green-wash: the
        # recorded-outcome assertion still decides green, not the agent's say-so.
        _is_unproven = _provenance(step) == "inferred" or _confidence(step) == "review"
        _unproven_mode = (os.getenv("NEXUS_UNPROVEN_STEPS", "attempt") or "attempt").strip().lower()
        if _is_unproven and not autonomous_resolve and _unproven_mode == "skip":
            # LEGACY semantics (env NEXUS_UNPROVEN_STEPS=skip): test.skip(true)
            # mid-body ABORTS the remaining test in Playwright, so the first
            # uncorroborated transition stops the whole run.
            out.append(f"  // step {n} — {action}")
            reason = f"UNPROVEN: step {n} not directly observed — {action}"
            out.append(f"  test.skip(true, {json.dumps(reason)});")
            continue
        if _is_unproven and not autonomous_resolve:
            # ATTEMPT (default): the action WAS observed (review confidence) —
            # execute it best-effort with the same emission a proven step gets.
            # The oracle asserts the RECORDED outcome; the annotation surfaces
            # the confidence. The test no longer aborts at the first gap, so
            # every proven fill downstream actually runs. Never green-wash:
            # nothing is asserted that the recording did not show.
            out.append(f"  // UNPROVEN: step {n} — observed at review confidence; executed BEST-EFFORT (attempt mode)")
        elif _is_unproven:
            out.append(f"  // AGSR autopilot: autonomously resolving UNPROVEN step {n} — {action}")

        out.append(f"  await test.step({json.dumps(f'step {n}: {action}')}, async () => {{")
        out.append(f"    // evidence: provenance={_provenance(step) or 'n/a'}, "
                   f"confidence={_confidence(step) or 'n/a'}")
        if _is_unproven and not autonomous_resolve:
            _ann = ("    test.info().annotations.push({ type: 'unproven-attempt', description: "
                    + json.dumps(f"step {n}: observed at review confidence — best-effort execution")
                    + " });")
            out.append(_ann)
        if (stabilize or {}).get(n):
            # P4 flake-wait synthesis: this step timed out on a prior run with the control
            # PRESENT (a timing/async flake — not a locator/kind bug). Settle the page
            # before acting. Best-effort (.catch) — never a fake green; the step's own
            # oracle still independently gates the result.
            out.append("    await page.waitForLoadState('networkidle', { timeout: 8000 })"
                       ".catch(() => {}); // P4 flake-wait: settle before a timing-flaky step")
        _padv = (pre_advance or {}).get(n)
        if _padv:
            for line in _emit_wizard_advance(step, int(_padv)):
                out.append(f"    {line}")
        for line in _action_lines(step, field_meta, parametrize,
                                  reanchor=(reanchors or {}).get(n),
                                  visual=(visual or {}).get(n),
                                  interaction=(interactions or {}).get(n),
                                  nav_override=(nav_overrides or {}).get(n) or "",
                                  nav_recover=bool((nav_recovers or {}).get(n)),
                                  autonomous_resolve=autonomous_resolve):
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

    # Case-level Expected Outcome -> one grounded final assertion (additive).
    # Grounded ONLY in observed OUTCOME text across the flow (getByText-safe).
    if _expected_outcome:
        _flow_after = " ".join(str(_observed(s2).get("after", "") or "") for s2 in flow)
        _ctok = _grounded_expectation_token(_expected_outcome, _flow_after)
        if _ctok:
            out.append(
                f"  await expect(page.getByText(/{_ctok}/i).first()).toBeVisible(); "
                "// grounded: case Expected Outcome verified against the observed outcome"
            )
    # ANSWERS P1 — grounded business-value assertions (client's expected outcomes
    # vs the live DOM). PROVEN on a real value miss (frozen reducer recognizes the
    # hard toContainText / "unexpected value" throw); UNVERIFIED comment when a node
    # cannot be grounded — never a silent pass.
    if _vo_lines:
        out.append("  // ── business-value oracle (ANSWERS P1) — proves the app's OUTPUT ──")
        out.extend(_vo_lines)
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
  // Config-driven login (DEFAULT OFF): no-op unless vkpower.auth.config.json strategy is 'form'.
  globalSetup: './vkpower.auth.setup.ts',
  fullyParallel: true,
  forbidOnly: true,
  retries: Number(process.env.PLAYWRIGHT_RETRIES ?? (process.env.CI ? '1' : '0')),
  // Per-test ceiling so one hanging spec fails alone instead of letting the
  // outer run timeout SIGKILL (and red-flag) the whole batch.
  timeout: 60_000,
  expect: { timeout: 15_000 },
  reporter: [['list'], ['html', { open: 'never' }], ['junit', { outputFile: 'results/junit.xml' }], ['./vkpower-reporter.ts']],
  use: {
    trace: 'on-first-retry',
    screenshot: 'only-on-failure',
    // Reuse a captured authenticated session when VKPower injects one (auth profile
    // → vkpower.auth.json in the run dir). Self-detecting, so a downloaded bundle
    // (no auth file) is unaffected; a normal unauthenticated run is unchanged.
    ...((() => { try { return require('fs').existsSync('./vkpower.auth.json') ? { storageState: './vkpower.auth.json' } : {}; } catch { return {}; } })()),
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
  // Config-driven login (DEFAULT OFF): vkpower.auth.setup.ts is a no-op unless
  // vkpower.auth.config.json sets strategy 'form' + credentials are in env.
  globalSetup: './vkpower.auth.setup.ts',
  fullyParallel: true,
  forbidOnly: true,
  retries: __RETRIES__,
  // Per-test ceiling so one hanging spec fails alone instead of letting the
  // outer run timeout SIGKILL (and red-flag) the whole batch.
  timeout: 60_000,
  expect: { timeout: 15_000 },
__WORKERS__  reporter: [['list'], ['html', { open: 'never' }], ['junit', { outputFile: 'results/junit.xml' }], ['./vkpower-reporter.ts']],
  use: {
    baseURL: nexusBaseURL(),
    headless: __HEADLESS__,
    trace: 'on-first-retry',
    screenshot: 'only-on-failure',
    // Reuse a captured authenticated session when VKPower injects one (auth profile
    // → vkpower.auth.json in the run dir). Self-detecting, so a downloaded bundle
    // (no auth file) is unaffected; a normal unauthenticated run is unchanged.
    ...(fs.existsSync('./vkpower.auth.json') ? { storageState: './vkpower.auth.json' } : {}),
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
// VKPower reporter — ships this run's results back to your VKPower platform so the
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

export default class VKPowerReporter implements Reporter {
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
      console.warn('[vkpower-reporter] NEXUS_ENDPOINT/TOKEN/ARTIFACT_ID not set — results not uploaded.');
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
      console.log(`[vkpower-reporter] uploaded ${this.steps.length} step result(s) -> HTTP ${resp.status}`);
    } catch (e) {
      console.warn('[vkpower-reporter] upload failed:', (e as Error).message);
    }
  }
}
"""

_AUTH_SETUP_TS = """\
import { chromium, FullConfig } from '@playwright/test';
import * as fs from 'fs';

// VKPower config-driven login -- DEFAULT OFF. Activates only when
// vkpower.auth.config.json sets "strategy":"form" AND credentials are in env
// (never in the file). On success it writes ./vkpower.auth.json (storageState),
// which playwright.config auto-loads. Fully defensive: any missing config or
// error is a silent no-op, so a non-auth run is completely unaffected.
export default async function globalSetup(config: FullConfig) {
  try {
    if (!fs.existsSync('./vkpower.auth.config.json')) return;
    const cfg = JSON.parse(fs.readFileSync('./vkpower.auth.config.json', 'utf-8'));
    if (!cfg || (cfg.strategy || 'none') !== 'form') return;
    const p0: any = (config.projects && config.projects[0]) || {};
    const baseURL = process.env.NEXUS_BASE_URL || cfg.baseURL || (p0.use && p0.use.baseURL) || '';
    const user = process.env[cfg.userEnv || 'NEXUS_LOGIN_USER'] || '';
    const pass = process.env[cfg.passwordEnv || 'NEXUS_LOGIN_PASSWORD'] || '';
    if (!user || !pass) { console.warn('[nexus-auth] form login configured but credentials env not set -- skipping.'); return; }
    const browser = await chromium.launch();
    const page = await browser.newPage(baseURL ? { baseURL } : {});
    await page.goto(cfg.loginPath || '/');
    for (const f of (cfg.fields || [])) {
      const v = f.value === 'user' ? user : f.value === 'password' ? pass : (f.value || '');
      await page.getByLabel(f.label).or(page.getByRole('textbox', { name: f.label })).first().fill(v);
    }
    await page.getByRole('button', { name: new RegExp(cfg.submitLabel || 'sign in', 'i') }).first().click();
    await page.waitForLoadState('networkidle').catch(() => {});
    await page.context().storageState({ path: './vkpower.auth.json' });
    await browser.close();
    console.log('[nexus-auth] form login OK -- wrote ./vkpower.auth.json');
  } catch (e) {
    console.warn('[nexus-auth] login skipped:', (e as Error).message);
  }
}
"""


_AUTH_CONFIG_JSON = """\
{
  "strategy": "none",
  "_comment": "Set strategy to 'form' to log in before every run. Credentials go in env vars (NEXUS_LOGIN_USER / NEXUS_LOGIN_PASSWORD), NEVER in this file. A field value of 'user'/'password' maps to those env vars; any other string is sent literally.",
  "loginPath": "/",
  "userEnv": "NEXUS_LOGIN_USER",
  "passwordEnv": "NEXUS_LOGIN_PASSWORD",
  "submitLabel": "Sign in",
  "fields": [
    { "label": "Email", "value": "user" },
    { "label": "Password", "value": "password" }
  ]
}
"""


_GITIGNORE = """\
node_modules/
test-results/
playwright-report/
results/
.env
vkpower.auth.json
*.auth.json
nexus.secrets.json
"""


_ENV_EXAMPLE = """\
# Upload run results to VKPower for the Grounded Triage view (baseline-vs-actual +
# a verdict per failure). Leave unset to run normally with no upload.
NEXUS_ENDPOINT=https://your-nexus-host
NEXUS_TOKEN=your-api-jwt
NEXUS_ARTIFACT_ID=your-artifact-id

# Config-driven login (optional). Set vkpower.auth.config.json strategy to "form",
# then provide credentials here (NEVER commit real secrets):
NEXUS_LOGIN_USER=
NEXUS_LOGIN_PASSWORD=
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
    against use.baseURL and data values read vkpower.data.json (observed defaults).
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
    files["vkpower-reporter.ts"] = _NEXUS_REPORTER_TS
    files[".env.example"] = _ENV_EXAMPLE
    files["vkpower.auth.setup.ts"] = _AUTH_SETUP_TS
    files["vkpower.auth.config.json"] = _AUTH_CONFIG_JSON
    files[".gitignore"] = _GITIGNORE
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


def _anchor_bundles(steps: list) -> list:
    """P3: per-step anchor bundle — label, kind, ranked locator rungs and a
    deterministic confidence from independent-signal count. Compile-time
    knowledge for warm runtime healing + explainable locators. Additive."""
    bundles: list = []
    for s in steps:
        obs = (s.get("observed") if isinstance(s, dict) else getattr(s, "observed", None)) or {}
        verb = str(obs.get("verb") or "").lower()
        label = str(obs.get("label") or "").strip()
        kind = str(obs.get("kind") or "").strip().lower()
        num = s.get("step_number") if isinstance(s, dict) else getattr(s, "step_number", None)
        if verb not in ("fill", "type", "select", "click", "check") or not label:
            continue
        esc = label.replace("'", "\\'")
        if kind in ("button", "link", "radio", "checkbox", "tab", "menu_item"):
            role = {"menu_item": "menuitem"}.get(kind, kind)
            rungs = [f"getByRole('{role}', {{ name: '{esc}' }})",
                     f"getByText('{esc}', {{ exact: true }})"]
        else:
            rungs = [f"getByLabel('{esc}')",
                     f"getByPlaceholder('{esc}')",
                     f"getByText('{esc}', {{ exact: false }})"]
        signals = sum([bool(label), bool(kind),
                       bool(obs.get("value")), bool(obs.get("url"))])
        bundles.append({
            "step_number": num,
            "label": label,
            "kind": kind or "field",
            "verb": verb,
            "rungs": rungs,
            "confidence": round(min(1.0, 0.4 + 0.15 * signals), 2),
            "signals": signals,
        })
    return bundles


def _step_provenance(steps: list) -> list:
    """P4(part): machine-readable evidence tier per step."""
    out: list = []
    for s in steps:
        num = s.get("step_number") if isinstance(s, dict) else getattr(s, "step_number", None)
        prov = (s.get("provenance") if isinstance(s, dict)
                else getattr(s, "provenance", None)) or ""
        out.append({"step_number": num,
                    "provenance": str(prov).lower() or "demonstrated"})
    return out


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
            "anchor_bundles": _anchor_bundles(steps),
            "step_provenance": _step_provenance(steps),
            "data_carry": list(getattr(tc, "data_carry", None) or []),
            "data_fields": _data_fields(steps, field_meta),
            "base_url": _recorded_origin([tc]),
        })

    project_files = [
        {"path": "playwright.config.ts", "code": _PLAYWRIGHT_CONFIG},
        {"path": "package.json", "code": _PACKAGE_JSON},
        {"path": "tsconfig.json", "code": _TSCONFIG},
        {"path": "vkpower-reporter.ts", "code": _NEXUS_REPORTER_TS},
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
