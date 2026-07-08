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
#: credit.  Free String(40) at rest — spelling here is for legibility, not a
#: hard enum, so an unforeseen outcome is never fabricated into ``navigation``.
OUTCOME_NAVIGATION = "navigation"
OUTCOME_VALUE_COMMITTED = "value_committed"
OUTCOME_DIALOG = "dialog"
OUTCOME_ERROR = "error"
OUTCOME_DOM_CHANGED = "dom_changed"
OUTCOME_NONE = "none"

AFTER_OUTCOMES = frozenset({
    OUTCOME_NAVIGATION, OUTCOME_VALUE_COMMITTED, OUTCOME_DIALOG,
    OUTCOME_ERROR, OUTCOME_DOM_CHANGED, OUTCOME_NONE,
})

_MAX_DETAIL = 500


def _norm_url(url: str) -> str:
    """Trailing-slash / fragment-insensitive URL identity for change detection.

    A pure scroll-anchor (``#section``) or a trailing ``/`` is cosmetic and must
    NOT read as a navigation; a real path/query change does.  Kept intentionally
    simple and deterministic (no network, no PSL).
    """
    u = (url or "").strip()
    if not u:
        return ""
    u = u.split("#", 1)[0]
    if len(u) > 1 and u.endswith("/"):
        u = u[:-1]
    return u


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
