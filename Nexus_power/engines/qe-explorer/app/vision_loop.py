"""M3.1 / T-VIS-01 — THE VISION ESCALATION LOOP.

    opaque/sparse UI
        -> should_perceive          (is vision justified at all?)
        -> capture                  (screenshot + PII redaction, fail-closed)
        -> perceive                 (the VLM enumerates what it can see)
        -> synthesize controls      (perception -> ControlRecord shapes)
        -> screen                   (is this control allowed to be actuated?)
        -> coordinate action        (click_at)
        -> R0 verification          (measured, never claimed)
        -> record ONLY if verified
        -> catalog

THE LAW THIS MODULE ENFORCES
============================
**A vision prediction is never catalog truth.**  What the model says is a
HYPOTHESIS about where a control is.  The only thing that can turn it into
evidence is an action this process performed and an outcome this process
measured.  So every perceived control leaves here on exactly one of two paths:

  * ``VERIFIED``   — it was clicked at its proposed coordinate and the page
    demonstrably responded.  It is returned in ``promoted`` and the caller folds
    it into the state's control inventory, from where it reaches the catalogue
    by the same route every DOM control does.
  * ``REFUSED``    — everything else.  It is recorded in the vision ledger with
    the reason, and it is returned in NOTHING the catalogue reads.  It is not a
    locator, not a journey step, not a control, not a coverage claim.

The refused half is written down on purpose (T-VIS-01 safeguard, and D-8 in the
milestone doc).  A wrong perception that leaves no trace is indistinguishable
from a perception that never happened, and the difference is exactly what an
operator needs in order to stop trusting a model.

R0 HAS TWO RUNGS, AND THE SECOND ONE IS EARNED
==============================================
``browser.verify_intent("click", …)`` returns ``True`` on a URL change, a DOM
change or a dialog.  On a genuine WebGL application NONE of those happen: the
canvas repaints and the DOM is byte-identical, so R0 returns ``None`` and — under
rung 1 alone — no canvas control could EVER be verified.  That would not be
safety, it would be a capability that cannot exist.

Rung 2 is therefore a PERCEPTUAL R0: the pixels changed.  It is admitted only
under a condition that makes it causal rather than coincidental — the surface
must first be proven STILL.  Two screenshots are taken before the click, one
settle apart; if they already differ the surface is animating, the pixel rung is
declared INADMISSIBLE for this state, and the control can only be verified by
rung 1.  An animated canvas therefore cannot manufacture verifications, which is
the failure mode a naive "did the pixels change?" check has.

Both rungs are MEASUREMENTS.  Neither asks the model whether it was right.

``None`` FROM R0 IS A REFUSAL, NOT A PASS
=========================================
``verify_intent`` distinguishes "provably unmet" (``False``) from "unverifiable"
(``None``), and elsewhere in this engine ``None`` preserves a fill because we
cannot prove it failed.  Here the polarity is inverted, deliberately: a DOM fill
has a read-back and a locator behind it, while a vision control has a model's
guess behind it.  Unverifiable evidence from an unverifiable source is not
evidence.  Only ``True`` — on either rung — promotes.

The loop takes its browser port, its oracle, its budget and its screening
function by injection, so the whole thing runs in a unit test with no browser.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Optional, Sequence

from . import perception
from . import vision_gate
from .pixel_redaction import redact_screenshot

logger = logging.getLogger(__name__)

# ── Outcome vocabulary.  VERIFIED is the ONLY value that promotes. ────────────

VERIFIED = "verified"
#: The coordinate click happened and the page did not demonstrably respond.
REFUSED_UNVERIFIED = "refused_unverified"
#: The click itself errored (bad coordinates, detached page, navigation blocked).
REFUSED_ACTION_ERROR = "refused_action_error"
#: The screening function refused to actuate this label (danger / commit shape).
REFUSED_NOT_ALLOWED = "refused_not_allowed"
#: The perception carried no usable click point.
REFUSED_NO_COORDINATE = "refused_no_coordinate"
#: The port cannot perform a coordinate action at all.
REFUSED_NO_COORDINATE_RUNG = "refused_no_coordinate_rung"

# ── Why an escalation did not happen at all. ─────────────────────────────────

SKIPPED_NOT_JUSTIFIED = "not_justified"
SKIPPED_BUDGET = "budget"
SKIPPED_REDACTION = "redaction_refused"
SKIPPED_NO_SCREENSHOT = "no_screenshot"
SKIPPED_NOTHING_PERCEIVED = "nothing_perceived"
SKIPPED_ORACLE_ERROR = "oracle_error"

#: R0 rungs, recorded on every verdict so an auditor can see WHICH measurement
#: promoted a control rather than only that something did.
R0_DOM = "dom"
R0_PIXEL = "pixel_stable_surface"
R0_NONE = ""


@dataclass
class VisionAttempt:
    """One perceived control and what actually happened to it."""

    label: str
    role: str
    signature: str
    click_x: Optional[int]
    click_y: Optional[int]
    status: str
    reason: str = ""
    r0_rung: str = R0_NONE
    url: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"label": self.label, "role": self.role, "signature": self.signature,
                "click_x": self.click_x, "click_y": self.click_y,
                "status": self.status, "reason": self.reason,
                "r0_rung": self.r0_rung, "url": self.url}


@dataclass
class VisionResult:
    """The outcome of one escalation on one state.

    ``promoted`` is the ONLY field a catalogue-feeding caller may read.
    """

    ran: bool = False
    skipped_reason: str = ""
    perceived: int = 0
    #: R0-VERIFIED synthetic controls, ready to join the state's inventory.
    promoted: list[dict[str, Any]] = field(default_factory=list)
    #: Every attempt, verified or refused — the audit trail.
    attempts: list[VisionAttempt] = field(default_factory=list)
    #: Displayed outcome values the perceiver read from pixels.  Carried ONLY
    #: when at least one control on this state was verified: a page whose
    #: perception could not be proven has not earned the right to contribute
    #: premium/decision figures to a journey's outcome evidence either.
    outcomes: list[dict[str, Any]] = field(default_factory=list)
    #: The perceptual hash of the state as it was captured, so the caller can
    #: feed identity (T-VIS-02) without paying for a second screenshot.
    perceptual_hash: str = ""
    #: False when the surface was repainting on its own, so the pixel R0 rung
    #: was refused for this state.  Recorded, not hidden.
    pixel_rung_admissible: bool = False

    @property
    def verified(self) -> int:
        return sum(1 for a in self.attempts if a.status == VERIFIED)

    @property
    def refused(self) -> int:
        return sum(1 for a in self.attempts if a.status != VERIFIED)

    def as_ledger(self) -> dict[str, Any]:
        return {
            "ran": self.ran,
            "skipped_reason": self.skipped_reason,
            "perceived": self.perceived,
            "verified": self.verified,
            "refused": self.refused,
            "pixel_rung_admissible": self.pixel_rung_admissible,
            "attempts": [a.as_dict() for a in self.attempts],
        }


def _click_point(control: Mapping[str, Any]) -> tuple[Optional[int], Optional[int]]:
    qec = control.get("qec") or {}
    try:
        x, y = qec.get("click_x"), qec.get("click_y")
        if x is None or y is None:
            return None, None
        return int(x), int(y)
    except (TypeError, ValueError):
        return None, None


#: ARIA role (what the perceiver reports it SAW) -> the ControlRecord ``kind``
#: the rest of the engine classifies on.
#:
#: WHY THIS MAPPING IS HERE AND NOT IN ``perception``.
#: ``synthesize_vision_controls`` stamps ``kind:"button"`` on every perceived
#: control, discarding the ``role`` it was just handed.  Downstream that is not
#: cosmetic: ``inventory.form_signal_for`` emits a form signal for a value-bearing
#: kind and ``None`` for a button, and ``form_snapshot_signals`` is the ONLY
#: payload qe-central's catalogue reads a question from.  So a canvas-rendered
#: TEXT FIELD — verified by a coordinate click and a measured response — could
#: never become a catalogue question, no matter how well it was proven.
#:
#: Applied here rather than in ``perception`` because the mapping is a
#: CONSEQUENCE of what R0 proved: only a control this loop verified is entitled
#: to a kind that carries it into the catalogue.  An unverified perception keeps
#: whatever the synthesizer gave it and reaches nothing.
_KIND_BY_ROLE = {
    "textbox": "text", "searchbox": "text", "spinbutton": "text",
    "combobox": "select", "listbox": "select",
    "checkbox": "checkbox", "radio": "radio", "switch": "toggle",
    "button": "button", "link": "link", "tab": "button", "menuitem": "button",
}


def kind_for_role(role: Any) -> str:
    """The ControlRecord kind a perceived ``role`` maps to.  Unknown → button.

    ``button`` is the fail-closed default: it is the kind that contributes NO
    catalogue question, so a role this engine does not understand cannot invent
    one.
    """
    return _KIND_BY_ROLE.get(str(role or "").strip().lower(), "button")


def default_screen(control: Mapping[str, Any]) -> tuple[bool, str]:
    """The floor screening rule when a caller injects none.

    Fail-closed on a consequential label.  A canvas button reading "Submit
    Application" is exactly as irreversible as a DOM one, and the A4.3 boundary
    doctrine does not stop applying because the pixels were painted rather than
    marked up.
    """
    from . import danger_signals

    name = str(control.get("name") or "").strip()
    if not name:
        # An unnamed coordinate is a click into an unknown, on a surface whose
        # effects we cannot read. It is refused, and it is ledgered as refused.
        return False, "unnamed perceived control"
    if danger_signals.is_consequential(name):
        return False, "consequential label — needs an operator approval, not a guess"
    return True, ""


class VisionEscalation:
    """The loop.  One instance per crawl; ``run`` is called per state."""

    def __init__(
        self,
        *,
        port: Any,
        oracle: Any,
        budget: vision_gate.VisionBudget,
        clock: Any = None,
        screen: Callable[[Mapping[str, Any]], tuple[bool, str]] = default_screen,
        max_actions_per_state: int = 2,
    ) -> None:
        self._port = port
        self._oracle = oracle
        self._budget = budget
        self._clock = clock
        self._screen = screen
        self._max_actions = max(0, int(max_actions_per_state))

    # -- capture ---------------------------------------------------------------

    async def _capture_redacted(self) -> tuple[Optional[Any], str]:
        """``(masked screenshot, "")`` or ``(None, reason)`` — never an original.

        The reason is returned rather than logged-and-forgotten because "the
        browser produced no image" and "we refused to send the image we have"
        are different findings, and a crawl that made no vision call has to be
        able to say which one it hit.

        The region read and the capture are both required to succeed.  A port
        without ``collect_pii_regions`` returns ``ok=False`` and this refuses,
        which is the intended asymmetry: every other optional verb on the port
        degrades to "nothing found", and this one degrades to "do not send".
        """
        png = b""
        try:
            png = await self._port.screenshot_png()
        except Exception as exc:
            logger.warning("qec.vision.screenshot_failed err=%s", str(exc)[:160])
            return None, SKIPPED_NO_SCREENSHOT
        if not png:
            return None, SKIPPED_NO_SCREENSHOT
        collect = getattr(self._port, "collect_pii_regions", None)
        if collect is None:
            logger.warning(
                "qec.vision.redaction_refused reason=port_cannot_locate_pii — "
                "this port cannot say where sensitive pixels are, so the "
                "screenshot is not sent")
            return None, SKIPPED_REDACTION
        try:
            probe = await collect() or {}
        except Exception as exc:
            logger.warning("qec.vision.pii_regions_failed err=%s", str(exc)[:160])
            return None, SKIPPED_REDACTION
        shot = redact_screenshot(
            png, list(probe.get("regions") or []),
            page_w=probe.get("page_w") or 0, page_h=probe.get("page_h") or 0,
            regions_ok=bool(probe.get("ok")),
        )
        return (shot, "") if shot is not None else (None, SKIPPED_REDACTION)

    async def _phash(self) -> str:
        try:
            return perception.perceptual_hash_png(await self._port.screenshot_png() or b"")
        except Exception:
            return ""

    # -- the loop --------------------------------------------------------------

    async def run(
        self,
        *,
        url: str,
        controls: Sequence[Mapping[str, Any]],
        opaque_surfaces: Sequence[Mapping[str, Any]],
        act: bool = True,
    ) -> VisionResult:
        """Escalate to vision on ONE state and return what was PROVEN.

        ``act=False`` runs perception without the coordinate rung — used when the
        crawl posture forbids interaction (observe-only).  Nothing is promoted in
        that mode, because nothing was verified; the perceptions are still
        ledgered as refused-unverified, which is what they are.
        """
        result = VisionResult()

        # 1 — IS VISION JUSTIFIED?  A page the DOM already explains never costs a
        #     vision call, however loudly it renders a canvas.
        if not perception.should_perceive(controls, opaque_surfaces):
            result.skipped_reason = SKIPPED_NOT_JUSTIFIED
            return result

        # 2 — CAPTURE + REDACT, before spending anything.  The budget is for MODEL
        #     calls; a capture that cannot be masked must not burn one.
        shot, why_capture = await self._capture_redacted()
        if shot is None:
            result.skipped_reason = why_capture
            logger.warning("qec.vision.escalation_refused url=%s reason=%s",
                           (url or "")[:120], why_capture)
            return result
        result.perceptual_hash = perception.perceptual_hash_png(shot.png)

        # 3 — CLAIM THE BUDGET (T-VIS-03).  The only door to a vision call.
        allowed, why = self._budget.try_spend()
        if not allowed:
            result.skipped_reason = SKIPPED_BUDGET + ":" + why
            logger.info("qec.vision.budget_refused url=%s reason=%s",
                        (url or "")[:120], why)
            return result

        # 4 — PERCEIVE.  The receipt travels WITH the image so the server can
        #     enforce redaction instead of trusting that it happened.
        started = self._now_ms()
        try:
            perceived = await self._oracle.perceive(
                shot.b64(), {"url": url, "pixel_redaction": shot.receipt()}) or {}
        except Exception as exc:
            self._budget.note_failure(self._now_ms() - started)
            result.skipped_reason = SKIPPED_ORACLE_ERROR
            logger.warning("qec.vision.perceive_failed url=%s err=%s",
                           (url or "")[:120], str(exc)[:160])
            return result
        raw_controls = list(perceived.get("controls") or [])
        if not raw_controls:
            # An honest empty answer is a SUCCESS of the call, not a failure of
            # the provider — it must not push the breaker toward opening.
            self._budget.note_success(self._now_ms() - started)
            result.ran = True
            result.skipped_reason = SKIPPED_NOTHING_PERCEIVED
            return result
        self._budget.note_success(self._now_ms() - started)
        result.ran = True

        synthesized = perception.synthesize_vision_controls(
            raw_controls, page_w=shot.page_w, page_h=shot.page_h)
        result.perceived = len(synthesized)
        logger.info("qec.vision.perceived url=%s controls=%d",
                    (url or "")[:120], len(synthesized))

        if not act:
            for c in synthesized:
                result.attempts.append(self._attempt(
                    c, REFUSED_UNVERIFIED,
                    "observe-only posture — no coordinate action was performed",
                    url))
            return result

        click_at = getattr(self._port, "click_at", None)
        if click_at is None:
            for c in synthesized:
                result.attempts.append(self._attempt(
                    c, REFUSED_NO_COORDINATE_RUNG,
                    "this port has no coordinate rung", url))
            return result

        # 5 — IS THE PIXEL R0 RUNG ADMISSIBLE HERE?  Prove the surface is still
        #     BEFORE acting, or a repaint will verify everything we click.
        still_a = await self._phash()
        still_b = await self._phash()
        result.pixel_rung_admissible = bool(still_a and still_b and still_a == still_b)
        if not result.pixel_rung_admissible:
            logger.info(
                "qec.vision.pixel_rung_inadmissible url=%s — the surface repaints "
                "on its own, so a post-click pixel change proves nothing",
                (url or "")[:120])
        baseline = still_b

        # 6 — ACT + VERIFY, control by control, bounded.
        acted = 0
        for control in synthesized:
            if acted >= self._max_actions:
                result.attempts.append(self._attempt(
                    control, REFUSED_UNVERIFIED,
                    "per-state coordinate-action budget spent", url))
                continue
            ok, why = self._screen(control)
            if not ok:
                result.attempts.append(
                    self._attempt(control, REFUSED_NOT_ALLOWED, why, url))
                continue
            x, y = _click_point(control)
            if x is None or y is None:
                result.attempts.append(self._attempt(
                    control, REFUSED_NO_COORDINATE,
                    "perception carried no click point", url))
                continue
            acted += 1
            try:
                obs = await click_at(x, y)
            except Exception as exc:
                result.attempts.append(self._attempt(
                    control, REFUSED_ACTION_ERROR, str(exc)[:160], url))
                continue
            verdict, rung, reason = await self._verify(obs, baseline,
                                                       result.pixel_rung_admissible)
            if verdict:
                promoted = dict(control)
                qec = dict(promoted.get("qec") or {})
                qec["r0_verified"] = True
                qec["r0_rung"] = rung
                promoted["qec"] = qec
                # The verified control is entitled to the KIND its perceived role
                # implies — which is what carries a canvas-rendered text field
                # into the catalogue as a question rather than dropping it as a
                # button. See ``_KIND_BY_ROLE``.
                promoted["kind"] = kind_for_role(promoted.get("role"))
                result.promoted.append(promoted)
                result.attempts.append(
                    self._attempt(control, VERIFIED, reason, url, rung))
                logger.info("qec.vision.control_verified url=%s label=%r rung=%s",
                            (url or "")[:120],
                            str(control.get("name") or "")[:60], rung)
            else:
                result.attempts.append(
                    self._attempt(control, REFUSED_UNVERIFIED, reason, url))
                logger.info(
                    "qec.vision.control_refused url=%s label=%r reason=%s — the "
                    "perception is NOT catalogued", (url or "")[:120],
                    str(control.get("name") or "")[:60], reason)
            # The page may have moved on; re-baseline so the NEXT control is
            # judged against what is on screen now, not against the entry state.
            if result.pixel_rung_admissible:
                baseline = await self._phash()

        # 7 — OUTCOMES ride on the same proof as the controls.
        if result.promoted:
            result.outcomes = perception.synthesize_vision_outcomes(
                perceived.get("displayed_values") or [])
        return result

    # -- R0 --------------------------------------------------------------------

    async def _verify(self, obs: Any, baseline: str,
                      pixel_admissible: bool) -> tuple[bool, str, str]:
        """``(verified, rung, reason)`` — measured, never asserted."""
        detail = str(getattr(obs, "error_detail", "") or "")
        if detail:
            return False, R0_NONE, "action error: " + detail[:120]
        if getattr(obs, "intent_met", None) is True:
            return True, R0_DOM, "the page responded (url / DOM / dialog)"
        if not pixel_admissible:
            return False, R0_NONE, (
                "R0 unverified: the DOM did not respond and the surface was "
                "already repainting, so pixels cannot prove causation")
        after = await self._phash()
        if after and baseline and after != baseline:
            return True, R0_PIXEL, "a still surface repainted in response to the click"
        return False, R0_NONE, (
            "R0 unverified: neither the DOM nor the pixels changed")

    # -- helpers ---------------------------------------------------------------

    def _attempt(self, control: Mapping[str, Any], status: str, reason: str,
                 url: str, rung: str = R0_NONE) -> VisionAttempt:
        x, y = _click_point(control)
        qec = control.get("qec") or {}
        return VisionAttempt(
            label=str(control.get("name") or "")[:160],
            role=str(control.get("role") or "")[:40],
            signature=str(qec.get("signature") or ""),
            click_x=x, click_y=y, status=status, reason=str(reason or "")[:300],
            r0_rung=rung, url=(url or "")[:300],
        )

    def _now_ms(self) -> int:
        try:
            return int(self._clock.now_ms())
        except Exception:
            return 0


__all__ = [
    "kind_for_role",
    "VERIFIED", "REFUSED_UNVERIFIED", "REFUSED_ACTION_ERROR",
    "REFUSED_NOT_ALLOWED", "REFUSED_NO_COORDINATE", "REFUSED_NO_COORDINATE_RUNG",
    "SKIPPED_NOT_JUSTIFIED", "SKIPPED_BUDGET", "SKIPPED_REDACTION",
    "SKIPPED_NO_SCREENSHOT", "SKIPPED_NOTHING_PERCEIVED", "SKIPPED_ORACLE_ERROR",
    "R0_DOM", "R0_PIXEL", "R0_NONE",
    "VisionAttempt", "VisionResult", "VisionEscalation", "default_screen",
]
