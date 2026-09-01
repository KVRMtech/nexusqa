"""MEASURE: does the RADIO UNBLOCK EXPERIMENT (Gate 1 / T-RG-01) clear a real
application's product-selection gate, on a live deployment?

    python measure_radio_unblock.py https://vkpowerlife.136-85-106-73.sslip.io/

WHY AN INSTRUMENT AND NOT A GATE.  The fixture lane already proves the mechanism
against fixture 27, which was built to exhibit it.  A fixture cannot surprise
you.  This runs the same production crawler against a real deployed app and
prints what actually came back -- including the parts that are less convenient
than the fixture's.  It ASSERTS NOTHING.

DELIBERATELY NO `boundary_approvals` AND NO WALK ATTESTATION, for the reason
measure_network_evidence.py states: this is somebody's live deployment.  The
crawl is given no authority to cross an irreversible control, so it fills and
advances and stops at the commit boundary -- which is where a measurement of
*progression* should stop.  Advancing past product selection does not require a
submit; it requires answering the question the app is gating on.
"""
from __future__ import annotations
import asyncio, json, os, shutil, sys
from pathlib import Path

EXPLORER = Path(__file__).resolve().parent
sys.path.insert(0, str(EXPLORER))

from app.auth import AuthWindow, Credentials
from app.crawl_constants import TRAVERSAL_FULL
from app.crawler import Budget, Crawler, GuardContext
from app.guard import load_refuse_pack
from app.main import EXPLORER_VERSION, PlaywrightBrowserPort
from tests.characterization.harness import disposable_attestation

TARGET = (sys.argv[1] if len(sys.argv) > 1
          else "https://vkpowerlife.136-85-106-73.sslip.io/")
OUT = Path(os.environ.get("QEC_MEASURE_OUT") or (EXPLORER / "_measure_out")) / "radio"
USER = os.environ.get("QEC_MEASURE_USER", "")
PASSWORD = os.environ.get("QEC_MEASURE_PASSWORD", "")
MFA_OTP = os.environ.get("QEC_MEASURE_OTP", "")

FORWARD = ("quote", "continue", "next", "proceed", "apply", "review", "start",
           "get", "see", "calculate")


async def stub_advance_oracle(candidates, page_title, page_url):
    names = [str(c.get("name") or "") for c in candidates]
    for want_button in (True, False):
        for i, (name, c) in enumerate(zip(names, candidates)):
            if (str(c.get("kind") or "") == "button") is not want_button:
                continue
            if any(w in name.lower() for w in FORWARD):
                return {"status": "picked", "index": i, "signature": "measure-forward"}
    return {"status": "none", "signature": "measure-none"}


def banner(text: str) -> None:
    print("\n" + "=" * 78, flush=True)
    print(text, flush=True)
    print("=" * 78, flush=True)


async def main() -> None:
    from playwright.async_api import async_playwright
    from app.playwright_port import context_defaults

    crawl_id, tenant = "measure-radio", "measure"
    pack = load_refuse_pack(str(EXPLORER / "app" / "refuse_pack.yaml"))
    guard_ctx = GuardContext(
        refuse_pack=pack,
        auth_window=AuthWindow(max_requests=600, window_ms=300_000),
        attestation=disposable_attestation(),
        submit_flow_approved=False,      # no submit authority on a live app
        walk_authorization=None,         # no crawl-time mutation authority
        idp_domains=frozenset(),
    )
    budget = Budget.from_dict({"max_states": 25, "max_actions": 200,
                               "max_requests": 3000, "max_duration_ms": 300_000})
    work = OUT
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)

    print(f"target      : {TARGET}", flush=True)
    print(f"credentials : {'member ' + USER if USER else 'NONE (public crawl)'}", flush=True)
    print(f"authority   : no boundary approvals, no walk attestation (read-only)", flush=True)

    pw = await async_playwright().start()
    browser = await pw.chromium.launch()
    ctx = await browser.new_context(**context_defaults())
    page = await ctx.new_page()
    crawler = None
    try:
        payload = {"username": USER, "password": PASSWORD}
        if MFA_OTP:
            payload["mfa"] = {"kind": "otp", "otp": MFA_OTP}
        crawler = Crawler(
            PlaywrightBrowserPort(page, ctx),
            crawl_id=crawl_id, tenant_id=tenant, target_url=TARGET,
            work_dir=str(work), refuse_pack=pack, budget=budget,
            explorer_version=EXPLORER_VERSION, guard_version=EXPLORER_VERSION,
            refuse_pack_version=pack.version,
            config_fingerprint="measure-radio",
            guard_context=guard_ctx, identity_seed="qec-measure-radio",
            observe_only=False, traversal=TRAVERSAL_FULL,
            advance_oracle=stub_advance_oracle,
            credentials=(Credentials.from_payload(payload) if USER else None),
        )
        await crawler.run()
    finally:
        await ctx.close(); await browser.close(); await pw.stop()

    cov = crawler._coverage.build()
    (work / "coverage.json").write_text(json.dumps(cov, indent=2, sort_keys=True),
                                        encoding="utf-8")

    flow = cov.get("flow_summary") or {}
    banner("PROGRESSION -- did the walk get past the gate?")
    for k in ("deepest_flow_steps", "deepest_flow_terminal", "flows_found",
              "flows_completed", "advances_by_tier"):
        print(f"  {k:26} {flow.get(k)}", flush=True)

    banner("THE UNBLOCK EXPERIMENT -- what it answered, and what the app said")
    blocked = cov.get("advance_blocked") or []
    if not blocked:
        print("  no advance was ever blocked by app validation on this crawl",
              flush=True)
    for b in blocked:
        print(f"  url      : {b.get('url')}", flush=True)
        print(f"  advance  : {b.get('label')!r}  reason={b.get('reason')}", flush=True)
        print(f"  missing  : {(b.get('missing_fields') or [])[:8]}", flush=True)
        print(f"  answered : {b.get('resolved_by_agent')!r} "
              f"(rule_reused={b.get('rule_reused')})", flush=True)
        print(f"  rule     : {b.get('business_rule')}", flush=True)
        print("", flush=True)

    banner("RULES PROVED (the deliverable M2.2 consumes)")
    for r in (cov.get("discovered_rules") or []) or [None]:
        print(f"  {r}" if r else "  none", flush=True)

    banner("IRREVERSIBLE RESIDUE (T-RG-01 -- experiments that could not be undone)")
    residue = cov.get("unblock_irreversible")
    if residue is None:
        print("  KEY ABSENT -- the payload cannot answer this question", flush=True)
    elif not residue:
        print("  none -- every experiment this run made was confirmed by the app,",
              flush=True)
        print("  or none ran; nothing was left committed by a failed attempt.",
              flush=True)
    else:
        for x in residue:
            print(f"  {x}", flush=True)

    banner("STILL NEEDING A HUMAN")
    print(f"  fields_needing_seed : {(cov.get('fields_needing_seed') or [])[:10]}",
          flush=True)
    print(f"\ncoverage written to {work / 'coverage.json'}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
