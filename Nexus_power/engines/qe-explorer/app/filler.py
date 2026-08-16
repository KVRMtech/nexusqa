"""Control probing and grounded value commitment (M0.3 / T-DE-08).

Extracted VERBATIM from :mod:`app.crawler`.  Everything here answers one
question: WHAT DOES THIS CONTROL ACTUALLY OFFER, and what happens when it is
operated?

  * :meth:`ControlFiller.probe_select_options` — open a custom dropdown and
    read the choices a static inventory cannot see.
  * :meth:`ControlFiller.set_options`          — record those choices honestly.
  * :meth:`ControlFiller.commit_act` /
    :meth:`ControlFiller.commit_choice`        — operate ONE driver control.
  * :meth:`ControlFiller.probe_dependencies`   — ACT-THEN-DIFF: what did that
    act reveal?

TWO DISCIPLINES SURVIVE THE MOVE UNCHANGED, and both are the reason this code
is written the way it is rather than the short way.

NEVER FABRICATE AN ANSWER SET.  The enumeration a question offers IS the test
data for the positive, negative and boundary cases generated from it.  So a
list that was CLIPPED must never be indistinguishable from one that was
complete — hence ``options_total`` and ``options_truncated`` — and a probe that
fails leaves the options EMPTY (an honest "unread choice") rather than
guessing.  An LLM may say which control to operate; it may never say what a
control offers.

FAIL CLOSED ON ACTUATION.  ``commit_act`` refuses anything not affirmatively
safe (``danger_signals.safe_to_actuate``), and every probe is bounded and
EXPLORE-phase only — none of these commit anything server-side.

Like the other extracted collaborators, this module declares the interface it
needs and never imports :mod:`app.crawler`.
"""
from __future__ import annotations

import logging
from typing import Any, Mapping, Protocol, Sequence

from . import danger_signals
from . import matcher
from .inventory import build_inventory

logger = logging.getLogger(__name__)

#: Value-bearing control kinds (a form FIELD, not a button/link) — a newly-appeared one
#: after a driver act is a conditionally-revealed field.
_FIELD_KINDS = frozenset({
    "text", "date", "select", "radio", "checkbox", "toggle", "slider", "color",
})


class FillerHost(Protocol):
    """The slice of crawl state the probes read."""

    _port: Any
    _tracker: Any
    _refuse_pack: Any
    _max_option_probes: int
    _max_probed_options: int
    _max_dep_probes: int

    def _in_scope_key(self, url: str) -> str: ...


class ControlFiller:
    """Probes what controls offer, and commits grounded acts to find out more."""

    def __init__(self, host: FillerHost) -> None:
        self._c = host

    async def probe_select_options(
        self, controls: Sequence[dict[str, Any]], *, url: str,
    ) -> None:
        """Read the option LABELS of CUSTOM dropdowns whose options the static inventory
        couldn't see (a widget that builds them only on OPEN). For each opener: click to
        open, read the revealed ``[role=option]`` LABELS, then dismiss (Escape) so the page
        is restored before the next read. Enriches the control's ``options`` in place so the
        form_snapshot carries the real choices.

        DISCIPLINE (never green-wash): LABELS only, never values/locators; native ``<select>``
        is skipped (optionsOf already reads it, and a browser-native popup isn't DOM-readable);
        ONE dropdown open at a time (dismissed before the next) so options are attributed to
        the control that was opened; bounded by ``_MAX_OPTION_PROBES``; any failure leaves the
        control's options empty — an honest 'unread choice', never a fabricated list."""
        c = self._c
        collect = getattr(c._port, "collect_controls", None)
        press = getattr(c._port, "press_key", None)
        if collect is None:
            return
        probed = 0
        for ctl in controls:
            if probed >= c._max_option_probes:
                break
            # The matcher registry decides which controls need the open-probe (a custom choice
            # whose options only appear on open) — new widgets plug in via a matcher rule.
            if not matcher.needs_open_probe(ctl):
                continue
            try:
                open_obs = await c._port.click(dict(ctl))
                c._tracker.note_action()
                c._tracker.note_request()
                # A dropdown that navigated is not a dropdown — bail on that control.
                if getattr(open_obs, "url_after", None) and getattr(open_obs, "url_before", None) \
                        and open_obs.url_after != open_obs.url_before:
                    continue
                revealed = build_inventory(await collect(), c._refuse_pack, url=url)
                opts: list[str] = []
                seen: set[str] = set()
                for r in revealed:
                    if (r.get("role") or "").strip().lower() == "option":
                        nm = str(r.get("name") or "").strip()
                        if nm and nm.lower() not in seen:
                            seen.add(nm.lower())
                            opts.append(nm)
                if press is not None:
                    await press("Escape")  # restore: dismiss the opened listbox
                if opts:
                    self.set_options(ctl, opts)
                    probed += 1
            except Exception:
                continue

    def set_options(self, control: dict[str, Any], options: Sequence[str]) -> None:
        """Write a READ option list onto a control, bounded and HONESTLY marked.

        The enumeration a question offers is not decoration — it is the answer set
        a generated positive/negative/boundary case is built from, so a list that
        was clipped must never be indistinguishable from one that was complete.
        Records ``options_total`` (what the page actually offers) and, when the
        bound bit, ``options_truncated`` — so a consumer can say "247 offered, 300
        captured" instead of presenting a prefix as the set of valid answers.
        """
        opts = list(options or ())
        total = len(opts)
        kept = opts[:self._c._max_probed_options]
        control["options"] = kept
        control["options_total"] = max(total, int(control.get("options_total") or 0))
        if total > len(kept):
            control["options_truncated"] = True
            logger.info(
                "qec.catalog.options_truncated control=%r offered=%d captured=%d",
                str(control.get("name") or "")[:40], total, len(kept))
        if isinstance(control.get("qec"), dict):
            control["qec"]["options"] = kept

    async def commit_act(self, control: dict[str, Any]) -> bool:
        """Perform ONE grounded, non-submitting act on a driver control so a dependent
        field/options can react: a SELECT commits its first option; a RADIO is clicked; a
        CHECKBOX/TOGGLE is switched on. Returns True iff an act fired. EXPLORE-phase only —
        none of these submit anything server-side."""
        c = self._c
        # SAFETY: never actuate a control that isn't affirmatively a safe value control —
        # a destructive / money-moving / account-consequential label (any language) or a
        # danger-flagged control is left alone (fail-closed).
        if not danger_signals.safe_to_actuate(control):
            return False
        kind = control.get("kind")
        if kind == "select":
            return await self.commit_choice(control)
        try:
            if kind == "radio":
                await c._port.click(dict(control))
            elif kind in ("checkbox", "toggle"):
                set_checked = getattr(c._port, "set_checked", None)
                if set_checked is not None:
                    await set_checked(dict(control), True)
                else:
                    await c._port.click(dict(control))
            else:
                return False
            c._tracker.note_action()
            return True
        except Exception:
            return False

    async def commit_choice(self, control: dict[str, Any]) -> bool:
        """Grounded-select a driver's FIRST real option so a dependent field can react:
        a native <select> via select_option; a custom combobox by opening it and clicking
        the matching [role=option]. Returns True iff a value was committed. EXPLORE-phase
        only — a chosen dropdown value commits NOTHING server-side (no submit)."""
        c = self._c
        opts = [o for o in (control.get("options") or []) if str(o).strip()]
        if not opts:
            return False
        first = str(opts[0]).strip()
        tag = (control.get("tag") or "").strip().lower()
        select_option = getattr(c._port, "select_option", None)
        if tag == "select" and select_option is not None:
            try:
                await select_option(dict(control), first)
                c._tracker.note_action()
                return True
            except Exception:
                return False
        collect = getattr(c._port, "collect_controls", None)
        if collect is None:
            return False
        try:
            await c._port.click(dict(control))            # open the listbox
            revealed = build_inventory(await collect(), c._refuse_pack, url="")
            for r in revealed:
                if (r.get("role") or "").strip().lower() == "option" \
                        and str(r.get("name") or "").strip() == first:
                    await c._port.click(dict(r))          # commit the choice
                    c._tracker.note_action()
                    return True
        except Exception:
            return False
        return False

    async def probe_dependencies(
        self, controls: list[dict[str, Any]], *, url: str,
    ) -> None:
        """ACT-THEN-DIFF: commit ONE driver act (select an option / pick a radio / switch a
        toggle), re-observe, and DIFF the inventory to capture what the act CHANGED:
          (a) a DEPENDENT select whose options only populate after the act (To Account after
              From Account) — captured + tagged depends_on;
          (b) a CONDITIONALLY-REVEALED field that only appears after the act (choose 'Other'
              -> a text field; 'Schedule for later' -> a date picker) — appended to the
              snapshot + tagged depends_on.
        Bounded, EXPLORE-phase (no submit). HONESTY: everything captured this way is tagged
        depends_on=<driver> so it reads as CONDITIONAL on that driver, never as always-present
        or a fixed list; any failure leaves the field an honest unread/absent state; if an act
        navigates away, the pass bails rather than attributing another page's fields."""
        c = self._c
        collect = getattr(c._port, "collect_controls", None)
        current_url = getattr(c._port, "current_url", None)
        if collect is None:
            return

        def _key(ctl: Mapping[str, Any]) -> str:
            return str(ctl.get("name") or "").strip().lower()

        # The matcher registry identifies ACT-THEN-DIFF drivers (a choice/radio/toggle whose
        # act can reveal a dependent); the safety gate in _commit_act still fail-closes each.
        drivers = [ctl for ctl in controls if matcher.is_diff_driver(ctl)]
        if not drivers:
            return
        seen_names = {_key(ctl) for ctl in controls if ctl.get("name")}
        empty_by_name = {
            str(ctl.get("name") or ""): ctl for ctl in controls
            if ctl.get("kind") == "select" and not ctl.get("options") and ctl.get("name")
        }
        acted = 0
        for d in drivers:
            if acted >= c._max_dep_probes:
                break
            if not await self.commit_act(d):
                continue
            acted += 1
            # If the act navigated away, this page is gone — do not attribute its fields.
            if current_url is not None:
                try:
                    if c._in_scope_key(await current_url()) != c._in_scope_key(url):
                        return
                except Exception:
                    pass
            after = build_inventory(await collect(), c._refuse_pack, url=url)
            driver_label = d.get("name") or ""

            # (a) DEPENDENT selects: empty -> populated (open-probe custom ones so they surface).
            pending = [ctl for ctl in after
                       if ctl.get("kind") == "select" and str(ctl.get("name") or "") in empty_by_name]
            await self.probe_select_options(
                [ctl for ctl in pending if not ctl.get("options")], url=url)
            for r in pending:
                nm = str(r.get("name") or "")
                if r.get("options") and nm in empty_by_name:
                    tgt = empty_by_name.pop(nm)
                    self.set_options(tgt, list(r.get("options") or []))
                    tgt["depends_on"] = driver_label
                    if isinstance(tgt.get("qec"), dict):
                        tgt["qec"]["options"] = tgt["options"]

            # (b) CONDITIONALLY-REVEALED fields: value-bearing controls that were not present
            # before the act. Append to the snapshot (so the manifest sees them) tagged
            # depends_on; if a revealed field is itself an empty custom select, register it as
            # a further dependent to probe on a later act.
            for r in after:
                k = _key(r)
                if not k or k in seen_names or r.get("kind") not in _FIELD_KINDS:
                    continue
                seen_names.add(k)
                r["depends_on"] = driver_label
                if isinstance(r.get("qec"), dict):
                    r["qec"]["depends_on"] = driver_label
                controls.append(r)
                if r.get("kind") == "select" and not r.get("options"):
                    empty_by_name[str(r.get("name") or "")] = r


__all__ = ["ControlFiller", "FillerHost", "_FIELD_KINDS"]
