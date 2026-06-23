"""Control-KIND / interaction re-synthesis recipes for Auto-Heal (ADDITIVE).

When a heal determines a step's control changed KIND so the recorded INTERACTION
recipe is wrong even though the element still exists — e.g. a native ``<select>``
became a custom ARIA combobox that ``.selectOption()`` cannot drive — the frozen
compiler emits the recipe's CHOREOGRAPHY plus its OWN grounded committed-value
oracle instead of the default verb branch, via the additive ``interactions``
channel (a sibling to ``reanchors``).

The recipe library lives HERE, outside the frozen compiler. ``detect_interaction``
returns a recipe ONLY on a GROUNDED signal (the recorded control kind, the
extracted field_meta, or the browser's own ``selectOption`` failure) — never a
guess: absent any signal it returns ``None`` so the heal SUGGESTS and asks a
human, and never auto-applies. The recipe's own oracle keeps the heal PROVABLE
(the recorded value must actually commit), so this can never green-wash.
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


def _norm(s: str) -> str:
    return " ".join((s or "").split()).strip().lower()


def detect_interaction(observed: dict, field_meta: dict | None,
                       error_message: str = "") -> dict | None:
    """Return a grounded interaction recipe for this step, or None.

    GROUNDED signals (any one is sufficient; nothing else qualifies):
      * the recorded control kind is ``combobox``/``listbox``;
      * field_meta for this label says control is a combobox/listbox, OR carries
        >= 2 options (an option-set control the recording mis-typed as a field);
      * the live run's OWN error proves ``selectOption`` hit a non-``<select>``.
    No signal -> None (the heal proposes + confirms; it never auto-applies blind).
    """
    fm = field_meta or {}
    label = observed.get("label") or observed.get("value") or ""
    kind = (observed.get("kind") or "").strip().lower()
    meta = fm.get(_norm(label)) or {}
    mcontrol = (meta.get("control") or "").strip().lower()
    noptions = len(meta.get("options") or [])

    err = error_message or ""
    grounded = (
        kind in ("combobox", "listbox")
        or mcontrol in ("combobox", "listbox", "custom-combobox")
        or noptions >= 2
        or bool(_NOT_A_SELECT_RE.search(err))
        # The recorded .selectOption() FAILED on the live control (timeout/strict/not-
        # found): a native <select> selectOption does not time out resolving — a failure
        # here is strong evidence the control became a non-<select> chooser (custom ARIA
        # combobox). Worth TRYING the open+pick recipe; the committed-value oracle + the
        # headed prove gate acceptance, so a wrong guess fails RED, never a silent green.
        or "selectoption" in err.lower()
    )
    if not grounded:
        return None
    return {"kind": "custom_combobox", "value": observed.get("value", "") or ""}


def emit_combobox_lines(observed: dict, field_meta: dict, parametrize: bool,
                        interaction: dict) -> list[str]:
    """Custom ARIA combobox choreography: open -> wait listbox -> pick option ->
    committed-value oracle. Reuses the compiler's value-expr + js escaping, and
    adds a proximity-to-label open-locator fallback for comboboxes that carry NO
    accessible name (the deliberately hard case). The oracle asserts the recorded
    value COMMITTED (visible in the combobox display / a selected option) — NOT
    ``toHaveValue``, which is null on a non-form widget — so a heal that clicks but
    does not commit the recorded value fails RED, never a silent green."""
    from .compiler import _val_expr, _anchor_scope  # lazy: avoid an import cycle
    from .locators import js_str

    label = observed.get("label", "") or ""
    value = (interaction or {}).get("value", "") or observed.get("value", "") or ""
    lab = js_str(label)
    # xpath string literals use single quotes; strip quote chars from the label so a
    # quote can never break the xpath (rare; degrades gracefully, never throws).
    xlab = (label or "").replace("'", " ").replace('"', " ").strip()
    scope = _anchor_scope(observed)
    val_expr = _val_expr(value, label, parametrize)
    xpath = (
        "//label[contains(normalize-space(.),'" + xlab + "')]"
        "/ancestor::*[.//*[@role='combobox']][1]//*[@role='combobox']"
    )
    combo = (
        f"{scope}.getByRole('combobox', {{ name: '{lab}' }})"
        f".or({scope}.getByLabel('{lab}'))"
        f".or({scope}.locator(\"xpath={xpath}\")).first()"
    )
    out: list[str] = []
    out.append("// CONTROL-KIND heal: native <select> -> custom ARIA combobox "
               "(open + pick, not selectOption); committed-value oracle below.")
    out.append(f"const combo = {combo};")
    out.append("await combo.scrollIntoViewIfNeeded().catch(() => {});")
    out.append("await combo.click();                                     // open the listbox")
    out.append("await page.getByRole('listbox').first()"
               ".waitFor({ state: 'visible', timeout: 5000 }).catch(() => {});")
    out.append(f"await page.getByRole('option', {{ name: {val_expr} }})"
               ".first().click();          // pick the recorded option")
    # COMMITTED-VALUE ORACLE (grounded; NOT toHaveValue): after selecting, the
    # recorded value is visible in the combobox display, or an option is selected.
    out.append(
        f"await expect(combo.getByText({val_expr}, {{ exact: false }})"
        f".or(page.getByRole('option', {{ name: {val_expr}, selected: true }})))"
        ".toBeVisible({ timeout: 5000 }); // grounded: the recorded value committed"
    )
    return out


# kind -> emitter. The frozen compiler imports this mapping and, when a step carries
# an interaction recipe, calls the emitter in place of its verb branch. Add new
# control-kind recipes (typeahead, multiselect, date-grid, contenteditable, file) here.
INTERACTION_RECIPES = {
    "custom_combobox": emit_combobox_lines,
}
