"""E2E stage 3 — create a properly-ATTESTED throwaway app and drive a REAL crawl,
then read the Phase 0 / Phase 1 / Phase 3 surfaces on real crawled substrate.

automationexercise.com is a public automation-practice site, so a `disposable`
attestation is honest here.
"""
import asyncio
import json
import sys
from datetime import datetime, timedelta, timezone

import httpx

from app.service_token import mint_service_jwt

BASE = "http://localhost:8093/api/v1/qec"
TENANT = "__platform__"
H = {"Authorization": f"Bearer {mint_service_jwt(TENANT)}"}
WHO = "e2e-verification@nexus"


def show(title, obj, limit=1100):
    print(f"\n===== {title} =====")
    print(json.dumps(obj, indent=2, default=str)[:limit])


async def main():
    target = sys.argv[1] if len(sys.argv) > 1 else "https://automationexercise.com/"
    expires = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
    attestation = {
        "env_kind": "disposable",
        "attested_by": WHO,
        "expires_at": expires,
        "rules_of_engagement": {"signed": True, "signed_by": WHO},
        "preflight": {"passed": True},
    }
    async with httpx.AsyncClient(timeout=120) as c:
        r = await c.post(f"{BASE}/apps", headers=H, json={
            "name": "E2E Phase Test ATTESTED (throwaway)",
            "base_url": target,
            "env_attestation": attestation,
        })
        print("CREATE_APP:", r.status_code)
        if r.status_code >= 300:
            print(r.text[:600]); return
        app_id = r.json().get("app_id")
        print("APP_ID:", app_id)

        r = await c.post(f"{BASE}/explorations", headers=H, json={"app_id": app_id})
        print("DISPATCH:", r.status_code, r.text[:500])
        if r.status_code >= 300:
            return
        print("EXPLORATION_ID:", r.json().get("exploration_id"))

        terminal = {"completed", "failed", "refused", "stalled"}
        for i in range(28):
            await asyncio.sleep(10)
            r = await c.get(f"{BASE}/apps/{app_id}", headers=H)
            crawl = r.json().get("crawl") or {}
            st, diag = crawl.get("status"), (crawl.get("diagnosis") or {}).get("code")
            print(f"[poll {i+1}] status={st} diagnosis={diag} pages={crawl.get('pages')}")
            if st in terminal:
                show("PHASE 0 — diagnosis AFTER a real crawl (LIVE)", crawl)
                break
        else:
            print("still running at poll limit — surfaces below reflect current state")

        r = await c.get(f"{BASE}/apps/{app_id}/seed-manifest", headers=H)
        m = r.json()
        print("\n===== PHASE 1 — seed-manifest (LIVE) =====")
        print("status:", m.get("status"), "| counts:", json.dumps(m.get("counts")))
        print("ask:", m.get("ask_count"), "approve:", m.get("approve_count"),
              "autonomous:", m.get("autonomous_count"))
        show("recommended (the human 1%)", m.get("recommended"), 800)
        show("full[:6]", (m.get("full") or [])[:6], 1000)

        r = await c.post(f"{BASE}/apps/{app_id}/data-agent/propose", headers=H)
        d = r.json()
        print("\n===== PHASE 3 — data-agent/propose (LIVE) =====")
        print("status:", d.get("status"), "| llm_used:", d.get("llm_used"),
              "| llm_ok:", d.get("llm_ok"), "| egress_safe:", d.get("egress_safe"))
        print("llm_error:", str(d.get("llm_error"))[:200])
        print("autonomy_delta:", d.get("autonomy_delta"), "| items:", len(d.get("items") or []))
        show("prefill (SYNTH/PICK/CARRY only — never ASK/OBSERVE)", d.get("prefill"), 500)
        print("\nAPP_ID:", app_id)


asyncio.run(main())
