"""GATE 3 / A24 — RECORD the M2.6 capture evidence a LIVE TENANT produces.

    python record_live_capture.py https://vkpowerlife.136-85-106-73.sslip.io/

M2.6 is proven on fixtures and on ``proving-grounds/acme-life``. Both are
first-party: their markup was written by the same people who wrote the capture,
and acme-life was in fact EDITED for M2.6 (it grew an accordion and a
``<details>`` so the expansion pass would have something to open). That is the
right way to prove a mechanism and it cannot answer A24's question, which is
whether the fixes hold on an application nobody shaped for them.

So this drives the PRODUCTION crawler against a live deployed tenant application
and writes down two artifacts:

    coverage.json   — the coverage account, which is where M2.6's counters live
                      (expansions_opened / expansions_skipped /
                      tab_views_recorded) and where the catalogue is folded from
    manifest.jsonl  — the manifest the emitter wrote, which is where the
                      per-control capture lives (options_total, disclosure,
                      locator, the classifier attributes)

Both go to ``Nexus_power/evidence/a24_live_capture/``. The GATE over them is
``tests/test_a24_live_tenant_capture.py``, which runs in CI on every push; this
script needs the network and a credential and therefore cannot.

IT ASSERTS ALMOST NOTHING, deliberately — the same doctrine as
``measure_network_evidence.py``. A recorder that refuses to record what it found
is a recorder that shapes the evidence to the claim. The one thing it does
insist on is that a crawl HAPPENED (states observed, no auth block), because an
empty recording is not evidence of anything and must not be committed as if it
were.

POSTURE: no boundary approvals, no walk attestation. This is somebody's live
deployment, so the crawl is given no authority to cross an irreversible control.
Capture does not need one — an application declares its controls while you are
reading its forms, not when you commit them.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
from pathlib import Path

EXPLORER = Path(__file__).resolve().parent
sys.path.insert(0, str(EXPLORER))

from app.auth import AuthWindow, Credentials          # noqa: E402
from app.crawl_constants import TRAVERSAL_FULL        # noqa: E402
from app.crawler import Budget, Crawler, GuardContext  # noqa: E402
from app.guard import load_refuse_pack                # noqa: E402
from app.main import EXPLORER_VERSION, PlaywrightBrowserPort  # noqa: E402
from tests.characterization.harness import disposable_attestation  # noqa: E402

TARGET = (sys.argv[1] if len(sys.argv) > 1
          else "https://vkpowerlife.136-85-106-73.sslip.io/")
EVIDENCE = EXPLORER.parent.parent / "evidence" / "a24_live_capture"
WORK = Path(os.environ.get("QEC_CAPTURE_OUT") or (EXPLORER / "_a24_live"))

USER = os.environ.get("QEC_MEASURE_USER", "")
PASSWORD = os.environ.get("QEC_MEASURE_PASSWORD", "")
MFA_OTP = os.environ.get("QEC_MEASURE_OTP", "")

FORWARD = ("quote", "continue", "next", "proceed", "apply", "review", "start",
           "get", "see", "calculate")


async def stub_advance_oracle(candidates, page_title, page_url):
    """The deterministic stub every gate in this repository uses. A gate that
    needs a live model is a gate that fails on network weather."""
    forward = [c for c in candidates
               if any(f in str(c.get("name") or "").lower() for f in FORWARD)]
    if not forward:
        return {}
    buttons = [c for c in forward if str(c.get("kind") or "").lower() == "button"]
    pick = (buttons or forward)[0]
    return {"name": pick.get("name"), "kind": pick.get("kind"),
            "reason": "deterministic stub: forward-shaped, button preferred"}


def _credential_payload() -> dict:
    payload: dict = {"username": USER, "password": PASSWORD}
    if MFA_OTP:
        payload["mfa"] = {"kind": "otp", "code": MFA_OTP}
    return payload


async def main() -> None:
    from playwright.async_api import async_playwright
    from app.playwright_port import context_defaults

    crawl_id, tenant = "a24-live-capture", "a24-live-tenant"
    pack = load_refuse_pack(str(EXPLORER / "app" / "refuse_pack.yaml"))
    guard_ctx = GuardContext(
        refuse_pack=pack,
        auth_window=AuthWindow(max_requests=600, window_ms=300_000),
        attestation=disposable_attestation(),
        submit_flow_approved=False,   # no submit authority on a live deployment
        walk_authorization=None,      # no crawl-time mutation authority
        idp_domains=frozenset(),
    )
    shutil.rmtree(WORK, ignore_errors=True)
    WORK.mkdir(parents=True, exist_ok=True)

    print(f"target      : {TARGET}", flush=True)
    print(f"credentials : {'member ' + USER if USER else 'NONE (public crawl)'}"
          f"{' + fixed-OTP second factor' if MFA_OTP else ''}", flush=True)
    print(f"authority   : no boundary approvals, no walk attestation", flush=True)

    pw = await async_playwright().start()
    browser = await pw.chromium.launch()
    ctx = await browser.new_context(**context_defaults())
    page = await ctx.new_page()
    try:
        crawler = Crawler(
            PlaywrightBrowserPort(page, ctx),
            crawl_id=crawl_id, tenant_id=tenant, target_url=TARGET,
            work_dir=str(WORK), refuse_pack=pack,
            budget=Budget.from_dict({"max_states": 30, "max_actions": 260,
                                     "max_requests": 3000,
                                     "max_duration_ms": 420_000}),
            explorer_version=EXPLORER_VERSION, guard_version=EXPLORER_VERSION,
            refuse_pack_version=pack.version,
            config_fingerprint="a24-live-capture",
            guard_context=guard_ctx, identity_seed="qec-a24-live-capture",
            observe_only=False, traversal=TRAVERSAL_FULL,
            advance_oracle=stub_advance_oracle,
            credentials=(Credentials.from_payload(_credential_payload())
                         if USER else None),
        )
        await crawler.run()
    finally:
        await ctx.close(); await browser.close(); await pw.stop()

    coverage = crawler._coverage.build()

    # THE ONE REFUSAL. An empty recording committed as evidence is worse than no
    # recording: it turns every assertion downstream into a statement about
    # nothing, and it looks exactly like a green run.
    assert coverage.get("states"), (
        "the crawl observed no states — refusing to write this as evidence")
    assert not coverage.get("auth_blocked"), (
        f"the crawl never got in (auth_blocked, reason="
        f"{coverage.get('auth_blocked_reason')!r}) — refusing to write a "
        f"recording of the login page as a capture of the application")

    EVIDENCE.mkdir(parents=True, exist_ok=True)
    (EVIDENCE / "coverage.json").write_text(
        json.dumps(coverage, indent=2, sort_keys=True, default=str),
        encoding="utf-8")
    shutil.copyfile(WORK / crawl_id / "manifest.jsonl",
                    EVIDENCE / "manifest.jsonl")

    import hashlib
    stamp = {
        "milestone": "GATE3-A24",
        "kind": "live deployed tenant application, crawled read-only",
        "target_url": TARGET,
        "crawl_id": crawl_id,
        "tenant_id": tenant,
        "explorer_version": EXPLORER_VERSION,
        "posture": "no boundary approvals, no walk attestation",
        "states": len(coverage.get("states") or []),
        "flows": len(coverage.get("flows") or []),
        "expansions_opened": coverage.get("expansions_opened"),
        "expansions_skipped": coverage.get("expansions_skipped"),
        "tab_views_recorded": coverage.get("tab_views_recorded"),
        "coverage_sha256": hashlib.sha256(
            (EVIDENCE / "coverage.json").read_text(encoding="utf-8")
            .encode("utf-8")).hexdigest(),
    }
    (EVIDENCE / "stamp.json").write_text(
        json.dumps(stamp, indent=2, sort_keys=True), encoding="utf-8")

    print("\nRECORDED")
    for key, value in stamp.items():
        print(f"  {key:20}: {value}")
    print(f"\n  -> {EVIDENCE}")


if __name__ == "__main__":
    asyncio.run(main())
