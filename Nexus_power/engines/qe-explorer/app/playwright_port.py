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

from . import emit
from . import field_signature
from . import page_lifecycle as pl
from .browser import BrowserPort, NavResult, RawObservation, verify_intent
from .fingerprint import interactive_signature
from .interaction_ladder import Rung, ladder_for
from . import observation_health
from .inventory_js import DISPLAYED_VALUES_JS, INVENTORY_JS, OPAQUE_JS

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

#: Cheap page-quiescence signature: visible-interactive count : readyState :
#: scrollHeight. Stable across two reads ⇒ the DOM has stopped mounting controls.
_QUIESCENCE_JS = (
    "(()=>{try{var e=document.querySelectorAll("
    "'a[href],button,input,select,textarea,[role],[tabindex]');"
    "var n=0;for(var i=0;i<e.length;i++){var el=e[i];"
    "if(el.offsetParent!==null||(el.getClientRects&&el.getClientRects().length))n++;}"
    "return n+':'+document.readyState+':'+Math.round("
    "document.body?document.body.scrollHeight:0);}catch(x){return 'err';}})()"
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
        self._artifact_dir = str(artifact_dir or "")
        self._artifacts_written = 0
        # API/network mining — a bounded buffer of the XHR/fetch calls the app
        # makes, filled by a passive `response` listener and drained per-visit by
        # the crawler.  Query strings are dropped + paths PII-scrubbed HERE (at
        # source) so raw PII never lingers in the buffer.
        self._net_buffer: list[dict[str, Any]] = []
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
        if len(self._net_buffer) < self._NET_BUFFER_MAX:
            self._net_buffer.append(entry)

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
            self._record_net({
                "method": str(getattr(request, "method", "") or "").upper(),
                "url": url,
                "has_query": bool(parts.query),
                "status": str(getattr(response, "status", "") or ""),
                "resource_type": "sse" if is_sse else rtype,
                "request_mime": req_headers.get("content-type", "").split(";", 1)[0],
                "response_mime": resp_mime,
                "response_bytes": resp_headers.get("content-length", ""),
                "timestamp_ms": int(time.monotonic() * 1000),
            })
        except Exception:  # never let a listener crash affect the page
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
                "timestamp_ms": int(time.monotonic() * 1000),
            })
        except Exception:
            pass

    async def drain_network(self) -> list[dict[str, Any]]:
        """Return + CLEAR the network calls buffered since the last drain."""
        drained = self._net_buffer
        self._net_buffer = []
        return drained

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

    async def error_texts(self) -> list[str]:
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
        self._action_label = label
        self._action_verb = verb

    async def _act(self, control: dict[str, Any], kind: str, *, value: str = "",
                   checked: bool = False, read_back: bool = False) -> RawObservation:
        self._set_action_context(control, kind)
        url_before = self._safe_url()
        sig_before = await self._interactive_signature()
        intended = value if kind != "checked" else ("true" if checked else "false")
        locator = self._locator(control)
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
            error_detail=err,
            dom_changed=(sig_before != sig_after),
            intended_value=intended, intent_met=met,
        )

    async def click_at(self, x: int, y: int) -> RawObservation:
        """Coordinate rung (U2/U3) — click absolute page ``(x, y)`` via
        ``page.mouse``, for a vision-proposed point on a DOM-opaque surface. Mirrors
        the ``_act`` observe pattern (url + interactive-signature before/after) so a
        coordinate action produces the SAME grounded ``RawObservation`` and R0
        verdict: a click that changes nothing is honestly ``intent_met=False``."""
        self._set_action_context("", "click_at")
        url_before = self._safe_url()
        sig_before = await self._interactive_signature()
        try:
            xi, yi = int(x), int(y)
        except (TypeError, ValueError):
            return RawObservation(url_before=url_before, url_after=url_before,
                                  error_detail="bad_coordinates", intent_met=False)
        try:
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
        return replace(last_obs, mechanic_used="") if last_obs else \
            RawObservation(error_detail="ladder_exhausted",
                           intended_value=value, intent_met=False)

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
            error_detail=err,
            dom_changed=(sig_before != sig_after),
            intended_value=value, intent_met=met)

    def _locator(self, control: dict[str, Any]) -> Any:
        """Build a Playwright locator mirroring the compiler ladder (best-effort)."""
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

        for builder in (
            _role_name_loc if role and name else None,
            (lambda: scope.get_by_label(name).first) if name else None,
            (lambda: scope.get_by_text(name).nth(nth)) if name and nth is not None else
            (lambda: scope.get_by_text(name).first) if name else None,
            (lambda: scope.locator(css_hint).first) if css_hint else None,
        ):
            if builder is None:
                continue
            try:
                return builder()
            except Exception:
                continue
        return None

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
