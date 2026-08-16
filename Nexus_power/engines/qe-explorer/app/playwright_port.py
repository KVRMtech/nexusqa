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
from typing import Any, Optional, Sequence
from urllib.parse import urlsplit

from . import emit
from . import field_signature
from .browser import BrowserPort, NavResult, RawObservation, verify_intent
from .fingerprint import interactive_signature
from .interaction_ladder import Rung, ladder_for
from .inventory_js import DISPLAYED_VALUES_JS, INVENTORY_JS, OPAQUE_JS

logger = logging.getLogger("qe-explorer")


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
                 medic_oracle: Any = None) -> None:
        self._page = page
        self._context = context
        self._proven_mechanics = dict(proven_mechanics or {})
        self._medic_oracle = medic_oracle
        # API/network mining — a bounded buffer of the XHR/fetch calls the app
        # makes, filled by a passive `response` listener and drained per-visit by
        # the crawler.  Query strings are dropped + paths PII-scrubbed HERE (at
        # source) so raw PII never lingers in the buffer.
        self._net_buffer: list[dict[str, Any]] = []
        try:
            self._page.on("response", self._on_response)
        except Exception:  # a fake/None page (defensive) — no network evidence.
            logger.warning("qec.explorer.network_listener_unavailable")
        try:  # (D) real-time transports — WebSocket opens are a distinct surface.
            self._page.on("websocket", self._on_websocket)
        except Exception:
            logger.warning("qec.explorer.websocket_listener_unavailable")

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

    async def goto(self, url: str) -> NavResult:
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

    async def collect_controls(self) -> list[dict[str, Any]]:
        try:
            result = await self._page.evaluate(INVENTORY_JS)
            return list(result or [])
        except Exception as exc:
            logger.warning("qec.explorer.inventory_failed error=%s", str(exc)[:200])
            return []

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

    async def _act(self, control: dict[str, Any], kind: str, *, value: str = "",
                   checked: bool = False, read_back: bool = False) -> RawObservation:
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
        seq = [str(k) for k in (keys or []) if str(k)]

        async def _do():
            for k in seq:
                await self._page.keyboard.press(k)

        return await self._gesture_observe(_do)

    async def scroll_until(self, control: dict[str, Any],
                           max_steps: int = 10) -> RawObservation:
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
