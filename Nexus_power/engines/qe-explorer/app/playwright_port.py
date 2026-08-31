"""The Playwright :class:`BrowserPort` adapter — the ONLY Playwright code.

Lifted VERBATIM out of :mod:`app.main` (M0.3 / T-DE-05).  Nothing in the class
changed; only where it lives.

WHY THE MOVE MATTERS.  The adapter never referenced FastAPI, yet it could not
be imported without one: ``from app.main import PlaywrightBrowserPort`` pulled
in 632 modules — 158 of them HTTP-layer — and CONSTRUCTED a live ``FastAPI``
application object as an import side effect, because ``app = FastAPI(...)``
runs at module scope.  The browser layer was reachable only THROUGH the HTTP
layer, which is the dependency arrow pointing exactly the wrong way.

Now the arrow runs::

    main.py (FastAPI)  ->  playwright_port.py  ->  browser.py (BrowserPort)

The HTTP layer is a CONSUMER of the browser layer.  A unit test, a CLI, a
future non-HTTP driver, or a second transport can drive a real browser without
booting a web server — and, just as importantly, cannot accidentally acquire a
web server by importing one.

The module-scope ``logger`` is deliberately still named ``"qe-explorer"``:
log lines are part of the behaviour this milestone must not change.
"""
from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence
from urllib.parse import urlsplit

from dataclasses import replace as _dc_replace

from . import emit
from . import field_signature
from . import network_evidence as netev
from . import page_lifecycle as pl
from . import perception
from .browser import (BrowserPort, NavResult, RawObservation,
                      is_challenge_dialog, is_rejection_text, verify_intent)
from .fingerprint import interactive_signature
from .interaction_ladder import Rung, ladder_for
from . import observation_health
from .inventory_js import (CAPTURE_HOOKS_JS, DISPLAYED_VALUES_JS, INVENTORY_JS,
                           OPAQUE_JS, PII_REGIONS_JS)

logger = logging.getLogger("qe-explorer")


def context_defaults() -> dict[str, Any]:
    """The ``browser.new_context`` options the BROWSER LAYER requires (M1.5).

    Declared here rather than inline in ``app.main`` because they are facts
    about how a browser must be driven, not about how an HTTP request is
    served — the same reason ``_LAUNCH_ARGS`` lives in this module.  ``app.main``
    merges these and then layers the per-crawl bindings (session, headers,
    credentials) on top.

    ``accept_downloads`` is T-ND-03's precondition and the one that had never
    been stated.  Playwright's current default happens to be ``True``, which is
    exactly why it is written down: a default that a version bump can flip is
    not a guarantee, and the difference between the two settings is whether a
    click on "Download Sales Packet" produces an artifact or is silently
    cancelled at the browser edge before any listener can see it.
    """
    return {
        "service_workers": "block",
        "ignore_https_errors": False,
        "accept_downloads": True,
    }


async def install_capture_hooks(context: Any) -> bool:
    """Install the capture init script on a freshly created browser context.

    M3.2 / T-FR-02.  This MUST be called between ``browser.new_context(...)`` and
    the creation of the first page.  ``add_init_script`` is evaluated in every
    page and every frame of the context BEFORE any application script runs,
    which is the only window in which a closed shadow root is observable at all:
    ``attachShadow({mode:"closed"})`` hands its root to the component and to
    nobody else, and no API recovers it afterwards.  Retrofitting after the
    component exists cannot work — see the contract on
    :data:`app.inventory_js.CAPTURE_HOOKS_JS`.

    Returns whether the script was installed.  A context that refuses it is
    logged and the crawl continues exactly as blind as it was before: a closed
    shadow root then stays a named opaque row, which is honest.  It is never a
    crawl-stopping failure, because capture degrading is not the same as capture
    lying.
    """
    if context is None:
        return False
    add = getattr(context, "add_init_script", None)
    if add is None:
        return False
    try:
        await add(CAPTURE_HOOKS_JS)
    except Exception as exc:                              # pragma: no cover
        logger.warning(
            "qec.explorer.capture_hooks_not_installed error=%s — closed shadow "
            "roots on this crawl stay opaque", str(exc)[:200])
        return False
    return True


# ─── Browser-layer tuning constants ──────────────────────────────────────────
# These describe how a BROWSER is driven, so they belong to the browser layer.
# ``_LAUNCH_ARGS`` and ``_ACTION_TIMEOUT_MS`` are additionally imported back by
# app.main, which owns browser LAUNCH; the values are unchanged.

# Playwright launch args: --no-sandbox is required for Chromium in an
# unprivileged container (mirrors Dockerfile.runner:28).
_LAUNCH_ARGS = ["--no-sandbox", "--disable-dev-shm-usage"]
_SETTLE_MS = 1500          # post-action network-settle budget (best-effort)
_ACTION_TIMEOUT_MS = 5000  # per-locator action timeout
# Hydration gate: after networkidle, poll a cheap DOM-quiescence signature until it
# is stable for N consecutive reads, so a slow-hydrating SPA (controls mounted after
# networkidle) is never inventoried half-rendered. Bounded + best-effort.
_STABILIZE_MS = 12000      # max hydration-stabilization budget beyond networkidle
                           # (heavy client-rendered SPAs mount controls late)
_STABLE_POLL_MS = 220      # interval between quiescence probes
_STABLE_READS = 2          # consecutive equal reads that count as settled
# A page whose visible-interactive count is below this has NOT rendered yet — a
# client-rendered SPA shell before its framework paints. A stable-EMPTY signature
# must NOT satisfy the hydration gate, else the crawler inventories a blank shell
# (0 forms) and misses the client-rendered login/form.
_MIN_INTERACTIVE = 2
#: Is the page VISIBLY WORKING? A running CSS animation is the one statement a
#: spinner makes in every framework, and it needs no vocabulary, no class name
#: and no page knowledge to read.
#:
#: WHY `_MIN_INTERACTIVE` IS NOT ENOUGH. That guard waits while a page has too
#: FEW interactive controls to be rendered. It is defeated by any application
#: whose chrome renders before its content: on vkpower-life's underwriting step,
#: the signed-in header alone offers Dashboard / Beneficiaries / Get a Quote, so
#: the count is comfortably over the floor for the ~1.8s the decision spinner is
#: on screen. The gate settled, the walk inventoried a page whose only controls
#: were nav links, tier 3 picked "Get a Quote", and a fifteen-step funnel became
#: an eleven-step loop back to the start.
#:
#: Checked ONLY once the signature has otherwise gone stable, so a page that is
#: not animating pays a single extra evaluate at the settle moment. Bounded by
#: `_MAX_BUSY_POLLS`, because a decorative infinite animation is not a reason to
#: wait forever -- after that the page settles regardless and the crawl is no
#: worse off than before this existed.
_BUSY_JS = (
    "(()=>{try{if(!document.getAnimations)return 0;"
    "var a=document.getAnimations();for(var i=0;i<a.length;i++){"
    "if(a[i].playState==='running')return 1;}return 0;}catch(x){return 0;}})()"
)
_MAX_BUSY_POLLS = 14       # ~3s at _STABLE_POLL_MS, inside _STABILIZE_MS
# Viewport materialization (lazy-load / virtual-scroll) — bounded step-scroll.
_MATERIALIZE_STEPS = 8
# Adaptive backoff on an explicit server rate-limit (429), then ONE retry.
_DEFAULT_BACKOFF_MS = 2000
_MAX_BACKOFF_MS = 15000

# ─── M1.5 page-lifecycle bounds ──────────────────────────────────────────────
#: How long a newly created page gets to reach ``domcontentloaded`` before the
#: adoption decision is made on whatever it has.  A popup that is still loading
#: after this is judged on its URL, which is enough to classify it.
_POPUP_LOAD_MS = 8000
#: How long a popup created at ``about:blank`` gets to navigate somewhere.
#: ``window.open()`` followed by ``location.href = …`` is the classic shape, and
#: judging it at creation time would always read "never navigated".  This is a
#: Playwright ``wait_for_url`` predicate, NOT a sleep.
_POPUP_NAVIGATE_MS = 5000
#: How long a download gets to finish streaming to disk.
_DOWNLOAD_MS = 30000
#: Hard cap on the between-drain browser-event buffer.
_EVENT_BUFFER_MAX = 400
#: Hard cap on captured download artifacts per crawl — an application that
#: streams a file on every click must not fill the evidence volume.
_MAX_ARTIFACTS = 50
#: Open pages tolerated before the port starts closing RETAINED ones (never the
#: active page, never the primary).  An app that opens a tab per click would
#: otherwise accumulate Chromium targets for the whole crawl.
_MAX_OPEN_PAGES = 8

# ─── M3.2 frame-entry bounds (T-FR-01) ───────────────────────────────────────
#: How many cross-origin frames one observation may enter.  A page that embeds a
#: dozen ad frames must not turn one inventory read into a dozen round trips.
_MAX_ENTERED_FRAMES = 6
#: How deep frame entry recurses.  1 = the embeds on the page; 2 = the embed a
#: vendor's own embed loads (a 3-D-Secure step inside a payment frame), which is
#: where real checkout flows stop.
_MAX_FRAME_DEPTH = 2
#: Controls read from a single frame.  A frame is a widget, not an application.
_MAX_FRAME_CONTROLS = 60

#: Cheap page-quiescence signature: visible-interactive count : readyState :
#: scrollHeight. Stable across two reads ⇒ the DOM has stopped mounting controls.
#: The DOM-quiescence signature the hydration gate polls.
#:
#: ``location.pathname`` is part of it, and that is not cosmetic. A client-side
#: route change (``router.push``) settles network instantly, so without the path
#: the gate could reach its "stable for N reads" verdict against the page being
#: LEFT — many interactive controls, nothing changing — and the observation that
#: follows would then read the page being ARRIVED AT, mid-render.
#:
#: Measured on vkpower-life: clicking "Continue to Underwriting Decision" routes
#: to /apply/decision/, which renders a spinner for ~1.8s before mounting
#: "Continue to Payment". The walk saw a state with ZERO controls, concluded
#: there was nothing to advance on, and tier 3 sent it back to the quote page —
#: turning a fifteen-step funnel into an eleven-step loop. The empty-shell guard
#: (``_MIN_INTERACTIVE``) was already designed to wait for exactly this and
#: never got the chance, because the signature it was watching belonged to the
#: previous page.
#:
#: With the path in the signature, a navigation mid-gate resets stability and
#: the gate waits for the page it actually landed on. Costs nothing when no
#: navigation happens: the path is constant and the signature is unchanged.
_QUIESCENCE_JS = (
    "(()=>{try{var e=document.querySelectorAll("
    "'a[href],button,input,select,textarea,[role],[tabindex]');"
    "var n=0;for(var i=0;i<e.length;i++){var el=e[i];"
    "if(el.offsetParent!==null||(el.getClientRects&&el.getClientRects().length))n++;}"
    "return n+':'+document.readyState+':'+Math.round("
    "document.body?document.body.scrollHeight:0)+':'+"
    "(location.pathname||'')+(location.hash||'');}catch(x){return 'err';}})()"
)


def _retry_after_ms(resp: Any) -> int:
    """Backoff for a 429 — the ``Retry-After`` seconds header when present (clamped),
    else a default."""
    try:
        ra = str((resp.headers or {}).get("retry-after", "")).strip()
        if ra.isdigit():
            return int(ra) * 1000
    except Exception:
        pass
    return _DEFAULT_BACKOFF_MS


def _stable_source_url(url: str) -> str:
    """A download's source URL, with per-load opaque handles collapsed.

    A client-generated download ("Export to CSV") is served from a
    ``blob:http://host/4d2c1836-fcfb-…`` URL whose UUID is minted fresh on every
    page load.  Recording it verbatim does two bad things and no good one: it
    identifies nothing a reader could ever resolve, and its digit runs trip the
    PII scrubber into stamping ``[REDACTED:phone]`` in the middle of a URL —
    a false positive that makes the evidence read as if it had leaked a phone
    number.  ``data:`` URIs are collapsed for a stronger reason: the whole file
    is IN the URL, so recording it would copy the payload into the manifest.

    An ordinary ``http(s)`` source URL is returned untouched; it is the real
    evidence of where the file came from.
    """
    raw = str(url or "").strip()
    lowered = raw.lower()
    if lowered.startswith("blob:"):
        # blob:http://host:port/<uuid> -> blob:http://host:port. Split off the
        # LAST segment only: the origin is the evidence, the handle is not, and
        # partitioning on the first "/" would leave the useless string "blob:http:".
        rest = raw[5:]
        origin = rest.rsplit("/", 1)[0] if "/" in rest else rest
        return f"blob:{origin}" if origin else "blob:"
    if lowered.startswith("data:"):
        mime = raw[5:].split(";", 1)[0].split(",", 1)[0]
        return f"data:{mime}" if mime else "data:"
    return raw


def _page_url(page: Any) -> str:
    """A page's URL, or ``""`` — never raises, including on a closed target."""
    try:
        return str(getattr(page, "url", "") or "")
    except Exception:
        return ""


def _frame_origin(frame: Any) -> str:
    """``scheme://host[:port]`` of a frame, or "".

    ORIGIN, NOT URL.  A vendor frame's URL routinely carries a client secret, a
    session token or a payment intent id in its query string, and this string
    travels into the manifest and out to qe-central.  The origin is what
    identifies the embed; the rest is somebody's secret.
    """
    try:
        parts = urlsplit(str(getattr(frame, "url", "") or ""))
    except Exception:
        return ""
    if not parts.scheme or not parts.netloc:
        return ""
    return "%s://%s" % (parts.scheme, parts.netloc)


def _page_is_closed(page: Any) -> bool:
    """True when Playwright says the page is gone (or we cannot tell)."""
    try:
        closed = getattr(page, "is_closed", None)
        return bool(closed()) if callable(closed) else False
    except Exception:
        return True


def _safe_headers(obj: Any) -> dict[str, str]:
    """The cheap SYNC ``headers`` dict of a Playwright request/response (keys are
    lowercased by Playwright), or ``{}`` — never awaits, never raises."""
    try:
        return {str(k).lower(): str(v) for k, v in dict(obj.headers or {}).items()}
    except Exception:
        return {}


def _safe_post_data(request: Any) -> str | None:
    """The SYNC ``post_data`` of a Playwright request, or ``None``.

    ``post_data`` is a property, not a coroutine, so a request body is one of the
    few things the synchronous ``response`` listener can honestly reach.  It is
    handed straight to :func:`app.network_evidence.describe_body`, which reduces
    it to a size + media type + masked KEY NAMES — the body never leaves this
    function as content.
    """
    try:
        data = getattr(request, "post_data", None)
        return str(data) if data else None
    except Exception:
        return None


# ─── The Playwright BrowserPort adapter (the only Playwright code) ───────────


class PlaywrightBrowserPort(BrowserPort):
    """Concrete :class:`BrowserPort` over a Playwright page/context.

    Every method is DEFENSIVE: a Playwright failure is surfaced as an honest
    observation (``NavResult.ok=False``, an empty inventory, an ``error`` after)
    rather than raised into the pure state machine.  Locators are built to
    resolve the SAME way the frozen compiler's ladder does — getByRole/getByLabel
    on the accessible NAME, scoped by an ``_ANCHOR_ROLE`` landmark and by the
    ``frame_selector`` frameLocator chain — so recorded evidence is faithfully
    re-bindable downstream.  (Live-crawl fidelity is verified on the VM.)
    """

    def __init__(self, page: Any, context: Any, *,
                 proven_mechanics: dict[str, str] | None = None,
                 medic_oracle: Any = None,
                 vision_medic_oracle: Any = None,
                 artifact_dir: str = "") -> None:
        # M1.5 — the ACTIVE page, not "the page".  A browser context holds many
        # pages and the journey's page can change (a popup, a target=_blank tab,
        # the original closing under us).  Every method below still reads
        # ``self._page``; it is now a property over the registry's ACTIVE entry,
        # so an adoption re-points the whole adapter — actions, inventory,
        # fingerprint inputs and evidence — in one assignment instead of in
        # thirty call sites that could each be forgotten.
        self._registry = pl.PageRegistry()
        self._active_page = page
        self._registry.register_primary(page, url=_page_url(page))
        self._context = context
        self._proven_mechanics = dict(proven_mechanics or {})
        self._medic_oracle = medic_oracle
        # A28 / R5 — the VISION medic: the rung after the text medic, for a
        # control whose behaviour is not in the DOM at all. Optional and
        # None-by-default, so a deployment without vision behaves exactly as
        # before: the ladder exhausts, the text medic tries, and the control
        # becomes named residue.
        self._vision_medic_oracle = vision_medic_oracle
        self._artifact_dir = str(artifact_dir or "")
        self._artifacts_written = 0
        # API/network mining — a bounded buffer of the XHR/fetch calls the app
        # makes, filled by a passive `response` listener and drained per-visit by
        # the crawler.  Query strings are dropped + paths PII-scrubbed HERE (at
        # source) so raw PII never lingers in the buffer.
        self._net_buffer: list[dict[str, Any]] = []
        # M3.2 / T-FR-01 — the frame-entry ledger, drained per-visit like the
        # network buffer.  One row per cross-origin frame this port MET: entered
        # (with what was read from inside it) or refused (with why).  A frame we
        # could not address must stay as visible as one we could, or "no frames
        # entered" reads as "no frames present".
        self._frames_seen: list[dict[str, Any]] = []
        # M2.5 / T-NET-02 — the CRAWL-WIDE event ordinal.  Assigned at capture and
        # never re-derived, so three retries stay three ordered events no matter
        # what any downstream transport does to list order.  It counts every
        # event the listener saw, including ones the buffer cap refused, so a gap
        # in the sequence is itself the evidence that something was dropped.
        self._net_sequence = 0
        self._net_dropped = 0
        # M2.5 — WHEN THE CURRENT CAPTURE WINDOW OPENED (the previous drain).
        #
        # Found on a live application, not on the fixture. A page_state's
        # ``first_seen_ms`` is stamped when the crawl OBSERVES the state, but the
        # requests attributed to that state include the ones the browser fired
        # while NAVIGATING to it — a Next.js route prefetch goes out before the
        # new page exists to be observed. Five of thirteen events in a real crawl
        # therefore carried a true timestamp that fell before the visit window
        # containing them, and the fixture could not show this because it is a
        # single page that never navigates.
        #
        # Clamping the timestamp into the window (what the screenshot path does)
        # would make the assertion pass by writing down a time the request did
        # not happen. Instead the record carries the window that ACTUALLY
        # corresponds to it: everything from the previous drain to this one.
        self._net_window_start_ms = 0
        # M2.5 / T-NET-01 — the CRAWL-RELATIVE clock.  A network event stamped
        # with raw ``time.monotonic()`` (an arbitrary epoch, system boot on
        # Linux) can never fall inside a visit window measured in ms-since-crawl-
        # start, which is why the baseline stream was unjoinable.  The adapter
        # starts its own clock so evidence captured before the crawler exists is
        # still on the right epoch, and :meth:`bind_clock` replaces it with the
        # crawl's own the moment the Crawler is constructed.
        self._clock = emit.MonotonicClock()
        # M1.5 — the special-browser-event buffer, drained by the crawler the
        # same way ``_net_buffer`` is.
        self._event_buffer: list[dict[str, Any]] = []
        # Pages the context reported but that have not been adjudicated yet.
        # The ``page`` listener is deliberately SYNCHRONOUS and does nothing but
        # append here: adoption needs awaits (load state, URL), and doing them
        # inside the event callback would race the very action that produced the
        # popup.  Adjudication happens at a defined synchronisation point —
        # :meth:`_reconcile_pages`, called from :meth:`_settle`, i.e. after
        # every action and every navigation.
        self._pending_pages: list[Any] = []
        self._closed_pages: list[Any] = []
        self._observed_pages: set[int] = set()
        # Observers whose subscription could not be sent because __init__ ran
        # outside a running event loop — retried by _ensure_observers(). See
        # _attach_page_observers for why this is deferred rather than dropped.
        self._pending_observers: list[tuple[Any, list[str]]] = []
        # In-flight download captures. A download is scheduled by a listener and
        # completes later; these are joined at every synchronisation point so a
        # drain can never outrun a capture (see _on_download).
        self._download_tasks: list[Any] = []
        # WHAT THE CRAWL WAS DOING when a dialog/download fires.  Set by the act
        # path before the action is performed, so dialog intent resolution is
        # not reduced to string-matching a message: the control's accessible
        # name and the verb are first-class inputs.
        self._action_label = ""
        self._action_verb = ""
        # M2.5 / T-NET-03 — the identity of the action currently in flight.  A
        # network event captured while this is set was caused by THIS action:
        # the adapter is the only object that knows a request arrived between
        # "click Get quote" starting and settling.  Bumped on every genuinely new
        # action by :meth:`_set_action_context`, which already exists for exactly
        # this reason on the dialog/download side.
        self._action_seq = 0
        self._action_token = ""
        # Has THIS action already adopted a popup? Scoped to the action rather
        # than to one reconcile pass, because a single click is adjudicated on
        # both sides of the settle quiesce (see _settle) and "first usable popup
        # wins" would otherwise mean "first per pass" — two windows from one
        # click would both take over, and which one ended up active would depend
        # on how fast each happened to load.
        self._adopted_this_action = False
        # Injected by the Crawler (see :meth:`bind_journey_context` /
        # :meth:`bind_scope_check`) — never imported, so the browser layer keeps
        # pointing away from the crawler.
        self._journey_context: Optional[Callable[[], Mapping[str, Any]]] = None
        self._scope_check: Optional[Callable[[str], bool]] = None
        self._attach_page_observers(page)
        try:
            # T-ND-01 — the listener that did not exist.  A context can hold
            # many pages; without this the crawler could not learn that one had
            # been created, let alone follow it.
            self._context.on("page", self._on_new_page)
        except Exception:
            logger.warning("qec.explorer.page_listener_unavailable")

    # -- M1.5 wiring (injected by the crawler; never imported) -----------------

    def bind_journey_context(
        self, provider: Optional[Callable[[], Mapping[str, Any]]]
    ) -> None:
        """Supply a zero-arg reader for the live journey context.

        Returns a mapping with ``phase`` (the guard phase), ``observe_only``
        (the resolved M0.5 posture) and ``approved_labels`` (A4.3 grants).  A
        CALLABLE rather than a snapshot because the phase changes several times
        per crawl and a value copied at construction would be stale by the first
        dialog.  Unbound, the policy runs on its own conservative defaults.
        """
        self._journey_context = provider

    def bind_scope_check(self, predicate: Optional[Callable[[str], bool]]) -> None:
        """Supply the crawl's in-scope test, so a popup that lands on a third
        party (an IdP, a help centre, a payment processor) is recorded but never
        adopted — the same gate ``_expand`` applies to an off-domain redirect."""
        self._scope_check = predicate

    @property
    def _page(self) -> Any:
        """THE ACTIVE PAGE.  Read-only on purpose: the only way to change which
        page the adapter drives is :meth:`_adopt`, which records why."""
        return self._active_page

    @property
    def registry(self) -> pl.PageRegistry:
        """The page lifecycle registry (evidence + tests read it; nothing writes)."""
        return self._registry

    def _journey(self) -> dict[str, Any]:
        ctx: Mapping[str, Any] = {}
        if self._journey_context is not None:
            try:
                ctx = self._journey_context() or {}
            except Exception:  # a context reader must never break an action
                ctx = {}
        return {
            "phase": str(ctx.get("phase") or ""),
            "observe_only": bool(ctx.get("observe_only", False)),
            "approved_labels": tuple(ctx.get("approved_labels") or ()),
        }

    def _in_scope(self, url: str) -> bool:
        if self._scope_check is None:
            return True                 # unbound (unit test / fake) — no gate
        try:
            return bool(self._scope_check(url))
        except Exception:
            return False                # a scope test that errors fails CLOSED

    #: Resource types worth recording as API evidence (the app's real surface);
    #: document/stylesheet/image/font/script/media are chrome, not API calls.
    #: ``eventsource`` (Server-Sent Events) joins xhr/fetch — a real-time stream is
    #: as much the app's API surface as a poll (D).
    _NET_RESOURCE_TYPES = frozenset({"xhr", "fetch", "eventsource"})
    #: Hard cap on the between-drain buffer so a runaway SPA cannot grow it without
    #: bound (the crawler applies its own per-state cap on drain).
    _NET_BUFFER_MAX = 500

    def _record_net(self, entry: dict[str, Any]) -> None:
        """Buffer ONE network event (bounded, ordered, correlated).

        The ordinal and the correlation stamp are applied HERE rather than by
        the caller so every producer — response, websocket, and any listener
        added later — is ordered and attributed on the same rule.  Reaching the
        cap is COUNTED and reported on drain: the baseline dropped silently, so
        a truncated stream read as a complete one.
        """
        self._net_sequence += 1
        entry["sequence"] = self._net_sequence
        entry["timestamp_ms"] = self._clock.now_ms()
        entry["action_token"] = self._action_token
        entry["action_label"] = self._action_label
        entry["action_verb"] = self._action_verb
        entry["page_token"] = self._registry.active_token()
        if len(self._net_buffer) < self._NET_BUFFER_MAX:
            self._net_buffer.append(entry)
        else:
            self._net_dropped += 1

    def _on_response(self, response: Any) -> None:
        """Passive `response` listener — record ONE XHR/fetch/SSE call's shape.

        Sync + fully defensive (a listener exception must never surface into the
        page): reads only cheap sync properties (never awaits a body), drops the
        query string, and PII-scrubs the path before buffering."""
        try:
            request = response.request
            rtype = (getattr(request, "resource_type", "") or "")
            resp_headers = _safe_headers(response)
            resp_mime = resp_headers.get("content-type", "").split(";", 1)[0]
            # capture xhr/fetch/eventsource, OR anything whose response is an SSE
            # stream regardless of how Chromium labelled the resource type.
            if rtype not in self._NET_RESOURCE_TYPES and resp_mime != "text/event-stream":
                return
            parts = urlsplit(str(getattr(response, "url", "") or ""))
            if (parts.scheme or "").lower() not in ("http", "https"):
                return
            url = emit.scrub_value(f"{parts.scheme}://{parts.netloc}{parts.path}").value
            req_headers = _safe_headers(request)
            is_sse = (resp_mime == "text/event-stream" or rtype == "eventsource")
            req_mime = req_headers.get("content-type", "").split(";", 1)[0]
            resp_bytes = resp_headers.get("content-length", "")
            self._record_net({
                "method": str(getattr(request, "method", "") or "").upper(),
                "url": url,
                "path_template": netev.path_template(parts.path),
                "has_query": bool(parts.query),
                "status": str(getattr(response, "status", "") or ""),
                "resource_type": "sse" if is_sse else rtype,
                "request_mime": req_mime,
                "response_mime": resp_mime,
                "response_bytes": resp_bytes,
                # M2.5 / T-NET-02 — headers + body METADATA, allow-listed and
                # value-redacted by :mod:`app.network_evidence`.  A header not
                # named in the allow-list is dropped; a header whose value is a
                # credential is recorded as a named presence, never as its value.
                "request_headers": netev.redact_headers(req_headers),
                "response_headers": netev.redact_headers(resp_headers),
                "request_body": netev.describe_body(_safe_post_data(request), req_mime),
                "auth_pattern": netev.auth_pattern(req_headers),
                "response_shape": netev.response_shape(resp_mime, resp_bytes),
                # The response BODY is not read: this listener is synchronous by
                # design (M1.5 — an awaiting listener races the action that
                # produced it), and reading a body requires an await.  Saying so
                # on the event is what stops a media-type inference downstream
                # from being mistaken for a body that was actually parsed.
                "shape_source": "media_type",
                "from_service_worker": bool(getattr(response, "from_service_worker", False)),
            })
        except Exception:  # never let a listener crash affect the page
            pass

    def _on_request_failed(self, request: Any) -> None:
        """Passive `requestfailed` listener (M2.5) — a request that got NO response.

        The baseline captured only responses, so a connection refused, a DNS
        failure or an aborted fetch left no evidence at all — and the network
        oracle's ``failed``/``error`` branch could therefore never fire from crawl
        evidence, however hard the adapter tried.  A request that died is exactly
        the evidence a reviewer needs, so it is recorded as an event with status
        0 and the failure text.
        """
        try:
            rtype = (getattr(request, "resource_type", "") or "")
            if rtype not in self._NET_RESOURCE_TYPES:
                return
            parts = urlsplit(str(getattr(request, "url", "") or ""))
            if (parts.scheme or "").lower() not in ("http", "https"):
                return
            failure = getattr(request, "failure", None)
            detail = str(failure or "")[:200]
            req_headers = _safe_headers(request)
            req_mime = req_headers.get("content-type", "").split(";", 1)[0]
            self._record_net({
                "method": str(getattr(request, "method", "") or "").upper(),
                "url": emit.scrub_value(
                    f"{parts.scheme}://{parts.netloc}{parts.path}").value,
                "path_template": netev.path_template(parts.path),
                "has_query": bool(parts.query),
                "status": "0",
                "failed": True,
                "error": detail or "request_failed",
                "resource_type": rtype,
                "request_mime": req_mime,
                "response_mime": "",
                "response_bytes": "",
                "request_headers": netev.redact_headers(req_headers),
                "response_headers": {},
                "request_body": netev.describe_body(_safe_post_data(request), req_mime),
                "auth_pattern": netev.auth_pattern(req_headers),
                "response_shape": "none",
                "shape_source": "request_failed",
                "from_service_worker": False,
            })
        except Exception:
            pass

    def _on_websocket(self, ws: Any) -> None:
        """Passive `websocket` listener (D) — record the WS OPEN as one evidence
        entry (endpoint + scheme). Frame PAYLOADS are deliberately NOT captured
        (a socket carries live user/session data — the same PII posture as the
        query-drop for HTTP); the app's real-time endpoint is the evidence."""
        try:
            parts = urlsplit(str(getattr(ws, "url", "") or ""))
            if (parts.scheme or "").lower() not in ("ws", "wss", "http", "https"):
                return
            url = emit.scrub_value(f"{parts.scheme}://{parts.netloc}{parts.path}").value
            self._record_net({
                "method": "WS",
                "url": url,
                "has_query": bool(parts.query),
                "status": "101",                      # WS upgrade
                "resource_type": "websocket",
                "request_mime": "",
                "response_mime": "",
                "response_bytes": "",
                "path_template": netev.path_template(parts.path),
                "request_headers": {},
                "response_headers": {},
                "request_body": netev.describe_body(None, ""),
                "auth_pattern": "none",
                "response_shape": "stream",
                "shape_source": "websocket_upgrade",
                "from_service_worker": False,
            })
        except Exception:
            pass

    async def drain_network(self) -> list[dict[str, Any]]:
        """Return + CLEAR the network calls buffered since the last drain.

        A drain that hit the buffer cap appends ONE ``buffer_truncated`` event
        naming how many were refused, mirroring what the browser-event buffer
        already does.  The baseline dropped silently, so a clipped stream was
        indistinguishable from a complete one — which is the same green-wash the
        sequence numbers exist to prevent.
        """
        drained = self._net_buffer
        self._net_buffer = []
        window_start = self._net_window_start_ms
        for entry in drained:
            entry["capture_window_start_ms"] = window_start
        self._net_window_start_ms = self._clock.now_ms()
        if self._net_dropped:
            drained.append({
                "event": "buffer_truncated",
                "method": "", "url": "", "status": "",
                "dropped": self._net_dropped,
                "reason": (f"more than {self._NET_BUFFER_MAX} network events "
                           f"between drains"),
                "sequence": self._net_sequence,
                "timestamp_ms": self._clock.now_ms(),
                "capture_window_start_ms": window_start,
            })
            self._net_dropped = 0
        return drained

    def bind_clock(self, clock: Any) -> None:
        """M2.5 / T-NET-01 — adopt the CRAWL's clock.

        Called by the Crawler once, immediately after it builds its own
        :class:`app.emit.MonotonicClock`.  Until then the adapter runs its own
        clock on the same epoch shape, so evidence captured during construction
        or first navigation is never stamped on a foreign epoch; after it, every
        network event, every visit window and every action timestamp are readings
        of ONE clock, which is the whole condition for a join.

        Guarded rather than trusting: a clock without ``now_ms`` is refused and
        the adapter keeps the one it has, because a listener that raises would
        take the page down with it.
        """
        if clock is not None and callable(getattr(clock, "now_ms", None)):
            self._clock = clock

    def network_sequence(self) -> int:
        """How many network events the listener has SEEN this crawl.

        Distinct from how many were buffered: the difference is what the buffer
        cap refused.
        """
        return self._net_sequence

    # ─── M1.5 · page lifecycle ────────────────────────────────────────────────

    def _record_event(self, record: dict[str, Any]) -> None:
        """Buffer ONE browser-event record (bounded).

        Bounded rather than unbounded for the same reason the network buffer is:
        an application that opens a tab or raises a dialog in a loop must not be
        able to grow the adapter's memory without limit.  Reaching the cap is
        itself recorded once, so a truncated stream never reads as a complete one.
        """
        if len(self._event_buffer) < _EVENT_BUFFER_MAX:
            self._event_buffer.append(record)
        elif len(self._event_buffer) == _EVENT_BUFFER_MAX:
            self._event_buffer.append({
                "event": "buffer_truncated",
                "reason": f"more than {_EVENT_BUFFER_MAX} browser events between drains",
                "timestamp_ms": int(time.time() * 1000),
            })

    async def drain_browser_events(self) -> list[dict[str, Any]]:
        """Return + CLEAR the special browser events buffered since the last drain.

        Joins any in-flight download capture FIRST: a drain that reported an
        empty list while a file was still being written would leave the artifact
        on disk with nothing in the manifest pointing at it.
        """
        await self._await_downloads()
        drained = self._event_buffer
        self._event_buffer = []
        return drained

    async def active_page_token(self) -> str:
        """T-ND-04 — which page the adapter is acting against right now."""
        return self._registry.active_token()

    #: The observers every page the journey touches must carry.
    _PAGE_OBSERVERS = (
        ("response", "_on_response", "network"),
        ("requestfailed", "_on_request_failed", "network_failed"),
        ("websocket", "_on_websocket", "websocket"),
        ("dialog", "_on_dialog", "dialog"),
        ("download", "_on_download", "download"),
        ("close", "_on_page_close", "close"),
    )

    def _attach_page_observers(self, page: Any) -> None:
        """Attach EVERY observer this adapter needs to ``page`` (idempotent).

        Called for the primary page at construction and for every ADOPTED page,
        so a popup that becomes the journey is observed exactly as richly as the
        page it replaced.  Before M1.5 the two network observers were attached
        once, to one page, in ``__init__`` — a tab the crawl moved into
        therefore produced no API evidence and no WebSocket evidence at all,
        silently.

        WHY A FAILURE HERE IS DEFERRED RATHER THAN SWALLOWED, and this is a
        defect M1.5 found rather than introduced.  Playwright subscribes to
        ``response`` and ``dialog`` by sending a protocol message, so
        ``page.on()`` for those two REQUIRES a running event loop — and this
        adapter's ``__init__`` is ordinary synchronous code.  Constructed from
        inside a coroutine (which is how ``app.main._run_job`` does it) both
        attach; constructed outside one they raise ``no running event loop`` and,
        before this change, were logged and abandoned.  That is exactly what
        happened in the characterization lane, where the port is built
        synchronously: every crawl there captured ZERO network evidence and said
        so only in a warning nobody was reading.  Failures are therefore queued
        and retried by :meth:`_ensure_observers` at the first async call, where
        a loop is guaranteed to exist.

        Idempotent by page identity: re-adopting a page (the original, after a
        popup closes) must not double-register a listener and record every
        response twice.
        """
        key = id(page)
        if key in self._observed_pages:
            return
        self._observed_pages.add(key)
        pending = self._attach_events(page, [e for e, _h, _w in self._PAGE_OBSERVERS])
        if pending:
            self._pending_observers.append((page, pending))

    def _attach_events(self, page: Any, events: Sequence[str]) -> list[str]:
        """Attach ``events`` to ``page``; return the ones that did not take."""
        handlers = {event: (getattr(self, attr), why)
                    for event, attr, why in self._PAGE_OBSERVERS}
        failed: list[str] = []
        for event in events:
            handler, why = handlers[event]
            try:
                page.on(event, handler)
            except Exception as exc:
                failed.append(event)
                # NAME THE REASON. A listener that silently failed to attach is
                # indistinguishable from an application that never raised the
                # event, and the two call for opposite investigations.
                logger.info("qec.explorer.%s_listener_deferred error=%s",
                            why, str(exc)[:200])
        return failed

    async def _ensure_observers(self) -> None:
        """Retry any observer that could not attach at construction time.

        Called from the two async chokepoints every path passes through
        (:meth:`goto` and :meth:`_settle`), so by the time a page can produce a
        response or raise a dialog its listeners are attached.  A no-op — one
        list truth-test — once everything has taken, which is after the first
        call of a crawl.
        """
        if not self._pending_observers:
            return
        still_pending: list[tuple[Any, list[str]]] = []
        for page, events in self._pending_observers:
            if _page_is_closed(page):
                continue
            failed = self._attach_events(page, events)
            if failed:
                still_pending.append((page, failed))
            else:
                logger.info("qec.explorer.listeners_attached_late events=%s",
                            ",".join(events))
        self._pending_observers = still_pending
        if still_pending:
            logger.warning(
                "qec.explorer.listeners_still_unattached events=%s — the crawl "
                "will under-report the corresponding evidence",
                ";".join(",".join(ev) for _p, ev in still_pending))

    def _on_new_page(self, page: Any) -> None:
        """T-ND-01 — ``context.on("page")``.  SYNCHRONOUS and near-free.

        This fires in the middle of the very click that produced the popup.
        Everything that would make the decision — waiting for a load state,
        reading a settled URL — needs an await, and awaiting here would
        interleave with the action still in flight.  So this does one thing:
        remember the handle.  :meth:`_reconcile_pages` adjudicates it at the
        next synchronisation point, which is the end of the action.
        """
        try:
            # The opener is whichever page is ACTIVE at the moment the context
            # reports the new one — recorded by token as well as by URL, so a
            # chain of hand-offs stays reconstructable.
            self._registry.register(page, opener_url=self._safe_url(),
                                    opener_token=self._registry.active_token())
            self._pending_pages.append(page)
        except Exception:  # a listener exception must never surface into the page
            pass

    def _on_page_close(self, page: Any) -> None:
        """A page left the browser.  SYNCHRONOUS; the promotion happens in
        :meth:`_reconcile_pages`, which can await the replacement's load state."""
        try:
            self._closed_pages.append(page)
        except Exception:
            pass

    async def _reconcile_pages(self) -> None:
        """THE synchronisation point: adjudicate every pending page event.

        Called from :meth:`_settle`, which runs after every action and every
        navigation — so adoption is decided at a defined moment in the walk
        rather than inside a listener racing the action.  Ordinary navigation
        pays two empty-list checks.

        The ordering this resolves, explicitly::

            click ─▶ popup event ─▶ popup navigation ─▶ DOMContentLoaded
                                                            │
                        _reconcile_pages ◀──────────────────┘
                                │
                                ▼  active page = popup
                        _settle (on the ADOPTED page)
                                │
                                ▼
                        url_after / inventory / fingerprint  ── all read the popup

        Closures are handled FIRST: if the active page died, the walk must be
        re-homed before a popup decision is taken against a dead handle.
        """
        if self._closed_pages:
            await self._handle_closures()
        if self._pending_pages:
            await self._adjudicate_pending()
        if self._registry.open_count() > _MAX_OPEN_PAGES:
            await self._prune_retained_pages()

    async def _handle_closures(self) -> None:
        closed, self._closed_pages = self._closed_pages, []
        for page in closed:
            entry = self._registry.get(page)
            if entry is None:
                continue
            was_active = page is self._active_page
            token, url = entry.token, entry.url or _page_url(page)
            self._registry.close(page)
            self._observed_pages.discard(id(page))
            promoted_token, promoted_url = "", ""
            if was_active:
                # PAGE REPLACEMENT.  The journey's page is gone; somebody has to
                # inherit it or every later action fails against a dead target.
                # Newest open page first (see PageRegistry.candidates_for_promotion);
                # if there is none, the adapter keeps the dead handle so its
                # failures are honest Playwright errors rather than AttributeErrors
                # on None, and the crawl's own goto can recover the context.
                for candidate in self._registry.candidates_for_promotion():
                    if _page_is_closed(candidate.handle):
                        continue
                    adopted = self._registry.adopt(
                        candidate.handle,
                        reason=f"promoted after the active page {token or 'primary'} closed")
                    if adopted is None:
                        continue
                    self._active_page = candidate.handle
                    self._attach_page_observers(candidate.handle)
                    promoted_token = adopted.token
                    promoted_url = await self._settle_page(candidate.handle)
                    self._registry.observe(candidate.handle, url=promoted_url)
                    break
            self._record_event(pl.page_closed_record(
                page_url=self._scrub(url), page_token=token,
                was_active=was_active, promoted_token=promoted_token,
                promoted_url=self._scrub(promoted_url),
                timestamp_ms=int(time.time() * 1000)))
            logger.info("qec.explorer.page_closed token=%s was_active=%s promoted=%s",
                        token or "primary", was_active, promoted_token or "(none)")

    async def _adjudicate_pending(self) -> None:
        pending, self._pending_pages = self._pending_pages, []
        adopted_in_batch = self._adopted_this_action
        for page in pending:
            entry = self._registry.get(page) or self._registry.register(page)
            url = await self._settle_new_page(page)
            closed = _page_is_closed(page)
            if not closed:
                self._registry.observe(page, url=url)
            opener_token, opener_url = await self._resolve_opener(page, entry)
            decision = pl.resolve_popup(
                popup_url=url,
                opener_url=opener_url,
                in_scope=self._in_scope(url) if url else False,
                closed=closed,
                already_adopted_this_batch=adopted_in_batch,
            )
            if decision.adopt:
                self._adopt(page, reason=decision.reason)
                adopted_in_batch = True
                self._adopted_this_action = True
            else:
                self._registry.retain(page, reason=decision.reason)
            self._record_event(pl.popup_record(
                opener_url=self._scrub(opener_url),
                opener_token=opener_token,
                popup_url=self._scrub(url), token=entry.token,
                decision=decision, timestamp_ms=int(time.time() * 1000),
                trigger_label=self._action_label))
            logger.info(
                "qec.explorer.popup token=%s disposition=%s url=%s reason=%s",
                entry.token, decision.disposition, (url or "")[:120],
                decision.reason[:160])

    async def _resolve_opener(self, page: Any, entry: pl.PageEntry) -> tuple[str, str]:
        """WHICH page actually opened ``page``, asked of Playwright.

        The obvious implementation — remember whichever page was ACTIVE when the
        ``page`` event fired — is wrong twice over.  It is not the browser's
        answer (a popup opened by a background tab records the foreground one),
        and it is not even STABLE: when one action opens several windows, the
        recorded opener depends on whether an earlier adoption happened to land
        before or after this event was dispatched.  Measured: the same fixture
        recorded the last popup's opener as ``p3``/index.html on one run and
        ``p6``/details.html on the next, from an identical crawl.

        ``page.opener()`` is the browser's own answer and does not move.  The
        registration-time guess is kept only as the fallback for the cases where
        Playwright legitimately has no answer — ``rel="noopener"``, or an opener
        that has already closed.
        """
        opener: Any = None
        try:
            opener = await page.opener()
        except Exception:
            opener = None
        if opener is None:
            return entry.opener_token, entry.opener_url
        known = self._registry.get(opener)
        live_url = _page_url(opener)
        if known is not None:
            return known.token, (live_url or known.url)
        # A page the registry never saw (created before this port existed).
        return "", live_url

    async def _settle_new_page(self, page: Any) -> str:
        """Wait for a brand-new page to become USABLE, then return its URL.

        Two bounded Playwright waits, no sleeps:

          1. ``wait_for_load_state("domcontentloaded")`` — the page has a document.
          2. if it is STILL ``about:blank``, ``wait_for_url`` on a
             not-blank predicate.  ``window.open()`` followed by
             ``location.href = …`` (or by the opener writing into it) creates the
             page at ``about:blank`` and navigates a tick later; judging it at
             creation would classify every such popup as "never navigated".

        Both are best-effort: a popup that never settles is judged on whatever
        URL it has, which is exactly enough to decide not to adopt it.
        """
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=_POPUP_LOAD_MS)
        except Exception:
            pass
        url = _page_url(page)
        if pl.is_blank_url(url) and not _page_is_closed(page):
            try:
                await page.wait_for_url(
                    lambda u: bool(u) and str(u) not in ("about:blank", ""),
                    timeout=_POPUP_NAVIGATE_MS)
            except Exception:
                pass
            try:
                await page.wait_for_load_state("domcontentloaded",
                                               timeout=_POPUP_LOAD_MS)
            except Exception:
                pass
            url = _page_url(page)
        return url

    def _adopt(self, page: Any, *, reason: str) -> None:
        """Make ``page`` the ACTIVE journey page.

        Six things have to happen together, and doing five of them is the bug
        this method exists to prevent: the registry records the transition, the
        adapter re-points, the new page gets EVERY observer the old one had,
        and — because ``self._page`` is a property over the active entry — every
        subsequent action, inventory read, dialog probe, screenshot and
        fingerprint input follows without a further call site changing.
        """
        entry = self._registry.adopt(page, reason=reason)
        if entry is None:
            return
        self._active_page = page
        self._attach_page_observers(page)
        try:  # a foreground tab renders; a background one may not paint at all
            fut = page.bring_to_front()
            if asyncio.iscoroutine(fut):
                asyncio.ensure_future(fut)
        except Exception:
            pass
        logger.warning(
            "qec.explorer.page_adopted token=%s url=%s reason=%s — the journey's "
            "active page has changed; identity and evidence follow it",
            entry.token, _page_url(page)[:160], reason[:200])

    async def _prune_retained_pages(self) -> None:
        """Close RETAINED pages beyond the cap — never the active one, never the
        primary.  An application that opens a tab per click would otherwise
        accumulate Chromium targets for the whole crawl."""
        for entry in reversed(self._registry.entries()):
            if self._registry.open_count() <= _MAX_OPEN_PAGES:
                return
            if entry.is_primary or entry.lifecycle != pl.LIFECYCLE_RETAINED:
                continue
            if entry.handle is self._active_page:
                continue
            try:
                await entry.handle.close()
            except Exception:
                pass
            self._registry.close(entry.handle)
            self._observed_pages.discard(id(entry.handle))

    async def _settle_page(self, page: Any) -> str:
        """Bounded load-state wait for an arbitrary page; returns its URL."""
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=_POPUP_LOAD_MS)
        except Exception:
            pass
        return _page_url(page)

    # ─── M1.5 · native dialogs (T-ND-02) ──────────────────────────────────────

    async def _on_dialog(self, dialog: Any) -> None:
        """Answer ONE native dialog, deterministically, and record why.

        THE BUG THIS CLOSES.  Playwright auto-dismisses every dialog on a page
        with no ``dialog`` listener.  A confirm-gated "Continue" therefore
        answered CANCEL every single time, the funnel silently did not advance,
        and the crawl recorded an honest-looking "nothing happened" — a
        no-outcome that looked exactly like a dead button.

        THE DECISION IS NOT MADE HERE.  :func:`app.page_lifecycle.resolve_dialog`
        makes it, from the dialog type, the message, the accessible name of the
        control being acted on, the verb, the guard phase, the observe-only
        posture and any operator approval.  This method only executes it and
        writes it down.

        FAIL-SAFE.  If anything at all goes wrong, the dialog is DISMISSED.  A
        dialog left unanswered blocks the page forever and every subsequent
        action times out, so "do nothing" is the one response that is never
        available.
        """
        decision: pl.DialogDecision = pl.DialogDecision(
            pl.ACTION_DISMISS, pl.INTENT_FUNNEL_CONFIRMATION, "not yet resolved")
        dtype, message, error = "", "", ""
        try:
            dtype = str(getattr(dialog, "type", "") or "")
            message = str(getattr(dialog, "message", "") or "")
            journey = self._journey()
            decision = pl.resolve_dialog(
                dialog_type=dtype, message=message,
                control_label=self._action_label, action_verb=self._action_verb,
                journey_phase=journey["phase"],
                observe_only=journey["observe_only"],
                approved_labels=journey["approved_labels"])
            if decision.accepted:
                await dialog.accept()
            else:
                await dialog.dismiss()
        except Exception as exc:
            error = str(exc)[:300]
            try:    # the page is BLOCKED until this is answered — answer it.
                await dialog.dismiss()
            except Exception:
                pass
        # Recorded whether or not the handling itself succeeded: a dialog that
        # appeared and could not be answered is exactly the kind of event whose
        # absence from the evidence would make a stalled crawl inexplicable.
        self._record_event(pl.dialog_record(
            dialog_type=dtype, message=self._scrub(message), decision=decision,
            timestamp_ms=int(time.time() * 1000),
            page_url=self._safe_url(), page_token=self._registry.active_token(),
            control_label=self._action_label, action_verb=self._action_verb,
            journey_phase=self._journey()["phase"],
            handled=not error, error=error))
        logger.warning(
            "qec.explorer.dialog type=%s action=%s intent=%s control=%r reason=%s",
            dtype or "?", decision.action, decision.intent,
            self._action_label[:60], decision.reason[:200])

    # ─── M1.5 · downloads (T-ND-03) ───────────────────────────────────────────

    def _on_download(self, download: Any) -> None:
        """SYNCHRONOUS: start the capture and REMEMBER the task.

        WHY IT IS NOT AN ``async def``, which is what it was first written as and
        which is racy.  Playwright dispatches the ``download`` event by calling
        the listener; an ``async`` listener is merely SCHEDULED, and the click
        that produced the download returns immediately afterwards.  A crawler
        that drained its evidence at that moment got an empty list and the
        artifact appeared — silently, later — with nothing referencing it.  Not
        theoretical: this exact race dropped the PDF capture on the first, slow,
        Chromium boot and captured it on every subsequent run.

        pyee calls listeners synchronously during ``emit``, so scheduling the
        task HERE makes the task's existence synchronous with the event.
        :meth:`_await_downloads` then joins it at the next synchronisation point,
        and a drain can no longer outrun a capture.
        """
        try:
            self._download_tasks.append(
                asyncio.ensure_future(self._capture_download(download)))
        except Exception as exc:  # no running loop — cannot capture, so say so
            self._record_event(pl.download_record(
                suggested_filename=str(getattr(download, "suggested_filename", "") or ""),
                source_url=self._scrub(
                    _stable_source_url(str(getattr(download, "url", "") or ""))),
                page_url=self._safe_url(), artifact_path="", bytes_written=0,
                timestamp_ms=int(time.time() * 1000),
                page_token=self._registry.active_token(),
                trigger_label=self._action_label, action_verb=self._action_verb,
                error=f"could not schedule capture: {str(exc)[:200]}"))

    async def _await_downloads(self) -> None:
        """Join every in-flight download capture (bounded).

        Called from :meth:`_settle` and from :meth:`drain_browser_events`, so by
        the time an action is observed or evidence is drained, a file that was
        started is a file that exists.  A capture that exceeds the budget is
        KEPT in the list rather than abandoned, so it is joined at the next
        synchronisation point instead of being lost.
        """
        if not self._download_tasks:
            return
        pending = [t for t in self._download_tasks if not t.done()]
        if not pending:
            self._download_tasks = []
            return
        _done, still_pending = await asyncio.wait(
            pending, timeout=_DOWNLOAD_MS / 1000.0)
        self._download_tasks = list(still_pending)
        if still_pending:
            logger.warning(
                "qec.explorer.download_capture_slow pending=%d — %d ms was not "
                "enough; the artifact will be joined at the next settle",
                len(still_pending), _DOWNLOAD_MS)

    async def _capture_download(self, download: Any) -> None:
        """Capture ONE download as a real artifact on disk.

        NOT a log line.  ``save_as`` streams the file to the crawl's artifact
        directory and waits for it to finish, and the recorded evidence carries
        the byte count — so "a download started" and "a file exists and is not
        empty" stay distinguishable, which is the whole difference between a
        claim and an artifact.

        The suggested filename is APPLICATION-CONTROLLED text and is never used
        as a path component before ``safe_artifact_name`` reduces it.
        """
        suggested, source_url, error = "", "", ""
        rel_path, written = "", 0
        try:
            suggested = str(getattr(download, "suggested_filename", "") or "")
            source_url = str(getattr(download, "url", "") or "")
            if not self._artifact_dir:
                error = "no artifact directory configured for this port"
            elif self._artifacts_written >= _MAX_ARTIFACTS:
                error = f"artifact cap of {_MAX_ARTIFACTS} reached for this crawl"
            else:
                self._artifacts_written += 1
                name = pl.safe_artifact_name(suggested, index=self._artifacts_written)
                directory = Path(self._artifact_dir)
                directory.mkdir(parents=True, exist_ok=True)
                target = directory / name
                await asyncio.wait_for(download.save_as(str(target)),
                                       timeout=_DOWNLOAD_MS / 1000.0)
                if target.exists():
                    written = target.stat().st_size
                    rel_path = f"{emit.ARTIFACT_SUBDIR}/{name}"
                else:
                    error = "save_as reported success but no file was written"
        except Exception as exc:
            error = str(exc)[:300]
            # A download that failed mid-stream may still have left a partial
            # file; report what is actually there rather than assuming.
            try:
                failure = getattr(download, "failure", None)
                if callable(failure):
                    detail = await failure()
                    if detail:
                        error = f"{error} | {str(detail)[:120]}"
            except Exception:
                pass
        self._record_event(pl.download_record(
            suggested_filename=suggested,
            source_url=self._scrub(_stable_source_url(source_url)),
            page_url=self._safe_url(), artifact_path=rel_path,
            bytes_written=written, timestamp_ms=int(time.time() * 1000),
            page_token=self._registry.active_token(),
            content_type=pl.content_type_for(suggested),
            trigger_label=self._action_label, action_verb=self._action_verb,
            error=error))
        logger.warning(
            "qec.explorer.download filename=%r bytes=%d artifact=%s error=%s",
            suggested[:120], written, rel_path or "(none)", error or "(none)")

    def _scrub(self, text: str) -> str:
        """PII-scrub a value bound for the evidence stream (never raises)."""
        try:
            return emit.scrub_value(str(text or "")).value
        except Exception:
            return ""

    async def goto(self, url: str) -> NavResult:
        # A navigation is its own trigger context: a dialog or a download that
        # fires here was raised by the load, not by a control, and recording a
        # stale control label against it would be a fabricated attribution.
        self._set_action_context("", "navigate")
        # BEFORE the request, not after: a response listener attached later
        # cannot observe the navigation it missed.
        await self._ensure_observers()
        # And BEFORE the navigation, adjudicate any page event still queued —
        # because the queued event may be the ACTIVE PAGE CLOSING. Navigating
        # first would drive a dead target and return
        # "Target page, context or browser has been closed" for the rest of the
        # crawl; reconciling first promotes an open page and the walk survives
        # an application that closes the tab it moved the journey into.
        await self._reconcile_quietly()
        try:
            resp = await self._page.goto(url, wait_until="domcontentloaded")
            # Adaptive backoff on an explicit server rate-limit (429), then ONE retry
            # — a throttled response would otherwise record a wrong-outcome state.
            if resp is not None and getattr(resp, "status", 0) == 429:
                await asyncio.sleep(min(_retry_after_ms(resp), _MAX_BACKOFF_MS) / 1000.0)
                try:
                    resp = await self._page.goto(url, wait_until="domcontentloaded")
                except Exception:
                    pass
            # SPA hash routers commonly render the current route only on the
            # 'hashchange' event; a FRESH document load with the hash already set
            # can render the default/landing route instead of the requested one
            # (so a direct #/route load — and every hash-route frontier goto —
            # would silently observe the wrong state). Nudge the router to
            # evaluate location.hash so the requested route actually mounts.
            if "#/" in (url or "") or "#!" in (url or ""):
                try:
                    await self._page.evaluate(
                        "window.dispatchEvent(new HashChangeEvent('hashchange', "
                        "{newURL: location.href, oldURL: location.href}))"
                    )
                except Exception:
                    pass
            await self._settle()
            return NavResult(url=self._page.url, ok=True,
                             status=resp.status if resp else 0)
        except Exception as exc:
            return NavResult(url=self._safe_url(), ok=False, error=str(exc)[:300])

    async def current_url(self) -> str:
        return self._safe_url()

    async def title(self) -> str:
        try:
            return await self._page.title()
        except Exception:
            return ""

    async def collect_controls_result(self) -> observation_health.InventoryResult:
        """The inventory read, WITH the health of the read (M1.7 / T-GW-01).

        This is now the real implementation and ``collect_controls`` is the lossy
        projection of it, rather than the other way round.  The exception is
        classified HERE because this is the last place that can still see it —
        one frame further out there is only a list, and a list cannot say whether
        it is a page with no controls or a page we failed to read.
        """
        try:
            payload = await self._page.evaluate(INVENTORY_JS)
        except Exception as exc:
            result = observation_health.InventoryResult.from_exception(exc)
            logger.warning(
                "qec.explorer.inventory_failed status=%s error=%s url=%s — this "
                "page was NOT observed; it must not be recorded as empty",
                result.status, result.error, self._safe_url()[:120])
            return result
        result = observation_health.InventoryResult.from_payload(payload)
        if result.failed:
            logger.warning(
                "qec.explorer.inventory_corrupt status=%s error=%s url=%s",
                result.status, result.error, self._safe_url()[:120])
            return result
        # M3.2 / T-FR-01 — COMPLETE THE WALK ACROSS THE ORIGIN BOUNDARY.
        #
        # INVENTORY_JS descends same-origin frames itself and stops, correctly,
        # at a foreign origin: `contentDocument` throws, and injecting anything
        # past that boundary would be defeating browser origin isolation rather
        # than working within it. Playwright's frame APIs cross it legitimately —
        # the browser hands us the frame's own execution context — so the part of
        # the walk that JavaScript structurally cannot do is done here, where the
        # driver can, and joined onto the same list.
        #
        # This is deliberately inside the read that produces the observation, not
        # bolted on beside it: every consumer of an observation (the fingerprint,
        # the form filler, the catalogue, the walker) then sees frame controls
        # without a single one of them being taught about frames, and a control
        # inside a payment iframe is evidence on exactly the same terms as one
        # beside it.
        framed = await self._observe_entered_frames()
        if framed:
            result = _dc_replace(result, controls=tuple(result.controls) + tuple(framed))
        return result

    async def collect_controls(self) -> list[dict[str, Any]]:
        """The BackCompat projection: controls only, health discarded.

        Every pre-M1.7 call site keeps working through this — the auth flow, the
        filler's re-reads, the walker's post-action refreshes.  Those paths act on
        the CONTENT of a page they have already decided to be on; the paths that
        decide whether a page EXISTS AS EVIDENCE go through
        ``collect_controls_result``.  Splitting the two is what let this land
        without re-auditing every read in the engine.
        """
        return (await self.collect_controls_result()).as_list()

    async def collect_opaque(self) -> list[dict[str, Any]]:
        """OPAQUE surfaces the DOM walker cannot read ``[{kind, label, reason}]`` — a
        cross-origin embed, a canvas app, a closed shadow host. Best-effort: ``[]`` on any
        failure so a detection hiccup never breaks the crawl."""
        try:
            result = await self._page.evaluate(OPAQUE_JS)
            return list(result or [])
        except Exception as exc:
            logger.warning("qec.explorer.opaque_failed error=%s", str(exc)[:200])
            return []

    async def drain_frame_evidence(self) -> list[dict[str, Any]]:
        """Take and clear the frame-entry evidence gathered since the last drain.

        One row per frame this port MET, entered or not, with the reason — so a
        frame that could not be addressed is as visible as one that was read.
        Drained (not accumulated) for the same reason the network buffer is:
        the crawler attributes evidence to the state it was observed on.
        """
        rows, self._frames_seen = list(self._frames_seen), []
        return rows

    async def _observe_entered_frames(self) -> list[dict[str, Any]]:
        """Controls read from INSIDE the cross-origin frames on this page.

        The routing decision is ``perception.route_opaque_surfaces`` — the same
        pure function the vision escalation consults — so "which surfaces are
        enterable" is answered in one place, by code that unit-tests without a
        browser, rather than by a second rule living in the driver.
        """
        try:
            surfaces = await self._page.evaluate(OPAQUE_JS)
        except Exception as exc:
            logger.warning("qec.explorer.frame_scan_failed error=%s", str(exc)[:200])
            return []
        routed = perception.route_opaque_surfaces(surfaces or [])
        for unaddressable in routed["blind"]:
            self._frames_seen.append({
                "status": "not_entered",
                "label": str(unaddressable.get("label") or "embedded frame"),
                "selector": "",
                "reason": "no deterministic selector could be built for this surface",
            })
        out: list[dict[str, Any]] = []
        for surface in routed["enter_frames"][:_MAX_ENTERED_FRAMES]:
            out.extend(await self._enter_frame(
                self._page, str(surface.get("frame_selector") or ""),
                prefix="", depth=1,
                label=str(surface.get("frame_host") or surface.get("label") or "")))
        return out

    async def _enter_frame(self, root: Any, selector: str, *, prefix: str,
                           depth: int, label: str) -> list[dict[str, Any]]:
        """Enter ONE frame through supported Playwright APIs and observe inside it.

        ``root`` is the page or the parent ``FrameLocator``, so the same code
        handles an embed and an embed-inside-an-embed. The chain of selectors is
        joined with the walker's own ``" >>> "`` separator, which is exactly what
        :meth:`_locator` splits on — so a control captured here is ACTIONABLE by
        the existing ladder, not merely recorded.

        NOTHING IS INJECTED ACROSS THE ORIGIN BOUNDARY. ``content_frame()`` asks
        the browser for the frame's own execution context; ``frame.evaluate``
        then runs the walker inside it under that frame's origin, exactly as the
        frame's own scripts run. Origin isolation is used, not circumvented.
        """
        full = (prefix + " >>> " + selector) if prefix else selector
        note = {"status": "not_entered", "label": label or selector,
                "selector": full, "reason": ""}
        if not selector:
            note["reason"] = "the surface carried no frame selector"
            self._frames_seen.append(note)
            return []
        # DETERMINISM IS CHECKED, NOT ASSUMED (T-FR-03). A selector that resolves
        # to two frames binds silently to the first, and every control read
        # through it would be attributed to a frame it is not in. Refusing is the
        # only honest answer, and the count is recorded so the refusal is legible.
        try:
            count = await root.locator(selector).count()
        except Exception as exc:
            note["reason"] = "selector did not resolve: %s" % str(exc)[:120]
            self._frames_seen.append(note)
            return []
        if count != 1:
            note["reason"] = (
                "selector resolved to %d frames, not 1 — refusing to attribute "
                "controls to a frame chosen by accident" % count)
            note["resolved"] = count
            self._frames_seen.append(note)
            logger.warning("qec.explorer.frame_ambiguous selector=%r resolved=%d",
                           full[:160], count)
            return []
        try:
            handle = await root.locator(selector).element_handle()
            frame = await handle.content_frame() if handle is not None else None
        except Exception as exc:
            note["reason"] = "could not reach the frame: %s" % str(exc)[:120]
            self._frames_seen.append(note)
            return []
        if frame is None:
            note["reason"] = "the element resolved but exposes no frame"
            self._frames_seen.append(note)
            return []
        try:
            payload = await frame.evaluate(INVENTORY_JS)
        except Exception as exc:
            note["reason"] = "the frame refused observation: %s" % str(exc)[:120]
            self._frames_seen.append(note)
            logger.warning("qec.explorer.frame_inventory_failed selector=%r error=%s",
                           full[:160], str(exc)[:200])
            return []
        raw = [c for c in list(payload or []) if isinstance(c, dict)]
        clipped = len(raw) > _MAX_FRAME_CONTROLS
        controls: list[dict[str, Any]] = []
        origin = _frame_origin(frame)
        for control in raw[:_MAX_FRAME_CONTROLS]:
            inner = str(control.get("frame_selector") or "").strip()
            control["frame_selector"] = (full + " >>> " + inner) if inner else full
            # WHERE THIS CAME FROM travels with it. A control read inside a
            # third-party embed is bindable evidence, but a reader deciding
            # whether to ACT on it (a payment field is not a form field) must be
            # able to tell without re-deriving it from the selector string.
            # Orthogonal to `shadow_scope`, which the walker set inside the
            # frame: a closed shadow root inside a foreign embed is both, and
            # overwriting one with the other would lose a fact a reader needs.
            control["capture_scope"] = "cross_origin_frame"
            control["frame_origin"] = origin
            controls.append(control)
        note.update({
            "status": "entered",
            "label": label or origin or selector,
            "origin": origin,
            "controls": len(controls),
            "clipped": clipped,
            "depth": depth,
            "reason": "entered through page.frame_locator and observed from inside"
                      " — %d control(s) catalogued%s" % (
                          len(controls),
                          " (clipped at %d)" % _MAX_FRAME_CONTROLS if clipped else ""),
        })
        self._frames_seen.append(note)
        logger.info("qec.explorer.frame_entered selector=%r origin=%s controls=%d depth=%d",
                    full[:160], origin, len(controls), depth)
        if depth >= _MAX_FRAME_DEPTH:
            return controls
        # A vendor embed that itself embeds a foreign frame (3-D Secure inside a
        # payment frame). Bounded by _MAX_FRAME_DEPTH; a scan failure here costs
        # the nested frame only, never the controls already read.
        try:
            nested = await frame.evaluate(OPAQUE_JS)
        except Exception:
            return controls
        child_root = root.frame_locator(selector)
        for surface in perception.route_opaque_surfaces(
                nested or [])["enter_frames"][:_MAX_ENTERED_FRAMES]:
            controls.extend(await self._enter_frame(
                child_root, str(surface.get("frame_selector") or ""),
                prefix=full, depth=depth + 1,
                label=str(surface.get("frame_host") or surface.get("label") or "")))
        return controls

    async def collect_pii_regions(self) -> dict[str, Any]:
        """M3.1 / T-VIS-05 — where a screenshot of this page would render
        something sensitive, in full-page CSS pixels.

        Returns ``{ok, regions, page_w, page_h, dpr}``.  NOT best-effort: a
        snippet that throws returns ``ok=False``, and
        :func:`app.pixel_redaction.redact_screenshot` refuses to produce an image
        from a failed read.  Degrading to ``[]`` here — the pattern every other
        capture verb correctly uses — would mean an evaluation error silently
        published an unmasked screenshot of a real application.
        """
        try:
            result = await self._page.evaluate(PII_REGIONS_JS)
        except Exception as exc:
            logger.warning("qec.explorer.pii_regions_failed error=%s", str(exc)[:200])
            return {"ok": False, "regions": [], "page_w": 0, "page_h": 0, "dpr": 1}
        if not isinstance(result, dict):
            return {"ok": False, "regions": [], "page_w": 0, "page_h": 0, "dpr": 1}
        return {
            "ok": bool(result.get("ok")),
            "regions": list(result.get("regions") or []),
            "page_w": result.get("page_w") or 0,
            "page_h": result.get("page_h") or 0,
            "dpr": result.get("dpr") or 1,
        }

    async def collect_displayed_values(self) -> list[dict[str, Any]]:
        """ANSWERS P1.B — rendered value nodes ``[{label, selector, text}]`` (a
        premium/total/decline shown as text). Best-effort: ``[]`` on any failure so
        a capture hiccup never breaks the crawl (the value oracle then just needs a
        client source_hint)."""
        try:
            result = await self._page.evaluate(DISPLAYED_VALUES_JS)
            return list(result or [])
        except Exception as exc:
            logger.warning("qec.explorer.displayed_values_failed error=%s", str(exc)[:200])
            return []

    async def dialog_flags(self) -> list[str]:
        flags: list[str] = []
        try:
            count = await self._page.get_by_role("dialog").count()
            for i in range(count):
                node = self._page.get_by_role("dialog").nth(i)
                if await node.is_visible():
                    label = (await node.get_attribute("aria-label")) or ""
                    flags.append(f"dialog:{label.strip().lower()[:60]}" if label else "dialog:open")
        except Exception:
            pass
        return flags

    async def _dialog_challenge(self) -> bool:
        """Does the open dialog ASK for something rather than TELL you something?

        Reads two structural facts out of the first visible ``role=dialog`` --
        how many interactive fields it carries, and what its buttons are called
        -- and hands them to the pure :func:`app.browser.is_challenge_dialog`.
        The judgement lives there so it is testable without a browser; this
        method only gathers.

        Never raises (same contract as the readings around it) and is called
        only when a dialog is already known to be open, so a page without a
        modal pays nothing.
        """
        try:
            node = self._page.get_by_role("dialog")
            if not await node.count():
                return False
            first = node.first
            if not await first.is_visible():
                return False
            facts = await first.evaluate(
                """(el) => {
                    const vis = e => { const r = e.getBoundingClientRect();
                        return r.width > 0 && r.height > 0; };
                    const fields = [...el.querySelectorAll(
                        'input:not([type=hidden]),select,textarea')]
                        .filter(e => vis(e) && !e.disabled && !e.readOnly).length;
                    const labels = [...el.querySelectorAll('button,[role=button]')]
                        .filter(vis)
                        .map(e => (e.innerText || e.getAttribute('aria-label') || '').trim());
                    return {fields, labels};
                }"""
            )
            return is_challenge_dialog(int((facts or {}).get("fields") or 0),
                                       list((facts or {}).get("labels") or []))
        except Exception:
            return False

    async def answer_challenge_dialog(self, secret: str):
        """Answer the re-auth modal an APPROVED commit opened, once.

        Bounded to the dialog that is already open: fills its visible, enabled
        fields with the operator's OWN secret and clicks its affirmative button
        — the one that re-offers the commit, never Cancel/Close/Dismiss. Returns
        the resulting observation, or ``None`` when there is nothing to answer
        (no dialog, no field, or no affirmative button), which leaves the caller
        holding the original observation and the crawl halting at the modal.

        A SECRET IS NEVER INVENTED. The caller passes the credential the
        operator already supplied for this tenant; with none configured this is
        never called.

        Measured: LifeOps' "Confirm PIN" e-signature modal (one required
        password field, buttons ["Sign document", "Cancel"]).
        """
        from .browser import is_commit_button_label
        try:
            dialog = self._page.get_by_role("dialog")
            if not await dialog.count():
                return None
            box = dialog.first
            if not await box.is_visible():
                return None
            url_before = self._safe_url()
            sig_before = await self._interactive_signature()

            fields = box.locator("input:not([type=hidden]), textarea")
            filled = 0
            for i in range(min(await fields.count(), 5)):
                node = fields.nth(i)
                try:
                    if await node.is_visible() and await node.is_enabled():
                        await node.fill(secret)
                        filled += 1
                except Exception:
                    continue

            buttons = box.locator("button, [role=button]")
            target = None
            for i in range(min(await buttons.count(), 12)):
                node = buttons.nth(i)
                try:
                    if not (await node.is_visible() and await node.is_enabled()):
                        continue
                    label = ((await node.inner_text())
                             or (await node.get_attribute("aria-label")) or "").strip()
                except Exception:
                    continue
                if is_commit_button_label(label):
                    target = node
                    break
            if target is None:
                logger.info("qec.explorer.challenge_no_affirmative filled=%d", filled)
                return None
            await target.click()
            await self._settle()

            errors = await self.error_texts()
            statuses = await self.status_texts()
            dialogs = await self.dialog_flags()
            logger.info("qec.explorer.challenge_answered fields=%d dialog_still_open=%s",
                        filled, bool(dialogs))
            return RawObservation(
                url_before=url_before, url_after=self._safe_url(),
                error_detail=(errors[0] if errors else ""),
                confirmation_detail=(statuses[0] if statuses else ""),
                dialog_opened=bool(dialogs),
                dialog_detail=(dialogs[0] if dialogs else ""),
                dialog_is_challenge=(await self._dialog_challenge() if dialogs else False),
                dom_changed=(sig_before != await self._interactive_signature()),
            )
        except Exception:
            logger.warning("qec.explorer.challenge_answer_failed", exc_info=True)
            return None

    async def error_texts(self) -> list[str]:
        """Visible REJECTION texts, wherever the application marked them.

        ``[role=alert]`` / ``[aria-live=assertive]`` are read unconditionally --
        those regions exist to interrupt, so their content is a rejection by
        construction.  ``[role=status]`` / ``[aria-live=polite]`` are read too,
        but ONLY where the text is rejection-shaped: a polite region is the
        correct markup for a non-interrupting validation message and
        applications legitimately refuse in it, while the same region also
        carries genuine success banners that must not be reported as errors.
        Polarity is decided by :func:`app.browser.is_rejection_text`.

        Measured on a third-party carrier platform: a failed sign-in rendered
        ``<div role="status" class="message error">Invalid member ID or PIN.</div>``
        and this method returned ``[]`` -- so ``validation_rejections`` was
        empty on every application crawled that day, and every refusal was
        recorded with ``missing_fields: []``.
        """
        texts: list[str] = []
        for selector in ('[role="alert"]', '[aria-live="assertive"]'):
            try:
                loc = self._page.locator(selector)
                count = await loc.count()
                for i in range(min(count, 5)):
                    node = loc.nth(i)
                    if await node.is_visible():
                        txt = (await node.inner_text()).strip()
                        if txt:
                            texts.append(txt[:300])
            except Exception:
                continue
        for selector in ('[role="status"]', '[aria-live="polite"]'):
            try:
                loc = self._page.locator(selector)
                count = await loc.count()
                for i in range(min(count, 5)):
                    node = loc.nth(i)
                    if await node.is_visible():
                        txt = (await node.inner_text()).strip()
                        if txt and is_rejection_text(txt) and txt[:300] not in texts:
                            texts.append(txt[:300])
            except Exception:
                continue
        return texts

    async def status_texts(self) -> list[str]:
        """Visible STATUS live-region texts (role=status / aria-live=polite).

        THE PRODUCER THAT WAS NEVER WRITTEN.  ``RawObservation.confirmation_detail``
        is declared in :mod:`app.browser` and read by ``classify_submit_after``;
        grepping ``app/`` for a WRITE of it returns nothing.  So the same-page
        ``confirmation`` branch of that classifier has been dead code since it
        was written, and ``confirmed`` could only ever be reached through a URL
        change or a dialog.  An application that answers a submit with a banner
        in place — which is the majority of single-page applications, including
        the one this milestone is proven on — could not complete a journey at
        any privilege level.

        Mirrors :meth:`error_texts` exactly (same bounds, same visibility check,
        same never-raise contract); only the selectors differ.  ``role=alert``
        and ``aria-live=assertive`` are deliberately NOT read here: those are
        errors, and an error read as a confirmation is the one misclassification
        that turns a failed submit into a green journey.
        """
        texts: list[str] = []
        for selector in ('[role="status"]', '[aria-live="polite"]'):
            try:
                loc = self._page.locator(selector)
                count = await loc.count()
                for i in range(min(count, 5)):
                    node = loc.nth(i)
                    if await node.is_visible():
                        txt = (await node.inner_text()).strip()
                        if txt:
                            texts.append(txt[:300])
            except Exception:
                continue
        return texts

    async def form_texts(self) -> list[str]:
        """Short visible text blocks INSIDE the page's forms — B1's read scope.

        The Phase-5 backlog's binding acceptance for the rejection reader is
        "form-scoped new-text-after-declined-submit": the text that matters is
        the text the FORM renders when its submit handler refuses, and scoping
        to the form is what keeps a cookie banner or a toast elsewhere on the
        page from being read as the form's verdict.

        STRUCTURAL, NEVER A CLASS NAME — the acceptance's first clause, and the
        rule seven R7 red-team rounds enforced: fitting a reader to one app's
        Tailwind palette makes the next application's silence invisible again.
        The scope is the ``form`` element and ``[role=form]``, which are the
        only form-ness the platform itself declares. A page with NO form falls
        back to the page-wide read: rejection polarity plus the transition diff
        still bound what can be claimed from it.

        Same hard bounds as :meth:`visible_texts`, same set-difference use: the
        text already on the page before the action is discarded, never stored.
        """
        texts: list[str] = []
        try:
            raw = await self._page.evaluate(
                """() => {
                    const roots = [...document.querySelectorAll('form,[role=form]')];
                    const scope = roots.length ? roots : [document];
                    const out = [];
                    const sel = 'p,span,div,h1,h2,h3,h4,li,td,strong,em,label';
                    for (const root of scope) {
                        for (const el of root.querySelectorAll(sel)) {
                            if (out.length >= 40) return out;
                            if (el.querySelector(sel)) continue;
                            const r = el.getBoundingClientRect();
                            if (!r.width || !r.height) continue;
                            const t = (el.innerText || '').trim();
                            if (t && t.length <= 300) out.push(t);
                        }
                    }
                    return out;
                }"""
            )
            for item in list(raw or [])[:40]:
                text = str(item or "").strip()
                if text:
                    texts.append(text[:300])
        except Exception:                                        # noqa: BLE001
            return texts
        return texts

    async def visible_texts(self) -> list[str]:
        """Short visible text blocks, for the before/after crossing diff.

        BOUNDED HARD, because this is the only place the crawl reads free page
        text and an unbounded read of a data-heavy page is both a latency cost
        and a PII surface.  Leaf-ish blocks only, 300 chars each, 40 blocks
        total, and the result is used ONLY as a set difference — the text that
        was already on the page before the click is discarded and never stored.

        Called twice per approved crossing and (since M1.4) twice per wizard
        advance, which is what lets a walk recognise the confirmation page it
        landed on.  The walk pays for these two reads and NOT for a third: it
        calls :func:`app.forms.capture_page_declarations`, which omits the
        expensive ``collect_controls`` the crossing helper also needs.
        """
        texts: list[str] = []
        try:
            raw = await self._page.evaluate(
                """() => {
                    const out = [];
                    const sel = 'p,span,div,h1,h2,h3,h4,li,td,strong,em,label';
                    for (const el of document.querySelectorAll(sel)) {
                        if (out.length >= 40) break;
                        // Leaf-ish only: a wrapper repeats its children's text and
                        // would make every diff look like the whole page changed.
                        if (el.querySelector(sel)) continue;
                        const r = el.getBoundingClientRect();
                        if (!r.width || !r.height) continue;
                        const t = (el.innerText || '').trim();
                        if (t && t.length <= 300) out.push(t);
                    }
                    return out;
                }"""
            )
            for item in list(raw or [])[:40]:
                text = str(item or "").strip()
                if text:
                    texts.append(text[:300])
        except Exception:
            return texts
        return texts

    async def screenshot_png(self) -> bytes:
        try:
            return await self._page.screenshot(full_page=True, type="png")
        except Exception as exc:
            logger.warning("qec.explorer.screenshot_failed error=%s", str(exc)[:200])
            return b""

    async def click(self, control: dict[str, Any]) -> RawObservation:
        return await self._act(control, "click")

    async def hover(self, control: dict[str, Any]) -> RawObservation:
        """Hover ``control`` (reveals menus/fly-outs/tooltips) and observe."""
        return await self._act(control, "hover")

    async def press_key(self, key: str) -> None:
        """Press a global key (e.g. Escape to dismiss an opened dropdown/overlay so the
        page is restored before the next read). Best-effort — never raises into the
        state machine."""
        try:
            await self._page.keyboard.press(key)
            await self._settle()
        except Exception:
            pass

    async def set_input_files(self, control: dict[str, Any],
                              paths: Sequence[str]) -> RawObservation:
        """Attach ``paths`` to a file-input ``control`` (Phase-A: choose the file,
        never submit) and read back the chosen filename."""
        self._set_action_context(control, "upload")
        url_before = self._safe_url()
        sig_before = await self._interactive_signature()
        locator = self._locator(control)
        if locator is None:
            return RawObservation(url_before=url_before, url_after=url_before,
                                  error_detail="locator_unresolved")
        try:
            await locator.set_input_files(list(paths))
        except Exception as exc:
            return RawObservation(url_before=url_before, url_after=self._safe_url(),
                                  error_detail=f"upload_error: {str(exc)[:200]}")
        await self._settle()
        committed = None
        try:
            committed = await locator.input_value()  # the chosen file name
        except Exception:
            pass
        sig_after = await self._interactive_signature()
        return RawObservation(
            url_before=url_before, url_after=self._safe_url(),
            committed_value=committed, dom_changed=(sig_before != sig_after),
        )

    async def fill(self, control: dict[str, Any], value: str) -> RawObservation:
        return await self._act_with_ladder(control, "fill", value=value, read_back=True)

    async def select_option(self, control: dict[str, Any], value: str) -> RawObservation:
        return await self._act_with_ladder(control, "select", value=value, read_back=True)

    async def set_checked(self, control: dict[str, Any], checked: bool) -> RawObservation:
        return await self._act_with_ladder(control, "checked", checked=checked, read_back=True)

    async def collect_grids(self) -> list[dict[str, Any]]:
        """Read this page's data grids as entities (rung 3 — see app.harvest).

        Best-effort by construction: a page with no grid returns [], and any
        evaluation failure returns [] as well, because a harvest that cannot run
        must cost the crawl nothing.
        """
        from .harvest import GRID_JS
        try:
            return list(await self._page.evaluate(GRID_JS) or [])
        except Exception:                                        # noqa: BLE001
            return []

    async def sleep_ms(self, ms: int) -> None:
        """Wait, for a page still deciding what it is (see
        ``_settle_undecided_page``). Bounded by its one caller; the port merely
        provides the primitive so a test double can supply its own."""
        await asyncio.sleep(max(0, int(ms)) / 1000.0)

    async def collect_labelled_values(self) -> list[dict[str, Any]]:
        """Read this page's "label: value" pairs (rung 4 — see app.minted).

        Best-effort by construction, for collect_grids' reason: a page with no
        such pairs returns [], and any evaluation failure returns [] too,
        because a rung that cannot run must cost the crawl nothing.
        """
        from .minted import MINTED_JS
        try:
            return list(await self._page.evaluate(MINTED_JS) or [])
        except Exception:                                        # noqa: BLE001
            return []

    async def storage_state(self) -> dict[str, Any]:
        return await self._context.storage_state()

    # -- internals -------------------------------------------------------------

    def _set_action_context(self, control: Any, verb: str) -> None:
        """Remember WHAT the crawl is doing, for the dialog/popup/download
        evidence and for dialog INTENT resolution (T-ND-02).

        This is the whole reason dialog handling is not string-matching: a
        confirm raised behind "Continue" and the same confirm raised behind
        "Delete Policy" are different questions, and only the adapter — which
        performed the click — knows which control asked it.  Set before the
        action, because a dialog fires DURING it.
        """
        try:
            label = (str(control.get("name") or "").strip()
                     if isinstance(control, Mapping) else str(control or "").strip())
        except Exception:
            label = ""
        label, verb = label[:200], str(verb or "")[:40]
        if (label, verb) != (self._action_label, self._action_verb):
            # A genuinely NEW action. Re-stating the same context (the ladder
            # re-enters _act for the same control) is not a new action and must
            # not re-arm the adoption budget.
            self._adopted_this_action = False
            # M2.5 / T-NET-03 — and it is a new causal window for network
            # evidence. Bumped on the SAME condition as the adoption budget, so
            # the ladder retrying one control cannot split that control's
            # requests across two tokens and hide a retry.
            self._action_seq += 1
            self._action_token = f"a{self._action_seq}"
        self._action_label = label
        self._action_verb = verb

    async def _act(self, control: dict[str, Any], kind: str, *, value: str = "",
                   checked: bool = False, read_back: bool = False) -> RawObservation:
        self._set_action_context(control, kind)
        url_before = self._safe_url()
        sig_before = await self._interactive_signature()
        intended = value if kind != "checked" else ("true" if checked else "false")
        locator = await self._bound_locator(control)
        if locator is None:
            return RawObservation(url_before=url_before, url_after=url_before,
                                  error_detail="locator_unresolved",
                                  intended_value=intended, intent_met=False)
        try:
            if kind == "click":
                await locator.click()
            elif kind == "hover":
                await locator.hover()
            elif kind == "fill":
                await locator.fill(value)
            elif kind == "select":
                await locator.select_option(label=value)
            elif kind == "checked":
                try:
                    await locator.set_checked(checked)
                except Exception:
                    if not checked:
                        raise
                    await locator.click()
        except Exception as exc:
            return RawObservation(url_before=url_before, url_after=self._safe_url(),
                                  error_detail=f"action_error: {str(exc)[:200]}",
                                  intended_value=intended, intent_met=False)
        await self._settle()
        url_after = self._safe_url()
        committed = (await self._read_value(locator, kind=kind) if read_back else None)
        errors = await self.error_texts()
        dialogs = await self.dialog_flags()
        sig_after = await self._interactive_signature()
        err = (errors[0] if errors else "")
        met = verify_intent(
            kind,
            intended_value=intended,
            intended_checked=checked,
            committed_value=committed,
            error_detail=err,
            url_before=url_before,
            url_after=url_after,
            dom_changed=(sig_before != sig_after),
            dialog_opened=bool(dialogs),
        )
        return RawObservation(
            url_before=url_before, url_after=url_after, committed_value=committed,
            dialog_opened=bool(dialogs), dialog_detail=(dialogs[0] if dialogs else ""),
            dialog_is_challenge=(await self._dialog_challenge() if dialogs else False),
            error_detail=err,
            dom_changed=(sig_before != sig_after),
            intended_value=intended, intent_met=met,
        )

    async def _page_point_to_viewport(self, x: int, y: int) -> tuple[int, int]:
        """PAGE coordinates -> VIEWPORT coordinates, scrolling if it has to.

        M3.1 — THE DEFECT THIS CLOSES, found by the canvas proving ground.
        ``page.mouse.click`` takes VIEWPORT coordinates, and the only producer of
        coordinates for :meth:`click_at` is a vision perception of a
        ``full_page=True`` screenshot, whose coordinate space is the PAGE.  On a
        page no taller than the viewport the two spaces coincide and the rung
        worked; on any page that scrolls, EVERY vision coordinate was off by the
        scroll offset.  It went unnoticed because the failure is silent and
        plausible: the click lands on nothing, R0 honestly reports unverified,
        and the perception is discarded as a hallucination.  A systematically
        mis-aimed rung would have read as a model that is always wrong.

        The point is scrolled into view when it is outside the viewport, and the
        offset is re-read AFTER the scroll rather than predicted — a scroll can
        be clamped at the document edge, and predicting it would reintroduce the
        same class of error one layer up.  Falls back to treating the input as
        viewport coordinates if the page cannot be measured, which is the
        historical behaviour.

        The gesture verbs (``drag`` / ``draw_stroke`` / ``press_keys``) are
        deliberately NOT changed: their callers derive points from element boxes,
        which are already viewport-relative.
        """
        try:
            m = await self._page.evaluate(
                "() => ({sx: window.scrollX || 0, sy: window.scrollY || 0,"
                " iw: window.innerWidth || 0, ih: window.innerHeight || 0})")
        except Exception:
            return x, y
        if not isinstance(m, dict) or not (m.get("iw") and m.get("ih")):
            return x, y
        vx, vy = x - int(m["sx"] or 0), y - int(m["sy"] or 0)
        if 0 <= vx < int(m["iw"]) and 0 <= vy < int(m["ih"]):
            return vx, vy
        try:
            await self._page.evaluate(
                "([px, py, ih]) => window.scrollTo(0, Math.max(0, py - ih / 2))",
                [x, y, int(m["ih"])])
            after = await self._page.evaluate(
                "() => ({sx: window.scrollX || 0, sy: window.scrollY || 0})")
            return x - int(after.get("sx") or 0), y - int(after.get("sy") or 0)
        except Exception:
            return vx, vy

    async def click_at(self, x: int, y: int) -> RawObservation:
        """Coordinate rung (U2/U3) — click absolute PAGE ``(x, y)`` via
        ``page.mouse``, for a vision-proposed point on a DOM-opaque surface. Mirrors
        the ``_act`` observe pattern (url + interactive-signature before/after) so a
        coordinate action produces the SAME grounded ``RawObservation`` and R0
        verdict: a click that changes nothing is honestly ``intent_met=False``.

        ``(x, y)`` really are PAGE coordinates — see
        :meth:`_page_point_to_viewport`, which is what makes that true rather
        than merely documented."""
        self._set_action_context("", "click_at")
        url_before = self._safe_url()
        sig_before = await self._interactive_signature()
        try:
            xi, yi = int(x), int(y)
        except (TypeError, ValueError):
            return RawObservation(url_before=url_before, url_after=url_before,
                                  error_detail="bad_coordinates", intent_met=False)
        try:
            xi, yi = await self._page_point_to_viewport(xi, yi)
            await self._page.mouse.click(xi, yi)
        except Exception as exc:
            return RawObservation(url_before=url_before, url_after=self._safe_url(),
                                  error_detail=f"action_error: {str(exc)[:200]}",
                                  intent_met=False)
        await self._settle()
        url_after = self._safe_url()
        errors = await self.error_texts()
        dialogs = await self.dialog_flags()
        sig_after = await self._interactive_signature()
        err = (errors[0] if errors else "")
        met = verify_intent(
            "click",
            intended_value="", intended_checked=False, committed_value=None,
            error_detail=err, url_before=url_before, url_after=url_after,
            dom_changed=(sig_before != sig_after), dialog_opened=bool(dialogs),
        )
        return RawObservation(
            url_before=url_before, url_after=url_after,
            dialog_opened=bool(dialogs), dialog_detail=(dialogs[0] if dialogs else ""),
            dialog_is_challenge=(await self._dialog_challenge() if dialogs else False),
            error_detail=err, dom_changed=(sig_before != sig_after),
            intent_met=met,
        )

    async def _gesture_observe(self, do) -> RawObservation:
        """Run a gesture coroutine ``do`` inside the same observe wrapper as
        ``click_at`` (url + interactive-signature before/after) so every gesture
        (U3) yields a grounded RawObservation + coarse R0 verdict. The precise
        gesture read-back (drag order / canvas ink / slider value) is applied by
        the caller via ``gesture_verify`` with the extra captured signals."""
        url_before = self._safe_url()
        sig_before = await self._interactive_signature()
        try:
            await do()
        except Exception as exc:
            return RawObservation(url_before=url_before, url_after=self._safe_url(),
                                  error_detail=f"action_error: {str(exc)[:200]}",
                                  intent_met=False)
        await self._settle()
        url_after = self._safe_url()
        errors = await self.error_texts()
        dialogs = await self.dialog_flags()
        sig_after = await self._interactive_signature()
        err = (errors[0] if errors else "")
        met = verify_intent(
            "click", intended_value="", intended_checked=False, committed_value=None,
            error_detail=err, url_before=url_before, url_after=url_after,
            dom_changed=(sig_before != sig_after), dialog_opened=bool(dialogs))
        return RawObservation(
            url_before=url_before, url_after=url_after,
            dialog_opened=bool(dialogs), dialog_detail=(dialogs[0] if dialogs else ""),
            dialog_is_challenge=(await self._dialog_challenge() if dialogs else False),
            error_detail=err, dom_changed=(sig_before != sig_after), intent_met=met)

    async def drag(self, path) -> RawObservation:
        self._set_action_context("", "drag")
        pts = []
        for p in (path or []):
            try:
                pts.append((int(p[0]), int(p[1])))
            except (TypeError, ValueError, IndexError):
                continue

        async def _do():
            if len(pts) < 2:
                raise ValueError("drag needs >= 2 points")
            await self._page.mouse.move(pts[0][0], pts[0][1])
            await self._page.mouse.down()
            for x, y in pts[1:]:
                await self._page.mouse.move(x, y)
            await self._page.mouse.up()

        return await self._gesture_observe(_do)

    async def draw_stroke(self, points) -> RawObservation:
        # A signature stroke is the same mouse down/move/up choreography as a drag.
        return await self.drag(points)

    async def press_keys(self, keys) -> RawObservation:
        self._set_action_context("", "press_keys")
        seq = [str(k) for k in (keys or []) if str(k)]

        async def _do():
            for k in seq:
                await self._page.keyboard.press(k)

        return await self._gesture_observe(_do)

    async def scroll_until(self, control: dict[str, Any],
                           max_steps: int = 10) -> RawObservation:
        self._set_action_context(control, "scroll")
        loc = self._locator(control)

        async def _do():
            for _ in range(max(1, int(max_steps))):
                try:
                    if loc is not None and await loc.is_visible():
                        return
                except Exception:
                    pass
                await self._page.mouse.wheel(0, 600)
                await self._settle()

        return await self._gesture_observe(_do)

    async def _act_with_ladder(
        self, control: dict[str, Any], kind: str, *, value: str = "",
        checked: bool = False, read_back: bool = False,
    ) -> RawObservation:
        """Try the native mechanic first; if R0 says intent_met=False, walk the
        archetype ladder until a rung verifies or the ladder is exhausted.

        R4 MECHANIC MEMORY: if a proven mechanic exists for this control's
        signature, try it FIRST — bypassing the full ladder walk.  Falls
        through to the normal ladder if the proven mechanic fails (the app
        may have changed since the mechanic was proven).

        Returns the observation from the winning rung (with ``mechanic_used``
        set), or the last failed observation if no rung succeeded.
        """
        from dataclasses import replace
        self._set_action_context(control, kind)
        rungs = ladder_for(kind)
        if not rungs:
            return await self._act(control, kind, value=value,
                                   checked=checked, read_back=read_back)
        # R4: try the proven mechanic FIRST (zero ladder walk when it works).
        proven_variant = ""
        if self._proven_mechanics:
            sig = field_signature.compute(control, kind=kind)
            proven_variant = self._proven_mechanics.get(sig.get("signature", ""), "")
        if proven_variant:
            for rung in rungs:
                if rung.variant == proven_variant:
                    obs = await self._run_rung(
                        rung, control, kind, value=value, checked=checked,
                        read_back=read_back)
                    if obs.intent_met is not False:
                        return replace(obs, mechanic_used=rung.variant)
                    break
        last_obs: RawObservation | None = None
        ladder_tried: list[dict[str, str]] = []
        for rung in rungs:
            obs = await self._run_rung(
                rung, control, kind, value=value, checked=checked,
                read_back=read_back)
            if obs.intent_met is not False:
                return replace(obs, mechanic_used=rung.variant)
            ladder_tried.append({
                "rung": rung.variant,
                "observation": (obs.error_detail or "intent_unmet")[:100],
            })
            last_obs = obs
        # R3 MEDIC: after the deterministic ladder exhausts, ask the caged
        # agent for a proposal.  Execute through existing primitives; R0
        # verifies.  An unverified proposal → the control is named residue.
        if self._medic_oracle and last_obs is not None:
            try:
                page_ctx = {
                    "title": (await self._page.title()) if self._page else "",
                    "url": self._safe_url(),
                }
                decision = await self._medic_oracle(
                    control, kind, ladder_tried, page_ctx)
                action = str(decision.get("action") or "")
                status = str(decision.get("status") or "")
                if status == "display_only":
                    return replace(last_obs, mechanic_used="medic:display_only",
                                   intent_met=None)
                if status == "proposed" and action:
                    medic_obs = await self._execute_medic_action(
                        control, kind, action, value=value, checked=checked,
                        read_back=read_back)
                    if medic_obs is not None and medic_obs.intent_met is not False:
                        return replace(medic_obs, mechanic_used=f"medic:{action}")
            except Exception as exc:
                logger.warning("qec.explorer.medic_failed control=%s error=%s",
                               control.get("name", "?"), str(exc)[:200])
        # R5 VISION MEDIC (A28): the deterministic ladder AND the text medic are
        # both exhausted. Everything tried so far has reasoned about the DOM; if
        # the control's behaviour is not IN the DOM, none of it could have
        # worked. So the last rung stops reading the page and looks at it.
        vision_obs = await self._vision_medic_rung(control, kind, ladder_tried)
        if vision_obs is not None:
            return vision_obs
        return replace(last_obs, mechanic_used="") if last_obs else \
            RawObservation(error_detail="ladder_exhausted",
                           intended_value=value, intent_met=False)

    async def _vision_medic_rung(
        self, control: dict[str, Any], kind: str,
        ladder_tried: list[dict[str, str]],
    ) -> RawObservation | None:
        """A28 / R5 — ask the vision medic WHERE to click, then click there.

        Returns ``None`` whenever this rung declines (not wired, no bbox, medic
        unavailable, unusable answer) so the caller falls through to its existing
        residue path unchanged. It returns an observation ONLY when a real click
        was executed, and that observation carries R0's honest verdict — a
        vision-proposed click that changes nothing is ``intent_met=False``, the
        same as any other rung's failure.

        THE COORDINATE SPACE, WHICH IS WHERE THIS GOES WRONG
        ====================================================
        The endpoint returns ``click_x``/``click_y`` RELATIVE TO THE ELEMENT
        BBOX. Playwright's ``bounding_box()`` is VIEWPORT-relative. And
        :meth:`click_at` takes PAGE coordinates. Three spaces, and M3.1 already
        lost a whole milestone's worth of vision clicks to conflating two of
        them: every coordinate was silently off by the scroll offset, the click
        landed on nothing, and R0 dutifully reported the model had hallucinated.

        So the conversion is done once, here, explicitly:

            page_point = bbox_viewport + scroll_offset + medic_offset

        and ``click_at`` then performs its own page→viewport conversion. The
        scroll offset is READ, never assumed to be zero.
        """
        if self._vision_medic_oracle is None:
            return None
        # A bbox is what makes the medic's answer meaningful — its contract is
        # "where INSIDE this element". Without one there is nothing to be
        # relative to, and a click at an invented origin is worse than no click.
        try:
            box = await self._locator(control).bounding_box()
        except Exception as exc:
            logger.info("qec.explorer.vision_medic_no_bbox control=%s error=%s",
                        control.get("name", "?"), str(exc)[:160])
            return None
        if not isinstance(box, dict) or not box.get("width") or not box.get("height"):
            return None

        shot = await self._redacted_screenshot()
        if shot is None:
            # collect_pii_regions failed or the mask could not be proven. The
            # image is NOT sent — an unmaskable screenshot of a real application
            # is exactly what T-VIS-05 exists to stop.
            return None
        try:
            scroll = await self._page.evaluate(
                "() => ({sx: window.scrollX || 0, sy: window.scrollY || 0})")
            sx, sy = int(scroll.get("sx") or 0), int(scroll.get("sy") or 0)
        except Exception:
            sx = sy = 0
        page_box = {"x": float(box["x"]) + sx, "y": float(box["y"]) + sy,
                    "width": float(box["width"]), "height": float(box["height"])}

        try:
            decision = await self._vision_medic_oracle(
                screenshot_b64=shot.b64(),
                control=control, kind=kind, bbox=page_box,
                ladder_tried=ladder_tried,
                page_context={"title": (await self._page.title()) if self._page else "",
                              "url": self._safe_url(),
                              "page_w": shot.page_w, "page_h": shot.page_h},
                redaction=shot.receipt(),
            ) or {}
        except Exception as exc:
            logger.warning("qec.explorer.vision_medic_failed control=%s error=%s",
                           control.get("name", "?"), str(exc)[:200])
            return None

        status = str(decision.get("status") or "")
        if status == "display_only":
            # An honest "this is output, not a control" — the same terminal
            # answer the text medic may give, and NOT a failure.
            return RawObservation(url_before=self._safe_url(),
                                  url_after=self._safe_url(),
                                  mechanic_used="vision_medic:display_only",
                                  intent_met=None)
        if status != "proposed":
            return None
        try:
            cx = float(decision.get("click_x"))
            cy = float(decision.get("click_y"))
        except (TypeError, ValueError):
            return None
        # REFUSE A POINT OUTSIDE THE ELEMENT. The medic was asked where inside
        # this control to click; an offset beyond its box is either a
        # hallucination or an answer in the wrong coordinate space, and clicking
        # it would actuate some OTHER control while attributing the result to
        # this one — a mis-attributed actuation is worse than no actuation.
        if not (0 <= cx <= page_box["width"] and 0 <= cy <= page_box["height"]):
            logger.warning(
                "qec.explorer.vision_medic_refused reason=point_outside_bbox "
                "control=%s point=%.1f,%.1f box=%.1fx%.1f",
                control.get("name", "?"), cx, cy,
                page_box["width"], page_box["height"])
            return None

        px = int(round(page_box["x"] + cx))
        py = int(round(page_box["y"] + cy))
        logger.info(
            "qec.explorer.vision_medic_click control=%s action=%s page_point=%d,%d",
            control.get("name", "?"), decision.get("action") or "click", px, py)
        obs = await self.click_at(px, py)
        from dataclasses import replace as _replace
        return _replace(obs, mechanic_used=f"vision_medic:{decision.get('action') or 'click'}")

    async def _redacted_screenshot(self):
        """A masked full-page screenshot + its receipt, or ``None``.

        Mirrors :mod:`app.vision_loop`'s discipline deliberately: the receipt is
        computed over the SAME bytes that are sent, and a page whose sensitive
        regions could not be located is never photographed.
        """
        from .pixel_redaction import redact_screenshot
        try:
            png = await self.screenshot_png()
        except Exception as exc:
            logger.warning("qec.explorer.vision_medic_screenshot_failed error=%s",
                           str(exc)[:160])
            return None
        if not png:
            return None
        try:
            probe = await self.collect_pii_regions() or {}
        except Exception as exc:
            logger.warning("qec.explorer.vision_medic_pii_probe_failed error=%s",
                           str(exc)[:160])
            return None
        return redact_screenshot(
            png, list(probe.get("regions") or []),
            page_w=probe.get("page_w") or 0, page_h=probe.get("page_h") or 0,
            regions_ok=bool(probe.get("ok")))

    async def _execute_medic_action(
        self, control: dict[str, Any], kind: str, action: str, *,
        value: str = "", checked: bool = False, read_back: bool = False,
    ) -> RawObservation | None:
        """Execute a medic-proposed action through existing primitives.

        Maps vocabulary terms to Rung objects or direct _act calls.  Returns
        the observation (R0 verifies upstream) or None if the action is
        unrecognizable.
        """
        if action == "click":
            rung = Rung("click", "medic_click")
            return await self._run_rung(rung, control, kind, value=value,
                                        checked=checked, read_back=read_back)
        if action.startswith("press:"):
            key = action.split(":", 1)[1]
            if key in ("Space", "Enter", "ArrowDown"):
                rung = Rung("press", f"medic_{key.lower()}")
                return await self._run_rung(rung, control, kind, value=value,
                                            checked=checked, read_back=read_back)
        if action == "open_then_pick":
            rung = Rung("click_option", "medic_open_pick")
            return await self._run_rung(rung, control, kind, value=value,
                                        checked=checked, read_back=read_back)
        return None

    async def _run_rung(
        self, rung: Rung, control: dict[str, Any], kind: str, *,
        value: str = "", checked: bool = False, read_back: bool = False,
    ) -> RawObservation:
        """Execute one ladder rung through the appropriate low-level primitive."""
        if rung.kind == kind:
            return await self._act(control, kind, value=value,
                                   checked=checked, read_back=read_back)
        if rung.kind == "click":
            obs = await self._act(control, "click", read_back=read_back)
            return RawObservation(
                url_before=obs.url_before, url_after=obs.url_after,
                committed_value=obs.committed_value,
                dialog_opened=obs.dialog_opened,
                dialog_detail=obs.dialog_detail,
                error_detail=obs.error_detail,
                dom_changed=obs.dom_changed,
                intended_value=("true" if checked else "false")
                    if kind == "checked" else value,
                intent_met=verify_intent(
                    kind, intended_value=("true" if checked else "false")
                        if kind == "checked" else value,
                    intended_checked=checked,
                    committed_value=obs.committed_value,
                    error_detail=obs.error_detail,
                    url_before=obs.url_before, url_after=obs.url_after,
                    dom_changed=obs.dom_changed,
                    dialog_opened=obs.dialog_opened),
            )
        if rung.kind == "press":
            locator = self._locator(control)
            if locator is None:
                return RawObservation(error_detail="locator_unresolved",
                                     intended_value=value, intent_met=False)
            url_before = self._safe_url()
            sig_before = await self._interactive_signature()
            try:
                await locator.focus()
                if rung.variant == "focus_space":
                    await self._page.keyboard.press("Space")
                elif rung.variant == "focus_arrow":
                    await self._page.keyboard.press("ArrowRight")
                elif rung.variant == "type_chars":
                    await locator.press_sequentially(value, delay=30)
                else:
                    await self._page.keyboard.press("Space")
            except Exception as exc:
                return RawObservation(
                    url_before=url_before, url_after=self._safe_url(),
                    error_detail=f"action_error: {str(exc)[:200]}",
                    intended_value=value, intent_met=False)
            await self._settle()
            url_after = self._safe_url()
            committed = (await self._read_value(locator, kind=kind) if read_back else None)
            errors = await self.error_texts()
            dialogs = await self.dialog_flags()
            sig_after = await self._interactive_signature()
            err = (errors[0] if errors else "")
            intended = ("true" if checked else "false") \
                if kind == "checked" else value
            met = verify_intent(
                kind, intended_value=intended, intended_checked=checked,
                committed_value=committed, error_detail=err,
                url_before=url_before, url_after=url_after,
                dom_changed=(sig_before != sig_after),
                dialog_opened=bool(dialogs))
            return RawObservation(
                url_before=url_before, url_after=url_after,
                committed_value=committed,
                dialog_opened=bool(dialogs),
                dialog_detail=(dialogs[0] if dialogs else ""),
                dialog_is_challenge=(await self._dialog_challenge() if dialogs else False),
                error_detail=err,
                dom_changed=(sig_before != sig_after),
                intended_value=intended, intent_met=met)
        if rung.kind == "click_option":
            return await self._select_via_open_click(
                control, value, read_back=read_back)
        return await self._act(control, kind, value=value,
                               checked=checked, read_back=read_back)

    async def _select_via_open_click(
        self, control: dict[str, Any], value: str, *,
        read_back: bool = True,
    ) -> RawObservation:
        """Open a custom select by clicking it, then click the matching
        ``[role=option]`` by its label.  Dismiss with Escape on failure."""
        url_before = self._safe_url()
        sig_before = await self._interactive_signature()
        locator = self._locator(control)
        if locator is None:
            return RawObservation(error_detail="locator_unresolved",
                                 intended_value=value, intent_met=False)
        try:
            await locator.click()
            await self._settle()
            option = self._page.get_by_role("option", name=value).first
            await option.click()
        except Exception as exc:
            try:
                await self._page.keyboard.press("Escape")
            except Exception:
                pass
            return RawObservation(
                url_before=url_before, url_after=self._safe_url(),
                error_detail=f"action_error: {str(exc)[:200]}",
                intended_value=value, intent_met=False)
        await self._settle()
        url_after = self._safe_url()
        committed = (await self._read_value(locator, kind="select")
                     if read_back else None)
        errors = await self.error_texts()
        dialogs = await self.dialog_flags()
        sig_after = await self._interactive_signature()
        err = (errors[0] if errors else "")
        met = verify_intent(
            "select", intended_value=value, committed_value=committed,
            error_detail=err, url_before=url_before, url_after=url_after,
            dom_changed=(sig_before != sig_after),
            dialog_opened=bool(dialogs))
        return RawObservation(
            url_before=url_before, url_after=url_after,
            committed_value=committed,
            dialog_opened=bool(dialogs),
            dialog_detail=(dialogs[0] if dialogs else ""),
            dialog_is_challenge=(await self._dialog_challenge() if dialogs else False),
            error_detail=err,
            dom_changed=(sig_before != sig_after),
            intended_value=value, intent_met=met)

    def _locator(self, control: dict[str, Any]) -> Any:
        """Build a Playwright locator mirroring the compiler ladder (best-effort).

        Returns the first rung that BUILDS. See :meth:`_locator_builders` for why
        that is not the same as the first rung that MATCHES, and
        :meth:`_bound_locator` for the acting path that resolves the difference.
        """
        for builder in self._locator_builders(control):
            try:
                return builder()
            except Exception:
                continue
        return None

    async def _bound_locator(self, control: dict[str, Any]) -> Any:
        """The first rung of the ladder that actually MATCHES AN ELEMENT.

        THE DEFECT THIS CLOSES (M2.6). ``_locator`` walked its rungs and took the
        first that did not raise — but ``get_by_role(role, name=...)`` does not
        raise when it matches nothing; it happily returns a locator over zero
        elements. So for every control carrying both a role and a name (which is
        nearly all of them) the rungs below it were UNREACHABLE, and a control
        whose role Playwright does not expose could not be acted on at all even
        though capture had recorded a perfectly good css handle for it.

        Measured, not reasoned: a ``<summary>`` inside a ``<details>`` matches
        ``get_by_role`` for NO role in Chromium — not button, not group, not
        generic — while capture calls it a button (the tag's behaviour, which is
        what the crawler needs). Every click the crawl ever aimed at a native
        disclosure therefore spent a full action timeout and came back
        ``action_error``, and no ``<details>`` on any application was ever opened.
        Its css rung, ``summary``, was sitting one line further down the ladder.

        Falls back to :meth:`_locator` when NOTHING matches, so "no handle
        resolved" still reads as ``locator_unresolved`` / an action timeout
        exactly as before rather than becoming a new silent skip. The cost is one
        ``count()`` per rung tried, which is a page round trip an order of
        magnitude cheaper than the action timeout it avoids.

        THE ONE RISK, AND WHAT IS DONE ABOUT IT. ``count()`` does not auto-wait,
        while ``click()`` does — so a top rung whose element has not mounted YET
        would be skipped in favour of a lower rung that matches something else,
        and the action would land on the wrong control. The top rung therefore
        gets a second chance behind the port's own quiescence wait before the
        ladder descends past it. That costs nothing on the healthy path (the
        first check already matched) and only ever runs where the old code was
        about to spend a full action timeout anyway.
        """
        rungs = self._locator_builders(control)
        for i, builder in enumerate(rungs):
            try:
                loc = builder()
                if await loc.count() > 0:
                    return loc
                if i == 0:
                    await self._settle()
                    if await loc.count() > 0:
                        return loc
            except Exception:
                continue
        return self._locator(control)

    def _locator_builders(self, control: dict[str, Any]) -> list[Any]:
        """The compiler ladder as thunks, most specific first (best-effort)."""
        root = self._page
        for seg in (control.get("frame_selector") or "").split(" >>> "):
            seg = seg.strip()
            if seg:
                try:
                    root = root.frame_locator(seg)
                except Exception:
                    return None
        anchor = control.get("anchor")
        scope = root
        if anchor and anchor.get("label"):
            try:
                scope = root.get_by_role(anchor["kind"]).filter(
                    has_text=anchor["label"]).first
            except Exception:
                scope = root
        role = str(control.get("role") or "").strip()
        name = str(control.get("name") or "").strip()
        css_hint = str((control.get("qec") or {}).get("css_hint")
                       or control.get("css_hint") or "").strip()
        # POSITIONAL targeting: when a control is one of several IDENTICAL ones
        # (same role+name) and has no anchor to scope by, target it by its DOM
        # ORDINAL — get_by_role(role, name).nth(k) — instead of always .first.
        # This is the only way to click a specific button in a bare-button
        # questionnaire (17 identical "Yes"/"No"). A present anchor already scopes
        # the search, so nth is used only when no anchor label narrowed the scope.
        mi = control.get("match_index")
        nth = mi if (isinstance(mi, int) and mi >= 0
                     and not (anchor and anchor.get("label"))) else None

        def _role_name_loc() -> Any:
            base = scope.get_by_role(role, name=name)
            return base.nth(nth) if nth is not None else base.first

        return [b for b in (
            _role_name_loc if role and name else None,
            (lambda: scope.get_by_label(name).first) if name else None,
            (lambda: scope.get_by_text(name).nth(nth)) if name and nth is not None else
            (lambda: scope.get_by_text(name).first) if name else None,
            (lambda: scope.locator(css_hint).first) if css_hint else None,
        ) if b is not None]

    async def _read_value(self, locator: Any, *, kind: str = "") -> Optional[str]:
        """Read back what a control COMMITTED, so R0 can verify intent.

        The read must be in the SAME VOCABULARY as the intent, or a fill that
        genuinely worked is recorded as a failure. ``input_value()`` is the
        wrong vocabulary for two whole control families, and it does not raise
        on either — so reading it first fails silently and looks like the app's
        fault:

        * checkbox/radio commit their CHECKEDNESS. input_value() returns the
          value attribute ("term-life") against an intended "true".
        * <select> commits an OPTION. We select BY LABEL ("$50,000"), but
          input_value() returns the option's value attribute ("50000"). These
          differ for any coded list — amounts, state codes, ids — i.e. most
          enterprise forms. Observed live: Coverage Amount and Term Length both
          selected correctly and were both recorded intent_unmet, so the funnel
          stalled; the one select that DID pass was the one whose label happened
          to equal its value.
        """
        if kind == "checked":
            readers = ("is_checked", "input_value")
        elif kind == "select":
            readers = ("selected_label", "input_value")
        else:
            readers = ("input_value", "is_checked")
        for reader in readers:
            try:
                if reader == "input_value":
                    return await locator.input_value()
                if reader == "selected_label":
                    label = await locator.evaluate(
                        "el => el.selectedOptions && el.selectedOptions.length"
                        " ? el.selectedOptions[0].textContent : null")
                    if label is None:
                        continue
                    return " ".join(str(label).split())
                checked = await locator.is_checked()
                return "true" if checked else "false"
            except Exception:
                continue
        try:
            return (await locator.inner_text()).strip()
        except Exception:
            return None

    async def _interactive_signature(self) -> tuple:
        try:
            controls = await self.collect_controls()
            return tuple(tuple(t) for t in interactive_signature(controls))
        except Exception:
            return ()

    async def _settle(self) -> None:
        """THE synchronisation point after every action and every navigation.

        M1.5 EVENT ORDERING, resolved explicitly.  Playwright delivers a
        ``page`` event over the protocol, so it does NOT necessarily arrive
        before the click that caused it returns::

            click ──▶ (returns) ──▶ _settle ──▶ … ──▶ popup event arrives

        Adjudicating only on entry therefore adopted the popups that were
        already queued and MISSED the ones still in flight — measured, and
        exactly split by shape: ``window.open('')`` + a deferred navigation was
        adopted (its event had time to land), while a plain ``target="_blank"``
        was not.  So the pages are adjudicated on BOTH sides of the quiesce:

          1. adopt whatever is already queued, so the quiesce below waits on the
             page the journey is actually on rather than the one it just left;
          2. quiesce (network idle + the hydration gate) — which is many
             protocol round-trips, and is what gives an in-flight ``page`` event
             time to be delivered;
          3. if anything arrived during (2), adopt it and quiesce ONCE more, on
             the newly adopted page.

        Bounded at one extra quiesce, and the second pass costs two empty-list
        checks in the overwhelmingly common case where no page event happened.
        """
        # A running loop exists HERE even when the adapter was constructed
        # without one, so this is where a deferred `response` / `dialog`
        # subscription finally takes.
        await self._ensure_observers()
        # ...and where a download started by the action just performed is joined,
        # so the observation that follows describes a page whose file has landed.
        await self._await_downloads()
        await self._reconcile_quietly()
        await self._quiesce()
        if self._pending_pages or self._closed_pages:
            await self._reconcile_quietly()
            await self._quiesce()

    async def _reconcile_quietly(self) -> None:
        """:meth:`_reconcile_pages`, but page bookkeeping can never break an action."""
        try:
            await self._reconcile_pages()
        except Exception:
            logger.exception("qec.explorer.page_reconcile_failed")

    async def _quiesce(self) -> None:
        # 1. best-effort network quiesce.
        try:
            await self._page.wait_for_load_state("networkidle", timeout=_SETTLE_MS)
        except Exception:
            pass  # settle is best-effort — a busy SPA never blocks the recorder
        # 2. HYDRATION gate: poll a cheap DOM-quiescence signature until it is stable
        #    for _STABLE_READS consecutive reads (controls that mount after networkidle
        #    would otherwise be inventoried in a half-rendered state → wrong fingerprint
        #    + missed controls). Bounded by _STABILIZE_MS; never blocks.
        try:
            last = None
            stable = 0
            busy_polls = 0
            for _ in range(max(1, _STABILIZE_MS // _STABLE_POLL_MS)):
                sig = await self._page.evaluate(_QUIESCENCE_JS)
                # Empty-shell guard: a signature with too few interactive controls is an
                # un-rendered SPA shell — never let a stable-EMPTY page satisfy the gate,
                # or a client-rendered login/form is missed (0 forms). Keep polling until
                # real content mounts, or the bounded budget expires.
                try:
                    _interactive = int(str(sig).split(":", 1)[0])
                except Exception:
                    _interactive = _MIN_INTERACTIVE  # unparseable ⇒ allow settle
                if sig == last and _interactive >= _MIN_INTERACTIVE:
                    stable += 1
                    if stable >= _STABLE_READS:
                        # The DOM has stopped changing -- but a page can be
                        # perfectly still and still be working. See _BUSY_JS:
                        # a spinner holds its own shape while the content it is
                        # waiting for has not mounted, so "nothing changed" and
                        # "nothing left to do" are not the same page.
                        if busy_polls < _MAX_BUSY_POLLS:
                            try:
                                working = bool(await self._page.evaluate(_BUSY_JS))
                            except Exception:
                                working = False
                            if working:
                                busy_polls += 1
                                stable = 0
                                await asyncio.sleep(_STABLE_POLL_MS / 1000.0)
                                continue
                        break
                else:
                    stable = 0
                    last = sig
                await asyncio.sleep(_STABLE_POLL_MS / 1000.0)
        except Exception:
            pass

    async def materialize(self) -> None:
        """Bounded viewport progression: step-scroll to the bottom to trigger
        lazy / IntersectionObserver content and materialize additional
        virtual-scroll rows, re-settling after each step. READ-ONLY (no mutation)
        and best-effort. Cheap on static pages: seeded with the current height, it
        stops after ONE scroll when nothing new mounts (inventory + full-page
        screenshots are scroll-independent, so there is no scroll-back cost)."""
        try:
            prev_h = await self._page.evaluate(
                "document.body?document.body.scrollHeight:0"
            )
            for _ in range(_MATERIALIZE_STEPS):
                h = await self._page.evaluate(
                    "(()=>{var h=document.body?document.body.scrollHeight:0;"
                    "window.scrollTo(0,h);return h;})()"
                )
                await self._settle()
                if h <= prev_h:
                    break  # no new content materialized → stop
                prev_h = h
        except Exception:
            pass

    def _safe_url(self) -> str:
        try:
            return self._page.url or ""
        except Exception:
            return ""


__all__ = ["PlaywrightBrowserPort"]
