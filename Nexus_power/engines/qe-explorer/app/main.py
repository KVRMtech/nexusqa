"""QE-Central Contained Explorer — the FastAPI SERVICE + Playwright adapter.

Lifecycle, single-flight job control and the HMAC-signed completion callback for
the ``qe-explorer`` container (design §3.2 §API surface).  The concrete
:class:`app.browser.BrowserPort` implementation (:class:`PlaywrightBrowserPort`)
lives HERE and only here, so every other module stays browser-free and
unit-testable.

Isolation invariants enforced at context creation (design §1.1):
  * the browser is launched with ``--proxy-server=<EGRESS_PROXY>`` — the only
    route to the internet is squid, which allowlists the client host(s);
  * ``service_workers='block'`` so a SW can't smuggle background requests past
    the guard;
  * ``context.route('**/*', …)`` is the FAIL-CLOSED net: every request is
    classified by :meth:`app.crawler.GuardContext.decide` and ABORTED unless
    explicitly allowed — squid enforces the HOST, the guard enforces the METHOD.

Auth: inbound ``/api/v1/explore`` requires the shared ``X-QEC-Token`` HMAC
secret (RUNNER_TOKEN pattern, constant-time compare); the completion callback to
qe-central is body-signed with the same secret so qe-central can trust it.  The
container holds NO DB creds and NO KMS — only the per-fleet token and, in
memory, the login creds for the current job.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from contextlib import asynccontextmanager
from typing import Any, Optional

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, status
from pydantic import BaseModel, Field

from . import emit
from .browser import BrowserPort, NavResult, RawObservation
from .config import settings
from .crawler import Budget, Crawler, CrawlSummary, GuardContext
from .fingerprint import interactive_signature
from .forms import AnswerKey
from .auth import AuthWindow, Credentials
from .guard import Attestation, RefusePack, load_refuse_pack
from .inventory_js import DISPLAYED_VALUES_JS, INVENTORY_JS, INVENTORY_JS_VERSION

logging.basicConfig(level=getattr(logging, settings.log_level.upper(), logging.INFO))
logger = logging.getLogger("qe-explorer")

EXPLORER_VERSION = f"qe-explorer/1.0+{INVENTORY_JS_VERSION}"

# Playwright launch args: --no-sandbox is required for Chromium in an
# unprivileged container (mirrors Dockerfile.runner:28).
_LAUNCH_ARGS = ["--no-sandbox", "--disable-dev-shm-usage"]
_SETTLE_MS = 1500          # post-action network-settle budget (best-effort)
_ACTION_TIMEOUT_MS = 5000  # per-locator action timeout


# ─── Request model (design §3.2 POST /api/v1/explore) ────────────────────────


class ExploreRequest(BaseModel):
    """Start-a-crawl payload."""

    crawl_id: str = Field(min_length=1, max_length=36)
    tenant_id: str = Field(min_length=1, max_length=64)
    # Opaque qe-central handle echoed VERBATIM in the completion callback so
    # qe-central can match the finished crawl to its pending exploration row
    # (the callback is keyed on (exploration_id, tenant_id); design §3.1/§3.2).
    exploration_id: str = Field(default="", max_length=64)
    target_url: str = Field(min_length=1, max_length=2000)
    credentials: Optional[dict[str, Any]] = None
    answer_key: dict[str, Any] = Field(default_factory=dict)
    budgets: dict[str, Any] = Field(default_factory=dict)
    allowed_hosts: list[str] = Field(default_factory=list)
    phase: str = "explore"
    submit_approvals: list[str] = Field(default_factory=list)
    attestation: Optional[dict[str, Any]] = None
    #: A pre-captured Playwright ``storageState`` (cookies + origins) to START the
    #: browser context authenticated — the tier-4 escape hatch for logins the
    #: crawler cannot script (captcha / SSO / hardware token): a human/client
    #: session is injected so the crawl begins already logged-in. qe-central
    #: resolves it (a stored client session, or a fetched auth-hook) and relays it
    #: here; ``None`` ⇒ a normal cold-session crawl.
    session: Optional[dict[str, Any]] = None
    #: Multi-env crawl bindings — routing cookies [{name,value,domain,path}] and
    #: extra request headers select a specific environment (Gloo/Istio); basic-auth
    #: passes a dev gate. Empty ⇒ byte-identical to a cold crawl.
    cookies: list[dict[str, Any]] = Field(default_factory=list)
    extra_http_headers: dict[str, str] = Field(default_factory=dict)
    http_credentials: Optional[dict[str, Any]] = None
    #: env_assertion {selector,expect_text}|{url_pattern} for the reference env. NOTE:
    #: crawl-time enforcement is not yet wired (the RUN path enforces it via the hard
    #: compiler env-pin); at crawl the fail-closed routing-cookie set (above) is the
    #: guard against landing on the wrong env. Carried here for when the crawl-side
    #: check lands.
    env_assertion: Optional[dict[str, Any]] = None


def _config_fingerprint(req: ExploreRequest, refuse_pack_version: str) -> str:
    """Deterministic dedup key for the crawl config (→ artifact media_fingerprint)."""
    material = json.dumps({
        "target_url": req.target_url,
        "budgets": Budget.from_dict(req.budgets).as_dict(),
        "explorer_version": EXPLORER_VERSION,
        "refuse_pack_version": refuse_pack_version,
        "allowed_hosts": sorted(h.lower() for h in req.allowed_hosts),
    }, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


# ─── Job manager (single-flight — one heavy browser at a time) ───────────────


class _Job:
    def __init__(self, crawler: Crawler) -> None:
        self.crawler = crawler
        self.task: Optional[asyncio.Task] = None
        self.summary: Optional[CrawlSummary] = None
        self.error: str = ""


class JobManager:
    """At most ONE active crawl per explorer container (409 otherwise).

    A crawl is ``pending`` from accept until its browser+crawler are built, then
    ``active`` until it finishes.  The accept gate (:meth:`accept`) checks and
    marks pending atomically (FastAPI handlers run single-threaded on the loop,
    so there is no await between check and mark) — the authoritative single-flight.
    """

    def __init__(self) -> None:
        self._active: Optional[_Job] = None
        self._pending: set[str] = set()
        self._by_id: dict[str, _Job] = {}

    @property
    def busy(self) -> bool:
        return self._active is not None or bool(self._pending)

    def accept(self, crawl_id: str) -> None:
        """Atomically reserve the single-flight slot, or 409 if busy."""
        if self.busy:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                                detail="explorer busy — a crawl is already running")
        self._pending.add(crawl_id)

    def activate(self, job: _Job) -> None:
        self._active = job
        self._by_id[job.crawler.crawl_id] = job
        self._pending.discard(job.crawler.crawl_id)

    def is_pending(self, crawl_id: str) -> bool:
        return crawl_id in self._pending

    def get(self, crawl_id: str) -> Optional[_Job]:
        return self._by_id.get(crawl_id)

    def finish(self, crawl_id: str, job: Optional[_Job]) -> None:
        self._pending.discard(crawl_id)
        if job is not None and self._active is job:
            self._active = None
        elif job is None and self._active is None:
            # a crawl that never activated still frees the slot.
            self._active = None


# ─── Lifespan ────────────────────────────────────────────────────────────────


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Fail-CLOSED: a missing/malformed refuse pack must stop the explorer from
    # starting (guard.load_refuse_pack raises FileNotFoundError/ValueError).
    refuse_pack: RefusePack = load_refuse_pack(settings.refuse_pack_path)
    app.state.refuse_pack = refuse_pack
    app.state.jobs = JobManager()
    app.state.http = httpx.AsyncClient(timeout=30.0)
    logger.info("qec.explorer.started version=%s refuse_pack=%s port=%d",
                EXPLORER_VERSION, refuse_pack.version, settings.port)
    try:
        yield
    finally:
        await app.state.http.aclose()
        logger.info("qec.explorer.stopped")


app = FastAPI(title="QE-Central Contained Explorer", version=EXPLORER_VERSION,
              lifespan=lifespan)


# ─── Auth dependency ─────────────────────────────────────────────────────────


async def require_token(x_qec_token: str = Header(default="")) -> None:
    """Constant-time X-QEC-Token check (fail-closed on empty secret/token)."""
    if not settings.token_matches(x_qec_token):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="invalid or missing X-QEC-Token")


# ─── Endpoints ───────────────────────────────────────────────────────────────


@app.get("/health")
async def health() -> dict[str, Any]:
    pack: RefusePack = app.state.refuse_pack
    jobs: JobManager = app.state.jobs
    return {
        "status": "ok",
        "service": settings.service_name,
        "version": EXPLORER_VERSION,
        "refuse_pack_version": pack.version,
        "egress_proxy_configured": bool(settings.egress_proxy),
        "busy": jobs.busy,
    }


@app.post("/api/v1/explore", status_code=status.HTTP_202_ACCEPTED)
async def explore(req: ExploreRequest, _: None = Depends(require_token)) -> dict[str, Any]:
    """Start a contained crawl (202); single-flight → 409 when busy."""
    pack: RefusePack = app.state.refuse_pack
    jobs: JobManager = app.state.jobs

    budget = Budget.from_dict(req.budgets)
    answer_key = AnswerKey.from_payload(req.answer_key)
    credentials = Credentials.from_payload(req.credentials)
    attestation = _attestation(req.attestation)
    guard_ctx = GuardContext(
        refuse_pack=pack,
        auth_window=AuthWindow(max_requests=settings.auth_max_requests,
                               window_ms=settings.auth_window_ms),
        attestation=attestation,
        submit_flow_approved=bool(req.submit_approvals),
    )

    # Atomically reserve the single-flight slot (409 if busy). The Crawler is
    # built inside the task (it needs the live port); the reservation bridges the
    # gap so a second request cannot slip in before the browser is ready.
    jobs.accept(req.crawl_id)

    fingerprint = _config_fingerprint(req, pack.version)
    asyncio.create_task(_run_job(
        req=req, budget=budget, answer_key=answer_key, credentials=credentials,
        guard_ctx=guard_ctx, config_fingerprint=fingerprint, pack=pack, jobs=jobs,
    ))
    logger.info("qec.explorer.accepted crawl_id=%s tenant_id=%s", req.crawl_id, req.tenant_id)
    return {"crawl_id": req.crawl_id, "status": "accepted",
            "explorer_version": EXPLORER_VERSION, "config_fingerprint": fingerprint}


@app.get("/api/v1/explore/{crawl_id}")
async def explore_status(crawl_id: str, _: None = Depends(require_token)) -> dict[str, Any]:
    """Progress / final summary for a crawl."""
    jobs: JobManager = app.state.jobs
    job = jobs.get(crawl_id)
    if job is None:
        if jobs.is_pending(crawl_id):
            return {"crawl_id": crawl_id, "running": True, "phase": "starting"}
        raise HTTPException(status_code=404, detail="unknown crawl_id")
    progress = job.crawler.progress()
    if job.summary is not None:
        progress["summary"] = _summary_public(job.summary)
    if job.error:
        progress["error"] = job.error
    return progress


@app.post("/api/v1/explore/{crawl_id}/cancel")
async def explore_cancel(crawl_id: str, _: None = Depends(require_token)) -> dict[str, Any]:
    """Request a graceful stop; the manifest is flushed and the partial crawl
    reported with ``stop_reason='cancelled'``."""
    jobs: JobManager = app.state.jobs
    job = jobs.get(crawl_id)
    if job is None:
        raise HTTPException(status_code=404, detail="unknown crawl_id")
    job.crawler.cancel()
    logger.info("qec.explorer.cancel_requested crawl_id=%s", crawl_id)
    return {"crawl_id": crawl_id, "status": "cancelling"}


# ─── Job runner (owns the Playwright lifecycle) ──────────────────────────────


async def _run_job(
    *, req: ExploreRequest, budget: Budget, answer_key: AnswerKey,
    credentials: Optional[Credentials], guard_ctx: GuardContext,
    config_fingerprint: str, pack: RefusePack, jobs: JobManager,
) -> None:
    """Build the browser + crawler, run the crawl, fire the completion callback.

    A single ``try/finally`` guarantees the browser is torn down and the
    single-flight slot released even on failure — a crashed crawl never wedges
    the container.
    """
    from playwright.async_api import async_playwright  # lazy: browser-only dep

    job: Optional[_Job] = None
    summary: Optional[CrawlSummary] = None
    error = ""
    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                headless=True,
                proxy={"server": settings.egress_proxy} if settings.egress_proxy else None,
                args=_LAUNCH_ARGS,
            )
            # Tier-4 session injection: when qe-central relays a pre-captured
            # storageState, start the context authenticated (cookies + origins) so
            # a crawl can proceed past a login the crawler cannot script. A bad/
            # empty session is ignored (a normal cold crawl), never a hard failure.
            _ctx_kwargs: dict[str, Any] = {"service_workers": "block", "ignore_https_errors": False}
            if isinstance(req.session, dict) and (req.session.get("cookies") or req.session.get("origins")):
                _ctx_kwargs["storage_state"] = req.session
                logger.info("qec.explorer.session_injected crawl_id=%s", req.crawl_id)
            # Multi-env crawl bindings (empty ⇒ byte-identical to today).
            if req.extra_http_headers:
                _ctx_kwargs["extra_http_headers"] = dict(req.extra_http_headers)
            if isinstance(req.http_credentials, dict) and req.http_credentials.get("username"):
                _ctx_kwargs["http_credentials"] = {
                    "username": req.http_credentials.get("username", ""),
                    "password": req.http_credentials.get("password", ""),
                }
            context = await browser.new_context(**_ctx_kwargs)
            context.set_default_timeout(_ACTION_TIMEOUT_MS)
            if req.cookies:  # routing cookies (Gloo/canary) — cookies are a method call
                # FAIL-CLOSED: if the env routing cookie can't be set, the crawl must
                # NOT proceed cookieless — that would land on the DEFAULT env (often
                # prod) and green-wash a wrong-env baseline every later run rebinds.
                # Abort honestly instead (the _run_job try/finally records a failure).
                await context.add_cookies(list(req.cookies))
                logger.info("qec.explorer.env_cookies_set crawl_id=%s n=%d",
                            req.crawl_id, len(req.cookies))
            page = await context.new_page()
            port = PlaywrightBrowserPort(page, context)

            crawler = Crawler(
                port,
                crawl_id=req.crawl_id, tenant_id=req.tenant_id,
                target_url=req.target_url, work_dir=settings.work_dir,
                refuse_pack=pack, budget=budget, explorer_version=EXPLORER_VERSION,
                guard_version=EXPLORER_VERSION, refuse_pack_version=pack.version,
                config_fingerprint=config_fingerprint, guard_context=guard_ctx,
                answer_key=answer_key, credentials=credentials,
                allowed_hosts=req.allowed_hosts, max_relogins=settings.max_relogins,
                submit_approvals=req.submit_approvals,
            )
            job = _Job(crawler)
            jobs.activate(job)

            # THE fail-closed net — wired now that the crawler (guard + emitter)
            # exists.  Squid enforces host; this enforces method + phase.
            await context.route("**/*", _make_route_handler(crawler))

            summary = await crawler.run()
            job.summary = summary
            try:
                await browser.close()
            except Exception:  # pragma: no cover
                pass
    except Exception as exc:  # honest failure — surfaced on GET + callback
        error = str(exc)[:500]
        logger.exception("qec.explorer.job_failed crawl_id=%s", req.crawl_id)
        if job is not None:
            job.error = error
    finally:
        jobs.finish(req.crawl_id, job)

    await _fire_callback(req, summary, error)


def _make_route_handler(crawler: Crawler):
    """Build the ``context.route`` handler bound to this crawl's guard state."""
    async def handler(route: Any, request: Any) -> None:
        try:
            decision = crawler.guard.decide(
                request.method, request.url, now_ms=crawler.now_ms(),
            )
        except Exception:  # fail-CLOSED: any classifier error aborts the request
            logger.exception("qec.explorer.guard_decide_error — aborting request")
            crawler.note_network_guard_block()
            await route.abort()
            return
        if decision.allow:
            await route.continue_()
            return
        crawler.note_network_guard_block()
        try:
            crawler.emitter.emit_guard_event(
                kind=decision.event_kind or "blocked_method",
                method=request.method, url=request.url, rule_id=decision.rule_id,
                severity=decision.severity, reason=decision.reason,
                phase=crawler.guard.phase.value,
            )
        except Exception:  # pragma: no cover
            logger.exception("qec.explorer.guard_event_emit_failed")
        await route.abort()

    return handler


async def _fire_callback(req: ExploreRequest, summary: Optional[CrawlSummary],
                         error: str) -> None:
    """POST the HMAC-signed completion callback to qe-central (best-effort).

    The body carries the manifest path (on the shared volume) + the in-memory
    ``storage_state`` (the ONLY channel a captured session leaves the container).
    Signed with the shared secret so qe-central can trust the caller.  A missing
    endpoint / network error is logged honestly — the durable manifest is the
    source of truth regardless.
    """
    body = {
        "crawl_id": req.crawl_id,
        "tenant_id": req.tenant_id,
        "exploration_id": req.exploration_id,
        "explorer_version": EXPLORER_VERSION,
        "stop_reason": summary.stop_reason if summary else "error",
        # Top-level field the qe-central CompletionCallback persists (§3.1); it
        # mirrors stats.guard_blocks so the guard-block count is not lost.
        "guard_events": summary.guard_blocks if summary else 0,
        "error": error,
        "manifest_path": summary.manifest_path if summary else "",
        "stats": {
            "states": summary.states if summary else 0,
            "actions": summary.actions if summary else 0,
            "screenshots": summary.screenshots if summary else 0,
            "guard_blocks": summary.guard_blocks if summary else 0,
        },
        "storage_state": summary.storage_state if summary else None,
        "coverage": summary.coverage if summary else None,
    }
    payload = json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
    signature = settings.sign_payload(payload)
    url = settings.callback_url.rstrip("/") + settings.callback_path(req.crawl_id)
    try:
        client: httpx.AsyncClient = app.state.http
        resp = await client.post(
            url, content=payload,
            headers={"Content-Type": "application/json",
                     "X-QEC-Signature": signature,
                     "X-QEC-Token": settings.explorer_token},
        )
        logger.info("qec.explorer.callback_sent crawl_id=%s status=%d",
                    req.crawl_id, resp.status_code)
    except Exception as exc:
        logger.warning("qec.explorer.callback_failed crawl_id=%s error=%s "
                       "(manifest at %s is authoritative)",
                       req.crawl_id, str(exc)[:200],
                       summary.manifest_path if summary else "?")


def _summary_public(summary: CrawlSummary) -> dict[str, Any]:
    return {
        "crawl_id": summary.crawl_id, "stop_reason": summary.stop_reason,
        "states": summary.states, "actions": summary.actions,
        "screenshots": summary.screenshots, "guard_blocks": summary.guard_blocks,
        "manifest_path": summary.manifest_path, "detail": summary.detail,
        "storage_state_captured": summary.storage_state is not None,
    }


def _attestation(payload: Optional[dict[str, Any]]) -> Optional[Attestation]:
    if not payload:
        return None
    try:
        return Attestation.model_validate(payload)
    except Exception as exc:
        logger.warning("qec.explorer.bad_attestation error=%s", str(exc)[:200])
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

    def __init__(self, page: Any, context: Any) -> None:
        self._page = page
        self._context = context

    async def goto(self, url: str) -> NavResult:
        try:
            resp = await self._page.goto(url, wait_until="domcontentloaded")
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

    async def fill(self, control: dict[str, Any], value: str) -> RawObservation:
        return await self._act(control, "fill", value=value, read_back=True)

    async def select_option(self, control: dict[str, Any], value: str) -> RawObservation:
        return await self._act(control, "select", value=value, read_back=True)

    async def set_checked(self, control: dict[str, Any], checked: bool) -> RawObservation:
        return await self._act(control, "checked", checked=checked, read_back=True)

    async def storage_state(self) -> dict[str, Any]:
        return await self._context.storage_state()

    # -- internals -------------------------------------------------------------

    async def _act(self, control: dict[str, Any], kind: str, *, value: str = "",
                   checked: bool = False, read_back: bool = False) -> RawObservation:
        url_before = self._safe_url()
        sig_before = await self._interactive_signature()
        locator = self._locator(control)
        if locator is None:
            return RawObservation(url_before=url_before, url_after=url_before,
                                  error_detail="locator_unresolved")
        try:
            if kind == "click":
                await locator.click()
            elif kind == "fill":
                await locator.fill(value)
            elif kind == "select":
                await locator.select_option(label=value)
            elif kind == "checked":
                await locator.set_checked(checked)
        except Exception as exc:
            return RawObservation(url_before=url_before, url_after=self._safe_url(),
                                  error_detail=f"action_error: {str(exc)[:200]}")
        await self._settle()
        url_after = self._safe_url()
        committed = await self._read_value(locator) if read_back else None
        errors = await self.error_texts()
        dialogs = await self.dialog_flags()
        sig_after = await self._interactive_signature()
        return RawObservation(
            url_before=url_before, url_after=url_after, committed_value=committed,
            dialog_opened=bool(dialogs), dialog_detail=(dialogs[0] if dialogs else ""),
            error_detail=(errors[0] if errors else ""),
            dom_changed=(sig_before != sig_after),
        )

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
        for builder in (
            (lambda: scope.get_by_role(role, name=name).first) if role and name else None,
            (lambda: scope.get_by_label(name).first) if name else None,
            (lambda: scope.get_by_text(name).first) if name else None,
        ):
            if builder is None:
                continue
            try:
                return builder()
            except Exception:
                continue
        return None

    async def _read_value(self, locator: Any) -> Optional[str]:
        for reader in ("input_value", "is_checked"):
            try:
                if reader == "input_value":
                    return await locator.input_value()
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
        try:
            await self._page.wait_for_load_state("networkidle", timeout=_SETTLE_MS)
        except Exception:
            pass  # settle is best-effort — a busy SPA never blocks the recorder

    def _safe_url(self) -> str:
        try:
            return self._page.url or ""
        except Exception:
            return ""


# ─── Entrypoint (container CMD is `python -m app.main`) ──────────────────────


def _run() -> None:
    """Launch the ASGI server binding 0.0.0.0:${QEC_EXPLORER_PORT}.

    A single uvicorn worker enforces the single-flight invariant (one heavy
    browser per container) at the process level.
    """
    import uvicorn

    uvicorn.run(
        app,
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.lower(),
        workers=1,
    )


if __name__ == "__main__":
    _run()
