"""GATE 2 - one real customer journey, on one real application, evidenced.

Runs the PRODUCTION :class:`app.crawler.Crawler` through the PRODUCTION
:class:`app.main.PlaywrightBrowserPort`, in real Chromium, against a real
proving-ground application served from ITS OWN DOCKER IMAGE, and writes an
evidence bundle a reviewer can check without rerunning anything.

    docker run -d --name pg-vk -p 8101:80 pg-vkpower-life:gate2
    python gate2_journey.py vkpower-life --url http://127.0.0.1:8101/

WHY DOCKER AND NOT A STATIC FILE SERVER
=======================================
``tests/browser/test_proving_grounds.py`` serves acme-life and vkpower-life
"straight from their source directory" locally, and from the Docker image in CI.
For vkpower-life those are NOT THE SAME APPLICATION: the source directory holds a
hand-written hash-routed ``index.html`` (#/quote -> #/apply -> #/review ->
#/confirm), while the image serves the Next.js export in ``out/`` (routes
``/login``, ``/life-insurance/...``, ``/portal/...``). A journey proven against
the first would not reproduce in CI, and reproducibility is the whole of Gate 2's
claim -- so this instrument only ever talks to a served URL, and the lane that
starts it is the same ``docker build`` CI runs.

WHAT IS REAL
============
Everything except the tier-3 oracle by default: the frontier, the guard, the
refuse pack, the inventory, the fingerprinter, the fill engine, the walker, the
flow ledger, the boundary model, the approval registry, the crossing ledger and
the outcome milestone are all production objects. Authorisation is the NARROWEST
the system offers -- one ``ApprovalGrant`` naming exactly one control, plus an
M1.3 provisioning proof minted by the test issuer and verified through the
PRODUCTION :func:`app.attest.verify_provisioning_proof`.

Pass ``--oracle live`` to consult the real qe-central tier-3 advance oracle
instead of the deterministic stand-in (A18). The stand-in is used otherwise so
the journey is reproducible offline, and WHICH ONE RAN is recorded in the bundle
rather than left to be inferred from a log line.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sys
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

EXPLORER = Path(__file__).resolve().parent
sys.path.insert(0, str(EXPLORER))
sys.path.insert(0, str(EXPLORER / "tests" / "browser"))

from app.attest import ProofReplayGuard, verify_provisioning_proof
from app.auth import AuthWindow, Credentials
from app.crawl_constants import TRAVERSAL_FULL
from app.crawler import Budget, Crawler, GuardContext
from app.guard import load_refuse_pack
from app.main import EXPLORER_VERSION, PlaywrightBrowserPort
from app.walk_persist import MutationAuditLog, WalkAuthorization
from tests._attest_kit import Issuer
from tests.characterization.harness import disposable_attestation

EVIDENCE = EXPLORER.parent.parent / "evidence" / "gate2"

# -- THE THREE APPLICATIONS -------------------------------------------------
# Every control named below was read from that application's own source. None
# was chosen to make a crawl pass.
#
#: ``commit`` is the ONE control whose crossing commits the application -- the
#: control the journey's confirmation is downstream of. ``transits`` are the
#: other controls the funnel cannot be walked past, each one recorded with WHY
#: it needs a grant at all. The distinction is the whole point: a reader must be
#: able to see that the journey committed ONCE, and that everything else granted
#: was a step the application labelled with an irreversible word while doing
#: nothing irreversible.
APPS: dict[str, dict[str, Any]] = {
    "vkpower-life": {
        # src/app/life-insurance/apply/signature/page.tsx -- gated on five
        # consent checkboxes AND a typed signature matching the legal name
        # entered seven steps earlier.
        "commit": "Sign & Submit Application",
        "transits": [
            # src/app/life-insurance/quote/review/page.tsx:114 -- an <a> to
            # /life-insurance/apply/member-lookup/. Approvable by COMMIT SHAPE
            # ("apply"), which app/boundary.py names explicitly as a control
            # that rung exists to catch. It navigates; it commits nothing.
            ("Apply Now", "commit-shaped label on a route link; navigates to "
                          "the application's first step and commits nothing"),
            # src/app/life-insurance/apply/lifestyle/page.tsx:255 -- a submit
            # button whose handler is router.push('/apply/decision/'). Flagged
            # DANGER by rp.verb.underwrite on button_name. The label is a
            # textbook destination advance ("Continue" + "to" + destination),
            # but classify_boundary checks danger BEFORE any shape rule, so the
            # Tier-2 destination rule never reaches it.
            ("Continue to Underwriting Decision",
             "rp.verb.underwrite matches the button NAME; the handler is a "
             "router.push to the decision step and underwrites nothing"),
        ],
        "credentials": {"member_number": "25000001", "password": "Vk!Passw0rd",
                        "mfa": {"kind": "otp", "otp": "123456"}},
        "container_port": 80,
    },
    "acme-life": {
        "commit": "Bind policy",
        "transits": [],
        "credentials": {"username": "qec.gate@example.test",
                        "password": "Gate!Passw0rd"},
        "container_port": 80,
    },
    "summit-life-carrier": {
        # src/app/(platform)/underwriting/new-business/new-application/page.tsx
        "commit": "Submit Application",
        # SCOPED TO THE WIZARD'S OWN PAGE, and it has to be. The dashboard's
        # left nav carries a LINK also called "Submit Application", so an
        # unscoped grant is spent on a navigation the first time the crawl sees
        # the dashboard -- measured: crossed at /dashboard/overview with
        # outcome=error, confirmed=false, and max_crossings=1 then refuses the
        # real commit button for the rest of the crawl. Grants match on the
        # normalised LABEL, so a label an application reuses for a link and a
        # commit button needs the url narrowing that ApprovalGrant already
        # supports.
        "commit_url": "http://127.0.0.1:8103/underwriting/new-business/new-application",
        "transits": [],
        # The sign-in is TWO-PHASE: Continue -> a 1200ms await -> an MFA field
        # animates in -> "Verify & Sign In". Without an `mfa` block the crawl
        # never grounds a second factor and the login cannot be verified, so the
        # operator supplies one -- which is what MfaConfig is for. The app does
        # not check the code (onSubmit ignores mfaCode); the crawl still has to
        # answer the challenge to get past the screen.
        "credentials": {"username": "qec.gate@summitlife.com",
                        "password": "Gate!Passw0rd",
                        "mfa": {"kind": "otp", "otp": "123456"}},
        # The image sets ENV PORT=3002 and EXPOSEs 3002. browser-harness.yml
        # published container port 3000, so this leg never served -- see A17.
        "container_port": 3002,
    },
}


def grants_for(app: str) -> list[dict[str, Any]]:
    """The ONLY grant this journey carries: the one control that COMMITS.

    ``transits`` are deliberately NOT granted, and the reason is a defect this
    instrument produced and then caught.

    Granting them appears to help -- on vkpower-life it walks four steps further
    -- but a ``boundary_approvals`` entry is not a hint, it is the operator
    ASSERTING that the named control is an irreversible commit. The platform
    believes that assertion, and it is load-bearing: ``OutcomeMilestone.verified``
    accepts ``RUNG_NAVIGATION`` precisely because "the click was a COMMIT and the
    landing is therefore evidence about that commit" (app/boundary.py). Grant a
    navigation control and that prior is false, so the landing proves nothing --
    yet ``verified`` is still True, ``journey_completed`` follows it, and
    ``journeys_completed`` counts it.

    Measured: granting "Continue to Underwriting Decision" produced a milestone
    with ``outcome=navigation``, ``confirmation_rung=navigation`` and an EMPTY
    ``confirmation_detail`` at /life-insurance/apply/decision/ -- step 6 of a
    10-step funnel -- and the crawl reported ``journeys_completed: 1``. A
    completed customer journey, claimed on a page the application never said
    anything about.

    So the transit list stays as DOCUMENTATION of what blocks each funnel, and
    the journey is measured with the commit grant alone. A funnel that cannot be
    walked without asserting something false about a control is a funnel this
    platform cannot complete today, and that is the honest measurement.
    """
    grant = {"control": APPS[app]["commit"], "approved_by": "gate2-operator",
             "max_crossings": 1, "role": "commit"}
    if APPS[app].get("commit_url"):
        grant["url"] = APPS[app]["commit_url"]
    return [grant]

#: Forward-shaped label fragments the stand-in recognises. Generic funnel
#: vocabulary, not any one application's wording.
FORWARD = ("quote", "continue", "next", "proceed", "apply", "review", "start",
           "see", "get", "sign in", "begin")


def _make_stub_oracle(log: list[dict[str, Any]]):
    """The deterministic stand-in for tier 3, recording every pick it makes.

    Not tuned to any application: over the candidate set the walker has ALREADY
    filtered (no danger, no commit words, no disabled, named only) it prefers a
    button over a link among forward-shaped labels -- the rule the Golden Gate
    uses, and the reasoning a model applies to the same set.
    """
    #: Labels seen on EVERY decision so far. Site chrome repeats; the controls
    #: that belong to the step you are standing on do not.
    seen_everywhere: dict[str, int] = {}
    decisions = 0

    async def _oracle(candidates: Sequence[Mapping[str, Any]],
                      page_title: str, page_url: str) -> dict[str, Any]:
        nonlocal decisions
        names = [str(c.get("name") or "") for c in candidates]
        decisions += 1
        for name in names:
            seen_everywhere[name] = seen_everywhere.get(name, 0) + 1

        # CHROME IS NOT A STEP. "Dashboard", "Beneficiaries", "Get a Quote" ride
        # the header of every page in the application, so they are offered at
        # every decision point -- and one of them ("Get a Quote") is
        # forward-shaped, which is enough to beat the control that actually
        # belongs to the step. Measured on vkpower-life's payment step: the
        # candidate set was
        #
        #   [Dashboard, Beneficiaries, Get a Quote, Monthly, Quarterly,
        #    Semi-Annual, Annual, Credit / Debit Card ..., Back]
        #
        # and this stand-in chose "Get a Quote", walking a twelve-step journey
        # back to the start page. A model reading that set would not; the
        # deterministic rule needed the same distinction, and repetition is a
        # value-free way to make it -- no label list, no page knowledge.
        def _is_chrome(name: str) -> bool:
            return decisions >= 3 and seen_everywhere.get(name, 0) >= decisions

        best: Optional[int] = None
        for allow_chrome in (False, True):
            for want_button in (True, False):
                for i, (name, control) in enumerate(zip(names, candidates)):
                    if (str(control.get("kind") or "") == "button") is not want_button:
                        continue
                    if not allow_chrome and _is_chrome(name):
                        continue
                    if any(word in name.lower() for word in FORWARD):
                        best = i
                        break
                if best is not None:
                    break
            if best is not None:
                break
        entry = {"url": page_url, "title": page_title, "candidates": names,
                 "picked": names[best] if best is not None else None,
                 "tier3_source": "deterministic-stub"}
        log.append(entry)
        print("  [tier3-stub] %s candidates=%s -> %r"
              % (page_url[-48:], names, entry["picked"]), flush=True)
        if best is None:
            return {"status": "none", "signature": "gate2-none"}
        return {"status": "picked", "index": best, "signature": "gate2-forward"}
    return _oracle


def _walk_authorization(crawl_id: str, tenant_id: str, url: str):
    """A REAL M1.3 provisioning proof, verified by the production verifier."""
    issuer = Issuer()
    scheme, _, rest = url.partition("//")
    origin = "%s//%s" % (scheme, rest.split("/")[0])
    verdict = verify_provisioning_proof(
        {"proof": issuer.proof(crawl_id=crawl_id, tenant_id=tenant_id,
                               target_origin=origin,
                               max_walk_mutations_per_step=8),
         "revocations": issuer.revocations()},
        trust=issuer.trust(), crawl_id=crawl_id, tenant_id=tenant_id,
        target_url=url, replay_guard=ProofReplayGuard())
    if not verdict.authorized:
        raise SystemExit("walk authorization refused: %s" % verdict.reason)
    return WalkAuthorization.from_verdict(
        verdict, workflow_id=crawl_id, audit=MutationAuditLog())


async def run(app: str, url: str, *, oracle_kind: str, out_root: Path,
              data_mode: str = "user",
              max_states: int, max_duration_ms: int) -> dict[str, Any]:
    from playwright.async_api import async_playwright
    from app.playwright_port import context_defaults

    cfg = APPS[app]
    crawl_id, tenant_id = "gate2-%s" % app, "gate2"
    pack = load_refuse_pack(str(EXPLORER / "app" / "refuse_pack.yaml"))
    oracle_log: list[dict[str, Any]] = []
    http_client = None

    if oracle_kind == "live":
        # A18. The REAL tier-3 path: the crawler POSTs the walker's filtered
        # candidate set to qe-central's /internal/pick-advance, which asks an
        # LLM through platform-api. It needs qe-central up, a fleet HMAC secret
        # both sides agree on, platform-api reachable, and a model credential --
        # so this branch is wired, not exercised, and says which it is in the
        # bundle it writes.
        import httpx

        from app.main import _make_advance_oracle
        http_client = httpx.AsyncClient(timeout=30.0)
        advance_oracle = _make_advance_oracle(http_client, tenant_id, crawl_id)
        oracle_source = "qe-central-live"
    else:
        advance_oracle = _make_stub_oracle(oracle_log)
        oracle_source = "deterministic-stub"

    guard_ctx = GuardContext(
        refuse_pack=pack,
        auth_window=AuthWindow(max_requests=600, window_ms=600_000),
        attestation=disposable_attestation(),
        submit_flow_approved=True,
        walk_authorization=_walk_authorization(crawl_id, tenant_id, url),
        idp_domains=frozenset(),
    )

    work = out_root / "crawl"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)

    pw = await async_playwright().start()
    browser = await pw.chromium.launch()
    ctx = await browser.new_context(**context_defaults())
    page = await ctx.new_page()
    crawler = None
    try:
        crawler = Crawler(
            PlaywrightBrowserPort(page, ctx),
            crawl_id=crawl_id, tenant_id=tenant_id, target_url=url,
            work_dir=str(work), refuse_pack=pack,
            budget=Budget.from_dict({"max_states": max_states,
                                     "max_actions": 900,
                                     "max_requests": 12000,
                                     "max_duration_ms": max_duration_ms}),
            explorer_version=EXPLORER_VERSION, guard_version=EXPLORER_VERSION,
            refuse_pack_version=pack.version, config_fingerprint="gate2-%s" % app,
            guard_context=guard_ctx, identity_seed="qec-gate2-%s" % app,
            data_mode=data_mode,
            observe_only=False, traversal=TRAVERSAL_FULL,
            advance_oracle=advance_oracle,
            # THE NARROWEST AUTHORISATION THE SYSTEM OFFERS: one named control,
            # once. No "*", no submit_approvals label list.
            boundary_approvals=[{k: v for k, v in g.items()
                                 if k in ("control", "approved_by",
                                          "max_crossings", "url")}
                                for g in grants_for(app)],
            credentials=Credentials.from_payload(cfg["credentials"]),
        )
        await crawler.run()
    finally:
        await ctx.close()
        await browser.close()
        await pw.stop()
        if http_client is not None:
            await http_client.aclose()

    coverage = crawler._coverage.build()
    return {"coverage": coverage, "oracle_log": oracle_log,
            "oracle_source": oracle_source, "work": work}


def _producing_code() -> dict[str, Any]:
    """WHICH CODE produced this bundle — the SHA, and whether the tree was dirty.

    Two live runs of this instrument against the same application, hours apart,
    measured depth 7 and depth 12. Both were correct; the difference was a
    refuse-pack carve-out and a hydration-gate change that landed in between.
    Reconstructing that took reading two commit messages and diffing a YAML
    file, and it was only possible because both runs happened to be mine.

    A bundle that cannot say what produced it is a measurement without units.
    ``dirty`` matters as much as the SHA: most of these runs are made while
    iterating, so a bundle stamped with a clean SHA it was not actually built
    from would be worse than no stamp at all.
    """
    import subprocess

    def _run(args: list[str]) -> str:
        try:
            done = subprocess.run(args, cwd=str(EXPLORER), capture_output=True,
                                  timeout=30, text=True)
            return done.stdout.strip() if done.returncode == 0 else ""
        except Exception:
            return ""

    head = _run(["git", "rev-parse", "HEAD"])
    dirty = _run(["git", "status", "--porcelain", "--", "app", "gate2_journey.py"])
    return {
        "head": head or "(unknown)",
        "dirty": bool(dirty),
        "dirty_paths": [ln[2:].strip() for ln in dirty.splitlines()][:20],
    }


def verdict_of(app: str, url: str, result: Mapping[str, Any]) -> dict[str, Any]:
    """The reviewable claim: what the journey did, and what proves it."""
    cov = result["coverage"]
    all_crossings = list(cov.get("boundary_crossings") or [])
    crossed = [c for c in all_crossings if str(c.get("status") or "") == "crossed"]
    refused = [c for c in all_crossings if str(c.get("status") or "") == "refused"]
    milestones = list(cov.get("outcome_milestones") or [])
    confirmed = [m for m in milestones
                 if str(m.get("outcome") or "") == "confirmation"
                 and str(m.get("confirmation_rung") or "")]
    flow = dict(cov.get("flow_summary") or {})
    agent = dict(cov.get("agent") or {})
    return {
        "app": app,
        "target_url": url,
        "explorer_version": EXPLORER_VERSION,
        #: WHAT PRODUCED THIS BUNDLE. Without it, two honest runs of the same
        #: journey that disagree are indistinguishable from one of them being
        #: wrong -- see _producing_code.
        "produced_by": _producing_code(),
        "commit_control": APPS[app]["commit"],
        "grants": grants_for(app),
        #: Controls the funnel cannot be walked past, which this journey
        #: deliberately did NOT grant -- see grants_for.__doc__.
        "known_blockers": [{"control": n, "reason": r}
                           for n, r in APPS[app]["transits"]],
        "oracle_source": result["oracle_source"],
        "oracle_telemetry": agent.get("advance_oracle") or {},
        "oracle_decisions": result["oracle_log"],
        "boundaries_crossed": len(crossed),
        "crossings": crossed,
        "crossings_refused": refused,
        "outcome_milestones": milestones,
        "confirmation_observed": bool(confirmed),
        "confirmation_rungs": [m.get("confirmation_rung") for m in confirmed],
        "confirmation_details": [str(m.get("confirmation_detail") or "")[:400]
                                 for m in confirmed],
        # A19 -- the depth telemetry, reported from the crawl's own account.
        "telemetry": {key: flow.get(key) for key in (
            "flows_found", "flows_completed", "journeys_completed",
            "boundaries_crossed", "deepest_flow_steps",
            "deepest_flow_proven_steps", "deepest_flow_capped",
            "deepest_flow_terminal", "advances_by_tier", "oracle_advances")},
        "stop_reason": cov.get("stop_reason"),
        "approvable_boundaries_seen": sorted(
            {str(r.get("label") or "")
             for r in (cov.get("approvable_boundary") or [])}),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("app", choices=sorted(APPS))
    parser.add_argument("--url", required=True,
                        help="the served application root (a running container)")
    parser.add_argument("--oracle", default="stub", choices=("stub", "live"))
    parser.add_argument("--data-mode", default="user",
                        choices=("user", "agent"),
                        help="agent answers semantic CHOICES; user leaves them")
    parser.add_argument("--out", default="")
    parser.add_argument("--max-states", type=int, default=60)
    parser.add_argument("--max-duration-ms", type=int, default=900000)
    args = parser.parse_args()

    out_root = Path(args.out) if args.out else (EVIDENCE / args.app)
    out_root.mkdir(parents=True, exist_ok=True)

    result = asyncio.run(run(args.app, args.url, oracle_kind=args.oracle,
                             data_mode=args.data_mode,
                             out_root=out_root, max_states=args.max_states,
                             max_duration_ms=args.max_duration_ms))
    verdict = verdict_of(args.app, args.url, result)

    (out_root / "coverage.json").write_text(
        json.dumps(result["coverage"], indent=2, default=str, sort_keys=True),
        encoding="utf-8")
    (out_root / "journey.json").write_text(
        json.dumps(verdict, indent=2, default=str), encoding="utf-8")

    print("\n=== GATE 2 . %s ===" % args.app, flush=True)
    print("  crossed              : %d %s"
          % (verdict["boundaries_crossed"],
             [c.get("control_name") for c in verdict["crossings"]]))
    print("  confirmation observed: %s %s"
          % (verdict["confirmation_observed"], verdict["confirmation_rungs"]))
    print("  telemetry            : %s" % json.dumps(verdict["telemetry"]))
    print("  boundaries offered   : %s" % verdict["approvable_boundaries_seen"])
    print("  evidence             : %s" % out_root)


if __name__ == "__main__":
    main()
