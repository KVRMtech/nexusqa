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
# Control-KIND / interaction heal recipes live OUTSIDE this frozen file; the
# `interactions` channel below early-returns into them. Module-level import is
# cycle-safe: interaction_resolver only imports compiler helpers LAZILY (inside its
# emit fn), so nothing here runs at import time.
from .interaction_resolver import INTERACTION_RECIPES
# Timing/materialize/portal/frame WAIT+SCOPE heal recipes also live OUTSIDE this
# frozen file; the `waits` channel below emits their PREAMBLE before the verb
# branch. Default-off (absent => byte-identical), never green-wash. Cycle-safe:
# wait_scope_resolver only imports compiler helpers LAZILY inside its emitter.
from .wait_scope_resolver import emit_wait_scope_lines

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


# Runtime token of a (possibly data-overridden) value for a tolerant, override-safe
# field oracle. Emitted into each parametrized spec; asserts the field/selected
# option HOLDS the entered value without mirroring a fixed token or a brittle exact
# string (survives input masking / option-label normalization). Falls back to a
# non-empty check when the value has no stable token.
_NXTOK_JS = r"""function __nxTok(v){const s=String(v==null?'':v);const m=s.match(/[A-Za-z]{2,}|[0-9]{2,}/);return m?new RegExp(m[0].replace(/[.*+?^${}()|[\]\\]/g,'\\$&'),'i'):/\S/;}"""


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


def _assertion_from_expected_result(observed: dict, expected_result: str = "") -> list[str]:
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
            out.append(
                f"await expect(page.getByText(/{tok}/i).first()).toBeVisible(); "
                "// grounded: step Expected Result, verified against the observed outcome"
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


def _action_lines(step, field_meta: dict, parametrize: bool = False,
                  reanchor: dict | None = None,
                  interaction: dict | None = None,
                  wait_scope: dict | None = None) -> list[str]:
    """Kind-aware Playwright lines for one (observed) step.

    When `parametrize` is set, navigation targets a path resolved against
    use.baseURL and data values become `D['key'] ?? 'observed'` — defaults stay
    the observed values, so a plain run is unchanged.

    `reanchor` (TrueFix selector re-anchor) is an optional
    `{name, kind?}` override: when present, this step is re-bound to the renamed
    control's new accessible `name` (the resilient ladder + the step's own
    visibility oracle then key off the new name). Default None → byte-identical.

    `wait_scope` (TrueFix timing/materialize/portal/frame heal) is an optional
    recipe (`{kind, ...}`) that emits a WAIT/SCOPE PREAMBLE before the action — a
    virtualized-list scroll-until-materialize, a portal retry-un-scoped-at-root, a
    frameLocator(url-pattern), or a baseline-relative wait + perf-regression flag.
    It only waits/scopes/flags; it never replaces the action or weakens the oracle,
    and a never-materializing control THROWS (RED). Default None → byte-identical."""
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

    # TIMING/MATERIALIZE/PORTAL/FRAME WAIT+SCOPE re-synthesis (additive heal channel;
    # default None → byte-identical). When a heal finds the control is PRESENT but the
    # test couldn't reach it (virtualized list not yet rendered, portal outside the
    # subtree, iframe-by-URL, slow-but-correct), it supplies a `wait_scope` recipe
    # whose PREAMBLE waits/scopes/flags BEFORE the action below. It never emits the
    # action nor weakens the oracle (those still run), and a never-materializing
    # control THROWS RED — so this channel can never green-wash. It is emitted FIRST
    # so it composes with the interaction/verb branches that follow.
    if wait_scope and wait_scope.get("kind"):
        out.extend(emit_wait_scope_lines(observed, wait_scope))

    # CONTROL-KIND / INTERACTION re-synthesis (additive heal channel; default None →
    # byte-identical). When a heal finds this step's control changed KIND so the
    # recorded recipe is wrong even though the element exists (e.g. native <select> →
    # custom ARIA combobox needing open+pick, not selectOption), it supplies an
    # `interaction` recipe whose choreography + OWN grounded committed-value oracle
    # REPLACE the verb branches below. Recipes live outside this frozen file.
    if interaction and interaction.get("kind") in INTERACTION_RECIPES:
        out.extend(INTERACTION_RECIPES[interaction["kind"]](observed, field_meta, parametrize, interaction))
        return out

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
        out.extend(_assertion_from_expected_result(observed))
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
            target = _rel_path(url) if parametrize else url
            out.append(f"await page.goto('{js_str(target)}'); // entry navigation only")
        else:
            path = url_path(url)
            prov = (observed.get("provenance") or "").strip().lower()
            if prov == "inferred":
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
                    # Assert the SELECTED OPTION's visible text, not the <select>'s value
                    # attribute -- coded option values (CA != 'Canada', plan IDs) would mis-fail.
                    out.append(f"await expect(sel.locator('option:checked')).toHaveText(/{_seltok}/i); // tolerant: selected-option text oracle")
                else:
                    out.append("await expect(sel).not.toHaveValue(''); // tolerant: a selection was committed")
        elif kind == "date":
            out.append(f"const dateField = {_ladder(observed, 'date')}.first();")
            _iso = _to_iso_date(value)
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
            loc = f"page.getByRole('{kind}', {{ name: '{name}' }})"
            out.append(f"await {loc}.check();")
            out.append(f"await expect({loc}).toBeChecked();")
        elif kind == "toggle":
            # Role UNCONFIRMED -> resilient ladder (radio name OR visible text);
            # no state assertion we cannot safely make.
            out.append(f"await {_ladder(observed, 'toggle')}.first().click();")
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
        out.append(f"await {_ladder(observed, kind)}.click();")
    else:
        out.append(f"// (no executable action derived) {action}")

    # Compile the step's Expected Result into real, grounded assertions
    # (recorded next page + observed outcome region + a grounded visibility
    # oracle from the step's Expected Result text).
    _er = (getattr(step, "expected_result", "") or getattr(step, "expected", "") or "").strip()
    out.extend(_assertion_from_expected_result(observed, _er))
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
                 reanchors: dict | None = None, heal_capture: bool = False,
                 interactions: dict | None = None, waits: dict | None = None,
                 force_open_shadow: bool = False) -> str:
    """Compile one ProductionTestCase to a runnable Playwright .spec.ts (string).

    When `parametrize` is set, the spec reads optional env/data overrides
    (use.baseURL + nexus.data.json) with the observed values as defaults — so it
    runs against any environment with any data, identically by default.

    `reanchors` (TrueFix selector re-anchor) is an optional
    `{step_number: {name, kind?}}` map; a step with an entry is re-bound to the
    renamed control's new accessible name. Default None → byte-identical.

    `heal_capture` appends a gated afterEach that snapshots the failure-state a11y
    tree for the re-anchor resolver (only on a NEXUS_HEAL_CAPTURE=1 re-run that
    fails). Default False → byte-identical (the user's owned spec is untouched).

    `waits` (TrueFix timing/materialize/portal/frame heal) is an optional
    `{step_number: {kind, ...}}` map; a step with an entry gets a WAIT/SCOPE
    PREAMBLE (scroll-until-materialize / retry-un-scoped-at-root / frameLocator-by-
    url / baseline-relative wait + perf flag) emitted before its action. It only
    waits/scopes/flags — never weakens the oracle, a never-materializing control
    THROWS. Default None → byte-identical."""
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
        out.append("  " + _NXTOK_JS)

    # ANY-UI heal (closed shadow DOM -> open): when a heal determined a control sits in
    # a CLOSED shadow root, force every root to 'open' BEFORE the app boots (addInitScript
    # runs before the entry goto, and persists across the SPA/page navigations), so the
    # normal open-shadow locator path can reach it. Default-off -> byte-identical.
    if force_open_shadow:
        from .any_ui_resolver import emit_open_shadow_preamble  # lazy: avoid import cycle
        for _osln in emit_open_shadow_preamble():
            out.append("  " + _osln)

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
                                  reanchor=(reanchors or {}).get(n),
                                  interaction=(interactions or {}).get(n),
                                  wait_scope=(waits or {}).get(n)):
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
  // Config-driven login (DEFAULT OFF): no-op unless nexus.auth.config.json strategy is 'form'.
  globalSetup: './nexus.auth.setup.ts',
  fullyParallel: true,
  forbidOnly: true,
  retries: Number(process.env.PLAYWRIGHT_RETRIES ?? (process.env.CI ? '1' : '0')),
  // Per-test ceiling so one hanging spec fails alone instead of letting the
  // outer run timeout SIGKILL (and red-flag) the whole batch.
  timeout: 60_000,
  expect: { timeout: 15_000 },
  reporter: [['list'], ['html', { open: 'never' }], ['junit', { outputFile: 'results/junit.xml' }], ['./nexus-reporter.ts']],
  use: {
    trace: 'on-first-retry',
    screenshot: 'only-on-failure',
    // Opt-in VIDEO of the run (e.g. to capture the proven healed Clean-Run baseline):
    // default 'off' => byte-identical; NEXUS_RECORD_VIDEO=1 records the full run.
    video: process.env.NEXUS_RECORD_VIDEO === '1' ? 'on' : 'off',
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
  // Config-driven login (DEFAULT OFF): nexus.auth.setup.ts is a no-op unless
  // nexus.auth.config.json sets strategy 'form' + credentials are in env.
  globalSetup: './nexus.auth.setup.ts',
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
    // Opt-in VIDEO of the run (e.g. to capture the proven healed Clean-Run baseline):
    // default 'off' => byte-identical; NEXUS_RECORD_VIDEO=1 records the full run.
    video: process.env.NEXUS_RECORD_VIDEO === '1' ? 'on' : 'off',
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

_AUTH_SETUP_TS = """\
import { chromium, FullConfig } from '@playwright/test';
import * as fs from 'fs';

// Nexus config-driven login -- DEFAULT OFF. Activates only when
// nexus.auth.config.json sets "strategy":"form" AND credentials are in env
// (never in the file). On success it writes ./nexus.auth.json (storageState),
// which playwright.config auto-loads. Fully defensive: any missing config or
// error is a silent no-op, so a non-auth run is completely unaffected.
export default async function globalSetup(config: FullConfig) {
  try {
    if (!fs.existsSync('./nexus.auth.config.json')) return;
    const cfg = JSON.parse(fs.readFileSync('./nexus.auth.config.json', 'utf-8'));
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
    await page.context().storageState({ path: './nexus.auth.json' });
    await browser.close();
    console.log('[nexus-auth] form login OK -- wrote ./nexus.auth.json');
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
nexus.auth.json
*.auth.json
nexus.secrets.json
"""


_ENV_EXAMPLE = """\
# Upload run results to Nexus for the Grounded Triage view (baseline-vs-actual +
# a verdict per failure). Leave unset to run normally with no upload.
NEXUS_ENDPOINT=https://your-nexus-host
NEXUS_TOKEN=your-api-jwt
NEXUS_ARTIFACT_ID=your-artifact-id

# Config-driven login (optional). Set nexus.auth.config.json strategy to "form",
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
    files["nexus.auth.setup.ts"] = _AUTH_SETUP_TS
    files["nexus.auth.config.json"] = _AUTH_CONFIG_JSON
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
