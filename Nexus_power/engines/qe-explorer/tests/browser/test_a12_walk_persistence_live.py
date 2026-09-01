"""A12 / T-WP-01 — WALK PERSISTENCE, PROVEN AGAINST A REAL APPLICATION.

WHY THIS FILE EXISTS, GIVEN T-WP-01 ALREADY HAD 20 TESTS
========================================================
``tests/test_gate1_twp01_execution.py`` proves the AUTHORISATION ALGEBRA: four
conditions, a budget, an audit chain, an origin binding. Every one of those
tests calls ``authorize_mutation`` directly and reads its boolean.
``tests/test_save_draft_wizard_e2e.py`` goes considerably further — the real
Crawler, the real GuardContext and a scripted application that will not serve
step 2 until a draft has really been persisted — and it is real prior art that
this module does not supersede.

Neither proves the thing A12 is actually accepted on — *"the save-draft
wizard successfully persists WALK state"* — because no request ever left a
browser and no server state ever changed. A subsystem that returns ``True`` and
a subsystem that lets a POST through to an application are different claims, and
only the second one is persistence.

The gap is concrete: the decision is made in ``guard_context.decide``, but it is
only ENFORCED by ``app.main._make_route_handler`` inside a Playwright
``context.route('**/*')``. Nothing had ever exercised that seam. This file runs
the production route handler against a real Chromium and a real HTTP server that
really stores a draft, and reads the state back **after a reload** — because a
``200`` proves a request was answered, and only a reload proves it was *kept*.

WHY THE APPLICATION IS DEFINED HERE AND NOT IN ``tests/browser/fixtures/``
=========================================================================
Fixture ``10-save-draft-wizard`` looks like the right target and is not: its
``Save Draft`` is ``<button type="button">`` with no handler and no ``<script>``
anywhere in the file. It cannot persist because it never issues a request — it
is a CAPTURE regression guard for the constraint block, and its own README says
its targeted defect is "None". The shared ``_harness.FixtureServer`` also
accepts POST only under the scripted ``/__net/`` namespace, which returns canned
statuses and stores nothing.

So this module ships its own tiny application with real server-side state. That
is the point: WALK persistence is only demonstrable against something that has
state to persist.

THE CHAIN UNDER TEST IS PRODUCTION CODE END TO END
==================================================
    _attest_kit.Issuer            (a real Ed25519 key, a real signature)
      -> attest.verify_provisioning_proof   (the red-teamed verifier)
      -> WalkAuthorization.from_verdict     (production)
      -> GuardContext(phase=WALK)           (production)
      -> app.main._make_route_handler       (production, the enforcement seam)
      -> Chromium context.route('**/*')     (real browser, real fetch)
      -> a real HTTP server that really stores the draft

Nothing is stubbed except the crawl's browser PORT (``None``), which the route
handler never touches.
"""
from __future__ import annotations

import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional

import pytest

from app.attest import ProofReplayGuard, verify_provisioning_proof
from app.config import Settings
from app.crawler import Budget, Crawler
from app.guard import load_refuse_pack
from app.guard_context import GuardContext, Phase
from app.main import _make_route_handler
from app.walk_persist import MutationAuditLog, WalkAuthorization

from _attest_kit import Issuer

TENANT = "tenant-a12"
CRAWL = "crawl-a12"

# --- the application: a wizard step that really keeps a draft ---------------

_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>A12 save-draft wizard</title></head>
<body>
  <h1>Coverage</h1>
  <form id="coverage-form">
    <label for="face-amount">Face amount</label>
    <input id="face-amount" name="face_amount" type="number" value="__FACE__">
    <label for="notes">Notes</label>
    <input id="notes" name="notes" type="text" value="">
    <button type="button" id="save-draft-btn">Save Draft</button>
    <button type="submit" id="continue-btn">Continue</button>
  </form>
  <!-- Rendered SERVER-SIDE from the stored draft. This is the element the
       tests read after a reload: it can only be non-empty if the POST reached
       the server AND the server kept it. -->
  <div id="persisted">__PERSISTED__</div>
  <div id="status">idle</div>
<script>
document.getElementById("save-draft-btn").addEventListener("click", function () {
  document.getElementById("status").textContent = "sending";
  fetch("/draft", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({
      face_amount: document.getElementById("face-amount").value,
      notes: document.getElementById("notes").value
    })
  }).then(function (r) {
    return r.ok ? "saved" : "http-" + r.status;
  }).catch(function () {
    // A guard-aborted request rejects the fetch promise. THIS is what a
    // refused WALK mutation looks like from inside the application.
    return "blocked";
  }).then(function (s) {
    document.getElementById("status").textContent = s;
  });
});
</script>
</body></html>
"""


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class _DraftApp:
    """An application with genuine server-side state."""

    def __init__(self) -> None:
        self.drafts: list = []
        self.post_count = 0
        self._lock = threading.Lock()
        self._port = _free_port()
        app = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a: Any) -> None:  # keep pytest output clean
                pass

            def _send(self, code: int, body: bytes, ctype: str) -> None:
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:  # noqa: N802
                if self.path.startswith("/draft"):
                    self._send(200, json.dumps(app.snapshot()).encode(),
                               "application/json")
                    return
                with app._lock:
                    latest = app.drafts[-1] if app.drafts else None
                page = (_PAGE
                        .replace("__FACE__", "250000")
                        .replace("__PERSISTED__",
                                 json.dumps(latest) if latest else ""))
                self._send(200, page.encode("utf-8"),
                           "text/html; charset=utf-8")

            def do_POST(self) -> None:  # noqa: N802
                if not self.path.startswith("/draft"):
                    self._send(404, b"{}", "application/json")
                    return
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n) if n else b"{}"
                try:
                    payload = json.loads(raw.decode("utf-8") or "{}")
                except Exception:
                    payload = {"unparsed": True}
                with app._lock:
                    app.post_count += 1
                    app.drafts.append(payload)
                self._send(200, b'{"saved":true}', "application/json")

        self._server = ThreadingHTTPServer(("127.0.0.1", self._port), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        daemon=True)

    def start(self) -> "_DraftApp":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    def snapshot(self) -> dict:
        with self._lock:
            return {"drafts": list(self.drafts), "post_count": self.post_count}

    def reset(self) -> None:
        with self._lock:
            self.drafts.clear()
            self.post_count = 0

    @property
    def origin(self) -> str:
        return "http://127.0.0.1:%d" % self._port

    @property
    def url(self) -> str:
        return self.origin + "/"


@pytest.fixture(scope="module")
def draft_app():
    app = _DraftApp().start()
    yield app
    app.stop()


# --- the production chain, assembled ----------------------------------------

def _authorization(origin: str, *, budget: int = 3) -> WalkAuthorization:
    """A REAL verdict from the REAL verifier over a REALLY-signed proof."""
    issuer = Issuer()
    verdict = verify_provisioning_proof(
        {"proof": issuer.proof(crawl_id=CRAWL, tenant_id=TENANT,
                               target_origin=origin,
                               max_walk_mutations_per_step=budget),
         "revocations": issuer.revocations()},
        trust=issuer.trust(max_mutations_per_step=budget),
        crawl_id=CRAWL, tenant_id=TENANT, target_url=origin,
        replay_guard=ProofReplayGuard())
    assert verdict.authorized, "could not attest: %s" % verdict.reason
    auth = WalkAuthorization.from_verdict(
        verdict, workflow_id=CRAWL, audit=MutationAuditLog())
    assert auth is not None
    return auth


def _crawler(tmp_path, app: _DraftApp,
             auth: Optional[WalkAuthorization]) -> Crawler:
    pack = load_refuse_pack(Settings().refuse_pack_path)
    guard = GuardContext(refuse_pack=pack, phase=Phase.WALK,
                         walk_authorization=auth)
    return Crawler(
        None, crawl_id=CRAWL, tenant_id=TENANT, target_url=app.url,
        work_dir=str(tmp_path), refuse_pack=pack,
        budget=Budget(rate_per_s=0, max_states=4),
        explorer_version="test/1.0", guard_version="test",
        refuse_pack_version=pack.version, config_fingerprint="fp",
        guard_context=guard)


def _arm(auth: WalkAuthorization, crawler: Crawler,
         control: str = "Save Draft") -> None:
    """Stand the walk on an authorised step with its window open.

    The clock MUST be the crawl's own monotonic clock, because that is the one
    the route handler passes to ``authorize_mutation``; mixing in wall-clock ms
    here would compare two different clock domains and the window would look
    closed for reasons that have nothing to do with the behaviour under test.
    """
    now = crawler.now_ms()
    auth.begin_step(journey_id="j-a12", step_index=1,
                    step_fingerprint="fp-a12", now_ms=now)
    auth.authorize_step(True)
    auth.open_window(control, now)


def _click_save(pw, app: _DraftApp, crawler: Optional[Crawler],
                *, clicks: int = 1) -> str:
    """Drive the real browser through the real route handler. Returns #status.

    ``crawler=None`` installs NO route handler at all. That is not a convenience
    — it is the falsification control for every ``"blocked"`` assertion in this
    file, which would otherwise be satisfied by any unrelated fetch rejection.
    """
    async def _run() -> str:
        ctx = await pw.fresh_context()
        try:
            if crawler is not None:
                await ctx.route("**/*", _make_route_handler(crawler))
            page = await ctx.new_page()
            await page.goto(app.url)
            for _ in range(clicks):
                await page.evaluate(
                    "document.getElementById('status').textContent = 'idle'")
                await page.click("#save-draft-btn")
                await page.wait_for_function(
                    "['idle','sending'].indexOf("
                    "document.getElementById('status').textContent) === -1",
                    timeout=15000)
            return await page.inner_text("#status")
        finally:
            await ctx.close()

    return pw.run(_run())


def _persisted_after_reload(pw, app: _DraftApp) -> str:
    """Re-fetch the page with NO guard in the way and read the server's state.

    Deliberately a separate, unguarded context: the question this answers is
    "what does the application hold now", which must not depend on the crawl's
    permission to ask.
    """
    async def _run() -> str:
        ctx = await pw.fresh_context()
        try:
            page = await ctx.new_page()
            await page.goto(app.url)
            return await page.inner_text("#persisted")
        finally:
            await ctx.close()

    return pw.run(_run())


# --- A12 acceptance ---------------------------------------------------------

def test_an_attested_walk_persists_the_saved_draft(pw, draft_app, tmp_path):
    """THE acceptance criterion: the save-draft wizard persists WALK state.

    Read back after a RELOAD, from a context with no guard on it, because a
    200 proves the request was answered and only the reload proves it was kept.
    """
    draft_app.reset()
    auth = _authorization(draft_app.origin)
    crawler = _crawler(tmp_path, draft_app, auth)
    _arm(auth, crawler)

    status = _click_save(pw, draft_app, crawler)

    assert status == "saved", (
        "the attested Save Draft did not reach the application (status=%r)"
        % status)
    assert draft_app.snapshot()["post_count"] == 1
    assert draft_app.snapshot()["drafts"][0]["face_amount"] == "250000"

    persisted = _persisted_after_reload(pw, draft_app)
    assert "250000" in persisted, (
        "the draft did not survive a reload — the POST was answered but the "
        "WALK state was not persisted (#persisted=%r)" % persisted)


def test_an_unattested_walk_cannot_persist_anything(pw, draft_app, tmp_path):
    """The control. Same page, same click, no provisioning proof.

    If this ever goes green alongside the test above, WALK persistence is not
    gated on attestation at all and A12's whole premise is gone.
    """
    draft_app.reset()
    crawler = _crawler(tmp_path, draft_app, None)   # <-- no authorization

    status = _click_save(pw, draft_app, crawler)

    assert status == "blocked", (
        "an UNATTESTED WALK mutation was not refused (status=%r)" % status)
    assert draft_app.snapshot() == {"drafts": [], "post_count": 0}, (
        "an unattested crawl changed the application's state")
    assert _persisted_after_reload(pw, draft_app) == ""


def test_the_guard_is_what_blocks_it_and_not_the_application(pw, draft_app):
    """FALSIFICATION CONTROL — without this the test above proves nothing.

    ``"blocked"`` is read from a rejected ``fetch`` promise, and a fetch rejects
    for many reasons that have nothing to do with attestation: a CORS refusal, a
    dead socket, a server 500, a typo in the URL. Any of those would make the
    unattested test pass while the WALK gate was wide open.

    So: the SAME page and the SAME click with NO route handler installed must
    reach the application. If this ever fails, the "blocked" above is being
    caused by something other than the guard and every conclusion drawn from it
    is void.
    """
    draft_app.reset()

    status = _click_save(pw, draft_app, None)      # no guard in the way at all

    assert status == "saved", (
        "with no route handler installed the POST still did not reach the "
        "application (status=%r) — so 'blocked' elsewhere in this file cannot "
        "be attributed to the WALK guard" % status)
    assert draft_app.snapshot()["post_count"] == 1


def test_the_mutation_is_audited_before_it_is_released(pw, draft_app, tmp_path):
    """Evidence precedes the request. The ledger names the proof it crossed on."""
    draft_app.reset()
    auth = _authorization(draft_app.origin)
    crawler = _crawler(tmp_path, draft_app, auth)
    _arm(auth, crawler)

    assert _click_save(pw, draft_app, crawler) == "saved"

    records = auth.audit.records
    assert len(records) == 1, "expected exactly one audit record, got %r" % records
    rec = records[0]
    assert rec["method"] == "POST"
    assert "/draft" in rec["endpoint"]
    assert rec["triggering_control"] == "Save Draft", (
        "the ledger must name the control the operator can recognise")

    # The verdict is embedded whole, so the record answers "on whose authority?"
    # without a join against anything that could have been edited since.
    approval = rec["approval"]
    assert approval["proof_id"] == auth.verdict.proof_id, (
        "the audit record does not name the proof the mutation was authorised by")
    assert approval["env_kind"] == "disposable", (
        "a mutation was recorded against an environment the platform did not "
        "certify disposable — A12 permits persistence nowhere else")
    assert approval["authorized"] is True

    # Evidence precedes the request: the chain is closed and verifies.
    from app.walk_persist import verify_audit_chain
    ok, why = verify_audit_chain(auth.audit.records)
    assert ok, "the audit chain does not verify: %s" % why


def test_a_proof_for_another_origin_does_not_persist_here(pw, draft_app, tmp_path):
    """A VALID, correctly-signed, unexpired proof — for a different environment.

    Every other negative in this file withholds the attestation. This one
    supplies a real one and changes only WHICH origin the platform certified, so
    it separates "a proof was presented" from "a proof for THIS environment was
    presented". Without this, a gate that merely checked for the presence of an
    authorization object would pass the whole file.
    """
    draft_app.reset()
    # Signed for a genuinely disposable environment — just not this one.
    auth = _authorization("https://some-other-disposable.example.test")
    crawler = _crawler(tmp_path, draft_app, auth)
    _arm(auth, crawler)

    status = _click_save(pw, draft_app, crawler)

    assert status == "blocked", (
        "a proof issued for another origin authorised a mutation here "
        "(status=%r) — the origin binding is not being enforced" % status)
    assert draft_app.snapshot()["post_count"] == 0
    assert auth.audit.records == [], (
        "a refused mutation was written to the audit ledger; the ledger must "
        "record what CROSSED, not what was attempted")


def test_the_per_step_budget_stops_the_second_save(pw, draft_app, tmp_path):
    """A budget of ONE means the application sees exactly one write."""
    draft_app.reset()
    auth = _authorization(draft_app.origin, budget=1)
    crawler = _crawler(tmp_path, draft_app, auth)
    _arm(auth, crawler)

    status = _click_save(pw, draft_app, crawler, clicks=2)

    assert status == "blocked", (
        "the second save in one step was not refused (status=%r)" % status)
    assert draft_app.snapshot()["post_count"] == 1, (
        "the per-step budget did not bound what reached the application")


def test_a_closed_window_refuses_even_under_a_valid_proof(pw, draft_app, tmp_path):
    """Attested, on an authorised step, but no control window open.

    This is the difference between "this environment may be mutated" and "this
    CLICK may mutate it", and it is the one an attestation alone must not buy.
    """
    draft_app.reset()
    auth = _authorization(draft_app.origin)
    crawler = _crawler(tmp_path, draft_app, auth)
    now = crawler.now_ms()
    auth.begin_step(journey_id="j-a12", step_index=1,
                    step_fingerprint="fp-a12", now_ms=now)
    auth.authorize_step(True)
    # deliberately NO open_window()

    status = _click_save(pw, draft_app, crawler)

    assert status == "blocked", "a closed window did not refuse (status=%r)" % status
    assert draft_app.snapshot()["post_count"] == 0
