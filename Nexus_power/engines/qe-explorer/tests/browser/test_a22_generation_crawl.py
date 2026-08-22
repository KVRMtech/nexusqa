"""GATE 3 / A22 — THE PRODUCER HALF: the journey a generated spec is built from
is DISCOVERED BY THE CRAWLER, not written by hand.

WHAT A22 CHANGES ABOUT M2.4
===========================
M2.4's proof already does the hard end of this: it compiles a journey into a
Playwright spec, EXECUTES it in a real browser against a real HTTP application,
and shows it turning red under two orthogonal seeded regressions. What it does
not do is discover the journey. ``m24_generation/crawl_evidence.py`` says so in
its own first paragraph:

    FIXTURE: the raw network events and the journey graph rows — i.e. what a
    crawl of the quote application WOULD have recorded.

So the pipeline has always been fed a hand-built account of a crawl that never
happened. Everything downstream of that account is production code and is
genuinely exercised; the account itself is the fixture A22 exists to replace.

This module runs the real thing: the production :class:`app.crawler.Crawler` and
:class:`app.main.PlaywrightBrowserPort`, in real Chromium, against the SAME
application the M2.4 proof executes its generated spec against — so the journey
that gets compiled and the application that gets tested are the same
application, discovered rather than described.

WHY THIS APPLICATION AND NOT A PROVING GROUND
=============================================
``proving-grounds/acme-life`` is the richer application and A21 uses it, but it
makes **zero** network calls — measured, ``grep -c 'fetch(' index.html`` is 0.
A22 requires the generated specification to carry NETWORK assertions, and an
application that never calls a backend cannot ground one.

``m24_generation/fixture_app.py`` is a two-page quote funnel with a real HTTP
backend: the entry page reads ``GET /api/config`` on load, the button POSTs
``/api/quote`` and renders the premium the backend returned. It also carries the
two seeded regressions the consumer half needs, and they are deliberately
orthogonal — ``NETWORK_SILENT`` renders the same premium from a constant so every
UI assertion still passes, and ``OUTCOME_DRIFT`` calls the API correctly and
returns a different number.

It is crawled here in BASELINE mode only. The regressions belong to the
consumer, which runs the compiled spec against them.

WHAT IS WRITTEN, AND WHY BOTH
=============================
``coverage.json``  — the coverage account, which the journey fold reads to build
                     the real journey graph rows (nodes, edges, traversals).
``manifest.jsonl`` — the manifest, which carries the per-visit ``network_calls``
                     the M2.5 endpoint inventory is built from. The inventory is
                     what grounds the spec's network assertions, and A23 has just
                     shown that reading it back off a manifest is a different code
                     path from reading it in-process.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

import pytest

import _harness as H

pytestmark = [pytest.mark.browser, pytest.mark.playwright, pytest.mark.proving_ground]

EVIDENCE_DIR = H.SERVICE_ROOT.parent.parent / "evidence" / "a22_generation"
#: The seeded quote application lives with the M2.4 proof that executes against
#: it. Loaded by PATH rather than imported as a package: `Nexus_power/tests` is
#: not on this service's sys.path, and putting it there would drag three other
#: services' `app` packages into the same interpreter (M1.7).
_FIXTURE_APP = (H.SERVICE_ROOT.parent.parent / "tests" / "m24_generation"
                / "fixture_app.py")

CRAWL_ID = "a22-generation"
TENANT_ID = "a22-generation"

#: The one control that advances this funnel. Named as an approval because
#: ``Crawler._submit_enabled`` is False without one, and a walk that cannot
#: actuate the button never reaches the result page — which is the whole journey.
FUNNEL_ADVANCES = [
    {"control": "Get Quote", "approved_by": "a22-generation", "max_crossings": 4},
]

_FORWARD = ("quote", "continue", "next", "proceed", "get", "start", "see")


def _load_fixture_app():
    assert _FIXTURE_APP.is_file(), f"the quote application is missing: {_FIXTURE_APP}"
    spec = importlib.util.spec_from_file_location("a22_fixture_app", _FIXTURE_APP)
    module = importlib.util.module_from_spec(spec)
    sys.modules["a22_fixture_app"] = module
    spec.loader.exec_module(module)
    return module


async def _stub_advance_oracle(candidates: Sequence[Mapping[str, Any]],
                               page_title: str, page_url: str) -> dict[str, Any]:
    """The deterministic stub every gate here uses — a gate that needs a live
    model is a gate that fails on network weather.

    IT MUST SPEAK THE ORACLE'S CONTRACT, AND IT DID NOT. This returned
    ``{name, kind, reason}``. The walker reads ``status`` and ``index``
    (``_pick_advance_e2e``, tier 3) and classifies everything else as
    "a decision NOT made" — so this stub could never return a pick, on any page,
    with any label. Every consultation it answered was scored
    ``oracle=unavailable``.

    That mattered more than a stub bug normally would, because this file carries
    A22's stop condition. The strict xfail was attributed wholly to the
    bare-button wizard gate; in fact a SECOND, independent cause sat inside the
    test itself, and it would have kept the funnel unwalked after the product
    gate was fixed. Two causes behind one xfail is exactly what the companion
    blocker test exists to prevent, and it could not see this one because it pins
    the crawl's OUTPUT, not the harness's own contract.

    The shape below is the production oracle's, verbatim (``app/main.py``
    ``_make_advance_oracle``): ``{"status": "picked", "index": <into candidates>,
    "signature": ...}``, or ``{"status": "none", "index": None}``. ``index`` is
    an offset into the ``candidates`` sequence this was handed — the walker
    indexes straight back into it.
    """
    names = [str(c.get("name") or "").lower() for c in candidates]
    # Button before link, mirroring the other gates' stubs: an anchor and a
    # button can both read "forward" and the button is the funnel's own control.
    for want_button in (True, False):
        for index, (name, control) in enumerate(zip(names, candidates)):
            if (str(control.get("kind") or "").lower() == "button") is not want_button:
                continue
            if any(f in name for f in _FORWARD):
                return {"status": "picked", "index": index,
                        "signature": "a22-forward"}
    return {"status": "none", "index": None, "signature": "a22-forward"}


def _walk_authorization(target_url: str) -> Any:
    from app.attest import ProofReplayGuard, verify_provisioning_proof
    from app.walk_persist import MutationAuditLog, WalkAuthorization
    from _attest_kit import Issuer

    issuer = Issuer()
    scheme, _, rest = target_url.partition("//")
    origin = f"{scheme}//{rest.split('/')[0]}"
    verdict = verify_provisioning_proof(
        {"proof": issuer.proof(crawl_id=CRAWL_ID, tenant_id=TENANT_ID,
                               target_origin=origin,
                               max_walk_mutations_per_step=8),
         "revocations": issuer.revocations()},
        trust=issuer.trust(), crawl_id=CRAWL_ID, tenant_id=TENANT_ID,
        target_url=target_url, replay_guard=ProofReplayGuard())
    assert verdict.authorized, (
        f"could not build a walk authorization: {verdict.reason}")
    return WalkAuthorization.from_verdict(
        verdict, workflow_id=CRAWL_ID, audit=MutationAuditLog())


@pytest.fixture(scope="module")
def crawled(pw, tmp_path_factory) -> dict[str, Any]:
    """ONE real crawl of the healthy quote application; coverage + manifest."""
    fixture_app = _load_fixture_app()
    from app.auth import AuthWindow
    from app.crawl_constants import TRAVERSAL_FULL
    from app.crawler import Budget, Crawler, GuardContext
    from app.guard import load_refuse_pack
    from app.main import EXPLORER_VERSION, PlaywrightBrowserPort
    from tests.characterization.harness import disposable_attestation

    server = fixture_app.QuoteAppServer(fixture_app.BASELINE).start()
    work = H.HERE / "_crawl_out" / "a22_generation"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)
    try:
        url = f"{server.origin}/"
        pack = load_refuse_pack(str(H.SERVICE_ROOT / "app" / "refuse_pack.yaml"))
        guard_ctx = GuardContext(
            refuse_pack=pack,
            auth_window=AuthWindow(max_requests=400, window_ms=240_000),
            attestation=disposable_attestation(),
            submit_flow_approved=True,
            walk_authorization=_walk_authorization(url),
            idp_domains=frozenset(),
        )
        crawler = Crawler(
            PlaywrightBrowserPort(pw.page, pw.context),
            crawl_id=CRAWL_ID, tenant_id=TENANT_ID, target_url=url,
            work_dir=str(work), refuse_pack=pack,
            budget=Budget.from_dict({"max_states": 20, "max_actions": 120,
                                     "max_requests": 2000,
                                     "max_duration_ms": 240_000}),
            explorer_version=EXPLORER_VERSION, guard_version=EXPLORER_VERSION,
            refuse_pack_version=pack.version,
            config_fingerprint="a22-generation",
            guard_context=guard_ctx, identity_seed="qec-a22-generation",
            observe_only=False, traversal=TRAVERSAL_FULL,
            advance_oracle=_stub_advance_oracle,
            boundary_approvals=FUNNEL_ADVANCES,
        )
        pw.run(crawler.run())
        coverage = crawler._coverage.build()
        manifest = (work / CRAWL_ID / "manifest.jsonl").read_text(encoding="utf-8")
        served = list(server.requests)
    finally:
        server.stop()

    events = []
    for line in manifest.splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        for call in (record.get("network_calls") or []):
            events.append(call)

    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    (EVIDENCE_DIR / "coverage.json").write_text(
        json.dumps(coverage, indent=2, sort_keys=True, default=str), encoding="utf-8")
    (EVIDENCE_DIR / "manifest.jsonl").write_text(manifest, encoding="utf-8")
    stamp = {
        "milestone": "GATE3-A22",
        "app": "m24_generation/fixture_app.py (quote funnel, real HTTP backend)",
        "mode": fixture_app.BASELINE,
        "crawl_id": CRAWL_ID,
        "tenant_id": TENANT_ID,
        "states": len(coverage.get("states") or []),
        "flows": len(coverage.get("flows") or []),
        "network_events": len(events),
        # What the SERVER says it answered — an independent record, so a claim
        # about the crawl's traffic is not made only by the crawl.
        "server_saw": [f"{m} {p}" for m, p in served],
        "coverage_sha256": hashlib.sha256(
            (EVIDENCE_DIR / "coverage.json").read_text(encoding="utf-8")
            .encode("utf-8")).hexdigest(),
    }
    (EVIDENCE_DIR / "stamp.json").write_text(
        json.dumps(stamp, indent=2, sort_keys=True), encoding="utf-8")
    return {"coverage": coverage, "events": events, "served": served,
            "stamp": stamp, "fixture_app": fixture_app,
            # The manifest records, kept alongside the account deliberately: the
            # blocker below is precisely that these two disagree.
            "manifest_records": [json.loads(line) for line in manifest.splitlines()
                                 if line.strip()]}


def _diagnose(cov: Mapping[str, Any]) -> str:
    return (f"\n  states       : {len(cov.get('states') or [])}"
            f"\n  flows        : {len(cov.get('flows') or [])}"
            f"\n  advance_blocked: {cov.get('advance_blocked')}"
            f"\n  boundaries   : {cov.get('boundaries_crossed')}")


def test_the_crawl_walked_the_funnel_to_its_result_page(crawled) -> None:
    """A journey is only a journey if the walk got to the end of it.

    WAS THE A22 STOP CONDITION; IT NOW HOLDS. This carried
    ``@pytest.mark.xfail(strict=True)`` from Gate 3, worded "the day that gap
    closes this XPASSes and A22 can proceed". A2.2 closed it and it XPASSed, so
    the marker is gone and this is an ordinary gate again — red if the funnel
    ever stops being walked.

    Three independent causes had to be removed, and only two of them were on
    record:

      1. **The bare-button wizard gate** (``discovery.py``). ``is_form`` requires
         a FILLABLE control, so an application whose only control is a
         ``<button>`` never reached the walk at all. Named in M2.1's
         "architectural concerns discovered" and left as somebody else's gap.
      2. **The outcome page dropped from the account**
         (``state_identity.note_state_signals``). ``if not signals and not
         controls: return`` discards a funnel's result page by construction —
         it has neither. Found by Gate 3.
      3. **This file's own stub oracle** — NOT previously on record. It returned
         ``{name, kind, reason}`` while the walker reads ``status``/``index``,
         so tier 3 scored every consultation ``oracle=unavailable`` and could
         never pick. Fixing (1) alone would have left the funnel unwalked and
         the cause would have looked like (1) again.

    The walk is now earned by tier 3, which is the point of the fixture:
    ``qec.oracle.picked control='Get Quote' — TIER 3 CHOSE A CONTROL no label
    rule could reach``. Neither tier-1's regex nor tier-2's destination rule
    matches "Get Quote"; a label-convention crawler cannot walk this funnel.
    """
    cov = crawled["coverage"]
    assert cov.get("states"), "the crawl observed no states" + _diagnose(cov)
    locations = {str(s.get("location") or "") for s in (cov.get("states") or [])}
    assert any("result" in loc for loc in locations), (
        f"the crawl never reached the result page, so there is no completed "
        f"journey to compile. Saw: {sorted(locations)}" + _diagnose(cov))
    assert cov.get("flows"), (
        "the crawl recorded states but walked no journey — entry snapshots, not "
        "an observation of the application" + _diagnose(cov))


def test_the_bare_button_funnel_is_walked_and_its_outcome_indexed(crawled) -> None:
    """THE A22 BLOCKER, KEPT AS A REGRESSION GATE NOW THAT IT IS CLOSED.

    What the evidence says, and the two halves do not agree:

      the SERVER's own log      GET /, GET /api/config, POST /api/quote,
                                GET /result.html
      the CRAWL's own account   states=1, flows=0, forms_found=0,
                                journeys_completed=0, and the single recorded
                                state carries ZERO actions

    So the crawler really did click the button — the backend answered the POST
    and served the result page — and then recorded none of it. Not the click,
    not the navigation, not the page it landed on.

    The cause is named in M2.1's own "architectural concerns discovered", where
    it was found and explicitly left as somebody else's gap:

        A page whose only questions are bare buttons is never walked.
        discovery.py's wizard gate requires `fill.filled or
        fill.has_unanswered_decisions`, and a step made of nothing but <button>
        answers commits nothing — so `_answer_questionnaire` never runs on it.

    ``forms_found == 0`` is that gate declining, measured. This application has
    no inputs at all: one button, and everything else in JavaScript.

    WHY IT BLOCKS A22 SPECIFICALLY, and why picking a different application does
    not help:

      * A22 requires the generated specification to carry NETWORK assertions, so
        the crawled application must call a backend. The only application in this
        repository that does is this one.
      * The applications the crawler walks WELL — acme-life, questionnaire-life,
        vkpower-life — make zero backend calls between them; acme-life's
        `grep -c 'fetch('` is 0 and vkpower-life is a static export whose every
        request is a route prefetch (A23 measured 68 GETs, all 200).

    So no application currently in the repository can produce a real discovered
    journey AND real endpoint traffic at the same time. That is the blocker,
    stated as a fact about the inventory rather than as a shortfall of effort.
    """
    cov = crawled["coverage"]
    served = {f"{m} {p}" for m, p in crawled["served"]}
    manifest_states = [r for r in crawled["manifest_records"]
                       if (r.get("type") or r.get("kind")) == "page_state"]

    # The application really did advance — this is not a crawl that failed to
    # reach anything.
    assert any(s.startswith("POST") and s.endswith("/api/quote") for s in served)
    assert any(s.endswith("/result.html") for s in served), (
        f"the server never served the result page, so the click did not advance "
        f"the funnel and the blocker below is a different one: {sorted(served)}")

    # ── LAYER 1 · THE WIZARD GATE NOW OPENS ON A BARE-BUTTON STEP ───────────
    #
    # ``forms_found`` stays 0 and that is CORRECT, not a leftover: this page has
    # no fillable control, so it is genuinely not a form and ``is_form`` is right
    # to say so. What changed is that not-a-form no longer means not-walked. The
    # assertion that matters is therefore the flow, not the form count.
    assert int(cov.get("forms_found") or 0) == 0, (
        f"forms_found={cov.get('forms_found')} — this application has no input "
        f"of any kind, so a non-zero count means the fixture changed shape and "
        f"this gate is no longer measuring the bare-button path")
    assert cov.get("flows"), (
        "the funnel was not walked — the bare-button wizard gate has regressed "
        "and an application the crawl ACTUATES is again recording no journey"
        + _diagnose(cov))

    # ── LAYER 2 · AND THE OUTCOME PAGE IS DROPPED FROM THE ACCOUNT ──────────
    #
    # The MANIFEST recorded the walk correctly. It holds the click, the edge, and
    # the result page — and the result page carries exactly what a hard outcome
    # assertion needs:
    #
    #     displayed_values: [{"label": "Your monthly premium",
    #                         "selector": "#premium-value", "text": "42.50",
    #                         "value_type": "number",
    #                         "value_reason": "number value under an outcome label"}]
    #
    # `coverage["states"]` does not have it, and the fold reads
    # `coverage.states`. The cause is one line in
    # state_identity.note_state_signals:
    #
    #     if not signals and not controls:
    #         return
    #
    # A funnel's RESULT page is by construction a page with neither: no inputs to
    # ask anything and no controls to press. So the one page whose VALUE the
    # generated spec has to assert on is the one page the account is designed to
    # discard. That is why crawl_evidence.py had to hand-write `outcome_values`
    # into its traversal fixture — the real crawl cannot supply them through this
    # path.
    result_records = [r for r in manifest_states
                      if "result" in str(r.get("location") or "")]
    assert result_records, (
        "the manifest no longer records the result page at all — that is a "
        "different and worse failure than the one pinned here")
    outcomes = [v for r in result_records for v in (r.get("displayed_values") or [])]
    assert any(str(v.get("text") or "") == crawled["fixture_app"].BASELINE_PREMIUM
               for v in outcomes), (
        f"the manifest's result page no longer carries the premium as a "
        f"displayed value, so this pin is about something else now: {outcomes}")

    covered = {str(s.get("location") or "") for s in (cov.get("states") or [])}
    assert any("result" in loc for loc in covered), (
        f"the result page is missing from coverage.states again ({sorted(covered)}) "
        f"— the fold reads coverage.states, so a generated spec has nothing to "
        f"assert the premium on and A22's chain is broken at the same place it "
        f"was before")

    # ── AND IT CARRIES THE SELECTOR THE ASSERTION IS GROUNDED ON ───────────
    #
    # Admitting the page is NOT sufficient on its own, which is worth pinning
    # because the half-fix looks identical from the coverage account's outside.
    # qe-central's `journey_spec.outcome_selectors` builds a journey's hard
    # outcome assertion out of `node.displayed_outcomes`, and its ground is a
    # captured SELECTOR — "an outcome with no captured selector is ungrounded".
    # `catalog.extract_outcomes` reads that off `page_state["displayed_values"]`.
    # The FLOW carries the premium's value; only the STATE carries the node it is
    # asserted against. Miss this and the blocker moves instead of closing.
    premium = crawled["fixture_app"].BASELINE_PREMIUM
    result_states = [s for s in (cov.get("states") or [])
                     if "result" in str(s.get("location") or "")]
    grounded = [dv for s in result_states
                for dv in (s.get("displayed_values") or [])
                if str(dv.get("text") or "") == premium and dv.get("selector")]
    assert grounded, (
        f"the result state carries no displayed value with BOTH the premium "
        f"{premium!r} and a selector, so a generated spec has nothing to anchor a "
        f"hard outcome assertion to: "
        f"{[s.get('displayed_values') for s in result_states]}")

    # THE SHAPES CHANNEL STAYS SHAPES. Outcomes cross in their OWN key, scrubbed
    # by the same `emit.scrub_value` the manifest applies. What must never cross
    # is a committed ANSWER into form_snapshot_signals — that is the boundary
    # note_state_signals exists to hold, and it is unaffected by any of this.
    for state in (cov.get("states") or []):
        signal_blob = json.dumps(state.get("form_snapshot_signals") or {})
        assert premium not in signal_blob, (
            f"the outcome VALUE {premium!r} reached form_snapshot_signals for "
            f"{state.get('location')!r}; that channel carries the shapes of "
            f"questions, never a value")


def test_the_backend_really_answered_the_crawl(crawled) -> None:
    """THE INDEPENDENT RECORD. The server keeps its own list of what it served,
    so 'the crawl called the API' is not a claim the crawl makes about itself.

    This is what makes the evidence usable for a NETWORK assertion: a spec
    generated from a journey whose POST was never actually made would be
    asserting on a call the application does not make.
    """
    served = crawled["served"]
    posts = [f"{m} {p}" for m, p in served if m == "POST"]
    assert any(p.endswith("/api/quote") for p in posts), (
        f"the server never answered POST /api/quote during the crawl, so the "
        f"funnel's commit did not reach the backend. It served: {served}")


def test_the_crawl_captured_the_call_the_backend_answered(crawled) -> None:
    """…and the crawl SAW it. The server's record and the crawl's record have to
    agree, or the endpoint inventory is built from a different application than
    the one that ran."""
    captured = {(str(e.get("method") or "").upper(), str(e.get("url") or ""))
                for e in crawled["events"]}
    assert captured, (
        "the crawl recorded no network events at all, so the generated spec "
        "could carry no network assertion")
    quote = [url for method, url in captured
             if method == "POST" and url.endswith("/api/quote")]
    assert quote, (
        f"the backend answered POST /api/quote but the crawl did not capture "
        f"it. Captured: {sorted(captured)}")


def test_the_evidence_is_written_for_the_consumer_half(crawled) -> None:
    for name in ("coverage.json", "manifest.jsonl", "stamp.json"):
        path = EVIDENCE_DIR / name
        assert path.is_file() and path.stat().st_size > 0, f"{path} was not written"
