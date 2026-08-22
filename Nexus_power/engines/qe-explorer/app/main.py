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
import os
import time
from collections import OrderedDict
from contextlib import asynccontextmanager
from typing import Any, Optional, Sequence

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, status
from pydantic import BaseModel, Field

from . import (completion_manifest, crawl_context, emit,
               evidence_publisher, metrics)
from .config import settings
from .crawl_context import CrawlTokenUsage
from .crawler import TRAVERSAL_FULL, Budget, Crawler, CrawlSummary, GuardContext
from .forms import AnswerKey
from .auth import AuthWindow, Credentials
from .attest import AttestReason, verify_provisioning_proof
from . import vision_gate
from .guard import Attestation, GuardRule, RefusePack, load_refuse_pack
from .walk_persist import WalkAuthorization
from .inventory_js import INVENTORY_JS_VERSION
from .playwright_port import (_ACTION_TIMEOUT_MS, _LAUNCH_ARGS,
                              PlaywrightBrowserPort, context_defaults,
                              install_capture_hooks)

logging.basicConfig(level=getattr(logging, settings.log_level.upper(), logging.INFO))
logger = logging.getLogger("qe-explorer")

EXPLORER_VERSION = f"qe-explorer/1.0+{INVENTORY_JS_VERSION}"



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
    #: A4.3 / T-AC-02 — PER-CONTROL BOUNDARY APPROVALS.
    #: ``submit_approvals`` is a flat list of LABELS. It cannot say "this
    #: control, on this page, once", so it was never able to authorise an
    #: irreversible action at least privilege — the only route to one was the
    #: ``"*"`` blanket, which authorises every submit in the application.
    #: Each entry here is ``{control, url?, state_fingerprint?, max_crossings?,
    #: approval_id?, approved_by?, approved_at?}``. ``"*"`` is REFUSED at parse
    #: time. Empty ⇒ behaviour is byte-identical to before this field existed.
    boundary_approvals: list[dict[str, Any]] = Field(default_factory=list)
    #: TARGET MODE (R3 Mode 2) — URL-path prefixes the crawl is CONFINED to
    #: (e.g. ["/quote"]): only URLs whose path equals a prefix or sits under it
    #: are enqueued/recorded; everything else on the host is out of scope. The
    #: journey the operator supplied is validated exhaustively instead of the
    #: whole app being explored. Empty ⇒ classic whole-app Explore mode
    #: (byte-identical behaviour).
    scope_path_prefixes: list[str] = Field(default_factory=list)
    #: Federated / SSO login (#7) — the DECLARED trusted Identity-Provider domains
    #: the login flow may redirect to (login.microsoftonline.com / okta.com / …).
    #: The guard treats an AUTH-phase POST to one of these as a login domain; the
    #: egress fence (qe-central) must also allowlist them. Empty ⇒ no SSO crossing.
    idp_domains: list[str] = Field(default_factory=list)
    #: Caged-planner exploration PLAN — grounded {priority_patterns:[{pattern,
    #: weight}]} from qe-central. Applied as FRONTIER PRIORITY ONLY (reorders; never
    #: adds a state or changes reachability). Re-bounded defensively on this side.
    plan: dict[str, Any] = Field(default_factory=dict)
    #: FIELD LEARNING (P1/P4). Two value-carrying-vs-shape-carrying halves, kept
    #: separate on purpose:
    #:   ``recalled_values``  {signature: value} — values THIS tenant supplied
    #:       before, decrypted by qe-central for this one crawl. Tenant-private;
    #:       never logged, never emitted, never leaves this process.
    #:   ``field_priors``     {signature: {type, confidence, ...}} — pooled,
    #:       VALUE-FREE knowledge of what a field with that signature is FOR.
    #: Both are optional: absent, the crawl behaves exactly as it did before.
    recalled_values: dict[str, str] = Field(default_factory=dict)
    field_priors: dict[str, Any] = Field(default_factory=dict)
    #: Seed for the crawl's fictional identity. Stable per tenant+app so the same
    #: client presents the same person every crawl — a quote that changes because
    #: the age changed between runs is a false difference, not a regression.
    identity_seed: str = Field(default="", max_length=200)
    #: M1.7 / T-GW-03 — THIS DISPATCH IS A RESUME of an existing crawl id.
    #: qe-central re-dispatches a stalled crawl under its ORIGINAL crawl_id and
    #: sets this. It does not change how the crawl runs; it changes what a
    #: MISSING durable prefix means. For a fresh crawl an empty prefix is normal;
    #: for a resume it means the evidence we were told to continue is gone, and
    #: walking the app from zero under that id would supersede a real crawl with
    #: an empty capture. Absent ⇒ byte-identical pre-M1.7 behaviour.
    resume: bool = False
    #: M1.7 / T-GW-04 — BUSINESS RULES earlier crawls of this app PROVED, already
    #: tenant- and app-scoped by qe-central. Value-free (labels are product UI
    #: text). Empty ⇒ every blocked advance runs the full experiment, exactly as
    #: before this field existed.
    known_rules: list[dict[str, Any]] = Field(default_factory=list)
    #: DATA MODE — the operator's dial. "user" (default) is byte-identical to the
    #: behaviour before field learning existed: a radio group is a semantic choice
    #: and is left to the client. "agent" answers everything honestly answerable so
    #: a funnel completes unattended, recording each choice in the field ledger.
    data_mode: str = Field(default="user", max_length=16)
    #: CRAWL MODE — "explore" / "target" / "e2e". Only the wizard STEP BUDGET
    #: differs: e2e walks a funnel to its end rather than sampling six steps of it.
    #: Every safety gate is identical in all three, because a deeper walk must not
    #: be a laxer one.
    crawl_mode: str = Field(default="explore", max_length=16)
    #: TRAVERSAL POSTURE — "full" | "probe" | "observe", derived by qe-central from
    #: the env attestation the operator signed (``prod_guard.traversal_posture``).
    #: HOW FAR a business journey may be walked — never WHAT may be clicked.
    #:   ``full``    the environment is attested non-prod: identify the forward
    #:               control with every available tier and walk each journey to its
    #:               end, so the catalogue holds whole journeys instead of samples.
    #:   ``probe``   fail-closed default; byte-identical to the behaviour before
    #:               this field existed (strict-regex advance, probe step budgets).
    #:   ``observe`` production: catalogue only.
    #: The refuse-pack danger gate and the disposable-attestation submit tier are
    #: unchanged and re-checked at click time regardless of this value.
    traversal: str = Field(default="probe", max_length=16)
    #: U2 — per-tenant vision autonomy, set by qe-central's autonomy_flags["vision"]
    #: (env flag AND tenant flag, fail-closed). Default OFF: no vision call is made.
    vision_enabled: bool = Field(default=False)
    #: BRANCH WALK (Journey Graph C4): {field signature → forced option label}.
    #: A planned walk takes the enumerated option the default data would not.
    #: Applies ONLY to enumerable controls and ONLY to options they themselves
    #: offer (fail-closed; ``planned`` provenance in the field ledger) — never
    #: free text, never a value injection, and no safety gate changes with it.
    choice_overrides: dict[str, str] = Field(default_factory=dict)
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
    #: R4 MECHANIC MEMORY. ``{control_sig: mechanic_variant}`` — the proven
    #: ladder rung for each control this tenant has interacted with before.
    #: Value-free (only rung variant names); never logged.
    proven_mechanics: dict[str, str] = Field(default_factory=dict)
    #: POSTURE: observe-only mode for production environments.  When True, the
    #: crawler captures pages/fields/locators/navigation but never fills a form,
    #: never submits, and never advances a commit.
    #:
    #: This is a FLOOR, not the decision: :func:`resolve_observe_only` raises it
    #: to True whenever ``env_kind`` is not an attested DISPOSABLE environment,
    #: so a manipulated or bypassed qe-central cannot dispatch a mutating crawl
    #: at a non-disposable target by simply sending False (M0.5 T-SEC-05).
    observe_only: bool = False
    #: The environment kind qe-central resolved for this crawl (``disposable`` |
    #: ``staging`` | ``uat`` | ``production_test`` | ``prod``).  EMPTY means "not
    #: stated", which is treated exactly like production: fail-closed.
    env_kind: str = Field(default="", max_length=32)


#: The ONLY env_kind on which a crawl may type, fill, submit or advance a commit.
#: Every other value — including an absent, unrecognised or blank one — is
#: observation-only.  This mirrors the submit doctrine already enforced by
#: ``guard.Attestation.is_submit_capable`` and extends it to FILL, which was the
#: hole: a staging/prod crawl could not submit, but could still type into a
#: real application's forms.
MUTABLE_ENV_KIND = "disposable"


def resolve_observe_only(req: "ExploreRequest",
                         attestation: Optional[Attestation]) -> bool:
    """The AUTHORITATIVE observe-only decision, made inside crawl execution.

    M0.5 T-SEC-05.  The previous design derived this in qe-central's
    configuration-resolution path and shipped the answer here as a boolean, so
    the invariant held only as long as that one caller behaved.  Now the crawl
    process decides for itself, from the attestation it was actually handed:

      * an explicit ``observe_only=True`` is always honoured (a floor);
      * otherwise mutation is permitted ONLY when the resolved ``env_kind`` is
        ``disposable``;
      * an absent/blank/unknown ``env_kind`` resolves to observe-only.

    ``req.env_kind`` and the attestation's own ``env_kind`` must AGREE — if the
    dispatch claims ``disposable`` but the signed attestation says otherwise,
    the attestation wins and the crawl is observation-only.
    """
    if bool(req.observe_only):
        return True
    declared = str(getattr(req, "env_kind", "") or "").strip().lower()
    attested = str(getattr(attestation, "env_kind", "") or "").strip().lower()
    if attested and attested != declared:
        # The signed statement is the authority; a dispatch that disagrees with
        # it is exactly the manipulation this gate exists to survive.
        logger.warning(
            "qec.explorer.env_kind_mismatch declared=%r attested=%r — "
            "using the attestation", declared, attested)
        declared = attested
    return declared != MUTABLE_ENV_KIND


def _config_fingerprint(req: ExploreRequest, refuse_pack_version: str) -> str:
    """Deterministic dedup key for the crawl config (→ artifact media_fingerprint)."""
    parts = {
        "target_url": req.target_url,
        "budgets": Budget.from_dict(req.budgets).as_dict(),
        "explorer_version": EXPLORER_VERSION,
        "refuse_pack_version": refuse_pack_version,
        "allowed_hosts": sorted(h.lower() for h in req.allowed_hosts),
    }
    # Only when SET — a scoped (Target-mode) crawl captures a different slice of
    # the app, so it must mint its own artifact; unscoped crawls keep their
    # historical fingerprints byte-stable (dedup reuse unaffected).
    if req.scope_path_prefixes:
        parts["scope_path_prefixes"] = sorted(req.scope_path_prefixes)
    material = json.dumps(parts, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


# ─── Job manager (single-flight — one heavy browser at a time) ───────────────


class _Job:
    def __init__(self, crawler: Crawler) -> None:
        self.crawler = crawler
        self.task: Optional[asyncio.Task] = None
        self.summary: Optional[CrawlSummary] = None
        self.error: str = ""


# ── Explicit job lifecycle (M0.5 T-SEC-09) ───────────────────────────────────
# A job used to have no terminal state: ``finish`` cleared the active slot but
# left the job in ``_by_id`` forever, so N sequential crawls grew the map
# without bound AND a completed crawl stayed indistinguishable from a live one
# in every lookup that authorises an operation.
JOB_CREATED = "created"      # slot reserved; browser not built yet
JOB_RUNNING = "running"      # crawler live
JOB_FINISHED = "finished"    # terminal — evicted from active lookup

#: How many FINISHED crawls keep a readable summary.  Bounded on purpose: the
#: status endpoint must still answer for a crawl that just ended, but terminal
#: state must never accumulate across a fleet's lifetime.
_FINISHED_RETAIN = 32


class JobManager:
    """At most ONE active crawl per explorer container (409 otherwise).

    A crawl is ``created`` from :meth:`reserve` until its browser+crawler are
    built, then ``running`` until it finishes, then ``finished`` — at which
    point it is EVICTED from the active map into a small bounded terminal
    ring.  The reservation gate checks and marks atomically (FastAPI handlers
    run single-threaded on the loop, so there is no await between check and
    mark) — the authoritative single-flight.

    OWNERSHIP (M0.5 T-SEC-03/T-SEC-07): every reservation records the tenant
    that made it.  A later ``explore``/``status``/``cancel`` for that crawl id
    must come from the SAME tenant, so one tenant can neither drive nor observe
    another tenant's job on a shared worker.
    """

    def __init__(self) -> None:
        self._active: Optional[_Job] = None
        self._pending: set[str] = set()
        self._by_id: dict[str, _Job] = {}
        #: crawl_id → owning tenant, for every non-terminal crawl.
        self._owner: dict[str, str] = {}
        #: crawl_id → lifecycle state, for every non-terminal crawl.
        self._state: dict[str, str] = {}
        #: bounded terminal ring: crawl_id → (tenant_id, public summary/error).
        self._finished: "OrderedDict[str, tuple[str, dict]]" = OrderedDict()

    @property
    def busy(self) -> bool:
        return self._active is not None or bool(self._pending)

    @property
    def active_count(self) -> int:
        """Number of crawls in a NON-terminal state (the leak canary)."""
        return len(self._by_id) + len(self._pending)

    # ── reservation ──────────────────────────────────────────────────────

    def reserve(self, crawl_id: str, tenant_id: str) -> None:
        """Atomically claim the single-flight slot for ``tenant_id``, or 409.

        This is the ONLY place the slot is taken.  qe-central calls it BEFORE it
        writes this worker's egress allowlist, so a worker that is busy with
        another tenant's crawl refuses here and its fence is never touched
        (T-SEC-03).
        """
        if self.busy:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                                detail="explorer busy — a crawl is already running")
        if not str(tenant_id or "").strip():
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                                detail="reservation requires a tenant_id")
        self._pending.add(crawl_id)
        self._owner[crawl_id] = str(tenant_id)
        self._state[crawl_id] = JOB_CREATED

    def accept(self, crawl_id: str, tenant_id: str) -> None:
        """Claim the slot for a dispatch, honouring an existing reservation.

        When ``crawl_id`` is already reserved BY THIS TENANT the slot is already
        held and this is a no-op; a reservation held by ANOTHER tenant, or a
        busy worker with no matching reservation, is a 409/403.
        """
        holder = self._owner.get(crawl_id)
        if holder is not None:
            if holder != str(tenant_id):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="crawl_id is reserved by another tenant",
                )
            return
        self.reserve(crawl_id, tenant_id)

    def release(self, crawl_id: str, tenant_id: str) -> None:
        """Give an unused reservation back (dispatch aborted before it started).

        Only the OWNER may release, and only while the crawl has not started —
        otherwise a second tenant could free a running crawl's slot and race in.
        """
        holder = self._owner.get(crawl_id)
        if holder is None:
            return
        if holder != str(tenant_id):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                                detail="crawl is owned by another tenant")
        if self._state.get(crawl_id) == JOB_RUNNING:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                                detail="crawl is running — cancel it instead")
        self._pending.discard(crawl_id)
        self._owner.pop(crawl_id, None)
        self._state.pop(crawl_id, None)

    def owner(self, crawl_id: str) -> str:
        """The tenant that owns ``crawl_id`` (active or recently finished)."""
        if crawl_id in self._owner:
            return self._owner[crawl_id]
        entry = self._finished.get(crawl_id)
        return entry[0] if entry else ""

    def assert_owner(self, crawl_id: str, tenant_id: str) -> None:
        """403 unless ``tenant_id`` owns ``crawl_id``.

        An UNKNOWN crawl id is deliberately NOT distinguished from one owned by
        somebody else at the call sites (they 404 uniformly), so this never
        becomes an existence oracle.
        """
        holder = self.owner(crawl_id)
        if holder and holder != str(tenant_id):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                                detail="crawl belongs to another tenant")

    # ── lifecycle ────────────────────────────────────────────────────────

    def activate(self, job: _Job) -> None:
        crawl_id = job.crawler.crawl_id
        self._active = job
        self._by_id[crawl_id] = job
        self._pending.discard(crawl_id)
        self._state[crawl_id] = JOB_RUNNING

    def is_pending(self, crawl_id: str) -> bool:
        return crawl_id in self._pending

    def state(self, crawl_id: str) -> str:
        if crawl_id in self._state:
            return self._state[crawl_id]
        return JOB_FINISHED if crawl_id in self._finished else ""

    def get(self, crawl_id: str) -> Optional[_Job]:
        return self._by_id.get(crawl_id)

    def finished_view(self, crawl_id: str) -> Optional[dict]:
        entry = self._finished.get(crawl_id)
        return dict(entry[1]) if entry else None

    def finish(self, crawl_id: str, job: Optional[_Job]) -> None:
        """Terminalise a crawl: free the slot AND EVICT it from active lookup.

        The job's final progress is copied into a bounded terminal ring so
        ``GET /api/v1/explore/{id}`` still answers, then every active-state map
        drops the id.  Without the eviction, ``_by_id`` grew by one per crawl
        forever and a finished job could still be resolved — and authorised —
        as if it were live.
        """
        tenant_id = self._owner.get(crawl_id, "")
        terminal: dict = {"crawl_id": crawl_id, "running": False,
                          "lifecycle": JOB_FINISHED}
        if job is not None:
            try:
                terminal.update(job.crawler.progress())
            except Exception:  # pragma: no cover — never fail terminalisation
                pass
            terminal["running"] = False
            terminal["lifecycle"] = JOB_FINISHED
            if job.summary is not None:
                terminal["summary"] = _summary_public(job.summary)
            if job.error:
                terminal["error"] = job.error

        self._pending.discard(crawl_id)
        self._by_id.pop(crawl_id, None)
        self._owner.pop(crawl_id, None)
        self._state.pop(crawl_id, None)
        if job is not None and self._active is job:
            self._active = None
        elif job is None and not self._by_id:
            # a crawl that never activated still frees the slot.
            self._active = None

        self._finished[crawl_id] = (tenant_id, terminal)
        self._finished.move_to_end(crawl_id)
        while len(self._finished) > _FINISHED_RETAIN:
            self._finished.popitem(last=False)


# ─── Lifespan ────────────────────────────────────────────────────────────────


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Fail-CLOSED: a missing/malformed refuse pack must stop the explorer from
    # starting (guard.load_refuse_pack raises FileNotFoundError/ValueError).
    refuse_pack: RefusePack = load_refuse_pack(settings.refuse_pack_path)
    app.state.refuse_pack = refuse_pack
    app.state.jobs = JobManager()
    app.state.http = httpx.AsyncClient(timeout=30.0)
    # Publish build identity the moment the process is scrapeable, so a
    # freshly-started explorer with no crawl yet still exports a series — the
    # difference between "alive and idle" and "down" must never be an absence.
    metrics.set_build_info(version=EXPLORER_VERSION,
                           refuse_pack_version=refuse_pack.version)
    logger.info("qec.explorer.started version=%s refuse_pack=%s port=%d",
                EXPLORER_VERSION, refuse_pack.version, settings.port)
    crawl_context.emit(crawl_context.EV_EXPLORER_STARTED,
                       version=EXPLORER_VERSION,
                       refuse_pack_version=refuse_pack.version,
                       port=settings.port)
    # ── M1.7 / T-GW-02 · RECONCILE ON STARTUP ───────────────────────────────
    # A restart is the single most likely moment for an orphan to exist: the
    # process that owned a completion delivery is exactly the one that died. The
    # startup sweep runs UNCONDITIONALLY (the periodic loop below is the part
    # that is env-gated), because gating recovery-after-a-crash behind a variable
    # nobody set is how a recovery path stays theoretical.
    #
    # Scheduled rather than awaited: a volume holding many crawls must not delay
    # the port opening, and an explorer that cannot answer /health because it is
    # busy recovering looks exactly like an explorer that is down.
    app.state.sweeper = asyncio.ensure_future(_startup_and_periodic_sweep())
    try:
        yield
    finally:
        sweeper = getattr(app.state, "sweeper", None)
        if sweeper is not None:
            sweeper.cancel()
            try:
                await sweeper
            except (asyncio.CancelledError, Exception):     # pragma: no cover
                pass
        await app.state.http.aclose()
        logger.info("qec.explorer.stopped")
        crawl_context.emit(crawl_context.EV_EXPLORER_STOPPED,
                           version=EXPLORER_VERSION)


async def _startup_and_periodic_sweep() -> None:
    """One sweep at startup, then the env-gated periodic loop."""
    try:
        cleared = await _sweep_orphaned_completions()
        if cleared:
            logger.warning(
                "qec.explorer.startup_sweep_recovered crawls=%d — completions "
                "left un-acknowledged by a previous process instance", cleared)
    except asyncio.CancelledError:
        raise
    except Exception:                                       # pragma: no cover
        logger.warning("qec.explorer.startup_sweep_failed", exc_info=True)
    await _sweeper_loop()


app = FastAPI(title="QE-Central Contained Explorer", version=EXPLORER_VERSION,
              lifespan=lifespan)

# Prometheus exposition (M0.6 / T-OB-01). Mounted OUTSIDE /api/* and without the
# X-QEC-Token dependency — a scraper holds no token, and the payload carries no
# crawl id, URL, credential or prompt, so it has the same public posture as
# /health. Rendering reads pre-aggregated counters only: no crawl computation,
# and it stays responsive while a crawl is running.
app.include_router(metrics.build_metrics_router())


# ─── Auth dependency ─────────────────────────────────────────────────────────


async def require_token(x_qec_token: str = Header(default="")) -> None:
    """Constant-time X-QEC-Token check (fail-closed on empty secret/token)."""
    if not settings.token_matches(x_qec_token):
        # A rejected dispatch is still a dispatch ATTEMPT — counting it here is
        # what makes "qe-central is calling with the wrong fleet token" visible
        # as a dispatch-funnel gap instead of a silent absence of crawls.
        metrics.record_dispatch(outcome="unauthorized")
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
    # M1.3 · CONTROLLED WALK PERSISTENCE. Verified HERE, once, before a browser
    # exists — and re-checked on every single request by the guard. A denial is
    # not an error: it is the default, and it is what production always gets.
    walk_authorization, walk_denied_reason = _walk_authorization(req)
    guard_ctx = GuardContext(
        refuse_pack=pack,
        auth_window=AuthWindow(max_requests=settings.auth_max_requests,
                               window_ms=settings.auth_window_ms),
        attestation=attestation,
        submit_flow_approved=bool(req.submit_approvals),
        walk_authorization=walk_authorization,
        walk_denied_reason=walk_denied_reason,
        idp_domains=frozenset(req.idp_domains),
    )

    # Atomically reserve the single-flight slot (409 if busy). The Crawler is
    # built inside the task (it needs the live port); the reservation bridges the
    # gap so a second request cannot slip in before the browser is ready.
    #
    # The 409 is the WORKER-CONTENTION signal qe-central sees as a failed
    # dispatch, so it is counted on the way out rather than inferred from the
    # absence of a crawl.
    try:
        jobs.accept(req.crawl_id, req.tenant_id)
    except HTTPException as exc:
        metrics.record_dispatch(
            outcome="busy_409" if exc.status_code == status.HTTP_409_CONFLICT
            else "rejected")
        crawl_context.emit(
            crawl_context.EV_CRAWL_REFUSED,
            reason="single_flight_busy" if exc.status_code == 409 else "rejected",
            status_code=exc.status_code,
            refused_crawl_id=crawl_context.sanitize_id(req.crawl_id),
        )
        raise

    metrics.record_dispatch(outcome="accepted")

    fingerprint = _config_fingerprint(req, pack.version)
    asyncio.create_task(_run_job(
        req=req, budget=budget, answer_key=answer_key, credentials=credentials,
        guard_ctx=guard_ctx, config_fingerprint=fingerprint, pack=pack, jobs=jobs,
    ))
    logger.info("qec.explorer.accepted crawl_id=%s tenant_id=%s", req.crawl_id, req.tenant_id)
    return {"crawl_id": req.crawl_id, "status": "accepted",
            "explorer_version": EXPLORER_VERSION, "config_fingerprint": fingerprint}


class ReserveRequest(BaseModel):
    """Claim the single-flight slot BEFORE the caller fences this worker's egress.

    M0.5 T-SEC-03.  The old order was: write the worker's squid allowlist, then
    POST /explore and find out it was busy.  That let tenant B rewrite the fence
    of a worker running tenant A's crawl — B's dispatch failed, but A's browser
    was left pointing at B's allowlist.  Reservation-first makes the fence write
    unreachable unless the slot is actually held by the caller.
    """

    crawl_id: str = Field(min_length=1, max_length=36)
    tenant_id: str = Field(min_length=1, max_length=64)


@app.post("/api/v1/reserve", status_code=status.HTTP_200_OK)
async def reserve_worker(
    req: ReserveRequest, _: None = Depends(require_token),
) -> dict[str, Any]:
    """Atomically claim this worker for ``(crawl_id, tenant_id)``, or 409."""
    jobs: JobManager = app.state.jobs
    jobs.reserve(req.crawl_id, req.tenant_id)
    logger.info("qec.explorer.reserved crawl_id=%s tenant_id=%s",
                req.crawl_id, req.tenant_id)
    return {"crawl_id": req.crawl_id, "status": "reserved",
            "explorer_version": EXPLORER_VERSION}


@app.post("/api/v1/reserve/{crawl_id}/release", status_code=status.HTTP_200_OK)
async def release_worker(
    crawl_id: str, req: ReserveRequest, _: None = Depends(require_token),
) -> dict[str, Any]:
    """Return an UNUSED reservation (the dispatch aborted before it started).

    Owner-only, and refused once the crawl is running — otherwise a second
    tenant could free a live crawl's slot and race into it.
    """
    jobs: JobManager = app.state.jobs
    jobs.release(crawl_id, req.tenant_id)
    return {"crawl_id": crawl_id, "status": "released"}


@app.get("/api/v1/explore/{crawl_id}")
async def explore_status(
    crawl_id: str,
    tenant_id: str = "",
    _: None = Depends(require_token),
) -> dict[str, Any]:
    """Progress / final summary for a crawl (owner-scoped).

    ``tenant_id`` is REQUIRED and must match the tenant that reserved the crawl:
    a shared fleet token proves the caller is qe-central, never WHICH tenant it
    is acting for, so without this check any tenant could read another tenant's
    live crawl progress (page urls, control names) off a shared worker.
    """
    jobs: JobManager = app.state.jobs
    if not tenant_id.strip():
        raise HTTPException(status_code=400, detail="tenant_id is required")
    jobs.assert_owner(crawl_id, tenant_id)

    job = jobs.get(crawl_id)
    if job is None:
        if jobs.is_pending(crawl_id):
            return {"crawl_id": crawl_id, "running": True, "phase": "starting",
                    "lifecycle": JOB_CREATED}
        terminal = jobs.finished_view(crawl_id)
        if terminal is not None:
            return terminal
        raise HTTPException(status_code=404, detail="unknown crawl_id")
    progress = job.crawler.progress()
    progress["lifecycle"] = jobs.state(crawl_id) or JOB_RUNNING
    if job.summary is not None:
        progress["summary"] = _summary_public(job.summary)
    if job.error:
        progress["error"] = job.error
    return progress


@app.post("/api/v1/explore/{crawl_id}/cancel")
async def explore_cancel(
    crawl_id: str,
    tenant_id: str = "",
    _: None = Depends(require_token),
) -> dict[str, Any]:
    """Request a graceful stop; the manifest is flushed and the partial crawl
    reported with ``stop_reason='cancelled'``.  Owner-scoped: one tenant can
    never cancel another tenant's crawl on a shared worker."""
    jobs: JobManager = app.state.jobs
    if not tenant_id.strip():
        raise HTTPException(status_code=400, detail="tenant_id is required")
    jobs.assert_owner(crawl_id, tenant_id)
    job = jobs.get(crawl_id)
    if job is None:
        raise HTTPException(status_code=404, detail="unknown crawl_id")
    job.crawler.cancel()
    logger.info("qec.explorer.cancel_requested crawl_id=%s", crawl_id)
    return {"crawl_id": crawl_id, "status": "cancelling"}


# ─── E2E advance oracle (LLM-assisted wizard advance) ─────────────────────


#: Oracle consultation statuses — the crawler's terminal classification depends
#: on the distinction: ``none`` (the agent honestly said nothing advances) ends
#: a walk as covered; ``unavailable`` (the decision could not be made) ends it
#: as NOT covered. They are never conflated.
_ORACLE_PICKED = "picked"
_ORACLE_NONE = "none"
_ORACLE_UNAVAILABLE = "unavailable"


class _CrawlTelemetry:
    """Per-crawl observability accumulator, shared by the crawl's oracles.

    Exists because the ORACLE-PARTICIPATION verdict cannot be read off any one
    oracle closure: three oracles may be wired, each keeps its own resilience
    state, and the terminal record needs one honest answer to "did the oracle
    take part?".

    ``oracle_calls`` counts consultations that REACHED THE WIRE.  A consultation
    short-circuited by the open circuit breaker or the per-crawl call cap is
    counted in Prometheus (so the resilience state is visible) but deliberately
    NOT here: a crawl whose every consultation was refused by its own breaker
    made zero oracle calls, and letting the short-circuits mark it ``used``
    would hide precisely the failure this milestone exists to expose.
    """

    def __init__(self, crawl_id: str) -> None:
        self.oracle_configured = False
        self.oracle_calls = 0
        self.tokens = CrawlTokenUsage(crawl_id=crawl_id)

    def note_call(self) -> None:
        self.oracle_calls += 1

    def note_usage(self, body: Any) -> None:
        """Fold any provider-reported token usage on an oracle reply into the
        crawl's spend record.  Silent when the body carries none — the usage is
        recorded when the provider reports it and never estimated."""
        try:
            if not isinstance(body, dict):
                return
            usage = crawl_context.usage_from_response(body.get("usage") or body)
            if usage["prompt_tokens"] is None and usage["completion_tokens"] is None:
                return
            self.tokens.record(**usage)
            metrics.record_llm_usage(
                provider=usage["provider"], model=usage["model"],
                outcome="success",
                prompt_tokens=usage["prompt_tokens"] or 0,
                completion_tokens=usage["completion_tokens"] or 0,
                cache_read_tokens=usage["cache_read_tokens"] or 0,
                cache_creation_tokens=usage["cache_creation_tokens"] or 0,
            )
            crawl_context.emit(
                crawl_context.EV_LLM_COMPLETED,
                provider=usage["provider"], model=usage["model"],
                prompt_tokens=usage["prompt_tokens"] or 0,
                completion_tokens=usage["completion_tokens"] or 0,
            )
        except Exception:  # telemetry must never break an oracle consultation
            logger.debug("qec.explorer.usage_capture_failed", exc_info=True)


def _note_oracle_outcome(
    oracle: str, outcome: str, started: float, *, failure_reason: str = "",
) -> None:
    """Record ONE completed oracle consultation: outcome, latency, failure class.

    Called from every terminating branch of every oracle so an ``unavailable``
    is instrumented exactly as thoroughly as a ``picked`` — an oracle whose
    calls all fail must show up as failures, not as an absence of calls.
    """
    elapsed = max(0.0, time.monotonic() - started)
    metrics.record_oracle_call(oracle=oracle, outcome=outcome,
                               duration_seconds=elapsed,
                               failure_reason=failure_reason)
    crawl_context.emit(crawl_context.EV_ORACLE_COMPLETED, oracle=oracle,
                       outcome=outcome, duration_ms=int(elapsed * 1000),
                       failure_reason=failure_reason)


def _make_advance_oracle(
    http_client: httpx.AsyncClient, tenant_id: str, crawl_id: str,
    telemetry: Optional[_CrawlTelemetry] = None,
):
    """Return an async callable the Crawler invokes when the deterministic
    regex cannot identify a wizard-advance control.  The callable POSTs the
    page's clickable controls to qe-central's ``/internal/pick-advance``
    endpoint, which asks the agent which control a human would click to move
    forward.

    Contract: returns ``{"index": int | None, "status": picked|none|
    unavailable, "signature": str}``.  ``unavailable`` covers every way the
    decision could not be made — transport failure, non-200, timeout, an
    unreadable body, the per-crawl call cap, or the open circuit — and the
    crawler turns it into the honest ``oracle_unavailable`` terminal.  The
    callable never raises and never blocks a crawl beyond its timeout.

    Resilience state is per-crawl (one oracle per crawl):
      * circuit breaker — after ``advance_oracle_breaker_threshold``
        CONSECUTIVE unavailable outcomes, no further HTTP attempts this crawl;
      * call cap — at ``advance_oracle_max_calls`` HTTP calls, consultations
        end ``unavailable`` (a pathological app cannot burn unbounded tokens).
    """
    state = {"consecutive_failures": 0, "circuit_open": False,
             "calls": 0, "cap_logged": False}
    unavailable = {"index": None, "status": _ORACLE_UNAVAILABLE, "signature": ""}

    async def oracle(
        controls: Sequence[dict[str, Any]],
        page_title: str,
        page_url: str,
    ) -> dict[str, Any]:
        if state["circuit_open"]:
            metrics.record_oracle_call(oracle="advance", outcome="circuit_open",
                                       failure_reason="circuit_open")
            return dict(unavailable)
        if state["calls"] >= settings.advance_oracle_max_calls:
            if not state["cap_logged"]:
                state["cap_logged"] = True
                logger.warning(
                    "qec.explorer.advance_oracle_cap_reached crawl_id=%s cap=%d",
                    crawl_id, settings.advance_oracle_max_calls)
            metrics.record_oracle_call(oracle="advance", outcome="cap_reached",
                                       failure_reason="cap_reached")
            return dict(unavailable)
        state["calls"] += 1
        if telemetry is not None:
            telemetry.note_call()
        started = time.monotonic()
        crawl_context.emit(crawl_context.EV_ORACLE_CALLED, oracle="advance",
                           controls=len(controls))
        body = {
            # M0.5 T-SEC-07: the crawl id is the SERVER-VERIFIABLE identity.
            # qe-central resolves the owning tenant from it and refuses when the
            # body's tenant_id disagrees, so the body can no longer name whose
            # data this consultation is about.
            "crawl_id": crawl_id,
            "tenant_id": tenant_id,
            "controls": [
                {"name": c.get("name", ""), "kind": c.get("kind", ""),
                 "disabled": c.get("disabled", False),
                 "danger": c.get("danger", False)}
                for c in controls
            ],
            "page_title": page_title or "",
            "page_url": page_url or "",
        }
        payload = json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
        url = settings.callback_url.rstrip("/") + "/internal/pick-advance"
        failure_reason = "bad_body"
        try:
            # Signing INSIDE the guarded block: with no fleet secret configured
            # (T-SEC-01 removed the shipped default) this raises, and an
            # unsignable consultation must degrade to the honest `unavailable`
            # the crawler already handles — never crash a running crawl.
            signature = settings.sign_payload(
                payload, scope=f"pick-advance:{crawl_id}")
            resp = await http_client.post(
                url, content=payload,
                headers=crawl_context.crawl_headers({
                    "Content-Type": "application/json",
                    "X-QEC-Signature": signature,
                    "X-QEC-Token": settings.explorer_token,
                }),
                timeout=settings.advance_oracle_timeout_s,
            )
            if resp.status_code == 200:
                data = resp.json()
                status = str(data.get("status") or "")
                idx = data.get("control_index")
                sig = str(data.get("signature") or "")
                # Token usage travels back on the oracle reply so the crawl that
                # SPENT the tokens is the crawl they are attributed to — captured
                # before the status branch, because a `none` verdict costs the
                # same tokens a `picked` one does.
                if telemetry is not None:
                    telemetry.note_usage(data)
                if status == _ORACLE_PICKED and isinstance(idx, int):
                    state["consecutive_failures"] = 0
                    _note_oracle_outcome("advance", _ORACLE_PICKED, started)
                    return {"index": idx, "status": _ORACLE_PICKED,
                            "signature": sig}
                if status == _ORACLE_NONE:
                    state["consecutive_failures"] = 0
                    _note_oracle_outcome("advance", _ORACLE_NONE, started)
                    return {"index": None, "status": _ORACLE_NONE,
                            "signature": sig}
                # Anything else — including a legacy body without ``status`` —
                # is a decision NOT made.
            else:
                failure_reason = "http_error"
        except Exception as exc:
            failure_reason = (
                "timeout" if isinstance(exc, httpx.TimeoutException) else "transport")
            logger.warning("qec.explorer.advance_oracle_failed crawl_id=%s error=%s",
                           crawl_id, str(exc)[:200])
        _note_oracle_outcome("advance", _ORACLE_UNAVAILABLE, started,
                             failure_reason=failure_reason)
        state["consecutive_failures"] += 1
        if (state["consecutive_failures"] >= settings.advance_oracle_breaker_threshold
                and not state["circuit_open"]):
            state["circuit_open"] = True
            logger.warning(
                "qec.explorer.advance_oracle_circuit_open crawl_id=%s failures=%d",
                crawl_id, state["consecutive_failures"])
        return dict(unavailable)

    return oracle


_MEDIC_PROPOSED = "proposed"
_MEDIC_DISPLAY_ONLY = "display_only"
_MEDIC_UNAVAILABLE = "unavailable"


def _make_medic_oracle(
    http_client: httpx.AsyncClient, tenant_id: str, crawl_id: str,
    telemetry: Optional[_CrawlTelemetry] = None,
):
    """Return an async callable the crawler invokes when the deterministic
    ladder is exhausted and R0 still reports intent-unmet.  The callable
    POSTs the control's shape to qe-central's ``/internal/operate-control``
    endpoint, which asks the medic agent what action to try.

    Contract: returns ``{"action": str, "status": proposed|display_only|
    unavailable}``.  Same resilience pattern as the advance oracle:
    per-crawl circuit breaker + call cap.  The callable never raises.
    """
    state = {"consecutive_failures": 0, "circuit_open": False,
             "calls": 0, "cap_logged": False}
    unavailable = {"action": "", "status": _MEDIC_UNAVAILABLE}

    async def medic(
        control: dict[str, Any],
        intent: str,
        ladder_results: list[dict[str, Any]],
        page_context: dict[str, Any],
    ) -> dict[str, Any]:
        if state["circuit_open"]:
            metrics.record_oracle_call(oracle="medic", outcome="circuit_open",
                                       failure_reason="circuit_open")
            return dict(unavailable)
        if state["calls"] >= settings.medic_oracle_max_calls:
            if not state["cap_logged"]:
                state["cap_logged"] = True
                logger.warning(
                    "qec.explorer.medic_oracle_cap_reached crawl_id=%s cap=%d",
                    crawl_id, settings.medic_oracle_max_calls)
            metrics.record_oracle_call(oracle="medic", outcome="cap_reached",
                                       failure_reason="cap_reached")
            return dict(unavailable)
        state["calls"] += 1
        if telemetry is not None:
            telemetry.note_call()
        started = time.monotonic()
        crawl_context.emit(crawl_context.EV_ORACLE_CALLED, oracle="medic")
        body = {
            "crawl_id": crawl_id,          # T-SEC-07 server-verifiable identity
            "tenant_id": tenant_id,
            "control": {
                "name": control.get("name", ""),
                "kind": control.get("kind", ""),
                "role": control.get("role", ""),
                "tag": control.get("tag", ""),
                "css_hint": control.get("css_hint", ""),
                "disabled": control.get("disabled", False),
                "danger": control.get("danger", False),
            },
            "intent": intent,
            "ladder_results": ladder_results[:8],
            "page_context": page_context,
        }
        payload = json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
        url = settings.callback_url.rstrip("/") + "/internal/operate-control"
        failure_reason = "bad_body"
        try:
            signature = settings.sign_payload(
                payload, scope=f"operate-control:{crawl_id}")
            resp = await http_client.post(
                url, content=payload,
                headers=crawl_context.crawl_headers({
                    "Content-Type": "application/json",
                    "X-QEC-Signature": signature,
                    "X-QEC-Token": settings.explorer_token,
                }),
                timeout=settings.medic_oracle_timeout_s,
            )
            if resp.status_code == 200:
                data = resp.json()
                status = str(data.get("status") or "")
                action = str(data.get("action") or "")
                if telemetry is not None:
                    telemetry.note_usage(data)
                if status == _MEDIC_PROPOSED and action:
                    state["consecutive_failures"] = 0
                    _note_oracle_outcome("medic", _MEDIC_PROPOSED, started)
                    return {"action": action, "status": _MEDIC_PROPOSED}
                if status == _MEDIC_DISPLAY_ONLY:
                    state["consecutive_failures"] = 0
                    _note_oracle_outcome("medic", _MEDIC_DISPLAY_ONLY, started)
                    return {"action": "display_only", "status": _MEDIC_DISPLAY_ONLY}
            else:
                failure_reason = "http_error"
        except Exception as exc:
            failure_reason = (
                "timeout" if isinstance(exc, httpx.TimeoutException) else "transport")
            logger.warning("qec.explorer.medic_oracle_failed crawl_id=%s error=%s",
                           crawl_id, str(exc)[:200])
        _note_oracle_outcome("medic", _MEDIC_UNAVAILABLE, started,
                             failure_reason=failure_reason)
        state["consecutive_failures"] += 1
        if (state["consecutive_failures"] >= settings.medic_oracle_breaker_threshold
                and not state["circuit_open"]):
            state["circuit_open"] = True
            logger.warning(
                "qec.explorer.medic_oracle_circuit_open crawl_id=%s failures=%d",
                crawl_id, state["consecutive_failures"])
        return dict(unavailable)

    return medic


def _make_vision_oracle(
    http_client: httpx.AsyncClient, tenant_id: str, crawl_id: str,
    telemetry: Optional[_CrawlTelemetry] = None,
    budget: Optional[Any] = None,
):
    """Return an async callable the crawler invokes on a DOM-opaque page (when
    ``perception.should_perceive`` is true): POST the page SCREENSHOT to
    qe-central's ``/internal/perceive-controls`` (U2), which asks the vision LLM to
    enumerate the interactive controls + displayed outcome values it can see.

    Contract: ``perceive(screenshot_b64, page_context) -> {"controls": [...],
    "displayed_values": [...]}``. Never raises; empties on any failure. The
    server side enforces the vision flag, so a disabled tenant gets empties here.

    M3.1 / T-VIS-03 — THE CAP, TIMEOUT AND BREAKER ARE VISION OWNED.
    ===============================================================
    This callable used to spend ``settings.medic_oracle_max_calls``,
    ``medic_oracle_timeout_s`` and ``medic_oracle_breaker_threshold``, i.e. the
    DOM interaction ladder repair budget. A canvas application that burned ten
    perceive calls therefore took ten repair calls away from the ladder, and a
    vision provider outage opened the breaker the ladder depended on — neither
    of which is visible in any number a crawl reports.

    It now spends a :class:`app.vision_gate.VisionBudget`, which additionally
    carries the T-VIS-04 double gate: ``try_spend`` refuses every call when the
    gate is shut, so this callable cannot make a request the gate did not
    authorise even if it is wired by mistake.
    """
    from . import vision_gate as _vg

    vbudget = budget if budget is not None else _vg.closed_budget()
    empty = {"controls": [], "displayed_values": []}

    async def perceive(screenshot_b64: str, page_context: dict[str, Any]) -> dict[str, Any]:
        if not screenshot_b64:
            return dict(empty)
        allowed, why = vbudget.try_spend()
        if not allowed:
            metrics.record_oracle_call(oracle="vision", outcome=why,
                                       failure_reason=why)
            logger.info("qec.explorer.vision_oracle_refused crawl_id=%s reason=%s",
                        crawl_id, why)
            return dict(empty)
        if telemetry is not None:
            telemetry.note_call()
        started = time.monotonic()
        crawl_context.emit(crawl_context.EV_ORACLE_CALLED, oracle="vision")
        # M3.1 / T-VIS-05 — the REDACTION RECEIPT is lifted out of the page
        # context and sent as a first-class field, because qe-central ENFORCES it
        # rather than trusting it: the receipt names a sha256, the server hashes
        # the bytes it was actually handed, and a mismatch is a refusal. A claim
        # buried in a free-form context dict is a checkbox; a claim bound to the
        # image is a control.
        ctx = dict(page_context or {})
        receipt = ctx.pop("pixel_redaction", None)
        body = {"crawl_id": crawl_id,      # T-SEC-07 server-verifiable identity
                "tenant_id": tenant_id, "screenshot_b64": screenshot_b64,
                "pixel_redaction": receipt or {},
                "page_context": ctx}
        payload = json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
        url = settings.callback_url.rstrip("/") + "/internal/perceive-controls"
        failure_reason = "bad_body"
        try:
            signature = settings.sign_payload(
                payload, scope=f"perceive-controls:{crawl_id}")
            resp = await http_client.post(
                url, content=payload,
                headers=crawl_context.crawl_headers({
                    "Content-Type": "application/json",
                    "X-QEC-Signature": signature,
                    "X-QEC-Token": settings.explorer_token,
                }),
                timeout=vbudget.timeout_s,
            )
            if resp.status_code == 200:
                data = resp.json()
                controls = data.get("controls")
                if telemetry is not None:
                    telemetry.note_usage(data)
                if isinstance(controls, list):
                    vbudget.note_success()
                    values = data.get("displayed_values")
                    _note_oracle_outcome(
                        "vision", "perceived" if controls else "empty", started)
                    return {"controls": controls,
                            "displayed_values": values if isinstance(values, list) else []}
            else:
                failure_reason = "http_error"
        except Exception as exc:
            failure_reason = (
                "timeout" if isinstance(exc, httpx.TimeoutException) else "transport")
            logger.warning("qec.explorer.vision_oracle_failed crawl_id=%s error=%s",
                           crawl_id, str(exc)[:200])
        _note_oracle_outcome("vision", "unavailable", started,
                             failure_reason=failure_reason)
        # The breaker lives on the budget now, so vision failing can never open a
        # circuit anything else is relying on.
        vbudget.note_failure()
        return dict(empty)

    return perceive


def _make_vision_medic_oracle(
    http_client: httpx.AsyncClient, tenant_id: str, crawl_id: str,
    telemetry: Optional[_CrawlTelemetry] = None,
    budget: Optional[Any] = None,
):
    """A28 / R5 — the caller ``/internal/vision-operate`` never had.

    THE DEFECT THIS CLOSES
    ======================
    ``/internal/vision-operate`` was built, authenticated, server-side
    flag-gated, tested, deployed and LIVE — and no code in this engine ever
    called it. ``OracleGateway.operate`` raises ``NotImplementedError``; the
    interaction ladder's medic rung calls ``/internal/operate-control`` (the
    TEXT medic) and stops there; the only vision the crawler ever performed went
    through ``/internal/perceive-controls``, which answers a different question
    ("what controls are on this screen?") from this one ("where inside THIS
    control do I click?").

    A live authenticated endpoint with no consumer is not free. It is billable
    surface area that no test exercises end to end, and its existence implies a
    capability the product does not actually have: on a DOM-opaque control the
    ladder simply ran out and the control became named residue.

    THE SHAPE, AND WHY IT MATCHES ITS SIBLINGS
    ==========================================
    Same HMAC signature + fleet token as ``perceive-controls`` and
    ``pick-advance``; same :class:`app.vision_gate.VisionBudget`, so the double
    gate (T-VIS-04) refuses every call when vision is shut even if this callable
    is wired by mistake, and a vision outage cannot open the breaker the DOM
    ladder depends on; same never-raises contract — every failure returns the
    documented ``unavailable`` status, which the ladder already handles by
    falling through to residue.

    The REDACTION RECEIPT is passed through as a first-class field rather than
    buried in the page context, because qe-central ENFORCES it against the bytes
    it was handed (``platform_api._assert_image_egress_clean``). A receipt that
    does not match the image is a refusal at the wire, not a warning.
    """
    from . import vision_gate as _vg

    vbudget = budget if budget is not None else _vg.closed_budget()
    unavailable = {"action": "", "status": "unavailable",
                   "click_x": 0, "click_y": 0, "reason": ""}

    async def vision_operate(*, screenshot_b64: str, control: dict[str, Any],
                             kind: str, bbox: dict[str, Any],
                             ladder_tried: list, page_context: dict[str, Any],
                             redaction: dict[str, Any]) -> dict[str, Any]:
        if not screenshot_b64:
            return dict(unavailable)
        allowed, why = vbudget.try_spend()
        if not allowed:
            metrics.record_oracle_call(oracle="vision_medic", outcome=why,
                                       failure_reason=why)
            logger.info(
                "qec.explorer.vision_medic_refused crawl_id=%s reason=%s",
                crawl_id, why)
            return dict(unavailable)
        if telemetry is not None:
            telemetry.note_call()
        started = time.monotonic()
        crawl_context.emit(crawl_context.EV_ORACLE_CALLED, oracle="vision_medic")
        body = {"crawl_id": crawl_id, "tenant_id": tenant_id,
                "screenshot_b64": screenshot_b64,
                "pixel_redaction": redaction or {},
                "control": {**{k: v for k, v in (control or {}).items()
                               if k in ("name", "role", "selector", "tag",
                                        "frame_selector", "kind")},
                            "kind": kind, "bbox": bbox},
                "ladder_tried": ladder_tried or [],
                "page_context": page_context or {}}
        payload = json.dumps(body, sort_keys=True,
                             separators=(",", ":")).encode("utf-8")
        url = settings.callback_url.rstrip("/") + "/internal/vision-operate"
        failure_reason = "bad_body"
        try:
            signature = settings.sign_payload(
                payload, scope=f"vision-operate:{crawl_id}")
            resp = await http_client.post(
                url, content=payload,
                headers=crawl_context.crawl_headers({
                    "Content-Type": "application/json",
                    "X-QEC-Signature": signature,
                    "X-QEC-Token": settings.explorer_token,
                }),
                timeout=vbudget.timeout_s,
            )
            if resp.status_code == 200:
                data = resp.json()
                if telemetry is not None:
                    telemetry.note_usage(data)
                status = str(data.get("status") or "")
                if status in ("proposed", "display_only", "unavailable"):
                    vbudget.note_success()
                    _note_oracle_outcome("vision_medic", status, started)
                    return {"action": str(data.get("action") or ""),
                            "status": status,
                            "click_x": data.get("click_x") or 0,
                            "click_y": data.get("click_y") or 0,
                            "reason": str(data.get("reason") or "")}
            else:
                failure_reason = "http_error"
        except Exception as exc:
            failure_reason = (
                "timeout" if isinstance(exc, httpx.TimeoutException) else "transport")
            logger.warning(
                "qec.explorer.vision_medic_failed crawl_id=%s error=%s",
                crawl_id, str(exc)[:200])
        _note_oracle_outcome("vision_medic", "unavailable", started,
                             failure_reason=failure_reason)
        vbudget.note_failure()
        return dict(unavailable)

    return vision_operate


# ─── Job runner (owns the Playwright lifecycle) ──────────────────────────────


async def _run_job(
    *, req: ExploreRequest, budget: Budget, answer_key: AnswerKey,
    credentials: Optional[Credentials], guard_ctx: GuardContext,
    config_fingerprint: str, pack: RefusePack, jobs: JobManager,
) -> None:
    """Build the browser + crawler, run the crawl, fire the completion callback.

    A single ``try/finally`` guarantees the browser is torn down and the
    single-flight slot released even on failure — a crashed crawl never wedges
    the container.  The SAME ``finally`` emits the terminal telemetry, so the
    metrics are structurally incapable of being success-only: a crawl that threw
    during browser launch, was cancelled, or timed out on its wall budget all
    leave through the same instrumented exit.
    """
    from playwright.async_api import async_playwright  # lazy: browser-only dep

    job: Optional[_Job] = None
    summary: Optional[CrawlSummary] = None
    error = ""
    # Bind the crawl identity ONCE, at the top of the task. Every log line and
    # lifecycle event emitted from here on — including ones raised deep inside
    # the crawler or an oracle callback — carries it without being threaded
    # through call signatures, so correlation is propagated, never reconstructed.
    crawl_context.bind_crawl(crawl_id=req.crawl_id, tenant_id=req.tenant_id)
    # M0.5 T-SEC-05 — the AUTHORITATIVE observe-only decision, made HERE, in the
    # crawl execution path, from the attestation this process was handed. It can
    # only ever RAISE the caller's floor: a dispatch that says False against a
    # non-disposable environment is overruled before a browser exists.
    observe_only = resolve_observe_only(req, guard_ctx.attestation)
    if observe_only and guard_ctx.walk_authorization is not None:
        # BELT AND BRACES. A verified proof must name a disposable environment,
        # so this pairing should be impossible — which is exactly why it is
        # checked rather than assumed. If the two independent gates ever
        # disagree, the more restrictive one wins and says so.
        logger.error(
            "qec.explorer.walk_persistence_revoked_by_posture crawl_id=%s — "
            "observe-only and an attested proof disagree; refusing walk mutation",
            req.crawl_id)
        guard_ctx.walk_authorization = None
        guard_ctx.walk_denied_reason = "observe_only_posture"
    if observe_only and not req.observe_only:
        logger.warning(
            "qec.explorer.observe_only_forced crawl_id=%s env_kind=%r — "
            "mutation (fill/submit/advance) is disabled for a non-disposable "
            "environment", req.crawl_id, req.env_kind or "(unstated)")
    # ── M3.1 / T-VIS-04 · THE VISION DOUBLE GATE, ON THE EXECUTION PATH ──
    # qe-central already refuses to SET ``vision_enabled`` unless the env flag
    # and the tenant flag are both on. That is a gate on the OPERATOR intent and
    # it says nothing about the TARGET: a tenant with vision switched on could
    # point a crawl at their live production portal and have full-page
    # screenshots of real customers travel to a third-party model, because no
    # gate on the vision path had ever consulted the environment attestation.
    #
    # Decided HERE for the same reason ``observe_only`` is (T-SEC-05): a
    # permission resolved in the caller is a permission the caller can be wrong
    # about, and this process holds the attestation it was actually handed. The
    # dispatch flag can only ever be NARROWED by this — never widened.
    vision_gate_decision = vision_gate.gate_for_crawl(
        tenant_enabled=bool(req.vision_enabled),
        attestation=guard_ctx.attestation,
        walk_authorization=guard_ctx.walk_authorization,
    )
    vision_budget = vision_gate.VisionBudget(
        gate=vision_gate_decision,
        max_calls=settings.vision_oracle_max_calls,
        timeout_s=settings.vision_oracle_timeout_s,
        breaker_threshold=settings.vision_oracle_breaker_threshold,
    )
    if req.vision_enabled and not vision_gate_decision.enabled:
        logger.warning(
            "qec.explorer.vision_refused crawl_id=%s reason=%s — the tenant "
            "enabled vision but this target is not attested; no screenshot will "
            "leave this container", req.crawl_id, vision_gate_decision.reason)
    elif vision_gate_decision.enabled:
        logger.info(
            "qec.explorer.vision_enabled crawl_id=%s rung=%s cap=%d timeout=%.1fs",
            req.crawl_id, vision_gate_decision.rung,
            vision_budget.max_calls, vision_budget.timeout_s)
    telemetry = _CrawlTelemetry(req.crawl_id)
    started_at = time.monotonic()
    metrics.record_crawl_started(crawl_mode=req.crawl_mode,
                                 traversal=req.traversal)
    crawl_context.emit(crawl_context.EV_CRAWL_STARTED,
                       crawl_mode=req.crawl_mode, traversal=req.traversal,
                       observe_only=observe_only,
                       # The RESOLVED decision, not the dispatch flag: an event
                       # that says "vision on" for a crawl that made no vision
                       # call is the shape of telemetry nobody can act on.
                       vision_enabled=vision_gate_decision.enabled,
                       vision_gate_reason=vision_gate_decision.reason,
                       vision_attestation_rung=vision_gate_decision.rung,
                       max_depth_budget=budget.max_depth,
                       max_states_budget=budget.max_states)
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
            # M1.5 / T-ND-03 — the browser layer owns what a context must be
            # configured with (``service_workers``, ``ignore_https_errors`` and
            # now ``accept_downloads``); this is a read of that declaration, not
            # a second copy of it. Without ``accept_downloads`` a download is
            # cancelled at the browser edge and no listener can ever see it.
            _ctx_kwargs: dict[str, Any] = dict(context_defaults())
            # sessionStorage is NOT part of Playwright's storageState, so a captured
            # session carries it under our own namespaced key. It must be stripped
            # before the state reaches Playwright (an unknown key is a schema error)
            # and replayed through an init script instead — see below.
            _session_storage: list = []
            if isinstance(req.session, dict):
                _session = {k: v for k, v in req.session.items()
                            if k != "__nx_session_storage"}
                _raw_ss = req.session.get("__nx_session_storage")
                _session_storage = list(_raw_ss) if isinstance(_raw_ss, list) else []
                # Substance rule — mirrors nexus_sdk.session.session_has_substance
                # (the quarantined explorer deliberately does NOT install the SDK,
                # so this stays vendored). NOTE it runs on POST-split data:
                # __nx_session_storage has already been lifted into _session_storage,
                # so the equivalent third term is `or _session_storage`, NOT a lookup
                # on _session. An app that keeps its whole sign-in in sessionStorage
                # has no cookies and no origins — dropping it here dropped exactly the
                # sessions this exists to carry.
                if _session.get("cookies") or _session.get("origins") or _session_storage:
                    _ctx_kwargs["storage_state"] = _session
                    logger.info("qec.explorer.session_injected crawl_id=%s session_storage_origins=%d",
                                req.crawl_id, len(_session_storage))
            # Multi-env crawl bindings (empty ⇒ byte-identical to today).
            if req.extra_http_headers:
                _ctx_kwargs["extra_http_headers"] = dict(req.extra_http_headers)
            if isinstance(req.http_credentials, dict) and req.http_credentials.get("username"):
                _ctx_kwargs["http_credentials"] = {
                    "username": req.http_credentials.get("username", ""),
                    "password": req.http_credentials.get("password", ""),
                }
            context = await browser.new_context(**_ctx_kwargs)
            # M3.2 / T-FR-02 — THE CAPTURE HOOKS GO ON FIRST, before any page
            # exists.  A closed shadow root can only be observed at the moment
            # `attachShadow` creates it, so the hook has to be in place before
            # the application's first script runs; `add_init_script` on the
            # CONTEXT is the only placement that guarantees that for every page
            # and every frame the crawl opens.  Moving this below `new_page()`,
            # or into the port, would silently restore the blind spot.
            await install_capture_hooks(context)
            context.set_default_timeout(_ACTION_TIMEOUT_MS)
            # Replay sessionStorage, for EVERY origin the recorded login walked
            # through. Playwright restores cookies and localStorage from a state but
            # has no equivalent for sessionStorage, so it is written by an init
            # script that runs before any page script. ONE script holding a map,
            # rather than one per origin: a federated login can touch several, and N
            # scripts would each run on every navigation of every page.
            _ss_map = [
                {"origin": str(e.get("origin") or ""), "entries": e.get("entries")}
                for e in _session_storage
                if isinstance(e, dict) and str(e.get("origin") or "")
                and isinstance(e.get("entries"), dict) and e.get("entries")
            ]
            # Bound what we inject: this is attacker-influenced only in the sense
            # that the app under test wrote it, but a multi-megabyte init script
            # would be paid on every navigation of the crawl.
            if _ss_map and len(json.dumps(_ss_map)) <= 512 * 1024:
                await context.add_init_script(
                    """((byOrigin) => {
                        try {
                            const here = window.location.origin;
                            for (const rec of byOrigin) {
                                if (rec.origin !== here) continue;
                                for (const [k, v] of Object.entries(rec.entries)) {
                                    // SEED ONLY, never overwrite. This runs before
                                    // EVERY navigation and sessionStorage survives
                                    // them, so re-setting would clobber whatever the
                                    // app wrote since — a rotated token replaced by
                                    // the stale recorded one, and the session dies
                                    // mid-crawl, silently. A key the app already
                                    // holds is the app's business.
                                    if (sessionStorage.getItem(k) === null) {
                                        sessionStorage.setItem(k, v);
                                    }
                                }
                            }
                        } catch (e) { /* storage blocked/partitioned — carry on */ }
                    })(%s)""" % json.dumps(_ss_map),
                )
            elif _ss_map:
                logger.warning(
                    "qec.explorer.session_storage_too_large crawl_id=%s origins=%d — "
                    "not replayed; the crawl may run signed out",
                    req.crawl_id, len(_ss_map))
            if req.cookies:  # routing cookies (Gloo/canary) — cookies are a method call
                # FAIL-CLOSED: if the env routing cookie can't be set, the crawl must
                # NOT proceed cookieless — that would land on the DEFAULT env (often
                # prod) and green-wash a wrong-env baseline every later run rebinds.
                # Abort honestly instead (the _run_job try/finally records a failure).
                await context.add_cookies(list(req.cookies))
                logger.info("qec.explorer.env_cookies_set crawl_id=%s n=%d",
                            req.crawl_id, len(req.cookies))
            page = await context.new_page()
            # A JOURNEY-COMPLETION crawl — an attested non-prod environment, or an
            # explicit e2e request. The agent-backed oracles (advance + medic) are
            # what let the crawl identify a forward control whose label no regex
            # can anticipate, which is the difference between cataloguing a whole
            # journey and cataloguing its first page. Mirrors Crawler._full_traversal.
            full_traversal = (
                str(req.traversal or "").strip().lower() == TRAVERSAL_FULL
                or req.crawl_mode == "e2e"
            )
            medic = (
                _make_medic_oracle(app.state.http, req.tenant_id, req.crawl_id,
                                   telemetry)
                if full_traversal else None
            )
            # ORACLE PARTICIPATION is decided by what was WIRED, recorded before
            # a single consultation happens. Reading it off the oracles after the
            # crawl could not tell "no oracle was ever wired" (legitimate) from
            # "one was wired and never answered" (the silent failure) — which is
            # the whole distinction the no-oracle signal exists to draw.
            telemetry.oracle_configured = bool(
                full_traversal or vision_gate_decision.enabled)
            port = PlaywrightBrowserPort(
                page, context, proven_mechanics=req.proven_mechanics,
                medic_oracle=medic,
                # A28 — the R5 vision medic, gated by the SAME resolved vision
                # decision as the perceiver. Not a second switch: if vision is
                # off for this crawl the oracle is None and the ladder's last
                # rung simply does not exist, which is the pre-A28 behaviour.
                vision_medic_oracle=(
                    _make_vision_medic_oracle(app.state.http, req.tenant_id,
                                              req.crawl_id, telemetry,
                                              budget=vision_budget)
                    if vision_gate_decision.enabled else None),
                # M1.5 / T-ND-03 — captured downloads are staged beside this
                # crawl's manifest on the shared volume, so the artifact travels
                # with the record that references it.
                artifact_dir=str(emit.artifact_dir(settings.work_dir, req.crawl_id)))

            crawler = Crawler(
                port,
                crawl_id=req.crawl_id, tenant_id=req.tenant_id,
                target_url=req.target_url, work_dir=settings.work_dir,
                refuse_pack=pack, budget=budget, explorer_version=EXPLORER_VERSION,
                guard_version=EXPLORER_VERSION, refuse_pack_version=pack.version,
                config_fingerprint=config_fingerprint, guard_context=guard_ctx,
                answer_key=answer_key, credentials=credentials,
                # A storageState was injected into the context above. It arrives
                # UNVERIFIED — the AUTH phase proves it still holds, so a session
                # that has since expired can never pass as an authenticated crawl.
                session_injected=bool(req.session),
                allowed_hosts=req.allowed_hosts, max_relogins=settings.max_relogins,
                submit_approvals=req.submit_approvals,
                boundary_approvals=req.boundary_approvals,
                wizard_enabled=settings.wizard_enabled,
                plan=req.plan,
                scope_path_prefixes=req.scope_path_prefixes,
                recalled_values=req.recalled_values,
                field_priors=req.field_priors,
                # Stable per tenant+app: the same client must present the same
                # fictional person every crawl, or a re-quote differs for a reason
                # that has nothing to do with the application.
                identity_seed=req.identity_seed or f"{req.tenant_id}::{req.target_url}",
                data_mode=req.data_mode,
                crawl_mode=req.crawl_mode,
                traversal=req.traversal,
                advance_oracle=(
                    _make_advance_oracle(app.state.http, req.tenant_id,
                                         req.crawl_id, telemetry)
                    if full_traversal else None
                ),
                # U2 vision Perceiver — only when the tenant has vision enabled
                # (qe-central's double-gate). Default OFF → None → the walk hook is a
                # no-op, and DOM-opaque pages are named but not perceived.
                # U2 vision Perceiver. Built ONLY when the T-VIS-04 double gate
                # returned enabled — and even then every call goes through
                # ``vision_budget``, which is the single door: cap, timeout,
                # breaker and gate are one object, so there is no second path a
                # future caller could take around them.
                vision_oracle=(
                    _make_vision_oracle(app.state.http, req.tenant_id,
                                        req.crawl_id, telemetry,
                                        budget=vision_budget)
                    if vision_gate_decision.enabled else None
                ),
                vision_budget=vision_budget,
                vision_max_actions_per_state=settings.vision_max_actions_per_state,
                choice_overrides=req.choice_overrides,
                e2e_wizard_steps=settings.e2e_wizard_steps,
                e2e_wizard_advances=settings.e2e_wizard_advances,
                # NOT req.observe_only — the resolved decision (T-SEC-05).
                observe_only=observe_only,
                # M1.7 — continue an existing crawl rather than start a new one
                # (T-GW-03), and consume what earlier crawls proved (T-GW-04).
                resume=req.resume,
                known_rules=req.known_rules,
            )
            job = _Job(crawler)
            jobs.activate(job)

            # THE fail-closed net — wired now that the crawler (guard + emitter)
            # exists.  Squid enforces host; this enforces method + phase.
            await context.route("**/*", _make_route_handler(crawler))
            # M1.3 · the audit's second half: response statuses for permitted
            # walk mutations. Registered only alongside the guard, so it can
            # never observe a request the guard did not authorise.
            context.on("response", _make_response_listener(crawler))

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
        # TERMINAL TELEMETRY — in the finally, so no crawl outcome can skip it.
        _record_crawl_terminal(
            req=req, summary=summary, error=error, job=job,
            telemetry=telemetry,
            duration_seconds=max(0.0, time.monotonic() - started_at),
        )

    await _fire_callback(req, summary, error, telemetry)


def _record_crawl_terminal(
    *, req: ExploreRequest, summary: Optional[CrawlSummary], error: str,
    job: Optional[_Job], telemetry: _CrawlTelemetry, duration_seconds: float,
) -> None:
    """Emit the metrics + structured event for a crawl reaching a terminal state.

    Reached from ``_run_job``'s ``finally``, so it runs for a completed crawl, a
    cancelled one, a wall-budget timeout, an exception inside the crawl loop AND
    a failure before the crawler ever existed.  That last case is why the
    fallback stop reason is ``"error"`` rather than ``""``: a crawl that died
    during browser launch has no summary, and letting it fall through
    uninstrumented is how a container that fails every launch looks identical to
    one that is merely idle.
    """
    stop_reason = summary.stop_reason if summary else "error"
    failed = bool(error) or summary is None
    # Classify the FAILURE, never the message. A crawl that never reached the
    # crawler failed at setup; one that has a summary failed inside the loop.
    failure_kind = ""
    if failed:
        failure_kind = "crawl_exception" if job is not None else "browser_launch"

    # Depth from the SUMMARY first — it is the authoritative record of the run.
    # The live crawler is the fallback for the paths that produce no summary (an
    # exception mid-loop), which is precisely when the depth reached is most
    # worth knowing.
    max_depth = None
    if summary is not None:
        max_depth = getattr(summary, "max_depth_reached", None)
    if max_depth is None and job is not None:
        max_depth = getattr(job.crawler, "max_depth_reached", None)

    metrics.record_crawl_terminal(
        stop_reason=stop_reason,
        duration_seconds=duration_seconds,
        max_depth=max_depth,
        states=summary.states if summary else 0,
        guard_blocks=summary.guard_blocks if summary else 0,
        oracle_configured=telemetry.oracle_configured,
        oracle_calls=telemetry.oracle_calls,
        failed=failed,
        failure_kind=failure_kind,
    )

    event = crawl_context.EV_CRAWL_FAILED if failed else crawl_context.EV_CRAWL_TERMINAL
    if stop_reason == "cancelled":
        event = crawl_context.EV_CRAWL_CANCELLED
    crawl_context.emit(
        event,
        stop_reason=stop_reason,
        terminal_reason=metrics.terminal_reason_for(stop_reason),
        oracle_state=metrics.oracle_state_for(
            oracle_configured=telemetry.oracle_configured,
            oracle_calls=telemetry.oracle_calls),
        oracle_calls=telemetry.oracle_calls,
        oracle_configured=telemetry.oracle_configured,
        duration_ms=int(duration_seconds * 1000),
        max_depth=max_depth if max_depth is not None else -1,
        states=summary.states if summary else 0,
        actions=summary.actions if summary else 0,
        guard_blocks=summary.guard_blocks if summary else 0,
        failure_kind=failure_kind,
        # The exception MESSAGE belongs in the log, never in a label — this is
        # the high-cardinality half of the Prometheus/log split.
        error=error[:_MAX_LOGGED_ERROR_LEN] if error else "",
    )
    # The per-crawl spend record — the answer to "which crawl spent these
    # tokens?", which Prometheus deliberately cannot give.
    telemetry.tokens.emit_summary()


#: Errors are diagnostic text, kept short enough that a pathological exception
#: cannot dominate the log stream.
_MAX_LOGGED_ERROR_LEN = 300


#: url -> audit request id, for the one hop between a permitted walk mutation
#: being released and its response arriving. Bounded by the per-step mutation
#: budget, so it cannot grow: at most a handful of entries exist at any moment.
_pending_walk_responses: dict[str, str] = {}


def _make_response_listener(crawler: Crawler):
    """Record the response status of a permitted walk mutation (T-WP-03).

    A SEPARATE, linked ledger entry — the authorisation record is never edited,
    because an append-only ledger that rewrites entries is not append-only."""
    async def on_response(response: Any) -> None:
        try:
            request_id = _pending_walk_responses.pop(response.url, "")
            if not request_id:
                return
            auth = getattr(crawler.guard, "walk_authorization", None)
            if auth is not None:
                auth.note_response(request_id, int(response.status))
        except Exception:  # pragma: no cover — evidence, never a crawl-stopper
            logger.exception("qec.explorer.walk_response_audit_failed")

    return on_response


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
            # M1.3 · link the observed response back to the audit entry this
            # mutation was authorised under. The ledger entry itself was already
            # written BEFORE this line — evidence precedes the request, never
            # follows it — so a crash here loses the status, never the record.
            request_id = ""
            if decision.rule_id == GuardRule.WALK_MUTATION_OK:
                request_id = crawler.guard.last_walk_request_id
            await route.continue_()
            if request_id:
                _pending_walk_responses[request.url] = request_id
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
                         error: str,
                         telemetry: Optional["_CrawlTelemetry"] = None) -> None:
    """Record this crawl's completion DURABLY, then deliver it (M1.7 / T-GW-02).

    The body carries the manifest path (on the shared volume), the adjudicated
    verdict, and the in-memory ``storage_state`` (the ONLY channel a captured
    session leaves the container).  Signed with the shared secret so qe-central
    can trust the caller.

    NO LONGER BEST-EFFORT, and the docstring that used to say so was the bug in
    miniature: it claimed "the durable manifest is the source of truth
    regardless", which was aspirational — the crawl manifest really does hold the
    evidence, but nothing ever READ it unless a callback arrived to point at it.
    One lost POST orphaned a finished crawl permanently.  The completion record
    is now written and fsynced BEFORE the first attempt, so the fact that this
    crawl reached a terminal state survives the delivery failing, this process
    dying, and the container being replaced.
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
        # ── M1.7 · THE ADJUDICATED VERDICT TRAVELS WITH THE CLAIM ───────────
        # ``stop_reason`` says WHAT happened; ``disposition`` says whether the
        # crawl may be BELIEVED, and it was decided against evidence qe-central
        # cannot see (the unrecovered-inventory count, the resumed-state count,
        # whether a completion claim was refused). Sending the verdict rather
        # than leaving qe-central to re-derive it from a string is what keeps one
        # judgement in one place: two services each mapping stop reasons onto a
        # belief is two mappings that drift, and a drift in THIS mapping is a
        # failed crawl reading as a completed one.
        #
        # A crawl with NO summary never reached the adjudicator at all — it died
        # before or during browser launch — so it is failed, explicitly. That
        # default is the fail-closed one: an absent verdict must never be
        # optimistic.
        "disposition": summary.disposition if summary else "failed",
        "evidence": summary.evidence if summary else {},
        "downgraded": bool(summary.downgraded) if summary else False,
    }
    # M0.6 — the per-crawl telemetry record travels back with the callback so
    # qe-central holds the exact spend and oracle participation for THIS crawl.
    # This is the non-Prometheus half of the split: the crawl id belongs here,
    # in a record keyed by it, not on a time-series label.
    if telemetry is not None:
        body["telemetry"] = {
            "oracle_configured": telemetry.oracle_configured,
            "oracle_calls": telemetry.oracle_calls,
            "oracle_state": metrics.oracle_state_for(
                oracle_configured=telemetry.oracle_configured,
                oracle_calls=telemetry.oracle_calls),
            "terminal_reason": metrics.terminal_reason_for(
                summary.stop_reason if summary else "error"),
            "tokens": telemetry.tokens.as_dict(),
        }
    # ── M1.7 / T-GW-02 · DURABLE FIRST, DELIVER SECOND ──────────────────────
    # The completion record is written and fsynced BEFORE the first POST is
    # attempted. That ordering is the whole recovery story: the FACT that this
    # crawl reached a terminal state now lives on the shared volume, and the POST
    # is only a notification of it. A dropped notification is then recoverable —
    # by this process's own sweeper, or by qe-central's reaper reading the same
    # file — instead of permanently orphaning a finished crawl.
    #
    # A failure to write it is NOT swallowed: with no durable record there is no
    # recovery path at all, and continuing as though there were is the green-wash
    # this milestone removes.
    try:
        completion_manifest.write_completion(settings.work_dir, req.crawl_id, body)
    except OSError as exc:
        logger.error(
            "qec.explorer.completion_record_failed crawl_id=%s error=%s — this "
            "crawl has NO durable completion record, so a dropped callback "
            "cannot be recovered", req.crawl_id, str(exc)[:200])

    # ── M3.3 / T-FL-03 · PUBLISH BEFORE YOU ANNOUNCE ─────────────────────
    # `{work_dir}/{crawl_id}` is a POD-LOCAL emptyDir in Kubernetes: invisible
    # to the qe-central pod that must ingest it, and destroyed when this pod is
    # replaced. Publishing here — after the durable local record, BEFORE the
    # callback — means the evidence is durable before anything is told the crawl
    # finished, so even a lost callback leaves a recoverable crawl.
    #
    # A no-op on the filesystem backend (single-node), so this cannot regress an
    # existing install. Never raises: a storage incident must not become a lost
    # crawl, so the outcome rides on the callback body where an operator sees it.
    publish = evidence_publisher.publish_crawl_evidence(
        settings.work_dir, req.crawl_id, req.tenant_id)
    if publish.get("published") or publish.get("error"):
        body["evidence_store"] = publish

    await _deliver_completion(req.crawl_id, body)


async def _deliver_completion(crawl_id: str, body: dict[str, Any], *,
                              max_attempts: int = completion_manifest.DEFAULT_MAX_ATTEMPTS,
                              ) -> bool:
    """POST a durable completion to qe-central, retried with backoff (T-GW-02).

    Returns True once qe-central has ACCEPTED it, and writes the acknowledgement
    that takes the crawl off the orphan list.

    RE-SIGNED PER ATTEMPT, and that is load-bearing. The v2 envelope carries a
    SINGLE-USE NONCE (T-SEC-06): re-POSTing one signed envelope is a replay and
    the receiver refuses it, so a retry loop that signed once would have been a
    retry loop that could only ever fail. Each attempt mints a fresh envelope
    over the same body.

    WHICH FAILURES ARE RETRIED. A transport error or a 5xx is retried — the
    receiver never saw it, or could not process it now. A 4xx is NOT: a bad
    signature, an unknown crawl or a malformed body will fail identically forever,
    and spending five attempts proving that delays the sweeper's honest report.
    A 2xx — including the endpoint telling us it already holds a terminal state
    for this crawl — is a successful delivery, because the crawl is landed either
    way; that is what makes duplicate delivery safe and idempotent.
    """
    payload = json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
    url = settings.callback_url.rstrip("/") + settings.callback_path(crawl_id)
    client: httpx.AsyncClient = app.state.http
    for attempt in range(1, max(1, int(max_attempts)) + 1):
        delay = completion_manifest.backoff_delay(attempt)
        if delay:
            await asyncio.sleep(delay)
        try:
            # Signing inside the guard: with no fleet secret configured this
            # raises, and an unsignable callback must be reported honestly rather
            # than propagate out of a job that has already finished its work.
            signature = settings.sign_payload(payload, scope=f"complete:{crawl_id}")
            resp = await client.post(
                url, content=payload,
                headers={"Content-Type": "application/json",
                         "X-QEC-Signature": signature,
                         "X-QEC-Token": settings.explorer_token},
            )
        except Exception as exc:
            completion_manifest.record_attempt(
                settings.work_dir, crawl_id, attempt=attempt, ok=False,
                error=str(exc)[:300])
            logger.warning(
                "qec.explorer.callback_attempt_failed crawl_id=%s attempt=%d/%d "
                "error=%s", crawl_id, attempt, max_attempts, str(exc)[:200])
            continue
        ok = 200 <= resp.status_code < 300
        completion_manifest.record_attempt(
            settings.work_dir, crawl_id, attempt=attempt, ok=ok,
            status=resp.status_code)
        if ok:
            completion_manifest.mark_delivered(
                settings.work_dir, crawl_id, status=resp.status_code)
            logger.info(
                "qec.explorer.callback_sent crawl_id=%s status=%d attempt=%d",
                crawl_id, resp.status_code, attempt)
            return True
        if 400 <= resp.status_code < 500:
            logger.error(
                "qec.explorer.callback_rejected crawl_id=%s status=%d — a client "
                "error will not resolve on retry; the durable completion record "
                "is left for the reaper to reconcile",
                crawl_id, resp.status_code)
            return False
        logger.warning(
            "qec.explorer.callback_attempt_failed crawl_id=%s attempt=%d/%d "
            "status=%d", crawl_id, attempt, max_attempts, resp.status_code)
    logger.error(
        "qec.explorer.callback_undelivered crawl_id=%s attempts=%d — the crawl is "
        "ORPHANED until the sweeper or the qe-central reaper reconciles its "
        "durable completion record", crawl_id, max_attempts)
    return False


async def _sweep_orphaned_completions() -> int:
    """Re-deliver every durable completion this volume holds un-acknowledged.

    THE SECOND LEG OF RECOVERY, and the one that survives the process dying. The
    in-line retry above only helps a crawl whose worker is still alive; a worker
    killed mid-delivery leaves a completion record with no ack and nobody to
    notice. This scan runs at STARTUP — so a restarted container immediately
    reconciles whatever its predecessor left behind — and on a slow timer for
    outages longer than the in-line backoff.

    Idempotent by construction: the receiving endpoint is a no-op on a crawl that
    already reached a terminal state, and it returns 2xx for that case, so a
    duplicate delivery ends with the ack written and the orphan cleared. Running
    this twice concurrently is therefore safe, if wasteful.

    Returns how many orphans were cleared.
    """
    try:
        pending = completion_manifest.pending_completions(settings.work_dir)
    except Exception as exc:                                # pragma: no cover
        logger.warning("qec.explorer.sweep_failed error=%s", str(exc)[:200])
        return 0
    if not pending:
        return 0
    logger.warning(
        "qec.explorer.sweep_found orphans=%d — completions that reached a "
        "terminal state and were never acknowledged", len(pending))
    cleared = 0
    for item in pending:
        if not completion_manifest.completion_body_is_sane(item.body):
            logger.error(
                "qec.explorer.sweep_unroutable crawl_id=%s — the durable "
                "completion is missing crawl_id/tenant_id/exploration_id and "
                "cannot be routed; left in place for an operator", item.crawl_id)
            continue
        if await _deliver_completion(item.crawl_id, item.body, max_attempts=2):
            cleared += 1
            logger.warning("qec.explorer.sweep_recovered crawl_id=%s prior_attempts=%d",
                           item.crawl_id, item.attempts)
    return cleared


async def _sweeper_loop() -> None:
    """The periodic orphan sweep. Self-gating on ``QEC_SWEEP_SECONDS`` > 0."""
    interval = _sweep_interval_seconds()
    if interval <= 0:
        logger.info("qec.explorer.sweeper_disabled — set QEC_SWEEP_SECONDS>0 to enable")
        return
    while True:
        try:
            await asyncio.sleep(interval)
            await _sweep_orphaned_completions()
        except asyncio.CancelledError:
            raise
        except Exception:                                   # pragma: no cover
            logger.warning("qec.explorer.sweeper_tick_failed", exc_info=True)


def _sweep_interval_seconds() -> float:
    """Seconds between orphan sweeps; <=0 disables the periodic loop.

    The STARTUP sweep runs regardless — a restart is the single most likely
    moment for an orphan to exist, and gating the recovery for that case behind
    an env var nobody set is how a recovery path stays theoretical.
    """
    try:
        return float(os.environ.get("QEC_SWEEP_SECONDS", "") or 300.0)
    except (TypeError, ValueError):
        return 300.0


def _summary_public(summary: CrawlSummary) -> dict[str, Any]:
    return {
        "crawl_id": summary.crawl_id, "stop_reason": summary.stop_reason,
        "states": summary.states, "actions": summary.actions,
        "screenshots": summary.screenshots, "guard_blocks": summary.guard_blocks,
        "manifest_path": summary.manifest_path, "detail": summary.detail,
        "storage_state_captured": summary.storage_state is not None,
        # How FAR the crawl got — the depth the operator asks for by name, and
        # the same number the depth histogram observes.
        "max_depth_reached": summary.max_depth_reached,
    }


#: The M1.3 envelope keys, carried INSIDE ``attestation`` alongside the legacy
#: operator statement.  ``guard.Attestation`` is ``extra="forbid"``, so they are
#: stripped before it parses — a dispatch that carries a provisioning proof must
#: not thereby break the SUBMIT tier that has nothing to do with it.
_WALK_ENVELOPE_KEYS = ("proof", "revocations")


def _attestation(payload: Optional[dict[str, Any]]) -> Optional[Attestation]:
    if not payload:
        return None
    legacy = {k: v for k, v in dict(payload).items() if k not in _WALK_ENVELOPE_KEYS}
    try:
        return Attestation.model_validate(legacy)
    except Exception as exc:
        logger.warning("qec.explorer.bad_attestation error=%s", str(exc)[:200])
        return None


def _walk_authorization(req: "ExploreRequest"):
    """``(WalkAuthorization | None, denied_reason)`` for this dispatch.

    M1.3 / T-WP-02.  The ONLY path by which walk mutation can ever be enabled.
    Everything it consults is either a configured PUBLIC key or a signature over
    claims; nothing the caller writes in the dispatch body is trusted, including
    ``req.env_kind`` and the legacy ``attestation.env_kind`` — a proof states its
    own environment kind inside the signed claims, and that is the only statement
    the verifier reads.

    Returns ``(None, reason)`` on every failure, and the Crawler treats ``None``
    exactly as it behaved before this feature existed.
    """
    verdict = verify_provisioning_proof(
        req.attestation,
        trust=settings.attestation_trust_store(),
        crawl_id=req.crawl_id,
        tenant_id=req.tenant_id,
        target_url=req.target_url,
    )
    if not verdict.authorized:
        # INFO, not ERROR: for the overwhelming majority of crawls (every
        # production one, forever) "no walk persistence" is the correct and
        # expected outcome, and logging it as a failure would train operators to
        # ignore the line that matters.
        logger.info(
            "qec.explorer.walk_persistence_denied crawl_id=%s reason=%s",
            req.crawl_id, verdict.reason)
        return None, verdict.reason
    auth = WalkAuthorization.from_verdict(
        verdict, workflow_id=req.crawl_id,
        window_ms=int(settings.walk_mutation_window_ms))
    logger.warning(
        "qec.explorer.walk_persistence_granted crawl_id=%s proof_id=%s env=%s "
        "budget=%d/step — bounded server-side mutation is ENABLED for this crawl",
        req.crawl_id, verdict.proof_id, verdict.environment_id,
        verdict.max_mutations_per_step)
    return auth, ""


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
