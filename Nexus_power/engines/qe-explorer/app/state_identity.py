"""State identity, observation and page-state recording (M0.3 / T-DE-06).

Extracted VERBATIM from :mod:`app.crawler`.  Three things live here because
they are one responsibility — deciding WHAT STATE WE ARE IN and writing that
state down:

  * :class:`StateFingerprinter` — the identity function, and the M1.1 seam.
  * :class:`StateRecorder`      — observe the page, emit one ``page_state``.
  * the page-state normalisers   — ``_form_snapshot`` / ``_displayed_values`` /
    ``_network_calls`` / ``_action_to_dict``, which exist only to shape a
    record and so belong beside the recorder that shapes it.

CYCLE RETIRED.  ``forms.py`` used to reach BACK into ``crawler.py`` for
``_displayed_values`` through a function-local import, with a comment naming
the cycle it was dodging.  A lazy import does not remove a cycle; it hides it.
Both modules now import that helper DOWNWARD from here, and the
``crawler -> forms -> crawler`` cycle is gone rather than deferred.

THE PHASE-1 SEAM.  :meth:`StateFingerprinter.fingerprint` accepts a
``perceptual_hash`` and threads it to the hasher.  Nothing computes one yet, so
it is always empty and every digest is bit-identical to today's.  M1.1 supplies
a real value without moving a signature or a call site.  ``None`` and ``""``
are normalised to the same request, which is what lets perceptual hashing be
switched on per-tenant without invalidating states already visited.
"""
from __future__ import annotations

import logging
from dataclasses import asdict
from typing import Any, Mapping, Optional, Protocol, Sequence
from urllib.parse import urlsplit

from . import emit
from . import value_infer
from .browser import PageObservation
from .fingerprint import state_fingerprint
from .guard import registrable_domain
from .inventory import form_signal_for

logger = logging.getLogger(__name__)

#: Bound on the diagnostics carried per state (mirrors the crawler's original).
_MAX_NETWORK_CALLS = 100
_MAX_COVERAGE_STATES = 400
_MAX_STATE_FIELDS = 200
_MAX_DANGER_NAMES = 40


# ─── Identity ────────────────────────────────────────────────────────────────


class StateFingerprinter:
    """Implements the frozen :class:`app.protocols.StateIdentity` contract."""

    def fingerprint(
        self,
        *,
        url: str,
        controls: Sequence[Mapping[str, Any]],
        dialogs: Sequence[str] = (),
        perceptual_hash: Optional[str] = None,
    ) -> str:
        """The 64-char sha256 hex identifying this page state.

        ``perceptual_hash=None`` and ``""`` are the SAME request — see the
        module docstring for why that equivalence matters to M1.1.
        """
        return state_fingerprint(url, controls, dialogs,
                                 perceptual_hash=perceptual_hash or "")


# ─── Page-state normalisers ──────────────────────────────────────────────────


def _is_password(control: dict[str, Any]) -> bool:
    it = str(control.get("input_type") or "").strip().lower()
    if not it:
        it = str((control.get("qec") or {}).get("input_type") or "").strip().lower()
    return it == "password"


def _form_snapshot(controls: Sequence[dict[str, Any]]) -> tuple[dict[str, str], dict[str, dict]]:
    """Build ``form_snapshot`` (label→scrubbed committed value) + signals."""
    snapshot: dict[str, str] = {}
    signals: dict[str, dict] = {}
    for control in controls:
        signal = form_signal_for(control)
        if signal is None:
            continue
        label = str(control.get("name") or "").strip()
        if not label:
            continue
        secret = _is_password(control)
        raw = control.get("value_committed") or ""
        snapshot[label] = emit.scrub_value(raw, is_secret=secret).value
        if secret:
            # A password input refines to kind 'text' (its password-ness lives in
            # input_type); stamp the signal so the substrate writer's redaction
            # recognises it (writer._is_password_signal reads type=='password').
            signal = {**signal, "type": "password"}
        signals[label] = signal
    return snapshot, signals


def _displayed_values(raw: Sequence[dict[str, Any]]) -> list[dict[str, str]]:
    """ANSWERS P1.B — normalize + scrub captured displayed value nodes into
    ``[{label, selector, text}]`` (deduped). The text is scrubbed like a form value
    (it may be PII-adjacent, e.g. an amount); label + selector let the value oracle
    ground an expected outcome to this rendered node without a client source_hint.

    VALUE-ORACLE INFERENCE (#2): each node is additively annotated with a
    crawl-side classification — ``value_type`` (currency/percent/decision/…),
    ``value_candidate`` (is this a likely expected OUTCOME to assert on?),
    ``value_confidence`` and ``value_reason``.  Inference ONLY — it surfaces
    candidates for confirmation; the frozen factory oracle still does the PROVING.
    The extra keys are all-string + additive (existing consumers read only
    label/selector/text via ``.get`` and ignore them)."""
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for r in raw or ():
        if not isinstance(r, dict):
            continue
        selector = str(r.get("selector") or "").strip()
        text = emit.scrub_value(str(r.get("text") or "")).value.strip()
        if not (selector and text):
            continue
        key = f"{selector}|{text}"
        if key in seen:
            continue
        seen.add(key)
        label = str(r.get("label") or "").strip()[:200]
        inferred = value_infer.infer_candidate(label, text)
        out.append({
            "label": label, "selector": selector[:300], "text": text[:200],
            "value_type": inferred["value_type"],
            "value_candidate": "true" if inferred["is_candidate"] else "false",
            "value_confidence": f"{inferred['confidence']:.2f}",
            "value_reason": inferred["reason"][:120],
        })
    return out


def _network_calls(raw: Sequence[dict[str, Any]]) -> list[dict[str, str]]:
    """API/network mining — normalize + PII-scrub captured XHR/fetch/SSE/WebSocket
    calls into ``[{method, url, has_query, status, resource_type, request_mime,
    response_mime, response_bytes, timestamp_ms}]`` (deduped, bounded, ALL-string
    values so the schema's ``dict[str, str]`` can never refuse a bundle over a
    diagnostic).

    Safety: the query string is DROPPED here regardless of what the adapter sent
    (a query param is the likeliest PII carrier — ``has_query`` preserves the
    honest fact that one existed, without its values), and the query-stripped URL
    is re-scrubbed for path-embedded PII (belt-and-suspenders over the adapter's
    source-side scrub).  http(s) API calls AND ws(s) real-time endpoints (D) are
    evidence; every other scheme is dropped."""
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for r in raw or ():
        if not isinstance(r, dict):
            continue
        parts = urlsplit(str(r.get("url") or "").strip())
        if (parts.scheme or "").lower() not in ("http", "https", "ws", "wss"):
            continue
        url = emit.scrub_value(f"{parts.scheme}://{parts.netloc}{parts.path}").value.strip()
        if not url:
            continue
        had_query = bool(parts.query) or bool(r.get("has_query"))
        method = str(r.get("method") or "").strip().upper()[:10]
        status = str(r.get("status") or "").strip()[:3]
        key = f"{method}|{url}|{status}"
        if key in seen:
            continue
        seen.add(key)
        out.append({
            "method": method,
            "url": url[:1000],
            "has_query": "true" if had_query else "false",
            "status": status,
            "resource_type": str(r.get("resource_type") or "").strip()[:20],
            "request_mime": str(r.get("request_mime") or "").strip()[:100],
            "response_mime": str(r.get("response_mime") or "").strip()[:100],
            "response_bytes": str(r.get("response_bytes") or "").strip()[:12],
            "timestamp_ms": str(r.get("timestamp_ms") or "").strip()[:15],
        })
        if len(out) >= _MAX_NETWORK_CALLS:
            break
    return out


def _action_to_dict(action: emit.ActionRecord) -> dict[str, Any]:
    return asdict(action)


# ─── Recording ───────────────────────────────────────────────────────────────


class RecorderHost(Protocol):
    """The slice of crawl state the recorder reads and writes.

    Declared HERE, by the consumer, rather than imported from the crawler: the
    recorder depends on an interface it owns, and ``Crawler`` happens to
    satisfy it.  That is the dependency inversion — and it is why this module
    never imports :mod:`app.crawler`.
    """

    _port: Any
    _clock: Any
    _emitter: Any
    _tracker: Any
    _next_seq: int
    _states: dict[str, dict[str, Any]]

    def _note_boundary_controls(self, controls: Sequence[dict[str, Any]]) -> None: ...


class StateRecorder:
    """Observes the live page and writes ONE ``page_state`` record per visit."""

    def __init__(self, host: RecorderHost) -> None:
        self._c = host

    # -- observation ----------------------------------------------------------

    async def observe(self) -> PageObservation:
        port = self._c._port
        return PageObservation(
            url=await port.current_url(),
            title=await port.title(),
            raw_controls=await port.collect_controls(),
            dialog_flags=await port.dialog_flags(),
            error_texts=await port.error_texts(),
        )

    # -- the states index (what each state ASKED) -----------------------------

    def note_state_signals(
        self, fingerprint: str, url: str, signals: Mapping[str, Any],
        controls: Sequence[Mapping[str, Any]] = (),
    ) -> None:
        """Remember WHAT THIS STATE ASKED, keyed by the fingerprint the journey
        graph uses.

        THE HOLE THIS FILLS, and it is a producer that was never written rather
        than a consumer that broke. qe-central folds a crawl by looking each
        journey step's fingerprint up in ``build_states_index(coverage)``, which
        reads ``coverage.states``. Nothing has ever emitted that key. The index
        was therefore ``{}`` on every crawl of every application, so
        ``journey_nodes.controls_inventory`` was never written, and the Master
        Catalog could only be fed by journey BRANCHES — which carry choices.

        Live consequence, measured on a five-step insurance application whose
        walk completed end to end: 24 catalogued questions, every one of them a
        choice (gender, product, premium mode, tobacco use, the health
        conditions), and not one text, date or number field. ``faceAmount`` — a
        number input declaring ``step=10000``, the clearest boundary rule on the
        form — was absent, so no boundary scenario could be derived from it. The
        walk was fine; the catalogue it fed was starved.

        VALUE-FREE BY CONSTRUCTION. Only ``form_snapshot_signals`` is carried:
        the label, the control type, the options offered, whether it is required.
        The sibling ``form_snapshot`` — label to committed VALUE — is deliberately
        NOT carried. Shapes cross the boundary; answers never do.

        Richest sighting wins. The same state is recorded more than once (a
        wizard step is met on entry and again mid-walk), and a dependent question
        offers nothing until its driver is answered — so keeping the first
        sighting would hold the emptiest view of exactly the questions whose
        enumeration is hardest to get.
        """
        if not fingerprint:
            return
        if not isinstance(signals, Mapping):
            signals = {}
        # A page that asks nothing can still REFUSE everything, and the page that
        # most needed the danger ratio — a hub whose only controls are links —
        # has no form fields at all. Recorded when it has questions OR controls.
        if not signals and not controls:
            return
        states = self._c._states
        prev = states.get(fingerprint)
        if prev is not None and len(prev.get("form_snapshot_signals") or {}) >= len(signals):
            return
        if prev is None and len(states) >= _MAX_COVERAGE_STATES:
            return                      # bounded: coverage is a report, not a mirror
        # HOW MUCH OF THIS PAGE THE CRAWL REFUSED TO TOUCH. A refuse rule that
        # matches too widely does not fail — it quietly flags ordinary controls
        # as dangerous, the walk skips them, and the funnel narrows for a reason
        # no number reports. That is exactly what happened when a URL-scoped
        # `underwrite` rule was matched against the PAGE's url instead of the
        # control's destination: 20 of 35 controls on the hub went critical, the
        # wizard was never entered, and it cost an investigation to find. As a
        # recorded ratio it is a gate assertion instead.
        danger_names = [str(c.get("name") or "").strip()[:120]
                        for c in controls
                        if isinstance(c, Mapping) and c.get("danger")
                        and str(c.get("name") or "").strip()]
        danger = sum(1 for c in controls if isinstance(c, Mapping) and c.get("danger"))
        states[fingerprint] = {
            "ax_fingerprint": fingerprint,
            "location": (url or "")[:2000],
            "form_snapshot_signals": {
                str(k)[:300]: v for k, v in list(signals.items())[:_MAX_STATE_FIELDS]
            },
            "controls_total": len(controls),
            "danger_controls": danger,
            # WHICH controls were refused, not just how many. A ratio catches a
            # rule that went broad; only the names catch a rule that took out
            # the ONE control a funnel depends on — live, `New Application`,
            # the single door into the wizard. Product UI text, never user data.
            "danger_names": danger_names[:_MAX_DANGER_NAMES],
        }

    def state_signals(self) -> list[dict[str, Any]]:
        """The states index as coverage carries it, in first-sighting order."""
        return list(self._c._states.values())

    # -- the page_state record ------------------------------------------------

    def record_state(
        self,
        *,
        url: str,
        title: str,
        controls: Sequence[dict[str, Any]],
        fingerprint: str,
        actions: Sequence[emit.ActionRecord],
        screenshots: Sequence[tuple[bytes, int]],
        first_seen_ms: Optional[int] = None,
        last_seen_ms: Optional[int] = None,
        displayed_values: Sequence[dict[str, Any]] = (),
        network_calls: Sequence[dict[str, Any]] = (),
    ) -> None:
        """Assemble + emit ONE ``page_state`` record with monotonic indices."""
        c = self._c
        # EVERY recorded state, whichever path reached it. Hung off _expand alone,
        # this missed every page the WIZARD WALK reaches — which is precisely where
        # a funnel ends. Live-observed: the quote-summary page rendered `Apply Now`,
        # `Start Over` and `Back to Dashboard` (confirmed in the crawl's own
        # screenshot) and contributed no boundary control at all, while the same fix
        # captured `Add Beneficiary` on a page reached by ordinary navigation.
        c._note_boundary_controls(controls)
        seq = c._next_seq
        c._next_seq += 1
        parts = urlsplit(url or "")
        host = (parts.hostname or "").lower()

        form_snapshot, form_signals = _form_snapshot(controls)

        shot_records: list[dict[str, Any]] = []
        first = first_seen_ms if first_seen_ms is not None else (
            min((ts for _, ts in screenshots), default=c._clock.now_ms()))
        last = last_seen_ms if last_seen_ms is not None else c._clock.now_ms()
        for png, ts in screenshots:
            # clamp the screenshot timestamp inside the visit window (the
            # factory's frame-window join requires it — schema
            # screenshot_outside_visit_window rule).
            clamped = min(max(int(ts), first), last)
            try:
                rec = c._emitter.store_screenshot(png, clamped)
                shot_records.append({"frame_index": rec.frame_index,
                                     "timestamp_ms": rec.timestamp_ms,
                                     "path": rec.path})
            except ValueError:
                logger.warning("qec.crawler.empty_screenshot_skipped seq=%d", seq)

        ordered_actions: list[dict[str, Any]] = []
        for i, action in enumerate(actions):
            action.subaction_index = i
            action.state_id = fingerprint
            ordered_actions.append(_action_to_dict(action))

        self.note_state_signals(fingerprint, url, form_signals, controls)
        record = emit.PageStateRecord(
            sequence_index=seq,
            location=url[:2000],
            first_seen_ms=first,
            last_seen_ms=max(first, last),
            title=(title or "")[:500],
            url_host=host[:500],
            url_path=(parts.path or "")[:2000],
            url_query=(parts.query or "")[:2000],
            canonical_host=(registrable_domain(host) or host)[:500],
            form_snapshot=form_snapshot,
            form_snapshot_signals=form_signals,
            displayed_values=_displayed_values(displayed_values),
            network_calls=_network_calls(network_calls),
            actions=ordered_actions,
            screenshots=shot_records,
            state_id=fingerprint,
            ax_fingerprint=fingerprint,
        )
        c._emitter.emit_page_state(record)
        c._tracker.note_state()


__all__ = ["StateFingerprinter", "StateRecorder", "RecorderHost",
           "_action_to_dict", "_displayed_values", "_form_snapshot",
           "_is_password", "_network_calls"]
