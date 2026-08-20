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

import hashlib
import logging
from dataclasses import asdict, dataclass
from typing import Any, Mapping, Optional, Protocol, Sequence
from urllib.parse import urlsplit

from . import emit
from . import network_evidence as net_evidence
from . import observation_health
from . import value_infer
from .browser import PageObservation
from .fingerprint import state_fingerprint
from .guard import registrable_domain
from .inventory import form_signal_for, question_groups_of

logger = logging.getLogger(__name__)

#: Bound on the diagnostics carried per state (mirrors the crawler's original).
_MAX_NETWORK_CALLS = 100
_MAX_COVERAGE_STATES = 400
_MAX_STATE_FIELDS = 200
_MAX_DANGER_NAMES = 40
#: M2.4 / T-GEN-03 — how many distinct (method, path) endpoints one state may
#: carry into ``coverage.states``. The full per-visit list already rides the
#: page_state record; this is the DEDUPED, value-free shape the journey graph
#: needs to build a network assertion, so the bound is far below
#: ``_MAX_NETWORK_CALLS``.
_MAX_STATE_ENDPOINTS = 24
#: Bound on the declared QUESTION groups carried per state. Sized well above the
#: 20-question health questionnaire that is the worst real case; coverage is a
#: report, not a mirror.
_MAX_QUESTION_GROUPS = 120


# ─── Identity ────────────────────────────────────────────────────────────────


#: Controls whose DECLARED grouping is meaningful (a question, not a lone toggle).
#: Mirrors ``groupKeyOf``: only radios and checkboxes ever carry a ``group_key``.
_GROUPED_KINDS = frozenset({"radio", "checkbox"})


def structural_signature(controls: Sequence[Mapping[str, Any]]) -> str:
    """T-SI-01/T-SI-03: a digest of WHICH QUESTIONS this page asks, ignoring what
    they are called and what was answered.

    THE SAME-SHAPE PROBLEM, STATED EXACTLY.  A one-question-at-a-time health
    questionnaire renders ``radio "Yes" / radio "No" / button "Continue"`` at
    every one of its twenty steps, from one URL, with no dialog.  All three
    signals :func:`app.fingerprint.state_fingerprint` hashes are therefore
    constant, and twenty logical steps collapse to ONE digest — measured, not
    supposed: 20 steps produced 1 fingerprint before this function existed.

    The DOM does distinguish them, just not through any signal the base
    fingerprint reads.  Question 3's radios declare ``name="q03"`` and question
    17's declare ``name="q17"``; ``groupKeyOf`` already lifts that into
    ``group_key`` (``name:doc:q03``) and ``build_inventory`` already hashes it
    into ``group_id``.  Both are pure DOM STRUCTURE — which question is on
    screen — and neither can carry a user value, an ordinal, a clock or a
    counter.  The discriminator was already being captured; nothing read it.

    Identity per control is ``group_id`` when the inventory stamped one (it
    folds in the frame selector, so the same question in two iframes stays two
    questions) and the raw ``group_key`` otherwise.  Distinct keys are sorted,
    so DOM order never moves the digest.

    Returns "" when the page declares no grouping at all — a page with no
    radio/checkbox question has nothing structural to say, and an empty return
    keeps it OUT of the payload so its digest is unchanged.  That "" is the
    honest answer, not a failure: it hands the decision to the next rung
    (perceptual) rather than inventing a difference.
    """
    keys: set[str] = set()
    for control in controls or ():
        if not isinstance(control, Mapping):
            continue
        if str(control.get("kind") or "").strip().lower() not in _GROUPED_KINDS:
            continue
        key = str(control.get("group_id") or "").strip()
        if not key:
            key = str(control.get("group_key") or "").strip()
        if key:
            keys.add(key[:120])
    if not keys:
        return ""
    basis = "\x1f".join(sorted(keys))
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:32]


@dataclass(frozen=True)
class StepSignals:
    """Everything OBSERVED about one page beyond ``url`` + controls + dialogs.

    Frozen, hashable, all-scalar: it is compared with ``==`` on the hot path and
    never mutated.  Every field is value-free — a structure digest, a coarse
    pixel hash and a set of ``kind:name`` control identities.  No timestamp, no
    counter and no user input can reach it.
    """

    #: The DOM-only digest (url + interactive controls + dialogs) — the
    #: historical fingerprint. Carried so the ladder can compare BASE-to-BASE.
    #: Comparing a new base against the previous COMPOSITE is the bug this field
    #: exists to prevent: once step 2 earns a composite digest, every later step
    #: looks "different from the previous fingerprint" at rung 1 and is handed
    #: back the collapsed base — so a 20-step wizard re-collapsed from step 3 on.
    base: str = ""
    structural: str = ""
    perceptual: str = ""
    revealed: tuple[str, ...] = ()

    def is_empty(self) -> bool:
        """True when nothing beyond the base signals was observed."""
        return not (self.structural or self.perceptual or self.revealed)


class CorruptObservationError(RuntimeError):
    """Raised when something asks for the identity of a page that was never read.

    M1.7 / T-GW-01.  Carries no recovery semantics of its own — the crawl loop
    decides what to do (retry once, then terminate as ``inventory_failed``).  It
    exists so that "we could not read the page" cannot be answered with a
    perfectly well-formed 64-character hex string.
    """


class StateFingerprinter:
    """Implements the frozen :class:`app.protocols.StateIdentity` contract.

    STATEFUL FOR EXACTLY ONE REASON (M1.5 / T-ND-04).  It remembers which PAGE
    each base digest was first seen on, so a popup that renders the same shape
    at the same URL as its opener does not inherit the opener's identity.  See
    :meth:`fingerprint`.  One instance per crawl, owned by the crawler.
    """

    #: Bound on the base-digest → first-page map.  A crawl that visits more
    #: distinct states than this stops discriminating by page, which degrades to
    #: exactly the pre-M1.5 behaviour rather than to a memory leak.
    _MAX_PAGE_CLAIMS = 4000

    __slots__ = ("_page_claims",)

    def __init__(self) -> None:
        self._page_claims: dict[str, str] = {}

    def fingerprint(
        self,
        *,
        url: str,
        controls: Sequence[Mapping[str, Any]],
        dialogs: Sequence[str] = (),
        perceptual_hash: Optional[str] = None,
        structural_hash: Optional[str] = None,
        revealed_delta: Sequence[str] = (),
        step_ordinal: int = 0,
        page_token: str = "",
        observation_ok: bool = True,
    ) -> str:
        """The 64-char sha256 hex identifying this page state.

        ``observation_ok=False`` RAISES :class:`CorruptObservationError` (M1.7 /
        T-GW-01).  This is the choke point, and it is a hard raise rather than a
        sentinel return on purpose: an identity minted from a failed read is the
        first link of the whole green-wash chain, and a caller that forgets to
        check a returned sentinel re-opens it silently.  A raise cannot be
        forgotten.  The default is ``True`` so every historical call site — the
        auth flow, the ladder, the wizard walk, every test — behaves byte for
        byte as before; only the paths that hold a health-carrying observation
        pass the flag, and those are precisely the paths that record states.

        ``perceptual_hash=None`` and ``""`` are the SAME request — see the
        module docstring for why that equivalence matters to M1.1.  The three
        signals added for T-SI-01 behave the same way: omitted or empty means
        "not observed", and the digest is then the historical one exactly.

        ``page_token`` (M1.5 / T-ND-04) IS NOT AN UNCONDITIONAL INPUT, and that
        is the whole design.  The milestone asks for two things that pull in
        opposite directions:

          * identity must FOLLOW the adopted page — a popup must not be handed
            the stale opener's fingerprint;
          * identity must not FRACTURE — a page must not mint a new identity
            merely because the Playwright object behind it changed.

        Folding the token into every digest would satisfy the first and violate
        the second (and would move every fingerprint ever persisted).  So the
        token is admitted on exactly one condition: **this base digest was first
        seen on a DIFFERENT page**.  The consequences, all of them:

        =====================================  =========================
        situation                               identity
        =====================================  =========================
        single-page crawl (token always "")     unchanged, byte for byte
        popup with a different URL/shape        already distinct — no token
        popup identical in shape AND url        DISTINCT (token folded in)
        adopted page revisiting its own state   same identity (same claim)
        opener revisiting its own state         same identity (claim is "")
        =====================================  =========================

        The claim map is what makes it conditional; without it this would be a
        counter, and a counter manufactures distinctness (see the warning on
        ``step_ordinal``).
        """
        if not observation_ok:
            raise CorruptObservationError(
                "refusing to fingerprint a page whose inventory read failed "
                "(url=%s): an identity minted from a failed observation is "
                "indistinguishable from one minted from an empty page, and that "
                "is what lets a crashed crawl report itself complete"
                % (str(url or "?")[:200])
            )

        base = state_fingerprint(
            url, controls, dialogs,
            perceptual_hash=perceptual_hash or "",
            structural_hash=structural_hash or "",
            revealed_delta=tuple(revealed_delta or ()),
            step_ordinal=int(step_ordinal or 0),
        )
        token = str(page_token or "")
        claimed = self._page_claims.get(base)
        if claimed is None:
            if len(self._page_claims) < self._MAX_PAGE_CLAIMS:
                self._page_claims[base] = token
            return base
        if claimed == token:
            return base
        # A DIFFERENT page produces the identical DOM digest. Re-hash WITH the
        # page identity so the two states stay two states.
        return state_fingerprint(
            url, controls, dialogs,
            perceptual_hash=perceptual_hash or "",
            structural_hash=structural_hash or "",
            revealed_delta=tuple(revealed_delta or ()),
            step_ordinal=int(step_ordinal or 0),
            page_token=token,
        )


class WalkIdentity:
    """The identity POLICY for one journey — which signals a state is entitled to.

    WHY THIS EXISTS AND WHY IT IS NOT A FUNCTION.  Deciding whether the DOM
    alone identifies a page needs TWO observations: this one and the one before
    it.  ``state_fingerprint`` sees one, so it can only ever hash what it is
    given; the choice of what to give it lives here, where the previous step is
    still in hand.

    THE LADDER, cheapest and most trustworthy first:

      1. **Base differs** (url template, interactive controls, or dialogs) —
         the DOM already identifies the page.  Emit the HISTORICAL digest with
         no extra signal, so every state the crawler has ever recorded still
         hashes to the value it hashed before.  This is the overwhelmingly
         common path and it costs one hash.
      2. **Base collapses, structure differs** — same Yes/No/Continue, different
         declared question (``name="q03"`` -> ``name="q04"``).  DOM-native,
         free, deterministic.  Admit the structural digest.
      3. **Base and structure collapse, an answer revealed something** — the
         trigger->child delta the walk already computes.  Admit it.
      4. **Every DOM signal collapses, the pixels differ** — the last honest
         resort, and the only rung that costs I/O.  Admit the perceptual hash.
      5. **Nothing differs** — then nothing differs.  Return the previous digest
         unchanged so ``walk_seen`` sees the loop it is there to see.

    RUNG 5 IS THE POINT.  It would be trivial to guarantee twenty distinct
    fingerprints by folding the step counter into the digest, and it would be a
    lie: a Continue that does nothing would mint a fresh state on every click,
    the walk would report twenty steps it never took, and ``TERMINAL_LOOP``
    would never fire again.  Distinctness has to be EARNED by an observed
    difference.  That is why ``step_ordinal`` exists on the hasher (a caller
    that has already established two states differ may legitimately key on it)
    and is not used here.

    NO HIDDEN GLOBAL STATE.  One instance per walk, owned by the walk, mutated
    only by :meth:`advance` / :meth:`resync` on that walk's own task.  Two
    crawls, or two journeys inside one crawl, share nothing.
    """

    __slots__ = ("_fingerprinter", "_prev_signals", "_prev_fingerprint", "_ordinal")

    def __init__(
        self,
        fingerprinter: "StateFingerprinter",
        *,
        entry_fingerprint: str,
        entry_signals: Optional[StepSignals] = None,
    ) -> None:
        self._fingerprinter = fingerprinter
        entry_fp = str(entry_fingerprint or "")
        # The entry state admits no extra signal, so its composite IS its base.
        self._prev_signals = entry_signals or StepSignals(base=entry_fp)
        self._prev_fingerprint = entry_fp
        self._ordinal = 0

    @property
    def ordinal(self) -> int:
        """How many steps this walk has ADVANCED THROUGH (0 at the entry state).

        Audit evidence and the depth metric read this.  It is deliberately not
        an identity input — see the class docstring.
        """
        return self._ordinal

    @property
    def previous_fingerprint(self) -> str:
        return self._prev_fingerprint

    @property
    def previous_signals(self) -> StepSignals:
        return self._prev_signals

    def needs_perception(
        self,
        *,
        url: str,
        controls: Sequence[Mapping[str, Any]],
        dialogs: Sequence[str] = (),
        structural: Optional[str] = None,
        revealed: Sequence[str] = (),
        page_token: str = "",
    ) -> bool:
        """Would rung 4 be reached — i.e. is a screenshot worth taking here?

        Lets the walk skip the perceptual hash on every state the DOM already
        separates, which is nearly all of them.  Pure; mutates nothing.
        """
        base = self._fingerprinter.fingerprint(
            url=url, controls=controls, dialogs=dialogs, page_token=page_token)
        if base != self._prev_signals.base:
            return False
        struct = (structural_signature(controls) if structural is None
                  else str(structural or ""))
        rev = tuple(str(r) for r in (revealed or ()) if str(r).strip())
        return (struct, rev) == (self._prev_signals.structural,
                                 self._prev_signals.revealed)

    def identify(
        self,
        *,
        url: str,
        controls: Sequence[Mapping[str, Any]],
        dialogs: Sequence[str] = (),
        structural: Optional[str] = None,
        revealed: Sequence[str] = (),
        perceptual_hash: str = "",
        page_token: str = "",
    ) -> tuple[str, StepSignals]:
        """Identify one observation against the previous step.  Pure — call
        :meth:`advance` (or :meth:`resync`) to make the result the new current
        step.

        Returns ``(fingerprint, signals)``.  ``structural=None`` means "compute
        it from ``controls``"; pass ``""`` to suppress that rung entirely.
        """
        struct = (structural_signature(controls) if structural is None
                  else str(structural or ""))
        # M1.5 / T-ND-04 — the page token reaches the ladder through the SAME
        # door every other signal does: the fingerprinter decides whether it is
        # admissible (only when this base digest was first claimed by a
        # different page), so the walk never fractures an identity just because
        # a popup was adopted somewhere earlier in the journey.
        base = self._fingerprinter.fingerprint(
            url=url, controls=controls, dialogs=dialogs, page_token=page_token)
        signals = StepSignals(
            base=base,
            structural=struct,
            perceptual=str(perceptual_hash or "").strip(),
            revealed=tuple(str(r) for r in (revealed or ()) if str(r).strip()),
        )
        # Rung 1 — the DOM already tells these two states apart. Historical
        # digest, byte for byte. Compared BASE-to-BASE (see StepSignals.base).
        if base != self._prev_signals.base:
            return base, signals
        # Rungs 2-4 — the base collapsed. Admit ONLY the signals that actually
        # DIFFER from the previous step; a signal identical on both sides
        # discriminates nothing and stays out of the payload, so a page reached
        # twice by the same route keeps one identity.
        admitted_struct = (signals.structural
                           if signals.structural != self._prev_signals.structural
                           else "")
        admitted_revealed = (signals.revealed
                             if signals.revealed != self._prev_signals.revealed
                             else ())
        # An EMPTY perceptual hash means NOT MEASURED (rung 4 was not reached on
        # that step, or Pillow was unavailable) — it does NOT mean "blank
        # screen". Comparing a measured hash against an unmeasured one answers
        # a question nobody asked and reads as a pixel change, which is how the
        # first advance of every walk used to look perceptually distinct from
        # its entry step regardless of what was on screen. Pixels are admitted
        # only when BOTH sides were actually measured and actually differ; when
        # they were not, the honest answer is that pixels say nothing here.
        prev_phash = self._prev_signals.perceptual
        admitted_phash = (signals.perceptual
                          if (signals.perceptual and prev_phash
                              and signals.perceptual != prev_phash)
                          else "")
        if not (admitted_struct or admitted_revealed or admitted_phash):
            # Rung 5 — indistinguishable by every signal we hold. Saying so is
            # the honest answer, and it is what lets the walk terminate as a
            # loop instead of inventing progress.
            return self._prev_fingerprint, signals
        return (
            self._fingerprinter.fingerprint(
                url=url, controls=controls, dialogs=dialogs,
                structural_hash=admitted_struct,
                revealed_delta=admitted_revealed,
                perceptual_hash=admitted_phash,
                page_token=page_token,
            ),
            signals,
        )

    def advance(self, fingerprint: str, signals: StepSignals) -> None:
        """Commit an :meth:`identify` result as this walk's new current step."""
        self._prev_fingerprint = str(fingerprint or "")
        self._prev_signals = signals
        self._ordinal += 1

    def resync(self, fingerprint: str, signals: StepSignals) -> None:
        """Restate the CURRENT step without counting an advance.

        A questionnaire answer re-observes the step it is standing on (it may
        have revealed a follow-up); that is new information about where we
        already are, not a step forward, and counting it as one would inflate
        the very depth metric T-SI-06 exists to make trustworthy.
        """
        self._prev_fingerprint = str(fingerprint or "")
        self._prev_signals = signals


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
    calls into an ORDERED, NON-DEDUPLICATED stream of ALL-string records (so the
    schema's ``dict[str, str]`` can never refuse a bundle over a diagnostic).

    M2.5 / T-NET-02 — THE DEDUPLICATION IS GONE, DELIBERATELY.  This function
    used to collapse on ``method|url|status``, which meant three retries of the
    same call became one record and a poll that fired forty times reported once.
    That is not noise reduction; it is destruction of the signal.  A retry
    sequence, a poll cadence and a rate-limit backoff are all *repetition* — they
    exist only in the repeated events, and an evidence stream that removes them
    cannot show a reviewer that the application retried, or how often, or in what
    order.  Every event now survives, carrying:

    * ``sequence`` — the crawl-wide ordinal assigned at CAPTURE, so ordering
      survives any downstream transport that does not preserve list order;
    * ``timestamp_ms`` — on the crawl clock (T-NET-01), so the event joins to the
      visit window and the step it happened in;
    * ``action_token`` / ``action_label`` / ``action_verb`` — WHICH UI action was
      in flight when the request went out (T-NET-03).

    Safety is unchanged and still belt-and-braces: the query string is DROPPED
    here regardless of what the adapter sent (a query param is the likeliest PII
    carrier — ``has_query`` preserves the honest fact that one existed, without
    its values), and the query-stripped URL is re-scrubbed for path-embedded PII
    over the adapter's source-side scrub.  Header and body evidence arrives
    already allow-listed and value-redacted by :mod:`app.network_evidence` and is
    serialized here without ever being widened.  http(s) API calls AND ws(s)
    real-time endpoints are evidence; every other scheme is dropped.

    The ``_MAX_NETWORK_CALLS`` cap still applies, but a clip is now REPORTED as a
    final ``stream_truncated`` record rather than being a silent ``break`` — a
    stream that stopped early must not read as one that ended.
    """
    out: list[dict[str, str]] = []
    kept = 0
    for r in raw or ():
        if not isinstance(r, dict):
            continue
        if str(r.get("event") or "") == "buffer_truncated":
            # The adapter refused events at its own cap. That is evidence about
            # the stream, not an API call — carry it through so the gap in the
            # sequence numbers has a stated cause.
            out.append({
                "event": "buffer_truncated",
                "method": "", "url": "", "has_query": "false", "status": "",
                "resource_type": "meta", "request_mime": "", "response_mime": "",
                "response_bytes": "",
                "dropped": str(r.get("dropped") or "")[:10],
                "reason": str(r.get("reason") or "")[:200],
                "sequence": str(r.get("sequence") or "")[:15],
                "timestamp_ms": str(r.get("timestamp_ms") or "")[:15],
            })
            continue
        parts = urlsplit(str(r.get("url") or "").strip())
        if (parts.scheme or "").lower() not in ("http", "https", "ws", "wss"):
            continue
        url = emit.scrub_value(f"{parts.scheme}://{parts.netloc}{parts.path}").value.strip()
        if not url:
            continue
        if kept >= _MAX_NETWORK_CALLS:
            out.append({
                "event": "stream_truncated",
                "method": "", "url": "", "has_query": "false", "status": "",
                "resource_type": "meta", "request_mime": "", "response_mime": "",
                "response_bytes": "",
                "reason": (f"more than {_MAX_NETWORK_CALLS} network events in one "
                           f"visit; the remainder is not recorded"),
                "sequence": str(r.get("sequence") or "")[:15],
                "timestamp_ms": str(r.get("timestamp_ms") or "")[:15],
            })
            break
        kept += 1
        had_query = bool(parts.query) or bool(r.get("has_query"))
        body = r.get("request_body") if isinstance(r.get("request_body"), Mapping) else {}
        out.append({
            "method": str(r.get("method") or "").strip().upper()[:10],
            "url": url[:1000],
            "path_template": str(r.get("path_template")
                                 or net_evidence.path_template(parts.path))[:500],
            "has_query": "true" if had_query else "false",
            "status": str(r.get("status") or "").strip()[:3],
            "resource_type": str(r.get("resource_type") or "").strip()[:20],
            "request_mime": str(r.get("request_mime") or "").strip()[:100],
            "response_mime": str(r.get("response_mime") or "").strip()[:100],
            "response_bytes": str(r.get("response_bytes") or "").strip()[:12],
            # T-NET-01 / 02 / 03 — the three joins.
            "sequence": str(r.get("sequence") or "")[:15],
            "timestamp_ms": str(r.get("timestamp_ms") or "").strip()[:15],
            # WHEN THIS VISIT'S CAPTURE WINDOW OPENED. The join key that makes
            # navigation traffic legible: a route prefetch fired on the way INTO
            # this state has a true timestamp earlier than the state's
            # ``first_seen_ms``, and is still correctly attributed here. Carrying
            # the window means a reviewer can verify that attribution instead of
            # having to take it on trust -- or having it hidden by a clamp.
            "capture_window_start_ms": str(r.get("capture_window_start_ms") or "0")[:15],
            "action_token": str(r.get("action_token") or "")[:20],
            "action_label": str(r.get("action_label") or "")[:200],
            "action_verb": str(r.get("action_verb") or "")[:40],
            "page_token": str(r.get("page_token") or "")[:20],
            # T-NET-02 — headers/body, already redacted at source.  Serialized
            # compactly because the manifest field is typed ``dict[str, str]``;
            # widening that type would ripple into the frozen substrate schema.
            "request_headers": _compact_headers(r.get("request_headers")),
            "response_headers": _compact_headers(r.get("response_headers")),
            "request_body_bytes": str(body.get("bytes") or "0")[:12],
            "request_body_keys": ",".join(str(k) for k in (body.get("keys") or []))[:500],
            "request_body_source": str(body.get("keys_source") or "")[:20],
            "auth_pattern": str(r.get("auth_pattern") or "")[:20],
            "response_shape": str(r.get("response_shape") or "")[:20],
            "shape_source": str(r.get("shape_source") or "")[:30],
            # A request that never got a response is evidence too, and is what
            # lets the network oracle's ``failed`` branch fire from a crawl.
            "failed": "true" if r.get("failed") else "false",
            "error": str(r.get("error") or "")[:200],
        })
    return out


def _compact_headers(headers: Any) -> str:
    """Serialize an ALREADY-REDACTED header dict into one manifest string.

    Never widens: whatever :func:`app.network_evidence.redact_headers` refused to
    include cannot reappear here, because this function only formats what it is
    given.  Sorted, so the same request serializes identically on every run.
    """
    if not isinstance(headers, Mapping):
        return ""
    return "; ".join(f"{str(k)}={str(v)}" for k, v in sorted(headers.items()))[:1000]


def _state_endpoints(raw: Sequence[dict[str, Any]]) -> list[dict[str, str]]:
    """M2.4 / T-GEN-03 — the ENDPOINT MAP for one state, value-free and
    env-portable.

    THE HOLE THIS FILLS. ``network_calls`` has ridden every ``page_state``
    record since the API-mining work, and every consumer of it is a DIAGNOSTIC
    one: the substrate stores the list, the failure attributor scans it after a
    red run. Nothing ever carried it into ``coverage.states`` — the index the
    journey fold reads — so the journey graph knew which CONTROL a walk clicked
    and never which CALL that click made. A generated spec could therefore only
    ever assert that a page rendered; the backend behind it was unasserted, and
    a silent API regression behind an unchanged UI passed green.

    Normalisation is what makes the result assertable rather than merely true:

      * PATH ONLY. The host is dropped, exactly as
        ``journey_case_linker.norm_path`` drops it for page comparison — a spec
        generated from a staging crawl has to run against prod, and a
        host-pinned network assertion is a spec that only ever runs once.
      * DOCUMENT NAVIGATIONS ARE NOT API CALLS. ``resource_type='document'`` is
        the page load itself; asserting it would re-prove the navigation oracle
        and add nothing. Dropped.
      * 2xx ONLY. A redirect hop is a hop, a call with no observed status
        never settled, and a 4xx/5xx the crawl happened to see is a DEFECT —
        compiling it into an assertion would freeze the application's bug into
        the regression suite as the expected behaviour. Only a settled success
        is a claim a generated spec may hold the app to.
      * DEDUPED AND SORTED. Two identical calls are one endpoint, and the order
        is the sort order — the ranking and the compiled spec both have to be
        byte-stable across two folds of the same crawl.

    Values never enter: the query string was already dropped upstream
    (``_network_calls``) and only the method, the path, the status and the
    response mime survive here.
    """
    by_key: dict[tuple[str, str], dict[str, str]] = {}
    for call in raw or ():
        if not isinstance(call, dict):
            continue
        if str(call.get("resource_type") or "").strip().lower() == "document":
            continue
        method = str(call.get("method") or "").strip().upper()[:10]
        path = (urlsplit(str(call.get("url") or "")).path or "").strip()[:300]
        if not method or not path:
            continue
        try:
            status = int(str(call.get("status") or "").strip() or 0)
        except ValueError:
            continue
        if not 200 <= status < 300:
            continue
        key = (method, path)
        if key in by_key:
            continue
        by_key[key] = {
            "method": method,
            "path": path,
            "status": str(status),
            "response_mime": str(call.get("response_mime") or "").strip()[:100],
        }
    ordered = [by_key[k] for k in sorted(by_key)]
    return ordered[:_MAX_STATE_ENDPOINTS]


def _merge_endpoints(
    previous: Any, fresh: Sequence[Mapping[str, str]],
) -> list[dict[str, str]]:
    """Union two endpoint maps for the same state, first-observation-wins per
    (method, path), sorted and bounded.

    First-wins rather than last-wins on purpose: the status recorded is the one
    the crawl SAW settle first for that call, and a later sighting overwriting
    it would silently rewrite the observation a spec was going to be built on.
    """
    merged: dict[tuple[str, str], dict[str, str]] = {}
    for source in (previous or (), fresh or ()):
        if not isinstance(source, Sequence):
            continue
        for entry in source:
            if not isinstance(entry, Mapping):
                continue
            method = str(entry.get("method") or "")
            path = str(entry.get("path") or "")
            if method and path:
                merged.setdefault((method, path), dict(entry))
    return [merged[key] for key in sorted(merged)][:_MAX_STATE_ENDPOINTS]


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

    def _note_boundary_controls(self, controls: Sequence[dict[str, Any]],
                                *, url: str = "") -> None: ...

    def _note_network_stream(self, events: Sequence[dict[str, Any]],
                             *, url: str = "") -> None: ...


async def _active_page_token(port: Any) -> str:
    """The port's ACTIVE page token, or ``""`` — OPTIONAL + best-effort.

    Reached through ``getattr`` for the same reason ``drain_network`` is: a
    scripted fake, a jsdom lane and every port written before M1.5 have exactly
    one page and nothing to report, and they must keep working unchanged.
    """
    getter = getattr(port, "active_page_token", None)
    if getter is None:
        return ""
    try:
        return str(await getter() or "")
    except Exception:
        return ""


async def _collect_controls_checked(port: Any) -> observation_health.InventoryResult:
    """The inventory read with its health, degrading to the legacy read.

    OPTIONAL on the port for the same reason ``active_page_token`` is: a scripted
    fake, the jsdom lane and every port written before M1.7 implement only
    ``collect_controls``.  For those, the returned list IS the truth — a fake's
    ``[]`` really is the page it is pretending to be — so it is wrapped as a
    HEALTHY result and behaviour is byte-identical to before this existed.

    ``None`` MEANS NOT IMPLEMENTED, and that rule is load-bearing.
    :class:`app.browser.BrowserPort` is a ``Protocol``, and the fakes across this
    suite SUBCLASS it — so they inherit its method bodies, which are ``...``.
    ``getattr`` therefore finds ``collect_controls_result`` on every one of them
    and awaiting it yields ``None``.  The sibling optional verbs get away with
    this because ``list(None or [])`` is ``[]``, which for network events means
    the same thing as "not implemented".  Here it emphatically does not: an empty
    inventory is a claim about the PAGE, and treating the stub's ``None`` as one
    would report every scripted fake's application as a blank document — the
    exact confusion this milestone exists to remove, reintroduced by the fix for
    it.  So ``None`` falls through to the legacy read instead.

    A port that raises out of the legacy read is treated as a failed read rather
    than allowed to propagate: this sits in the observation path of every visit,
    and turning a read failure into a crawl-killing exception would trade one
    dishonest outcome for a different one.
    """
    checked = getattr(port, "collect_controls_result", None)
    if checked is not None:
        try:
            result = await checked()
        except Exception as exc:                      # pragma: no cover - defensive
            return observation_health.InventoryResult.from_exception(exc)
        if isinstance(result, observation_health.InventoryResult):
            return result
        if result is not None:
            # A port that answered the new name with the old shape: honour the
            # data, not the signature.
            return observation_health.InventoryResult.from_payload(result)
    try:
        return observation_health.InventoryResult.from_payload(await port.collect_controls())
    except Exception as exc:
        return observation_health.InventoryResult.from_exception(exc)


class StateRecorder:
    """Observes the live page and writes ONE ``page_state`` record per visit."""

    def __init__(self, host: RecorderHost) -> None:
        self._c = host

    # -- observation ----------------------------------------------------------

    async def observe(self) -> PageObservation:
        port = self._c._port
        # M1.7 / T-GW-01 — the CHECKED read.  The controls and the health of the
        # read that produced them arrive together, so an inventory failure cannot
        # be laundered into "this page has no controls" by the time this
        # observation reaches the fingerprinter.
        inventory = await _collect_controls_checked(port)
        return PageObservation(
            url=await port.current_url(),
            title=await port.title(),
            raw_controls=inventory.as_list(),
            inventory_status=inventory.status,
            inventory_error=inventory.error,
            dialog_flags=await port.dialog_flags(),
            error_texts=await port.error_texts(),
            # M1.5 / T-ND-04 — WHICH page this coherent read came from. Part of
            # the SAME observation as the url and the controls on purpose: a
            # token fetched separately could name a page the inventory did not
            # come from, which is precisely the stale-page bug in miniature.
            # OPTIONAL on the port (a fake or an older adapter has no pages to
            # swap), so it degrades to "" — the primary-page identity.
            page_token=await _active_page_token(port),
        )

    # -- the states index (what each state ASKED) -----------------------------

    def note_state_signals(
        self, fingerprint: str, url: str, signals: Mapping[str, Any],
        controls: Sequence[Mapping[str, Any]] = (),
        network_calls: Sequence[Mapping[str, Any]] = (),
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

        M2.4 / T-GEN-03 — ``endpoints`` (see :func:`_state_endpoints`) rides
        here too, and it accumulates on a rule of its OWN. Richest-sighting is
        about the questions a page asks; the API calls it makes are a different
        axis, and the sighting that makes the load-bearing call is very often
        the sighting with the FEWEST questions — a submit that clears the form.
        Letting the questions rule govern the endpoints would have discarded
        precisely the call a journey most needs to assert, so endpoints are
        UNIONED across every sighting of the state, including the ones whose
        signals are rejected below.
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
        endpoints = _merge_endpoints(
            (prev or {}).get("endpoints"), _state_endpoints(network_calls))
        if prev is not None and len(prev.get("form_snapshot_signals") or {}) >= len(signals):
            # The signals lose, the endpoints still win: this sighting's calls
            # are facts about the state whatever its question count says.
            prev["endpoints"] = endpoints
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
            # WHAT THIS STATE ASKED, AS QUESTIONS RATHER THAN AS CONTROLS (M2.1).
            #
            # ``form_snapshot_signals`` above is keyed by a control's accessible
            # name, and for a choice group that name is the name of an ANSWER.
            # A "Gender" question crossed to the catalogue as two questions,
            # "Male" and "Female", each offering no answers; a 20-question health
            # questionnaire whose every control is named "Yes" or "No" crossed as
            # TWO, because a dict keyed by name cannot hold forty. The grouping
            # was known in this process the whole time and had no way across.
            #
            # Carried ALONGSIDE the signals, never instead of them: the frozen
            # compiler reads ``form_snapshot_signals[label]`` and must keep
            # reading exactly what it always has.
            "question_groups": question_groups_of(controls)[:_MAX_QUESTION_GROUPS],
            # M2.4 / T-GEN-03 — THE ENDPOINT MAP: the producer half of the join
            # that lets a generated spec assert the BACKEND behaved, instead of
            # only that the page did not crash.
            "endpoints": endpoints,
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
        c._note_boundary_controls(controls, url=url)
        # M2.5 / T-NET-04 + T-NET-05 — fold THIS visit's network stream into the
        # crawl-level endpoint inventory and the oracle feed.  Done here, beside
        # the boundary note, because this is the one place every recorded state
        # passes through whichever path reached it (discovery, walk, submit) —
        # hanging it off one caller would silently miss the others, which is the
        # same shape of hole `_note_boundary_controls` was written to close.
        c._note_network_stream(network_calls, url=url)
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

        self.note_state_signals(fingerprint, url, form_signals, controls,
                                network_calls)
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


__all__ = ["StateFingerprinter", "WalkIdentity", "StepSignals",
           "structural_signature", "StateRecorder", "RecorderHost",
           "_action_to_dict", "_displayed_values", "_form_snapshot",
           "_is_password", "_network_calls"]
