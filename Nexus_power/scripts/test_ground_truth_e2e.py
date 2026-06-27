#!/usr/bin/env python3
"""Production-like end-to-end test of the Road-B ground-truth path.

Runs against a DEPLOYED stack (after applying scripts/apply_ground_truth_events.sql
and rebuilding platform-api). Exercises the real HTTP path — no mocks:

  1. POST instrumented events (incl. a fast-transition page NO frame captured, and
     a form field carrying a fake SSN) to the ingest endpoint.
  2. GET them back and assert the SSN was REDACTED at ingest (never raw at rest).
  3. Force a storyboard re-derivation so the Tier-0 overlay runs.
  4. GET the visual-evidence graph and assert a GROUND_TRUTH page_visit appears for
     the injected missing page with the EXACT URL (.html preserved).

Usage:
  python scripts/test_ground_truth_e2e.py \
      --api-base http://34.21.232.80:3000 \
      --token "<JWT>" \
      --artifact-id <an artifact that has frames>

Exit 0 on all-pass, 1 otherwise.
"""
from __future__ import annotations

import argparse
import sys
import time

import httpx

OK, FAIL = "PASS", "FAIL"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--api-base", required=True, help="e.g. http://host:3000 (gateway) — /api/v1 is appended")
    ap.add_argument("--token", required=True, help="Bearer JWT for an authenticated user")
    ap.add_argument("--artifact-id", required=True)
    ap.add_argument("--timeout", type=float, default=300.0)
    args = ap.parse_args()

    base = args.api_base.rstrip("/") + "/api/v1/artifacts/" + args.artifact_id
    h = {"authorization": f"Bearer {args.token}"}
    client = httpx.Client(timeout=args.timeout, headers=h, follow_redirects=True)
    passed = True

    def check(name: str, cond: bool, detail: str = "") -> None:
        nonlocal passed
        passed = passed and cond
        print(f"  [{OK if cond else FAIL}] {name}{(' — ' + detail) if detail else ''}")

    # 1) Ingest a sidecar: inventory, a fast cart.html (no frame), checkout-step-one,
    #    plus a form field carrying a fake SSN (must be redacted at ingest).
    events = [
        {"timestamp_ms": 1000, "kind": "navigate", "url": "https://www.saucedemo.com/inventory.html",
         "url_host": "www.saucedemo.com", "url_path": "/inventory.html"},
        {"timestamp_ms": 12000, "kind": "navigate", "url": "https://www.saucedemo.com/cart.html",
         "url_host": "www.saucedemo.com", "url_path": "/cart.html"},
        {"timestamp_ms": 24000, "kind": "navigate", "url": "https://www.saucedemo.com/checkout-step-one.html",
         "url_host": "www.saucedemo.com", "url_path": "/checkout-step-one.html"},
        {"timestamp_ms": 25000, "kind": "type", "target_label": "Social Security Number",
         "value": "my ssn is 123-45-6789 ok", "target_kind": "text_field"},
    ]
    r = client.post(base + "/ground-truth", json={"session_id": "e2e", "recorder_version": "test", "events": events})
    check("ingest returns 200", r.status_code == 200, f"status={r.status_code} body={r.text[:200]}")
    if r.status_code == 200:
        check("ingested all events", r.json().get("ingested") == len(events), str(r.json()))

    # 2) Read back — the SSN must be redacted, the nav URLs intact (.html preserved).
    r = client.get(base + "/ground-truth")
    if r.status_code == 200:
        evs = r.json().get("events", [])
        ssn_rows = [e for e in evs if e.get("kind") == "type"]
        check("SSN value redacted at rest", bool(ssn_rows) and "123-45-6789" not in (ssn_rows[0].get("value") or ""),
              f"value={ssn_rows[0].get('value') if ssn_rows else None!r}")
        paths = {e.get("url_path") for e in evs}
        check(".html preserved in stored URL", "/cart.html" in paths, f"paths={sorted(paths)}")
    else:
        check("read-back returns 200", False, f"status={r.status_code}")

    # 3) Force a re-derivation so the Tier-0 overlay runs over the frames.
    rr = client.post(base + "/storyboard/regenerate")
    check("regenerate accepted", rr.status_code in (200, 202), f"status={rr.status_code}")
    # poll until derivation settles
    deadline = time.time() + args.timeout
    deriving = True
    while time.time() < deadline:
        s = client.get(base + "/storyboard")
        if s.status_code == 200 and not (s.json().get("summary", {}) or {}).get("deriving"):
            deriving = False
            break
        time.sleep(4)
    check("derivation completed", not deriving)

    # 4) The graph's page_visits must include a GROUND_TRUTH row for the injected
    #    missing page (cart.html) — the headline structural fix.
    g = client.get(base + "/visual-evidence-graph", params={"include_storyboard_overlays": "true"})
    if g.status_code == 200:
        visits = g.json().get("page_visits", []) or []
        gt = [v for v in visits if (v.get("source") == "ground_truth")]
        cart = [v for v in gt if "/cart.html" in (v.get("url_path") or "")]
        check("GROUND_TRUTH page_visits present", bool(gt), f"count={len(gt)}")
        check("injected missing cart.html recovered (GROUND_TRUTH)", bool(cart),
              f"gt_paths={[v.get('url_path') for v in gt]}")
    else:
        check("graph returns 200", False, f"status={g.status_code}")

    print("\nRESULT:", "ALL PASS ✅" if passed else "SOME FAILED ❌")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
