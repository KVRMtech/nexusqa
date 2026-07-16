"""E2E stage 2 — dispatch a REAL crawl on the throwaway app, poll to terminal, then
read the Phase 0 diagnosis / Phase 1 seed-manifest / Phase 3 data-agent surfaces on
real crawled data.
"""
import asyncio
import json
import sys

import httpx

from app.service_token import mint_service_jwt

BASE = "http://localhost:8093/api/v1/qec"
TENANT = "__platform__"
H = {"Authorization": f"Bearer {mint_service_jwt(TENANT)}"}


def show(title, obj, limit=1200):
    print(f"\n===== {title} =====")
    print(json.dumps(obj, indent=2, default=str)[:limit])


async def main():
    app_id = sys.argv[1]
    async with httpx.AsyncClient(timeout=120) as c:
        # Pre-crawl diagnosis (proves the never-crawled branch now emits one).
        r = await c.get(f"{BASE}/apps/{app_id}", headers=H)
        show("Phase 0 diagnosis BEFORE crawl (never-crawled branch)", r.json().get("crawl"), 700)

        # Dispatch the real crawl.
        r = await c.post(f"{BASE}/explorations", headers=H, json={"app_id": app_id})
        print("\nDISPATCH:", r.status_code, r.text[:400])
        if r.status_code >= 300:
            return
        exp_id = r.json().get("exploration_id")
        print("EXPLORATION_ID:", exp_id)

        # Poll to terminal (bounded).
        terminal = {"completed", "failed", "refused", "stalled"}
        for i in range(60):
            await asyncio.sleep(10)
            r = await c.get(f"{BASE}/apps/{app_id}", headers=H)
            crawl = r.json().get("crawl") or {}
            st = crawl.get("status")
            diag = (crawl.get("diagnosis") or {}).get("code")
            print(f"[poll {i+1}] status={st} diagnosis={diag} pages={crawl.get('pages')}")
            if st in terminal:
                show("Phase 0 diagnosis AFTER crawl (LIVE)", crawl)
                break
        else:
            print("TIMED OUT waiting for terminal")

        # Phase 1 — seed manifest on real crawled substrate.
        r = await c.get(f"{BASE}/apps/{app_id}/seed-manifest", headers=H)
        m = r.json()
        print("\n===== Phase 1 seed-manifest (LIVE) =====")
        print("status:", m.get("status"), "| artifact:", (m.get("artifact_id") or "")[:12])
        print("counts:", json.dumps(m.get("counts")))
        print("ask:", m.get("ask_count"), "approve:", m.get("approve_count"),
              "autonomous:", m.get("autonomous_count"))
        show("recommended (the human 1%)", m.get("recommended"), 900)
        show("full[:5]", (m.get("full") or [])[:5], 900)
        show("prefill", m.get("prefill"), 500)

        # Phase 3 — data agent propose.
        r = await c.post(f"{BASE}/apps/{app_id}/data-agent/propose", headers=H)
        d = r.json()
        print("\n===== Phase 3 data-agent/propose (LIVE) =====")
        print("status:", d.get("status"), "| llm_used:", d.get("llm_used"),
              "| llm_ok:", d.get("llm_ok"), "| egress_safe:", d.get("egress_safe"))
        print("llm_error:", str(d.get("llm_error"))[:160])
        print("autonomy_delta:", d.get("autonomy_delta"), "| items:", len(d.get("items") or []))
        show("recommended (ASK+APPROVE)", d.get("recommended"), 700)
        show("prefill", d.get("prefill"), 400)


asyncio.run(main())
