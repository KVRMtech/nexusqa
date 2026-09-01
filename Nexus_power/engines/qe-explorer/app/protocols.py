"""FROZEN structural contracts for the crawler subsystem (M0.3 / T-DE-01).

WHY THIS MODULE EXISTS.  The decomposition of the ``Crawler`` god object moves
~3.000 lines of behaviour into cohesive modules.  A move is only safe if the
seam it crosses is named BEFORE the code travels through it — otherwise each
extraction re-negotiates its own boundary and the "architecture" is just the
call graph that happened to fall out.  These Protocols are that naming.

TWO RULES GOVERN EVERY DECLARATION HERE.

1.  **A frozen contract must describe code that exists.**  Every Protocol below
    was read off the current implementation, not designed for it.  Where the
    M0.3 brief proposed an idealised shape that today's code cannot satisfy
    without a behavioural rewrite (``Walker.walk(entry) -> Flow``), the
    *binding* Protocol matches reality and the idealised shape is recorded as
    a documented Phase-1 target.  A contract that lies is worse than no
    contract: it silently licenses the very rewrite Rule #1 forbids.

2.  **No runtime imports.**  Every referenced type is imported under
    ``TYPE_CHECKING`` only, so :mod:`app.protocols` can be imported from any
    layer — including a unit test that must not boot Playwright, FastAPI or the
    forms engine — and can never take part in an import cycle.

``BrowserPort`` is deliberately NOT redeclared here.  It is already a Protocol
in :mod:`app.browser` and the brief freezes it as-is; re-stating it would
create two sources of truth for one contract.
"""
from __future__ import annotations

from typing import (TYPE_CHECKING, Any, Mapping, Optional, Protocol, Sequence,
                    runtime_checkable)

if TYPE_CHECKING:  # pragma: no cover - typing only, never imported at runtime
    from .browser import BrowserPort, PageObservation
    from .emit import ActionRecord
    from .forms import FormFillResult, SubmitResult
    from .identity_pack import Identity
    from .frontier import FrontierItem


# ─── Type aliases for shapes the codebase already speaks ─────────────────────

#: One recorded business journey, exactly as :func:`app.flow_ledger.build_flow`
#: returns it: ``{flow_id, entry_fingerprint, entry_url, entry_title, steps,
#: terminal, completed, ...}``.  ``completed`` is DERIVED from ``terminal`` by
#: the ledger and must never be supplied by a caller — a truncated walk cannot
#: be reported as a covered journey.  This is the brief's ``Flow``.
Flow = dict


#: A control as the inventory yields it (``role`` / ``name`` / ``disabled`` /
#: selector hints).  Deliberately loose: the inventory is schema-versioned
#: independently and narrowing it here would freeze a contract this milestone
#: does not own.
Control = dict


# ─── State identity ──────────────────────────────────────────────────────────


@runtime_checkable
class StateIdentity(Protocol):
    """Reduces an observed page to the id that decides "have we been here?".

    THE SEAM, NOW LOAD-BEARING (T-SI-01/02).  ``perceptual_hash`` was accepted
    and threaded to the hasher but always empty, because nothing computed one.
    It is joined here by the three signals a same-shape wizard needs, and the
    walk now supplies them: ``url`` + controls + dialogs are all constant across
    the twenty steps of a one-question-at-a-time questionnaire, so an
    implementation reading only those returns ONE identity for twenty states and
    traversal stops at step one.

    EVERY ADDED SIGNAL IS OPTIONAL AND OFF BY DEFAULT.  Omitted or empty means
    "not observed", and the digest is then exactly the digest this contract has
    always produced — which is what lets the signals be switched on per-tenant
    without re-fingerprinting a single previously-visited state.

    An implementation MUST NOT decide for itself which signals matter; that
    needs the PREVIOUS state, which this contract never sees.  That policy lives
    in :class:`app.state_identity.WalkIdentity`.
    """

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
    ) -> str:
        """Return the 64-char sha256 hex identifying this state.

        ``None`` and ``""`` are the SAME request (no signal) for every optional
        argument, and must produce the same digest.

        Args:
            perceptual_hash: coarse aHash of the rendered screen.
            structural_hash: digest of the page's DECLARED question grouping.
            revealed_delta: value-free ids of controls an answer activated.
            page_token: M1.5 / T-ND-04 — WHICH browser page the observation was
                read from. ``""`` is the page the crawl started with; an ADOPTED
                popup / new tab carries ``"p1"``, ``"p2"``, … An implementation
                MUST NOT fold it into every digest: doing so fractures the
                identity of a page whose Playwright object merely changed, and
                moves every fingerprint already persisted. It is admitted only
                when the DOM signals alone cannot separate two DIFFERENT pages.
            step_ordinal: walk-local position; hashed only when > 0.  An
                implementation must treat this as manufacturing distinctness —
                see the warning on :func:`app.fingerprint.state_fingerprint`.
        """
        ...


# ─── Form filling ────────────────────────────────────────────────────────────


class Filler(Protocol):
    """Phase-A form filling: answer what can be answered honestly, record the
    residue, and NEVER invent a value an LLM supplied.

    The value ladder (answer key → recalled → journey memory → priors →
    synthesised default) lives in :mod:`app.forms` and :mod:`app.field_values`
    and is not re-implemented behind this seam.  An LLM may say WHICH control
    to operate (see :class:`OracleGateway`); it may never say what to type
    into one.
    """

    async def fill(
        self,
        controls: Sequence[Mapping[str, Any]],
        identity: "Identity",
        *,
        state_id: str = "",
        phase: str = "explore",
    ) -> "FormFillResult":
        """Fill one page state, returning the honest per-field outcome."""
        ...


# ─── Journey walking ─────────────────────────────────────────────────────────


class Walker(Protocol):
    """Walks a multi-step funnel from a filled entry step to its terminal.

    CONTRACT DEVIATION — READ BEFORE CHANGING.  The brief specifies
    ``async def walk(entry: FrontierItem) -> Flow``.  The implementation
    (``Crawler._walk_wizard``) takes ten keyword arguments and returns
    ``bool``, because the orchestrator has already observed the entry step —
    screenshot, first-seen timestamp, displayed values, drained network calls,
    base actions — and the walker records those into the flow it builds rather
    than re-observing them.  Collapsing that to ``walk(entry)`` would move
    observation INTO the walker: a redesign, not a move, and a guaranteed
    change to action ordering and timestamps in the manifest.

    So the binding contract below is today's shape.  The brief's shape is the
    Phase-1 target and is recorded in :data:`WALKER_TARGET_CONTRACT`.
    """

    async def walk(
        self,
        *,
        item: "FrontierItem",
        url: str,
        title: str,
        controls: Sequence[Mapping[str, Any]],
        fingerprint: str,
        base_actions: list["ActionRecord"],
        entry_shot: tuple[bytes, int],
        first_seen_ms: int,
        displayed_values: Sequence[Mapping[str, Any]],
        network_calls: Sequence[Mapping[str, Any]],
        entry_pick: Optional[Any] = None,
    ) -> bool:
        """Return True when the walk recorded at least one deeper step."""
        ...


#: The Phase-1 destination for :class:`Walker`, kept as prose rather than as an
#: unimplementable Protocol.  Reaching it requires moving entry OBSERVATION
#: behind the walker seam, which changes manifest action ordering and is
#: therefore out of scope for a behaviour-preserving milestone.
WALKER_TARGET_CONTRACT = "async def walk(entry: FrontierItem) -> Flow"


# ─── Submission ──────────────────────────────────────────────────────────────


class Submitter(Protocol):
    """Phase-B attested submit.

    Default-OFF and double-gated: fires only when the operator supplied a
    per-flow approval list AND a disposable-environment attestation is present.
    The guard is re-verified at the point of the click, never only at
    admission — an approval is not a standing permission.
    """

    async def maybe_submit(
        self,
        controls: Sequence[Mapping[str, Any]],
        *,
        url: str,
        fingerprint: str,
    ) -> Optional["SubmitResult"]:
        """Attempt the approved submit; None when nothing was eligible."""
        ...


# ─── Oracle access ───────────────────────────────────────────────────────────


class OracleGateway(Protocol):
    """The ONE seam through which crawler internals may consult an LLM.

    Today ``Crawler`` holds two raw callables (``advance_oracle``,
    ``vision_oracle``) and threads them through the walk and the fill.  That
    means every consumer independently owns the "is it configured? did it error?
    am I over the cap?" logic, and the telemetry counters
    (``_oracle_consults`` / ``_oracle_errors`` / ``_oracle_unavailable`` /
    ``_oracle_latency_ms`` / ``_oracle_picks``) are incremented at scattered
    call sites.  Behind this gateway there is exactly one place to look.

    THE CAPS DO NOT MOVE.  The per-crawl circuit breaker and call cap live
    inside the callables built by ``app.main``; the gateway wraps, it does not
    re-implement.  An ``unavailable`` verdict stays a first-class, non-raising
    outcome — a missing oracle degrades the crawl honestly, it never fails it.
    """

    async def advance(
        self,
        controls: Sequence[Mapping[str, Any]],
        page_title: str,
        page_url: str,
    ) -> dict[str, Any]:
        """Which control moves this funnel forward?

        Returns ``{"index": int | None, "status": "picked"|"none"|
        "unavailable", "signature": str}``.
        """
        ...

    async def operate(
        self,
        control: Mapping[str, Any],
        intent: str,
        ladder_results: Sequence[Mapping[str, Any]],
        page_context: Mapping[str, Any],
    ) -> dict[str, Any]:
        """The deterministic ladder failed — what action should be tried?

        Returns ``{"action": str, "status": "proposed"|"display_only"|
        "unavailable"}``.
        """
        ...

    async def perceive(
        self,
        screenshot_b64: str,
        page_context: Mapping[str, Any],
    ) -> dict[str, Any]:
        """The DOM is opaque — enumerate what is visibly on screen.

        Returns ``{"controls": [...], "displayed_values": [...]}``; empty on
        any failure or for a tenant without the vision flag.
        """
        ...

    @property
    def telemetry(self) -> Mapping[str, int]:
        """Consult counters for the coverage ledger (consults / errors /
        unavailable / latency_ms / picks)."""
        ...


# ─── Observation ─────────────────────────────────────────────────────────────


class Observer(Protocol):
    """Assembles one coherent read of the page from the browser port.

    Exists so that state recording, discovery and the walk all obtain a page
    the same way; a partial read (controls from one moment, dialogs from
    another) is the classic source of a fingerprint that matches nothing.
    """

    async def observe(self) -> "PageObservation":
        ...

    async def drain_network(self) -> list[dict[str, Any]]:
        ...


__all__ = [
    "Control",
    "Filler",
    "Flow",
    "Observer",
    "OracleGateway",
    "StateIdentity",
    "Submitter",
    "WALKER_TARGET_CONTRACT",
    "Walker",
]
