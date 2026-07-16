"""E2E driver for the Phase 0/1/3 surfaces — runs INSIDE the nexus-qe-central container.

Stage 1: create a throwaway app (public test site), then read the NEW surfaces before
any crawl — proving the diagnosis + seed-manifest endpoints are live and honest.
"""
import asyncio
import json
import sys

import httpx

from app.service_token import mint_service_jwt

BASE = "http://localhost:8093/api/v1/qec"
TENANT = "__platform__"
H = {"Authorization": f"Bearer {mint_service_jwt(TENANT)}"}


def show(title, obj, limit=900):
    print(f"\n===== {title} =====")
    print(json.dumps(obj, indent=2, default=str)[:limit])


async def main():
    target = sys.argv[1] if len(sys.argv) > 1 else "https://automationexercise.com/"
    async with httpx.AsyncClient(timeout=90) as c:
        r = await c.post(f"{BASE}/apps", headers=H, json={
            "name": "E2E Phase Test (throwaway)",
            "base_url": target,
        })
        print("CREATE_APP:", r.status_code)
        if r.status_code >= 300:
            print(r.text[:800])
            return
        app_id = r.json().get("app_id")
        print("APP_ID:", app_id)

        r = await c.get(f"{BASE}/apps/{app_id}", headers=H)
        body = r.json()
        show("GET /apps/{id} -> crawl (Phase 0 diagnosis, BEFORE any crawl)", body.get("crawl"))
        print("onboarding:", json.dumps(body.get("onboarding"), default=str)[:200])
        print("status:", body.get("status"))

        r = await c.get(f"{BASE}/apps/{app_id}/seed-manifest", headers=H)
        show("GET /apps/{id}/seed-manifest (Phase 1, BEFORE any crawl)", r.json(), 500)

        print("\nAPP_ID_FOR_NEXT_STAGE:", app_id)


asyncio.run(main())
