"""Control-KIND / interaction re-synthesis recipes for Auto-Heal (ADDITIVE).

When a heal determines a step's control changed KIND so the recorded INTERACTION
recipe is wrong even though the element still exists — e.g. a native ``<select>``
became a custom ARIA combobox that ``.selectOption()`` cannot drive, a range
slider, a role=switch toggle, an accordion-disclosure, or a progressively-
revealed conditional field — the frozen compiler emits the recipe's CHOREOGRAPHY
plus its OWN grounded committed-value oracle instead of the default verb branch,
via the additive ``interactions`` channel (a sibling to ``reanchors``).

The recipe library lives HERE, outside the frozen compiler. ``detect_interaction``
returns a recipe ONLY on a GROUNDED signal (a real control-class FAILURE already
classified by ``diagnose`` for a recorded type/select step) — never a blind guess:
absent any signal it returns ``None`` so the heal SUGGESTS and asks a human, and
never auto-applies. Every recipe carries its OWN committed-value oracle (the
recorded value must actually commit) and is BOUNDED (~6s), so a wrong recipe fails
RED fast — it can never green-wash and never hangs to the 60s test timeout.

UACR (Universal Adaptive Control Recipe) — the design that kills the 2-iteration
.fill->.selectOption "dance":
  * The recorded steps carry verb=type/select kind=field/toggle with NO field_meta
    entry and NO live-DOM info at detect time, so STATIC detection cannot tell a
    combobox from a slider from a switch from an accordion. The ONLY reliable
    disambiguator is the LIVE DOM.
  * So ``detect_interaction`` fires on the FIRST control-kind failure (not on the
    iter-B selectOption error) and routes to ONE runtime-introspecting recipe
    ``universal_control``. That recipe introspects the live element ONCE and
    dispatches to the right sub-handler (combobox / slider / switch / checkbox /
    accordion / conditional). A NON-GATING ``hint`` (from the recorded value-shape
    + kind) only biases which branch it TRIES first; live truth, not the hint,
    decides — so a wrong hint cannot green-wash, it just costs one extra branch.
  * The kind-specific emitters below (custom_combobox / range_slider /
    toggle_switch / conditional_text) stay REGISTERED for back-compat with any
    persisted override that names them directly; new routing goes through
    ``universal_control``.
"""
from __future__ import annotations

import re

# A selectOption failure whose text PROVES the live control is not a native
# <select> (so the recorded recipe is genuinely wrong — a grounded signal).
_NOT_A_SELECT_RE = re.compile(
    r"did not find some options|element is not a (?:<select>|select)|"
    r"is not a well-formed <select>|no <option>|not an HTMLSelectElement|"
    r"selectOption[^\n]*(?:combobox|listbox|not.*select)",
    re.I,
)

# A recorded value that is purely a number/currency/amount (after stripping format
# chars) is grounded evidence the control is a NUMERIC chooser — a range slider /
# stepper — not a text combobox. A combobox's recorded value is a text option label.
_NUMERIC_VALUE_RE = re.compile(r"^[\s$€£₹%,.\d]+$")

# Boolean-ish recorded values for a switch/checkbox toggle.
_BOOLISH = {"true", "false", "on", "off", "yes", "no", "checked", "unchecked"}

# A recorded value that reads like a disclosure/section HEADER (the click-to-expand
# label of an accordion) rather than a typed value or an option label. GROUNDED only
# as a *hint* — runtime introspection still decides the actual recipe, so a wrong
# hint cannot green-wash.
_HEADER_HINT_RE = re.compile(
    r"\b(disclosure|disclosures|terms|agreement|details?|section|policy|"
    r"hazardous|activity|acknowledge|expand|show more)\b", re.I)

# Labels whose recorded VALUE is genuinely free-typed text (name / id / signature /
# date / email) — NEVER a chooser. Used by the BATCHED PRE-PASS (and as a static
# guard) to leave plain text fields untouched so the seam stays byte-identical.
_PLAIN_TEXT_LABEL_RE = re.compile(
    r"\b(first name|last name|full name|middle|name|ssn|tax id|e-?mail|email|phone|"
    r"date of birth|dob|signature|e-?signature|address|street|city|zip|postal)\b", re.I)

# A live error that the target is ABSENT / not-yet-rendered (vs a wrong KIND). On a
# plain-text field this is the signature of a CONDITIONAL field captured before its
# reveal toggle (out-of-order recording) — route to conditional_text (reveal then fill).
_LOCATOR_ABSENT_RE = re.compile(
    r"waiting for|not found|0 elements|resolved to 0|not visible|is hidden|not attached",
    re.I)


def _norm(s: str) -> str:
    return " ".join((s or "").split()).strip().lower()


def _looks_numeric(value: str) -> bool:
    v = (value or "").strip()
    return bool(v) and bool(re.search(r"\d", v)) and bool(_NUMERIC_VALUE_RE.match(v))


def _looks_like_header(value: str) -> bool:
    """A non-gating accordion HINT: a multi-word, digit-free phrase that reads like a
    disclosure / section header (the click-to-expand label), not a typed value.

    Option labels often carry a separator (em/en dash, pipe, slash — e.g.
    'Platinum — Concierge') so a value containing one is treated as a combobox
    option, NOT a header. With no separator we require the disclosure keyword, or a
    longer phrase (>=4 words). Non-gating: live introspection still decides."""
    v = (value or "").strip()
    if not v or re.search(r"\d", v):
        return False
    if re.search(r"[—–|/]", v):  # option-label separator -> not a header
        return False
    return bool(_HEADER_HINT_RE.search(v)) or len(v.split()) >= 4


def _hint_for(value: str, kind: str) -> str:
    """A NON-GATING first-dispatch hint from the recorded value-shape + kind.

    The hint only biases which sub-handler the universal recipe TRIES first at
    runtime; live DOM introspection (not this hint) decides the recipe that runs,
    so a wrong hint can never green-wash — it costs one extra in-browser branch."""
    v = (value or "").strip().lower()
    k = (kind or "").strip().lower()
    if _looks_numeric(value):
        return "range_slider"
    if k == "toggle" and not v:
        return "switch"
    if v in _BOOLISH:
        return "switch"
    if _looks_like_header(value):
        return "accordion"
    return "custom_combobox"


# Route a value-shape HINT to the PROVEN per-kind recipe. The specific recipes carry
# the per-control locator fallbacks (the combobox role/xpath drill, the input[type=
# range] fallback, the switch role xpath) that reliably resolve the live control; the
# genericized universal handle-ladder lost those and lands on wrapper <div>s, so we
# route to the specific recipe whose locator is known to work. (universal_control stays
# registered as a catch-all but is no longer the default route.)
_HINT_TO_RECIPE = {
    "range_slider": "range_slider",
    "custom_combobox": "custom_combobox",
    "switch": "toggle_switch",
    "accordion": "accordion",
}


def _recipe_for_hint(hint: str) -> str:
    return _HINT_TO_RECIPE.get(hint, "custom_combobox")


def is_plain_text_field(observed: dict) -> bool:
    """True when this recorded step is an ordinary free-text input (no chooser
    signature) so it must NOT be pre-seeded / routed to a control recipe.

    Conservative: a step is plain-text only when verb==type, kind is blank/'field'/
    'text', the value is NOT numeric and NOT boolish, and the label matches a known
    free-text pattern (name/ssn/email/signature/date/...). Everything else is left
    to live introspection. Plain text -> the compiler stays byte-identical."""
    verb = (observed.get("verb") or "").strip().lower()
    kind = (observed.get("kind") or "").strip().lower()
    if verb != "type" or kind not in ("", "field", "text"):
        return False
    value = (observed.get("value") or "")
    if _looks_numeric(value) or value.strip().lower() in _BOOLISH:
        return False
    label = observed.get("label") or observed.get("value") or ""
    return bool(_PLAIN_TEXT_LABEL_RE.search(label))


def has_control_kind_signature(observed: dict) -> bool:
    """GROUNDED static signal (recorded fields only — NO live DOM) that this step
    drives a NON-plain control, so the BATCHED PRE-PASS may pre-seed a
    universal_control recipe for it. True when verb in (type,select) AND it is NOT a
    plain free-text field. Plain text -> False -> left byte-identical.

    This is a COVERAGE optimization only — it decides which steps get an
    introspecting recipe pre-attached so all custom controls heal in ONE recompile
    instead of one-per-iteration. The recipe's runtime introspection still does the
    real disambiguation and its committed-value oracle still gates green."""
    verb = (observed.get("verb") or "").strip().lower()
    if verb not in ("type", "select"):
        return False
    if is_plain_text_field(observed):
        return False
    return True


def universal_interaction_for(observed: dict) -> dict | None:
    """Build a ``universal_control`` recipe dict for a recorded step IF it carries a
    control-kind signature, else None. Shared by ``detect_interaction`` (live-failure
    path) and the BATCHED PRE-PASS (static coverage path) so both produce identical
    recipe dicts. Plain text -> None -> byte-identical seam."""
    if not has_control_kind_signature(observed):
        return None
    value = observed.get("value", "") or ""
    label = observed.get("label", "") or ""
    kind = (observed.get("kind") or "").strip().lower()
    return {
        "kind": _recipe_for_hint(_hint_for(value, kind)),
        "value": value,
        "label": label,
        "hint": _hint_for(value, kind),
    }


def detect_interaction(observed: dict, field_meta: dict | None,
                       error_message: str = "") -> dict | None:
    """Return a grounded interaction recipe for this step, or None.

    UACR ROUTING (one iteration, NO .fill->.selectOption dance): the FIRST
    control-kind failure on a recorded type/select step routes DIRECTLY to the ONE
    runtime-introspecting recipe ``universal_control``. ``diagnose()`` only calls
    this from its WRONG_CONTROL_KIND / SELECTOR_DRIFT / LOCATOR_NOT_FOUND /
    NEEDS_REVIEW branch (self_heal.py) — i.e. a real control-class failure already
    happened — so firing here is grounded in a live failure, never a blind guess.
    The recipe carries its OWN committed-value oracle, so a wrong recipe fails RED.

    We STILL return None when there is no control-kind signal at all (a plain
    free-text field with no chooser signature, AND no grounded chooser hint from
    field_meta / the live error) so plain-text steps never get a recipe and the
    compiler stays byte-identical.

    GROUNDED chooser signals (any one upgrades a plain-text-looking field to a
    control recipe even before the live error is conclusive):
      * the recorded control kind is combobox/listbox/toggle;
      * field_meta for this label says control is a combobox/listbox/custom-combobox,
        OR carries >= 2 options;
      * the live run's OWN error proves a fill/selectOption hit a non-text control.
    """
    fm = field_meta or {}
    label = observed.get("label") or observed.get("value") or ""
    kind = (observed.get("kind") or "").strip().lower()
    meta = fm.get(_norm(label)) or {}
    mcontrol = (meta.get("control") or "").strip().lower()
    noptions = len(meta.get("options") or [])
    err = error_message or ""

    chooser_signal = (
        kind in ("combobox", "listbox", "toggle")
        or mcontrol in ("combobox", "listbox", "custom-combobox", "switch", "slider")
        or noptions >= 2
        or bool(_NOT_A_SELECT_RE.search(err))
        or "selectoption" in err.lower()
    )

    # CONDITIONAL TEXT FIELD: a plain-text field that is ABSENT on the live page is most
    # likely hidden until a reveal toggle (a recording captured out of order, e.g. fill
    # "Beneficiary full name" BEFORE checking "Add a beneficiary"). Route to conditional_text
    # (reveal the toggle, then fill); the recipe is safe for a slow-but-present field too
    # (it fills directly when already editable) and THROWS/escalates if no reveal toggle is
    # found — never a green-wash. This wins over the chooser fallback, whose selectOption-
    # fallback error would otherwise mis-route a text field to a combobox.
    if is_plain_text_field(observed) and _LOCATOR_ABSENT_RE.search(err):
        return {"kind": "conditional_text",
                "value": observed.get("value", "") or "",
                "label": observed.get("label", "") or "",
                "reveal_label": ""}

    intr = universal_interaction_for(observed)
    if intr is not None:
        return intr
    # The static plain-text guard rejected it, but a GROUNDED chooser signal (recorded
    # kind / field_meta / live error) overrides the guard: drive it as a control too.
    if chooser_signal:
        value = observed.get("value", "") or ""
        return {
            "kind": _recipe_for_hint(_hint_for(value, kind)),
            "value": value,
            "label": observed.get("label", "") or "",
            "hint": _hint_for(value, kind),
        }
    return None


# ════════════════════════════════════════════════════════════════════════════
# UACR — the single runtime-introspecting recipe.
# ════════════════════════════════════════════════════════════════════════════


def emit_universal_control_lines(observed: dict, field_meta: dict, parametrize: bool,
                                 interaction: dict) -> list[str]:
    """ONE runtime-introspecting control recipe: resolve a handle near the recorded
    label, introspect the LIVE element ONCE, then dispatch IN-BROWSER to the right
    interaction (custom combobox / range slider / role=switch / checkbox /
    accordion-disclosure -> inner checkbox / progressively-revealed conditional text).

    WHY runtime, not static: the recorded steps carry verb=type/select kind=field/
    toggle with NO field_meta and NO live DOM at detect time, so static value-shape
    can only split numeric (slider) from non-numeric (everything else). The live DOM
    is the only reliable disambiguator — Playwright pierces Aegis's OPEN shadow root
    automatically and a frameLocator handles its <iframe srcdoc>.

    HONESTY (never green-wash, always bounded):
      * The recorded VALUE drives the desired end-state; the recipe ends with a
        GROUNDED committed-value oracle (toHaveValue / toBeChecked / toBeVisible on
        the value, anchored to THIS control) so a click-without-commit fails RED.
      * The ``hint`` (range_slider | switch | accordion | custom_combobox) only
        biases which branch is TRIED first; the live ``tagName/type/role`` decides,
        so a wrong hint cannot green-wash.
      * Every locator / evaluate / click / fill / expect carries an explicit ~6s
        timeout, so a mis-routed or absent control fails FAST + RED, never hanging to
        the 60s test timeout.
      * An UNRESOLVABLE control (truly absent / cross-origin frame / no reveal toggle
        for a hidden conditional) THROWS a descriptive RED rather than a tolerant
        no-op — preserving honest escalation.
    """
    from .compiler import _val_expr, _anchor_scope  # lazy: avoid an import cycle
    from .locators import js_str

    label = observed.get("label", "") or ""
    value = (interaction or {}).get("value", "") or observed.get("value", "") or ""
    hint = (interaction or {}).get("hint", "") or _hint_for(value, observed.get("kind", ""))
    reveal_label = (interaction or {}).get("reveal_label", "") or ""
    lab = js_str(label)
    rlab = js_str(reveal_label)
    hnt = js_str(hint)
    xlab = (label or "").replace("'", " ").replace('"', " ").strip()
    scope = _anchor_scope(observed)
    val_expr = _val_expr(value, label, parametrize)

    # A broad resolution ladder near the recorded label. We resolve a HANDLE first,
    # introspect it, then dispatch — so the same handle works for every control kind.
    xpath = (
        "//label[contains(normalize-space(.),'" + xlab + "')]"
        "/ancestor::*[self::label or .//input or .//*[@role] or .//*[@data-control]][1]"
    )
    # ACCORDION-header rung (conditional on hint=accordion ONLY): an accordion's
    # clickable header carries the recorded VALUE as its TEXT (not the label), so a
    # label-keyed ladder misses it. Adding getByText(value) unconditionally would let
    # a control's displayed value (e.g. a slider's "$950,000" readout) win the
    # .first() DOM-order race ahead of the real input, so we gate it to the accordion
    # hint where the role/label rungs are known to miss.
    _text_rung = ""
    if hint == "accordion" and value:
        _vjs = js_str(value)
        _text_rung = f".or({scope}.getByText('{_vjs}', {{ exact: false }}))"
    handle = (
        f"{scope}.getByLabel('{lab}')"
        f".or({scope}.getByRole('combobox', {{ name: '{lab}' }}))"
        f".or({scope}.getByRole('switch', {{ name: '{lab}' }}))"
        f".or({scope}.getByRole('slider', {{ name: '{lab}' }}))"
        f".or({scope}.getByRole('checkbox', {{ name: '{lab}' }}))"
        f".or({scope}.locator(\"xpath={xpath}\"))"
        + _text_rung +
        f".or(page.frameLocator('iframe').getByLabel('{lab}'))"
        ".first()"
    )

    out: list[str] = []
    out.append("// CONTROL-KIND heal (UACR): resolve a handle near the label, introspect")
    out.append("// the LIVE element once, then dispatch to the right interaction. Every")
    out.append("// step is bounded (~6s) and ends in a grounded committed-value oracle.")
    out.append(f"const _hint = {js_str_literal(hnt)};")
    out.append(f"const uc = {handle};")
    # BOUNDED resolve: an absent / cross-origin / wrong-page control fails fast + RED.
    out.append("await uc.waitFor({ state: 'attached', timeout: 6000 }); "
               "// absent control -> fast RED, never a hang")
    out.append("await uc.scrollIntoViewIfNeeded().catch(() => {});")
    # INTROSPECT the live element ONCE — the single source of truth for dispatch.
    out.append("const _info = await uc.evaluate((el) => {")
    out.append("  const cs = el.getAttribute('aria-controls');")
    out.append("  let dc = el; while (dc && !(dc.dataset && dc.dataset.control)) dc = dc.parentElement;")
    out.append("  // is the element inside a collapsed (hidden) accordion body?")
    out.append("  let inHidden = false; let p = el;")
    out.append("  while (p) { if (p.hidden || (p.getAttribute && p.getAttribute('aria-hidden') === 'true')) { inHidden = true; break; } p = p.parentElement; }")
    out.append("  return {")
    out.append("    tag: (el.tagName || '').toLowerCase(),")
    out.append("    type: (el.getAttribute('type') || '').toLowerCase(),")
    out.append("    role: (el.getAttribute('role') || '').toLowerCase(),")
    out.append("    ariaChecked: el.getAttribute('aria-checked'),")
    out.append("    ariaExpanded: el.getAttribute('aria-expanded'),")
    out.append("    ariaControls: cs || null,")
    out.append("    dataControl: dc && dc.dataset ? (dc.dataset.control || '') : '',")
    out.append("    editable: !!(el.isContentEditable) || (el.tagName === 'INPUT' && !['range','checkbox','radio','button'].includes((el.getAttribute('type')||'').toLowerCase())) || el.tagName === 'TEXTAREA',")
    out.append("    inHidden,")
    out.append("  };")
    out.append("});")
    out.append("const _want = String(" + val_expr + ");")
    # ── DISPATCH. Order: definitive live signals first; hint only breaks ties.
    out.append("const _isRange = _info.tag === 'input' && _info.type === 'range' || _info.role === 'slider';")
    out.append("const _isSwitch = _info.role === 'switch' || (_info.tag === 'input' && _info.type === 'checkbox' && (_hint === 'switch'));")
    out.append("const _isCheckbox = (_info.tag === 'input' && _info.type === 'checkbox') || _info.role === 'checkbox';")
    out.append("const _isCombobox = _info.role === 'combobox' || _info.dataControl === 'custom-combobox' || !!_info.ariaControls;")
    out.append("const _isAccordion = (_info.dataControl === 'accordion') || (_info.ariaExpanded !== null && !_isCombobox) || (_hint === 'accordion' && !_info.editable && !_isCheckbox && !_isRange);")
    out.append("const _isNativeSelect = _info.tag === 'select';")

    # ---- RANGE SLIDER ----
    out.append("if (_isRange) {")
    out.append("  await expect(uc).toBeEnabled({ timeout: 6000 }); // disabled -> RED, never write .value through it")
    out.append("  const _raw = _want.replace(/[^\\d.]/g, '');")
    out.append("  const _g = await uc.evaluate((el, raw) => {")
    out.append("    const n = parseFloat(raw);")
    out.append("    const lo = parseFloat(el.min); const hi = parseFloat(el.max); const st = parseFloat(el.step);")
    out.append("    const loN = isFinite(lo) ? lo : 0; const hiN = isFinite(hi) ? hi : (isFinite(n) ? n : 0);")
    out.append("    const stN = (isFinite(st) && st > 0) ? st : 1;")
    out.append("    let v = isFinite(n) ? n : loN; if (v < loN) v = loN; if (v > hiN) v = hiN;")
    out.append("    v = loN + Math.round((v - loN) / stN) * stN; if (v < loN) v = loN; if (v > hiN) v = hiN;")
    out.append("    const cur = parseFloat(el.value);")
    out.append("    return { snap: v, lo: loN, hi: hiN, st: stN, cur: isFinite(cur) ? cur : null };")
    out.append("  }, _raw);")
    out.append("  const _snap = String(_g.snap);")
    out.append("  await uc.focus({ timeout: 6000 }).catch(() => {});")
    out.append("  if (_g.cur === _g.snap) { await uc.press('Home', { timeout: 6000 }).catch(() => {}); } // defeat no-op: prove WE drive it")
    out.append("  await uc.press('Home', { timeout: 6000 }).catch(() => {});")
    out.append("  const _steps = Math.min(Math.max(0, Math.round((_g.snap - _g.lo) / _g.st)), 5000);")
    out.append("  for (let i = 0; i < _steps; i++) { await uc.press('ArrowRight', { timeout: 6000 }); }")
    out.append("  const _after = await uc.inputValue({ timeout: 6000 }).catch(() => null);")
    out.append("  if (_after !== _snap) { await uc.fill(_snap, { timeout: 6000 }).catch(() => {}); }")
    out.append("  const _disp = await uc.evaluate((el, snap) => {")
    out.append("    const n = parseFloat(snap); const loc = isFinite(n) ? n.toLocaleString('en-US') : snap;")
    out.append("    let root = el; for (let i = 0; i < 4 && root.parentElement; i++) root = root.parentElement;")
    out.append("    const want = [snap, loc, '$' + loc, '$' + snap];")
    out.append("    const hit = Array.from(root.querySelectorAll('*')).find(e => e.children.length === 0 && want.some(w => (e.textContent || '').includes(w)));")
    out.append("    return { loc, found: hit ? (hit.textContent || '').trim() : null };")
    out.append("  }, _snap);")
    out.append("  if (_disp.found === null) { throw new Error('UACR range: no orthogonal display reflected ' + _disp.loc + ' — refusing to green-wash on .value alone'); }")
    out.append(f"  await expect({scope}.getByText('$' + _disp.loc, {{ exact: false }})"
               ".or(page.getByText('$' + _disp.loc, { exact: false })).first())"
               ".toBeVisible({ timeout: 6000 }); // ORTHOGONAL oracle: app display reflects the drive")
    out.append("  await expect(uc).toHaveValue(_snap, { timeout: 6000 }); // corroborating")
    out.append("}")

    # ---- NATIVE <select> (recorded control is still / again a real <select>) ----
    out.append("else if (_isNativeSelect) {")
    out.append("  await uc.selectOption({ label: _want }, { timeout: 6000 })")
    out.append("    .catch(async () => { await uc.selectOption(_want, { timeout: 6000 }); }); // by label, then value")
    out.append("  await expect(uc.locator('option:checked')).toHaveText(_grounded_token(_want), { timeout: 6000 }); // grounded: chosen option text holds the recorded value")
    out.append("}")

    # ---- ACCORDION DISCLOSURE -> inner checkbox/field ----
    out.append("else if (_isAccordion) {")
    out.append("  // expand the disclosure (idempotent), then drive the inner control the value targets.")
    out.append("  const _exp = await uc.getAttribute('aria-expanded').catch(() => null);")
    out.append("  if (_exp !== 'true') { await uc.click({ timeout: 6000 }).catch(() => {}); }")
    out.append("  // the accordion body is revealed near the header; find the inner checkbox/input.")
    out.append("  const _body = uc.locator('xpath=following::*[self::div or self::section][1]')"
               ".or(uc.locator('xpath=ancestor::*[contains(@class,\"acc\")][1]')).first();")
    out.append("  const _inner = _body.getByRole('checkbox').or(_body.locator(\"input[type='checkbox']\")).first();")
    out.append("  if (await _inner.count().catch(() => 0)) {")
    out.append("    await _inner.waitFor({ state: 'visible', timeout: 6000 });")
    out.append("    const _on = await _inner.isChecked({ timeout: 6000 }).catch(() => false);")
    out.append("    if (_on === false) await _inner.check({ timeout: 6000 });")
    out.append("    await expect(_inner).toBeChecked({ timeout: 6000 }); // grounded: the disclosure's control committed")
    out.append("  } else {")
    out.append("    // pure disclosure (no inner control): prove it actually expanded.")
    out.append("    await expect(uc).toHaveAttribute('aria-expanded', 'true', { timeout: 6000 });")
    out.append("  }")
    out.append("}")

    # ---- role=switch (and checkbox treated as switch by hint) ----
    out.append("else if (_isSwitch) {")
    out.append("  const _wv = _want.trim().toLowerCase();")
    out.append("  const _desiredOn = !(_wv === 'false' || _wv === 'off' || _wv === 'no' || _wv === 'unchecked');")
    out.append("  if (_info.role === 'switch') {")
    out.append("    const _cur = (_info.ariaChecked === 'true');")
    out.append("    if (_cur !== _desiredOn) { await uc.click({ timeout: 6000 }); }")
    out.append("    await expect(uc).toHaveAttribute('aria-checked', _desiredOn ? 'true' : 'false', { timeout: 6000 }); // grounded")
    out.append("  } else {")
    out.append("    const _on = await uc.isChecked({ timeout: 6000 }).catch(() => false);")
    out.append("    if (_on !== _desiredOn) { await (_desiredOn ? uc.check({ timeout: 6000 }) : uc.uncheck({ timeout: 6000 })); }")
    out.append("    if (_desiredOn) { await expect(uc).toBeChecked({ timeout: 6000 }); } else { await expect(uc).not.toBeChecked({ timeout: 6000 }); }")
    out.append("  }")
    out.append("}")

    # ---- CUSTOM COMBOBOX ----
    out.append("else if (_isCombobox) {")
    out.append("  await uc.click({ timeout: 6000 }); // open the listbox (bounded: a non-combobox fails fast)")
    out.append("  const _lid = _info.ariaControls;")
    out.append("  const _listbox = _lid ? page.locator('#' + _lid).or(page.getByRole('listbox')).first() : page.getByRole('listbox').first();")
    out.append("  await _listbox.waitFor({ state: 'visible', timeout: 5000 }).catch(() => {});")
    out.append("  await _listbox.getByRole('option', { name: _want, exact: false })"
               ".or(page.getByRole('option', { name: _want, exact: false }))"
               ".first().click({ timeout: 6000 }); // pick the recorded option (bounded)")
    out.append("  await expect(uc.getByText(_want, { exact: false })"
               ".or(page.getByRole('option', { name: _want, selected: true })))"
               ".toBeVisible({ timeout: 5000 }); // grounded: the recorded value committed on THIS combo")
    out.append("}")

    # ---- plain CHECKBOX (consent / certify / agree) ----
    out.append("else if (_isCheckbox) {")
    out.append("  const _wv = _want.trim().toLowerCase();")
    out.append("  const _desiredOn = !(_wv === 'false' || _wv === 'off' || _wv === 'no' || _wv === 'unchecked');")
    out.append("  const _on = await uc.isChecked({ timeout: 6000 }).catch(() => false);")
    out.append("  if (_on !== _desiredOn) { await (_desiredOn ? uc.check({ timeout: 6000 }) : uc.uncheck({ timeout: 6000 })); }")
    out.append("  if (_desiredOn) { await expect(uc).toBeChecked({ timeout: 6000 }); } else { await expect(uc).not.toBeChecked({ timeout: 6000 }); }")
    out.append("}")

    # ---- EDITABLE / CONDITIONAL TEXT (revealed by a sibling toggle) ----
    out.append("else if (_info.editable || _info.inHidden) {")
    out.append("  let _ready = await uc.isEditable({ timeout: 1200 }).catch(() => false);")
    out.append("  if (!_ready) {")
    if reveal_label:
        out.append(f"    const _reveal = {scope}.getByRole('checkbox', {{ name: '{rlab}' }})"
                   f".or({scope}.getByRole('switch', {{ name: '{rlab}' }})).first();")
    else:
        out.append(f"    const _reveal = {scope}.getByRole('checkbox', {{ name: /add|reveal|include|enable/i }})"
                   f".or({scope}.getByRole('switch', {{ name: /add|reveal|include|enable/i }})).first();")
    out.append("    if (await _reveal.count().catch(() => 0)) {")
    out.append("      await _reveal.scrollIntoViewIfNeeded({ timeout: 6000 });")
    out.append("      const _ron = await _reveal.isChecked({ timeout: 6000 }).catch(() => false);")
    out.append("      if (_ron === false) await _reveal.check({ timeout: 6000 });")
    out.append("    } else {")
    out.append("      throw new Error('UACR conditional: field hidden and no reveal toggle found — escalating (possible removed field)');")
    out.append("    }")
    out.append("    await uc.waitFor({ state: 'visible', timeout: 6000 });")
    out.append("  }")
    out.append("  await uc.fill(_want, { timeout: 6000 });")
    out.append("  await expect(uc).toHaveValue(_grounded_token(_want)); // grounded: the field committed the recorded value")
    out.append("}")

    # ---- UNRESOLVED KIND -> honest RED ----
    out.append("else {")
    out.append("  throw new Error('UACR: could not classify control kind for ' + " + js_str_literal(lab)
               + " + ' (tag=' + _info.tag + ' role=' + _info.role + ') — escalating instead of green-washing');")
    out.append("}")

    # A tiny in-spec token helper for the conditional-text oracle (tolerant to
    # autocomplete normalization but RED on a no-op). Emitted once per recipe block;
    # harmless if duplicated across steps (function re-declaration is block-scoped via
    # the surrounding test.step closure). Defined BEFORE use via hoisting.
    out.insert(0, "function _grounded_token(v){ const s=String(v||''); "
                  "const a=s.match(/[A-Za-z]{2,}/); if(a) return new RegExp(a[0].replace(/[.*+?^${}()|[\\]\\\\]/g,'\\\\$&'),'i'); "
                  "const d=s.match(/\\d{2,}/); if(d) return new RegExp(d[0],'i'); return /\\S/; }")
    return out


def js_str_literal(s: str) -> str:
    """A complete single-quoted JS string literal (the value is already js_str-escaped
    where needed by the caller). Used where we need the literal, not an interpolation."""
    return "'" + (s or "").replace("\\", "\\\\").replace("'", "\\'") + "'"


# ════════════════════════════════════════════════════════════════════════════
# Back-compat kind-specific emitters (KEPT verbatim in spirit; still registered so a
# persisted override that names them directly keeps working). New routing goes
# through universal_control above.
# ════════════════════════════════════════════════════════════════════════════


def emit_combobox_lines(observed: dict, field_meta: dict, parametrize: bool,
                        interaction: dict) -> list[str]:
    """Custom ARIA combobox choreography: resolve -> open -> wait the CONTROLLED
    listbox -> pick the recorded option -> committed-value oracle. Bounded + grounded
    (NOT toHaveValue — a div has no .value), so a click-without-commit fails RED."""
    from .compiler import _val_expr, _anchor_scope  # lazy: avoid an import cycle
    from .locators import js_str

    label = observed.get("label", "") or ""
    value = (interaction or {}).get("value", "") or observed.get("value", "") or ""
    lab = js_str(label)
    xlab = (label or "").replace("'", " ").replace('"', " ").strip()
    scope = _anchor_scope(observed)
    val_expr = _val_expr(value, label, parametrize)
    # Proximity-to-label anchor FIRST: the disp div has no accessible name, and a
    # by-name combobox match would bind to the value text after a commit.
    xpath = (
        "//label[contains(normalize-space(.),'" + xlab + "')]"
        "/ancestor::*[.//*[@role='combobox']][1]//*[@role='combobox']"
    )
    combo = (
        f"{scope}.locator(\"xpath={xpath}\")"
        f".or({scope}.getByLabel('{lab}'))"
        f".or({scope}.getByRole('combobox', {{ name: '{lab}' }})).first()"
    )
    out: list[str] = []
    out.append("// CONTROL-KIND heal: native <select> -> custom ARIA combobox "
               "(open + pick, not selectOption); committed-value oracle below.")
    out.append(f"const combo = {combo};")
    out.append("await combo.waitFor({ state: 'attached', timeout: 6000 }); "
               "// absent control -> fast RED, never a hang")
    out.append("await combo.scrollIntoViewIfNeeded().catch(() => {});")
    out.append("await combo.click({ timeout: 6000 });                   // open the listbox (bounded: a non-combobox fails fast)")
    out.append("const _lid = await combo.getAttribute('aria-controls').catch(() => null);")
    out.append("const _listbox = _lid "
               "? page.locator('#' + _lid).or(page.getByRole('listbox')).first() "
               ": page.getByRole('listbox').first();")
    out.append("await _listbox.waitFor({ state: 'visible', timeout: 5000 }).catch(() => {});")
    out.append(f"await _listbox.getByRole('option', {{ name: {val_expr}, exact: false }})"
               f".or(page.getByRole('option', {{ name: {val_expr}, exact: false }}))"
               ".first().click({ timeout: 6000 });   // pick the recorded option (bounded)")
    out.append(
        f"await expect(combo.getByText({val_expr}, {{ exact: false }})"
        f".or(page.getByRole('option', {{ name: {val_expr}, selected: true }})))"
        ".toBeVisible({ timeout: 5000 }); // grounded: the recorded value committed on THIS combo"
    )
    return out


def emit_slider_lines(observed: dict, field_meta: dict, parametrize: bool,
                      interaction: dict) -> list[str]:
    """DEPRECATED — back-compat shim. The registry routes ``range_slider`` to
    ``emit_range_slider_lines``."""
    return emit_range_slider_lines(observed, field_meta, parametrize, interaction)


def emit_range_slider_lines(observed: dict, field_meta: dict, parametrize: bool,
                            interaction: dict) -> list[str]:
    """Range-slider / stepper choreography, hardened against a green-wash audit.

    A native ``<input type=range>`` CLAMPS to ``[min,max]`` and SNAPS to the nearest
    ``min + k*step``, so the committed value is rarely the raw recorded digits. We
    read live min/max/step ONCE, compute the snapped+clamped target in-browser, drive
    via a REAL user gesture, and gate green on an ORTHOGONAL display oracle (not just
    the slider's own .value, which a fallback would write). Bounded everywhere (~6s).
    """
    from .compiler import _val_expr, _anchor_scope  # lazy: avoid an import cycle
    from .locators import js_str

    label = observed.get("label", "") or ""
    value = (interaction or {}).get("value", "") or observed.get("value", "") or ""
    lab = js_str(label)
    xlab = (label or "").replace("'", " ").replace('"', " ").strip()
    scope = _anchor_scope(observed)
    val_expr = _val_expr(value, label, parametrize)
    xpath = (
        "//label[contains(normalize-space(.),'" + xlab + "')]"
        "/ancestor::*[.//input[@type='range'] or .//*[@role='slider']][1]"
        "//*[self::input[@type='range'] or @role='slider']"
    )
    slider = (
        f"{scope}.getByLabel('{lab}')"
        f".or({scope}.getByRole('slider', {{ name: '{lab}' }}))"
        f".or({scope}.locator(\"xpath={xpath}\"))"
        f".or({scope}.locator(\"input[type='range']\")).first()"
    )

    out: list[str] = []
    out.append("// CONTROL-KIND heal: recorded fill -> range slider. Drive via a real")
    out.append("// user gesture; gate green on an ORTHOGONAL display surface (not just")
    out.append("// the slider's own .value, which a fallback would write).")
    out.append(f"const slider = {slider};")
    out.append("await slider.waitFor({ state: 'attached', timeout: 6000 });")
    out.append("await slider.scrollIntoViewIfNeeded().catch(() => {});")
    out.append("await expect(slider).toBeEnabled({ timeout: 6000 }); // disabled -> RED, never set .value through it")
    out.append(f"const _raw = String({val_expr}).replace(/[^\\d.]/g, '');")
    out.append("const _g = await slider.evaluate((el, raw) => {")
    out.append("  const n = parseFloat(raw);")
    out.append("  const lo = parseFloat(el.min); const hi = parseFloat(el.max);")
    out.append("  const st = parseFloat(el.step);")
    out.append("  const loN = isFinite(lo) ? lo : 0;")
    out.append("  const hiN = isFinite(hi) ? hi : (isFinite(n) ? n : 0);")
    out.append("  const stN = (isFinite(st) && st > 0) ? st : 1;")
    out.append("  let v = isFinite(n) ? n : loN;")
    out.append("  if (v < loN) v = loN; if (v > hiN) v = hiN;")
    out.append("  v = loN + Math.round((v - loN) / stN) * stN;")
    out.append("  if (v < loN) v = loN; if (v > hiN) v = hiN;")
    out.append("  const cur = parseFloat(el.value);")
    out.append("  return { snap: v, lo: loN, hi: hiN, st: stN, cur: isFinite(cur) ? cur : null };")
    out.append("}, _raw);")
    out.append("const _snap = String(_g.snap);")
    out.append("await slider.focus({ timeout: 6000 }).catch(() => {});")
    out.append("if (_g.cur === _g.snap) {")
    out.append("  await slider.press('Home', { timeout: 6000 }).catch(() => {});")
    out.append("}")
    out.append("await slider.press('Home', { timeout: 6000 }).catch(() => {});")
    out.append("const _steps = Math.max(0, Math.round((_g.snap - _g.lo) / _g.st));")
    out.append("const _capped = Math.min(_steps, 5000); // defensive bound; honest RED if unreachable")
    out.append("for (let i = 0; i < _capped; i++) { await slider.press('ArrowRight', { timeout: 6000 }); }")
    out.append("const _after = await slider.inputValue({ timeout: 6000 }).catch(() => null);")
    out.append("if (_after !== _snap) { await slider.fill(_snap, { timeout: 6000 }).catch(() => {}); }")
    out.append("const _disp = await slider.evaluate((el, snap) => {")
    out.append("  const n = parseFloat(snap);")
    out.append("  const loc = isFinite(n) ? n.toLocaleString('en-US') : snap;")
    out.append("  let root = el; for (let i = 0; i < 4 && root.parentElement; i++) root = root.parentElement;")
    out.append("  const want = [snap, loc, '$' + loc, '$' + snap];")
    out.append("  const hit = Array.from(root.querySelectorAll('*')).find(e =>")
    out.append("    e.children.length === 0 && want.some(w => (e.textContent || '').includes(w)));")
    out.append("  return { loc, found: hit ? (hit.textContent || '').trim() : null };")
    out.append("}, _snap);")
    out.append("if (_disp.found === null) {")
    out.append("  throw new Error('range_slider heal: no orthogonal display surface reflected ' +")
    out.append("    _disp.loc + ' near the slider — refusing to green-wash on .value alone');")
    out.append("}")
    out.append(f"await expect({scope}.getByText('$' + _disp.loc, {{ exact: false }})"
               ".or(page.getByText('$' + _disp.loc, { exact: false }))"
               ".first()).toBeVisible({ timeout: 6000 });"
               " // ORTHOGONAL oracle: app display reflects the drive")
    out.append("await expect(slider).toHaveValue(_snap, { timeout: 6000 }); "
               "// corroborating: the range committed the snapped value")
    return out


def emit_toggle_switch_lines(observed: dict, field_meta: dict, parametrize: bool,
                             interaction: dict) -> list[str]:
    """role=switch toggle choreography (Aegis <div role='switch' aria-checked>).

    Resolve the switch ONLY via its accessible name or a label-proximity xpath —
    NO bare [role='switch'] catch-all — so a label-less switch that no rung matches
    fails RED rather than positionally binding to an arbitrary switch. The desired
    end-state comes from the recorded value (empty/truthy -> ON; explicit false/off/
    no -> OFF). Idempotent: click ONLY when the current aria-checked differs from the
    target, so the 2nd confirmation green never toggles it back. Grounded oracle:
    aria-checked equals the desired state (a click-without-commit fails RED)."""
    from .compiler import _val_expr, _anchor_scope  # lazy: avoid an import cycle
    from .locators import js_str

    label = observed.get("label", "") or ""
    value = (interaction or {}).get("value", "") or observed.get("value", "") or ""
    lab = js_str(label)
    xlab = (label or "").replace("'", " ").replace('"', " ").strip()
    scope = _anchor_scope(observed)
    val_expr = _val_expr(value, label, parametrize)
    xpath = (
        "//label[contains(normalize-space(.),'" + xlab + "')]"
        "/ancestor::*[.//*[@role='switch']][1]//*[@role='switch']"
    )
    sw = (
        f"{scope}.getByRole('switch', {{ name: '{lab}' }})"
        f".or({scope}.locator(\"xpath={xpath}\")).first()"
    )
    out: list[str] = []
    out.append("// CONTROL-KIND heal: recorded toggle -> role=switch (click iff state differs).")
    out.append(f"const sw = {sw};")
    out.append("await sw.waitFor({ state: 'attached', timeout: 6000 }); // absent -> fast RED")
    out.append("await sw.scrollIntoViewIfNeeded().catch(() => {});")
    out.append(f"const _wv = String({val_expr}).trim().toLowerCase();")
    out.append("const _desiredOn = !(_wv === 'false' || _wv === 'off' || _wv === 'no' || _wv === 'unchecked');")
    out.append("const _cur = (await sw.getAttribute('aria-checked', { timeout: 6000 }).catch(() => null)) === 'true';")
    out.append("if (_cur !== _desiredOn) { await sw.click({ timeout: 6000 }); } // idempotent")
    out.append("await expect(sw).toHaveAttribute('aria-checked', _desiredOn ? 'true' : 'false', "
               "{ timeout: 6000 }); // grounded: the switch committed the recorded state")
    return out


def _grounded_value_oracle_js(value: str, val_expr: str, parametrize: bool) -> str:
    """A HARD, token-tolerant ``toHaveValue`` matcher expression (no ``.catch``, no
    ``not.toHaveValue('')`` escape hatch) for the conditional field's oracle."""
    if parametrize:
        return f"__nxTok({val_expr})"
    from .compiler import _value_oracle  # lazy: avoid an import cycle
    tok = _value_oracle(value)
    if tok:
        return f"/{tok}/i"
    return "/\\S/"


def emit_conditional_text_lines(observed: dict, field_meta: dict, parametrize: bool,
                                interaction: dict) -> list[str]:
    """Conditional / progressively-revealed text field choreography.

    The recorded fill targets a text input that does NOT exist until a sibling
    REVEAL control (a checkbox / role=switch labelled e.g. "Add a beneficiary") is
    enabled — and the recording captured the steps OUT OF ORDER (the fill recorded
    BEFORE its reveal toggle). A plain ``.fill()`` hits an absent / hidden element.
    Bounded + idempotent + grounded ``toHaveValue`` oracle (no .catch)."""
    from .compiler import _val_expr, _anchor_scope  # lazy: avoid an import cycle
    from .locators import js_str

    label = observed.get("label", "") or ""
    value = (interaction or {}).get("value", "") or observed.get("value", "") or ""
    reveal_label = (interaction or {}).get("reveal_label", "") or ""
    lab = js_str(label)
    rlab = js_str(reveal_label)
    xlab = (label or "").replace("'", " ").replace('"', " ").strip()
    scope = _anchor_scope(observed)
    val_expr = _val_expr(value, label, parametrize)
    oracle = _grounded_value_oracle_js(value, val_expr, parametrize)

    xpath_field = (
        "//label[contains(normalize-space(.),'" + xlab + "')]"
        "/ancestor-or-self::*[.//input[not(@type) or @type='text']][1]"
        "//input[not(@type) or @type='text']"
    )
    field = (
        f"{scope}.getByLabel('{lab}')"
        f".or({scope}.getByRole('textbox', {{ name: '{lab}' }}))"
        f".or({scope}.locator(\"xpath={xpath_field}\")).first()"
    )
    if reveal_label:
        reveal = (
            f"{scope}.getByRole('checkbox', {{ name: '{rlab}' }})"
            f".or({scope}.getByRole('switch', {{ name: '{rlab}' }})).first()"
        )
    else:
        reveal = (
            f"{scope}.getByRole('checkbox', {{ name: /add|reveal|include|enable/i }})"
            f".or({scope}.getByRole('switch', {{ name: /add|reveal|include|enable/i }}))"
            ".first()"
        )

    out: list[str] = []
    out.append("// CONTROL-KIND heal: conditional / progressively-revealed text field "
               "(enable the reveal toggle first, then fill); toHaveValue oracle proves commit.")
    out.append(f"const condField = {field};")
    out.append("const _alreadyReady = await condField.isEditable({ timeout: 1200 })"
               ".catch(() => false);")
    out.append("if (!_alreadyReady) {")
    out.append(f"  const reveal = {reveal};")
    out.append("  if (await reveal.count().catch(() => 0)) {")
    out.append("    await reveal.scrollIntoViewIfNeeded({ timeout: 6000 });")
    out.append("    const _on = await reveal.isChecked({ timeout: 6000 }).catch(() => false);")
    out.append("    if (_on === false) await reveal.check({ timeout: 6000 });   // reveal (bounded, idempotent)")
    out.append("  }")
    out.append("  await condField.waitFor({ state: 'visible', timeout: 6000 });")
    out.append("}")
    out.append(f"await condField.fill({val_expr}, {{ timeout: 6000 }});")
    out.append(f"await expect(condField).toHaveValue({oracle}); "
               "// grounded: the revealed field committed the recorded value")
    return out


def emit_accordion_lines(observed: dict, field_meta: dict, parametrize: bool,
                         interaction: dict) -> list[str]:
    """Disclosure / accordion choreography. The recorded VALUE is the clickable HEADER
    TEXT (not the label), so we resolve the header by its text + click it to EXPAND.
    Grounded oracle: the revealed body becomes visible — a no-op (header not clickable
    / nothing expands) fails RED, never green-washes. Bounded (~6s)."""
    from .compiler import _anchor_scope  # lazy: avoid import cycle
    from .locators import js_str

    value = (interaction or {}).get("value", "") or observed.get("value", "") or ""
    scope = _anchor_scope(observed)
    vjs = js_str(value)
    xval = (value or "").replace("'", " ").replace('"', " ").strip()
    header = (
        f"{scope}.getByText('{vjs}', {{ exact: false }})"
        f".or({scope}.locator(\"xpath=//*[contains(@class,'hd') or @role='button' or self::summary]"
        f"[contains(normalize-space(.),'{xval}')]\")).first()"
    )
    out: list[str] = []
    out.append("// CONTROL-KIND heal: disclosure/accordion — the recorded value is the header")
    out.append("// TEXT; click it to EXPAND. Grounded oracle: the revealed body becomes visible.")
    out.append(f"const acc = {header};")
    out.append("await acc.waitFor({ state: 'visible', timeout: 6000 });")
    out.append("await acc.scrollIntoViewIfNeeded().catch(() => {});")
    out.append("await acc.click({ timeout: 6000 }); // expand the disclosure")
    out.append("await expect(acc.locator(\"xpath=following-sibling::*[1]\")"
               ".or(acc.locator(\"xpath=ancestor::*[contains(@class,'acc') or "
               "contains(@class,'accordion')][1]//*[contains(@class,'bd') or "
               "contains(@class,'body') or contains(@class,'content')]\"))"
               ".first()).toBeVisible({ timeout: 6000 }); "
               "// grounded: the disclosure expanded (a no-op fails RED)")
    return out


# kind -> emitter. The frozen compiler imports this mapping and, when a step carries
# an interaction recipe, calls the emitter in place of its verb branch. detect_interaction
# routes a value-shape HINT to the PROVEN kind-specific emitter (combobox/slider/switch/
# accordion/conditional); ``universal_control`` stays registered as a catch-all.
INTERACTION_RECIPES = {
    "universal_control": emit_universal_control_lines,
    "custom_combobox": emit_combobox_lines,
    "range_slider": emit_range_slider_lines,
    "toggle_switch": emit_toggle_switch_lines,
    "conditional_text": emit_conditional_text_lines,
    "accordion": emit_accordion_lines,
}
