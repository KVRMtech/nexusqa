"""One real crawl, in its own OS process, so it can be SIGKILLed mid-crossing.

This is deliberately a SCRIPT and not a pytest test. A35's fault injection has
to kill the process that owns the crossing, and killing the pytest worker would
take the assertions, the report and the parent's bookkeeping with it. Splitting
the crawl into a child means the parent survives to read the ledger and to run
the resume.

Nothing here is a stand-in except the tier-3 advance oracle, which is the same
deterministic substitution ``test_boundary_crossing_gate.py`` documents and uses
— a gate that needed a live model would fail on network weather. Everything on
the path that A35 actually measures (the crossing ledger, the manifest write,
the guard, the boundary authorisation, real Chromium, the real application) is
production code.
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

_HERE = Path(__file__).resolve()
SERVICE_ROOT = _HERE.parents[2]
for _p in (str(SERVICE_ROOT), str(_HERE.parent), str(_HERE.parents[1])):
    if _p not in sys.path:
        sys.path.insert(0, _p)

CRAWL_ID = "gate4-a35-crossing"
TENANT_ID = "gate4-a35"
BOUNDARY_CONTROL = "Bind policy"
_FORWARD = ("quote", "continue", "next", "proceed", "apply", "review", "start",
            "see", "bind")


async def _stub_advance_oracle(candidates: Sequence[Mapping[str, Any]],
                               page_title: str, page_url: str) -> dict[str, Any]:
    names = [str(c.get("name") or "") for c in candidates]
    for want_button in (True, False):
        for index, (name, control) in enumerate(zip(names, candidates)):
            if (str(control.get("kind") or "") == "button") is not want_button:
                continue
            if any(word in name.lower() for word in _FORWARD):
                return {"status": "picked", "index": index, "signature": "a35-forward"}
    return {"status": "none", "signature": "a35-none"}


def _walk_authorization(crawl_id: str, tenant_id: str, target_url: str):
    from _attest_kit import Issuer

    from app.attest import ProofReplayGuard, verify_provisioning_proof
    from app.walk_persist import MutationAuditLog, WalkAuthorization

    issuer = Issuer()
    scheme, _, rest = target_url.partition("//")
    origin = f"{scheme}//{rest.split('/')[0]}"
    verdict = verify_provisioning_proof(
        {"proof": issuer.proof(crawl_id=crawl_id, tenant_id=tenant_id,
                               target_origin=origin,
                               max_walk_mutations_per_step=8),
         "revocations": issuer.revocations()},
        trust=issuer.trust(), crawl_id=crawl_id, tenant_id=tenant_id,
        target_url=target_url, replay_guard=ProofReplayGuard())
    if not verdict.authorized:
        raise SystemExit(f"could not build a walk authorization: {verdict.reason}")
    return WalkAuthorization.from_verdict(
        verdict, workflow_id=crawl_id, audit=MutationAuditLog())


async def _run() -> int:
    from playwright.async_api import async_playwright

    from tests.characterization.harness import disposable_attestation

    from app.auth import AuthWindow
    from app.crawl_constants import TRAVERSAL_FULL
    from app.crawler import Budget, Crawler, GuardContext
    from app.guard import load_refuse_pack
    from app.main import EXPLORER_VERSION, PlaywrightBrowserPort

    work_dir = Path(os.environ["A35_WORK_DIR"])
    url = os.environ["A35_URL"]
    resume = os.environ.get("A35_RESUME") == "1"

    pack = load_refuse_pack(str(SERVICE_ROOT / "app" / "refuse_pack.yaml"))
    guard_ctx = GuardContext(
        refuse_pack=pack,
        auth_window=AuthWindow(max_requests=400, window_ms=240_000),
        attestation=disposable_attestation(),
        submit_flow_approved=True,
        walk_authorization=_walk_authorization(CRAWL_ID, TENANT_ID, url),
        idp_domains=frozenset(),
    )

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()
        crawler = Crawler(
            PlaywrightBrowserPort(page, context),
            crawl_id=CRAWL_ID, tenant_id=TENANT_ID, target_url=url,
            work_dir=str(work_dir), refuse_pack=pack,
            budget=Budget.from_dict({"max_states": 20, "max_actions": 120,
                                     "max_requests": 2000,
                                     "max_duration_ms": 180_000}),
            explorer_version=EXPLORER_VERSION, guard_version=EXPLORER_VERSION,
            refuse_pack_version=pack.version, config_fingerprint="a35",
            guard_context=guard_ctx, identity_seed="qec-a35",
            observe_only=False, traversal=TRAVERSAL_FULL,
            advance_oracle=_stub_advance_oracle,
            # The narrowest grant the system offers: ONE control, ONCE. If the
            # resumed crawl crossed again it would have to do so having spent
            # this single crossing, which is exactly the failure A35 hunts.
            boundary_approvals=[{"control": BOUNDARY_CONTROL,
                                 "approved_by": "gate4-a35",
                                 "max_crossings": 1}],
            resume=resume,
        )
        summary = await crawler.run()
        cov = crawler._coverage.build()
        print(f"A35_CHILD resume={resume} "
              f"crossings={cov.get('boundaries_crossed')} "
              f"states={getattr(summary, 'states', '?')}", flush=True)
        try:
            await browser.close()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_run()))
