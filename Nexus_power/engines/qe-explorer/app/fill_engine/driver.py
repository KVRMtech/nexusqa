"""THE BRIDGE FROM ONE COMMIT TO ONE VERDICT.

:mod:`app.fill_engine.repair` needs exactly two things from the browser: a way
to commit a value, and a way to learn what the application thought of it.  This
module is the only place in the engine that touches either, which is what keeps
the loop itself pure and the whole architecture testable against a fake.

WHAT "LEARN WHAT THE APPLICATION THOUGHT" COSTS, and why it is not paid on every
fill.  Reading the verdict properly means re-collecting the page's controls so
the control's own ``aria-invalid`` and error node can be read.  On a page with
thirty fields that is thirty extra DOM reads for the twenty-nine that were
accepted first time — a latency cost the non-functional requirements explicitly
rule out.

So the verdict is read in TWO STAGES:

  1. **Free signals, always.**  The fill's own observation already carries the
     browser's read-back, its ``intent_met`` verdict and the page's alert
     regions.  A fill whose read-back matched, whose intent was met and which
     raised no NEW page alert is accepted with no extra work at all.
  2. **The expensive read, only on suspicion.**  When any of those is off, the
     controls are re-collected once and
     :func:`app.fill_engine.validation.signals_for_control` decides whether the
     complaint is genuinely about THIS control.

The second stage is what makes a repair possible; the first is what makes it
free on the ninety-odd per cent of fields that never need one.

FAIL TOWARD ACCEPTING A CLEAN FILL.  If the verdict cannot be read — the port
has no such method, the read threw, the page navigated — the fill is accepted on
its read-back alone, which is exactly the behaviour that existed before this
module.  A repair loop that invents rejections is worse than none.
"""
from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Mapping, Optional, Sequence

from .repair import FillVerdict
from .validation import PageAlertFilter, signals_for_control

logger = logging.getLogger(__name__)

__all__ = ["ControlFillDriver", "CommitFn", "read_page_alerts"]

#: Commit one value and report ``(committed_value, intent_met, mechanical_error)``.
#: Supplied by :mod:`app.forms`, which owns the locators and the R1 mechanic
#: ladder — none of which belong in the engine.
CommitFn = Callable[[Mapping[str, Any], str],
                    Awaitable["tuple[Optional[str], Optional[bool], str]"]]


async def read_page_alerts(port: Any) -> list[str]:
    """The page's visible alert regions, or an empty list.

    Never raises and never blocks a fill: a port without the method, or one
    whose read throws, simply yields no alerts — which makes every later fill
    look clean rather than making every later fill look broken."""
    reader = getattr(port, "error_texts", None)
    if reader is None:
        return []
    try:
        return [str(t) for t in (await reader() or ()) if str(t).strip()]
    except Exception:
        return []


class ControlFillDriver:
    """Commits one control's value and reads the application's verdict on it."""

    __slots__ = ("_port", "_commit", "_alerts", "_reads", "_control_reads")

    def __init__(self, port: Any, commit: CommitFn, alerts: PageAlertFilter) -> None:
        self._port = port
        self._commit = commit
        self._alerts = alerts
        #: Counted so the performance claim is measured rather than asserted.
        self._reads = 0
        self._control_reads = 0

    @property
    def verdict_reads(self) -> int:
        """How many times the expensive stage-2 read actually ran."""
        return self._control_reads

    async def commit(self, control: Mapping[str, Any], value: str) -> FillVerdict:
        committed, intent_met, mechanical = await self._commit(control, value)
        if mechanical:
            return FillVerdict(accepted=False, committed=committed,
                               mechanical_failure=mechanical)
        if intent_met is False:
            # The widget did not take the value.  That is a mechanical failure,
            # not a rejection of the VALUE, and repairing the value cannot help.
            return FillVerdict(accepted=False, committed=committed,
                               mechanical_failure="intent_unmet")

        # ── stage 1: the free signals ────────────────────────────────────
        self._reads += 1
        fresh = self._alerts.fresh(await read_page_alerts(self._port))
        native = str(control.get("validation_message") or "").strip()
        suspicious = bool(fresh) or bool(native)
        if not suspicious:
            return FillVerdict(accepted=True, committed=committed)

        # ── stage 2: is the complaint about THIS control? ────────────────
        after = await self._collect()
        self._control_reads += 1
        # The message captured BEFORE the commit is not evidence about the value
        # written AFTER it. An empty `required` field already reports "Please
        # fill out this field." while it is still untouched, so carrying that
        # message forward rejected every such field at the instant it was
        # correctly filled -- and a login form is nothing but required fields,
        # which is how a crawl came to stop at the sign-in page. Only a message
        # read back after the commit can be a verdict on it, and a control that
        # has vanished from the re-read carries no verdict at all.
        native_now = ""
        for candidate in after:
            if _same(candidate, control):
                native_now = str(candidate.get("validation_message") or "").strip()
                break
        signals = signals_for_control(
            control, fresh_alerts=fresh, after_controls=after,
            native_message=native_now,
            control_name=str(control.get("name") or ""))
        if not signals:
            # A NEW page alert that anchors to nothing is page context, not a
            # verdict on this field.  This single branch is the fix for "a
            # cookie banner failed every fill on the page".
            logger.info(
                "qec.fill.page_alert_unattributed control=%r alerts=%d — the "
                "alert is recorded as page context and fails no field",
                str(control.get("name") or "")[:40], len(fresh))
            return FillVerdict(accepted=True, committed=committed)
        return FillVerdict(accepted=False, committed=committed,
                           signals=tuple(signals))

    async def _collect(self) -> list[Mapping[str, Any]]:
        collect = getattr(self._port, "collect_controls", None)
        if collect is None:
            return []
        try:
            return list(await collect() or ())
        except Exception:
            return []


def _same(candidate: Mapping[str, Any], control: Mapping[str, Any]) -> bool:
    for key in ("id", "testid", "css_hint"):
        mine = str(control.get(key) or (control.get("qec") or {}).get(key) or "")
        theirs = str(candidate.get(key) or (candidate.get("qec") or {}).get(key) or "")
        if mine.strip() and mine.strip() == theirs.strip():
            return True
    mine_name = str(control.get("name") or "").strip().lower()
    return bool(mine_name) and str(candidate.get("name") or "").strip().lower() == mine_name
