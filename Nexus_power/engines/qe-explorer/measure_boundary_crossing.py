"""MEASURE: does a real crawl reach and CROSS an irreversible boundary?

Runs the PRODUCTION Crawler, through the PRODUCTION Playwright port, in real
Chromium, against a real proving-ground application, and prints what happened.
It asserts nothing -- it is the instrument the Phase 1 boundary-crossing claim
was measured with, kept so the measurement can be repeated rather than believed.

    python measure_boundary_crossing.py acme-life

Everything is supplied the way a real dispatch supplies it: credentials, a
signed M1.3 provisioning proof verified through `app.attest`, and a per-control
`boundary_approvals` grant naming exactly one irreversible control.

THE ONE STAND-IN is `stub_advance_oracle`. Tier 3 is normally an LLM reached
through qe-central; here it is a deterministic function so the run is
reproducible offline and so what it picked is printed rather than inferred. It
is not tuned to these applications -- see its own comment for the rule.
"""
from __future__ import annotations
import asyncio, json, os, shutil, sys
from pathlib import Path

EXPLORER = Path(__file__).resolve().parent
sys.path.insert(0, str(EXPLORER))
sys.path.insert(0, str(EXPLORER / "tests" / "browser"))

import _harness as H
from app.attest import ProofReplayGuard, verify_provisioning_proof
from app.auth import AuthWindow, Credentials
from app.crawl_constants import TRAVERSAL_FULL
from app.crawler import Budget, Crawler, GuardContext
from app.guard import load_refuse_pack
from app.main import EXPLORER_VERSION, PlaywrightBrowserPort
from app.walk_persist import MutationAuditLog, WalkAuthorization
from tests._attest_kit import Issuer
from tests.characterization.harness import disposable_attestation

PG = EXPLORER.parent.parent / "proving-grounds"
OUT = Path(os.environ.get("QEC_MEASURE_OUT") or (EXPLORER / "_measure_out"))

NAME = sys.argv[1] if len(sys.argv) > 1 else "acme-life"
WALK = os.environ.get("RECON_WALK", "1") == "1"
CREDS = {"username": "qec.recon@example.test", "password": "Recon!Passw0rd"}
GRANTS = [{"control": "Bind policy", "approved_by": "recon", "max_crossings": 1},
          {"control": "Bind coverage", "approved_by": "recon", "max_crossings": 1}]


# A DETERMINISTIC stand-in for the tier-3 advance oracle. It is not a model and
# it is not tuned to these apps: given the candidate set the walker already
# filtered (no danger, no commit words, no disabled, named only), it picks the
# candidate whose label most looks like "the way onward", and logs every pick so
# the choice is auditable rather than magic. The point is to test the MECHANISM
# -- if the funnel opens when tier 3 answers, the gap is configuration; if it
# stays shut, the gap is design.
FORWARD = ("quote", "continue", "next", "proceed", "apply", "review", "start", "see")


async def stub_advance_oracle(candidates, page_title, page_url):
    names = [str(c.get("name") or "") for c in candidates]
    # Prefer a BUTTON over a LINK among the forward-shaped candidates. Site
    # navigation renders as links; the control that advances the step you are
    # standing on is that step's own submit button. App-agnostic, and the same
    # reasoning a model would apply to this candidate set.
    best = None
    for want_button in (True, False):
        for i, (n, c) in enumerate(zip(names, candidates)):
            if (str(c.get("kind") or "") == "button") is not want_button:
                continue
            if any(w in n.lower() for w in FORWARD):
                best = i
                break
        if best is not None:
            break
    where = page_url.split("/")[-1]
    chose = ("PICK " + repr(names[best])) if best is not None else "no pick"
    print(f"  [oracle] {where} candidates={names} -> {chose}", flush=True)
    if best is None:
        return {"status": "none", "signature": "stub"}
    return {"status": "picked", "index": best, "signature": "stub-forward"}


async def main() -> None:
    from playwright.async_api import async_playwright
    from app.playwright_port import context_defaults

    srv = H.FixtureServer(root=PG).start()
    url = srv.url(NAME)
    crawl_id, tenant = f"recon-{NAME}", "recon"
    print(f"serving {NAME} at {url}", flush=True)

    pack = load_refuse_pack(str(EXPLORER / "app" / "refuse_pack.yaml"))

    walk_auth = None
    if WALK:
        # M1.3: mint a REAL platform-signed provisioning proof and verify it
        # through the PRODUCTION verifier - the same object the dispatch path
        # builds. Only the issuer's identity is supplied by the test kit.
        iss = Issuer()
        origin = url.split("//")[0] + "//" + url.split("//")[1].split("/")[0]
        env = {"proof": iss.proof(crawl_id=crawl_id, tenant_id=tenant,
                                  target_origin=origin,
                                  max_walk_mutations_per_step=8),
               "revocations": iss.revocations()}
        verdict = verify_provisioning_proof(
            env, trust=iss.trust(), crawl_id=crawl_id, tenant_id=tenant,
            target_url=url, replay_guard=ProofReplayGuard())
        print(f"walk attestation authorized={verdict.authorized} "
              f"reason={verdict.reason!r}", flush=True)
        walk_auth = WalkAuthorization.from_verdict(
            verdict, workflow_id=crawl_id, audit=MutationAuditLog())
        print(f"walk_authorization={'BUILT' if walk_auth else 'NONE'}", flush=True)

    guard_ctx = GuardContext(
        refuse_pack=pack,
        auth_window=AuthWindow(max_requests=400, window_ms=240_000),
        attestation=disposable_attestation(),
        submit_flow_approved=True,
        walk_authorization=walk_auth,
        idp_domains=frozenset(),
    )
    budget = Budget.from_dict({"max_states": 40, "max_actions": 250,
                              "max_requests": 4000, "max_duration_ms": 420_000})
    work = OUT / NAME
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
            crawl_id=crawl_id, tenant_id=tenant, target_url=url,
            work_dir=str(work), refuse_pack=pack, budget=budget,
            explorer_version=EXPLORER_VERSION, guard_version=EXPLORER_VERSION,
            refuse_pack_version=pack.version,
            config_fingerprint=f"recon-{NAME}",
            guard_context=guard_ctx, identity_seed="qec-recon",
            observe_only=False,
            traversal=TRAVERSAL_FULL,
            advance_oracle=stub_advance_oracle,
            boundary_approvals=GRANTS,
            credentials=Credentials.from_payload(CREDS),
        )
        await crawler.run()
    finally:
        await ctx.close(); await browser.close(); await pw.stop(); srv.stop()

    cov = crawler._coverage.build()
    print(f"\n=== {NAME} RESULT", flush=True)
    for k in ("boundaries_crossed", "boundary_crossings", "outcome_milestones"):
        print(f"  {k}: {json.dumps(cov.get(k), default=str)[:1200]}", flush=True)
    print(f"  approvable_boundary labels: "
          f"{sorted({r.get('label') for r in (cov.get('approvable_boundary') or [])})}",
          flush=True)

asyncio.run(main())
