"""QE-Central Contained Explorer — the BROWSER PORT (dependency inversion seam).

The crawler / auth / forms state machines drive the live app ONLY through the
:class:`BrowserPort` protocol — never a concrete Playwright object.  This keeps
every piece of decision logic (frontier expansion, budget accounting, the
after-outcome classifier, login verification, form Phase-A) unit-testable with a
scripted fake and NO browser runtime, which matters doubly here because the
Playwright runtime is not available in the local/CI Windows environment (the
live crawl is verified on the VM).

Design references (verified against the local repo 2026-07-08):
  * §3.2 ``action`` record: ``after {outcome, detail, navigated}`` + ``url_changed``
    map onto ``page_actions.evidence_signals`` (writer.py:159-174,
    service.py:152-176).  Only ``outcome=='navigation'`` earns the generator's
    PROVEN navigation credit (schema.py AfterBundle._normalise; generator
    ``_action_navigated``) — every other outcome degrades honestly.
  * §3.2 isolation: the port's concrete Playwright implementation lives ONLY in
    :mod:`app.main`, launched behind the egress proxy with
    ``service_workers='block'`` and a fail-closed ``context.route`` guard.

This module is deliberately dependency-free (stdlib + typing only) so that
importing it can never pull Playwright, FastAPI or httpx into a pure-logic test.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol, Sequence, runtime_checkable

# ─── after.outcome vocabulary (design §3.2; schema.py AfterBundle) ──────────
#: The honest, observed outcome of one action.  ``navigation`` is the only value
#: that earns the downstream PROVEN navigation credit; the rest degrade with no
#: credit.  ``confirmation`` is the Phase-B (submit-tier) same-page terminal
#: success — it earns a baseline visual_frame but NO navigation credit.  Free
#: String(40) at rest — spelling here is for legibility, not a hard enum, so an
#: unforeseen outcome is never fabricated into ``navigation``/``confirmation``.
OUTCOME_NAVIGATION = "navigation"
OUTCOME_VALUE_COMMITTED = "value_committed"
OUTCOME_DIALOG = "dialog"
OUTCOME_ERROR = "error"
OUTCOME_DOM_CHANGED = "dom_changed"
OUTCOME_NONE = "none"
OUTCOME_CONFIRMATION = "confirmation"

AFTER_OUTCOMES = frozenset({
    OUTCOME_NAVIGATION, OUTCOME_VALUE_COMMITTED, OUTCOME_DIALOG,
    OUTCOME_ERROR, OUTCOME_DOM_CHANGED, OUTCOME_NONE, OUTCOME_CONFIRMATION,
})

_MAX_DETAIL = 500


def _norm_url(url: str) -> str:
    """Trailing-slash / scroll-anchor-insensitive URL identity for change detection.

    A pure scroll-anchor (``#section``) or a trailing ``/`` is cosmetic and must
    NOT read as a navigation; a real path/query change — INCLUDING a client-side
    hash-ROUTE change on a single-page app (``#/...`` or ``#!/...``) — DOES. Without
    keeping the hash route, every SPA route collapses to one URL and route-to-route
    navigation is invisible (the crawler then never leaves the entry page). Kept
    deterministic (no network, no PSL).
    """
    u = (url or "").strip()
    if not u:
        return ""
    base, _sep, frag = u.partition("#")
    if len(base) > 1 and base.endswith("/"):
        base = base[:-1]
    # Keep a hash-ROUTE fragment (SPA routing: ``#/path`` or ``#!/path``); drop a
    # pure scroll-anchor (``#section``) — it is not a navigation.
    frag = frag.strip()
    if frag[:1] in ("/", "!"):
        return base + "#" + frag
    return base


# ─── Raw observations (what the adapter measures; the classifier's input) ────


@dataclass(frozen=True)
class NavResult:
    """Outcome of a ``goto`` — the honest result of a navigation attempt."""

    url: str
    ok: bool = True
    status: int = 0
    error: str = ""


@dataclass(frozen=True)
class RawObservation:
    """The measurable, grounded signals around ONE action.

    The Playwright adapter records ``url_before``, performs the action, then
    records ``url_after`` / the read-back ``committed_value`` / any dialog or
    error live-region that appeared / whether the interactive DOM signature
    changed.  :func:`classify_after` turns this into a schema-shaped outcome —
    the split keeps the "what happened" decision PURE and browser-free.
    """

    url_before: str = ""
    url_after: str = ""
    committed_value: str | None = None
    dialog_opened: bool = False
    dialog_detail: str = ""
    error_detail: str = ""
    dom_changed: bool = False
    #: A grounded, positive success/confirmation live-region text captured after
    #: a Phase-B submit (role=status / aria-live=polite success banner), kept
    #: SEPARATE from ``error_detail`` (role=alert).  Its presence is the honest
    #: signal that distinguishes a same-page ``confirmation`` from a bare
    #: ``dom_changed`` — :func:`classify_submit_after` never invents one.
    confirmation_detail: str = ""
    #: R0 intent contract — what the action was trying to achieve (fill value,
    #: select label, "true"/"false" for checked state).
    intended_value: str = ""
    #: R0 intent contract — True if the intended effect was verified, False if
    #: definitively unmet (errored, empty read-back on non-empty intended,
    #: checked state mismatch), None if unverifiable (no read-back, reformatted
    #: value, hover with no visible effect).
    intent_met: bool | None = None
    #: R1 interaction ladder — which mechanic rung verified intent (e.g.
    #: ``native_set_checked``, ``click_element``, ``focus_space``).  Empty when
    #: no ladder was used or the native attempt succeeded on first try.
    mechanic_used: str = ""


@dataclass(frozen=True)
class AfterOutcome:
    """A classified action outcome (→ manifest ``after`` bundle + ``url_changed``).

    ``outcome`` / ``detail`` / ``navigated`` map 1:1 onto the substrate
    ``AfterBundle``; ``url_changed`` is the sibling ``page_actions`` flag.
    """

    outcome: str
    detail: str
    navigated: bool
    url_changed: bool


def classify_after(obs: RawObservation) -> AfterOutcome:
    """Classify a :class:`RawObservation` into a grounded :class:`AfterOutcome`.

    Deterministic precedence (documented + pinned by unit tests):

      1. the URL actually changed         → ``navigation`` (navigated=True);
      2. a visible error live-region       → ``error``;
      3. a dialog/modal opened             → ``dialog``;
      4. a value was read back committed   → ``value_committed`` (fills, selects,
         checkbox/radio toggles — the adapter supplies the read-back);
      5. the interactive DOM shape changed → ``dom_changed``;
      6. nothing observable                → ``none``.

    Navigation wins first because a real URL change is the single strongest,
    most machine-checkable fact and is what the downstream PROVEN gate rewards;
    everything below it is a same-page effect.  Nothing is ever invented — an
    action with no observable effect is honestly ``none``.
    """
    url_changed = bool(obs.url_before and obs.url_after
                       and _norm_url(obs.url_before) != _norm_url(obs.url_after))
    if url_changed:
        return AfterOutcome(OUTCOME_NAVIGATION, _norm_url(obs.url_after)[:_MAX_DETAIL],
                            True, True)
    if (obs.error_detail or "").strip():
        return AfterOutcome(OUTCOME_ERROR, obs.error_detail.strip()[:_MAX_DETAIL],
                            False, False)
    if obs.dialog_opened:
        return AfterOutcome(OUTCOME_DIALOG, (obs.dialog_detail or "").strip()[:_MAX_DETAIL],
                            False, False)
    if obs.committed_value is not None:
        return AfterOutcome(OUTCOME_VALUE_COMMITTED, "", False, False)
    if obs.dom_changed:
        return AfterOutcome(OUTCOME_DOM_CHANGED, "", False, False)
    return AfterOutcome(OUTCOME_NONE, "", False, False)


# ─── R0 Intent contracts (pure verification) ───────────────────────────────

_CHECKED_TRUTHY = frozenset({"true", "1", "on", "yes", "checked"})


#: Verbs a "nothing chosen yet" option opens with. Kept local (and deliberately
#: narrow) so this module stays import-free of the fill layer; forms.py owns the
#: richer rule used to CHOOSE an option, this one only has to recognise that a
#: control is still unset after we tried to set it.
_UNSET_LEAD_VERBS = ("select", "choose", "pick")


def _reads_as_unset(committed: str) -> bool:
    """Does this committed label mean "no option is chosen"?"""
    text = " ".join(str(committed or "").split()).lower()
    if not text:
        return True
    stripped = text.strip("-–—_ .·:…")
    if not stripped:
        return True
    return stripped.split()[0] in _UNSET_LEAD_VERBS


def verify_intent(
    kind: str,
    *,
    intended_value: str = "",
    intended_checked: bool = False,
    committed_value: str | None = None,
    error_detail: str = "",
    url_before: str = "",
    url_after: str = "",
    dom_changed: bool = False,
    dialog_opened: bool = False,
) -> bool | None:
    """Pure intent verification — did the action achieve what was intended?

    Returns ``True`` when positively verified, ``False`` when definitively
    unmet, ``None`` when unverifiable.  The split matters: a ``False`` becomes
    honest residue (the fill is dropped); a ``None`` preserves current behaviour
    (the fill is kept, because we cannot prove it failed).
    """
    if (error_detail or "").strip():
        return False

    if kind in ("fill", "select"):
        if committed_value is None:
            return None
        c = (committed_value or "").strip()
        i = (intended_value or "").strip()
        if not c and i:
            return False
        if c.lower() == i.lower():
            return True
        # A select still sitting on its "Select term length..." entry did NOT
        # take the value — that option's value is "", so the field is unset. It
        # is provably unmet, not merely unverifiable, and calling it unverifiable
        # KEEPS the fill: the crawl then believes the form is complete, clicks a
        # Continue that is still disabled, and loops. Observed live — the quote
        # funnel stalled at step 2 while reporting both selects filled.
        if kind == "select" and i and _reads_as_unset(c):
            return False
        return None

    if kind == "checked":
        if committed_value is None:
            return None
        committed_bool = committed_value.strip().lower() in _CHECKED_TRUTHY
        return committed_bool == intended_checked

    if kind == "click":
        url_changed = bool(
            url_before and url_after
            and _norm_url(url_before) != _norm_url(url_after)
        )
        if url_changed or dom_changed or dialog_opened:
            return True
        return None

    return None


def classify_submit_after(obs: RawObservation) -> AfterOutcome:
    """Classify the TERMINAL outcome of a Phase-B submit click (design §3.2).

    A submit is the one place the platform mutates a customer app, so its
    outcome is judged conservatively and ONLY from grounded, observed signals —
    a positive ``confirmation`` is never fabricated from a bare DOM change:

      1. the URL changed              → ``navigation`` (navigated=True,
         url_changed=True): the strongest, most machine-checkable terminal
         proof; earns the downstream PROVEN ``toHaveURL`` credit;
      2. a visible error live-region  → ``error``: the submit was REJECTED — an
         honest failure, NEVER a confirmation;
      3. an explicit success/confirmation live-region
         (:attr:`RawObservation.confirmation_detail`) → ``confirmation``: a
         same-page terminal success (earns a baseline visual_frame, no
         navigation credit);
      4. a non-error dialog/modal      → ``confirmation`` (a confirmation/receipt
         modal), its detail carried through;
      5. otherwise                     → the generic :func:`classify_after`
         outcome (``value_committed`` / ``dom_changed`` / ``none``) — honestly
         NOT a confirmation.

    Navigation is checked before error so a submit that flashes a transient
    banner AND navigates is credited as the stronger navigation proof; error is
    checked before confirmation so a rejected submit is never green-washed.
    """
    url_changed = bool(obs.url_before and obs.url_after
                       and _norm_url(obs.url_before) != _norm_url(obs.url_after))
    if url_changed:
        return AfterOutcome(OUTCOME_NAVIGATION, _norm_url(obs.url_after)[:_MAX_DETAIL],
                            True, True)
    if (obs.error_detail or "").strip():
        return AfterOutcome(OUTCOME_ERROR, obs.error_detail.strip()[:_MAX_DETAIL],
                            False, False)
    detail = (obs.confirmation_detail or "").strip()
    if detail:
        return AfterOutcome(OUTCOME_CONFIRMATION, detail[:_MAX_DETAIL], False, False)
    if obs.dialog_opened:
        return AfterOutcome(OUTCOME_CONFIRMATION,
                            (obs.dialog_detail or "").strip()[:_MAX_DETAIL], False, False)
    return classify_after(obs)


# ─── The port ───────────────────────────────────────────────────────────────


@runtime_checkable
class BrowserPort(Protocol):
    """The minimal live-browser surface the explorer state machines require.

    Implemented for real by the Playwright adapter in :mod:`app.main` and by a
    scripted fake in the test-suite.  All methods are async: the real adapter
    awaits Playwright; the fake returns canned data.  Every method is expected
    to be non-raising for control-flow purposes — an adapter surfaces failure as
    an honest observation (e.g. ``NavResult.ok=False``, an empty inventory),
    never by throwing into the pure state machine.
    """

    async def goto(self, url: str) -> NavResult:
        """Navigate to ``url`` (subject to the fail-closed network guard)."""
        ...

    async def current_url(self) -> str:
        """The live page URL right now."""
        ...

    async def title(self) -> str:
        """The live document title (best-effort; ``""`` if unavailable)."""
        ...

    async def collect_controls(self) -> list[dict[str, Any]]:
        """Run the inventory walker across frames → raw control dicts.

        Returns the ``RawControl`` shape :func:`app.inventory.build_inventory`
        consumes (``app.inventory_js.INVENTORY_JS`` output).
        """
        ...

    async def collect_displayed_values(self) -> list[dict[str, Any]]:
        """ANSWERS P1.B — rendered value nodes ``[{label, selector, text}]`` a value
        oracle can ground against (``app.inventory_js.DISPLAYED_VALUES_JS`` output).
        Best-effort: ``[]`` on any failure (never breaks a crawl)."""
        ...

    async def dialog_flags(self) -> list[str]:
        """Open modal/dialog markers for the fingerprint (``[]`` when none)."""
        ...

    async def error_texts(self) -> list[str]:
        """Visible error live-region texts (role=alert / aria-live=assertive)."""
        ...

    async def screenshot_png(self) -> bytes:
        """A full-page PNG of the current state (raw bytes)."""
        ...

    async def click(self, control: Mapping[str, Any]) -> RawObservation:
        """Click ``control`` and return the measured raw observation."""
        ...

    async def hover(self, control: Mapping[str, Any]) -> RawObservation:
        """Hover ``control`` (reveal a menu / fly-out / tooltip) and observe."""
        ...

    async def set_input_files(self, control: Mapping[str, Any],
                              paths: Sequence[str]) -> RawObservation:
        """Attach ``paths`` to a file-input ``control`` (Phase-A: choose the file,
        never submit) and read back the chosen filename."""
        ...

    async def materialize(self) -> None:
        """Bounded viewport progression — step-scroll to trigger lazy /
        virtual-scroll content before the state is inventoried.  READ-ONLY and
        best-effort; a no-op is a valid implementation."""
        ...

    async def drain_network(self) -> list[dict[str, Any]]:
        """API/network mining — return + CLEAR the XHR/fetch calls the adapter
        buffered since the last drain, as ``[{method, url, has_query, status,
        resource_type, request_mime, response_mime, response_bytes,
        timestamp_ms}]``.  The adapter drops query strings + PII-scrubs at source.
        OPTIONAL + best-effort — the crawler reaches it via ``getattr``, so a fake
        or older adapter that omits it degrades to no network evidence."""
        ...

    async def fill(self, control: Mapping[str, Any], value: str) -> RawObservation:
        """Type ``value`` into ``control`` and read the committed value back."""
        ...

    async def select_option(self, control: Mapping[str, Any], value: str) -> RawObservation:
        """Select ``value`` in a dropdown ``control`` and read it back."""
        ...

    async def set_checked(self, control: Mapping[str, Any], checked: bool) -> RawObservation:
        """Toggle a checkbox/radio ``control`` and read the committed state."""
        ...

    async def storage_state(self) -> dict[str, Any]:
        """The context storage state (cookies + origins) — auth handoff only.

        The explorer relays this to qe-central IN MEMORY (never to disk); it is
        the sole channel by which a captured session leaves the container.
        """
        ...


@dataclass
class PageObservation:
    """A single observed page state: url/title + raw controls + state flags.

    Assembled by the crawler from the port so the fingerprint + inventory +
    screenshot for one visit come from a single coherent read of the page.
    """

    url: str
    title: str = ""
    raw_controls: Sequence[Mapping[str, Any]] = field(default_factory=list)
    dialog_flags: Sequence[str] = field(default_factory=list)
    error_texts: Sequence[str] = field(default_factory=list)
