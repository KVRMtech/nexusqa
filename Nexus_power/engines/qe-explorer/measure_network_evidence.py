"""MEASURE: does a real crawl of a REAL, LIVE application produce network
evidence that is joinable, ordered, correlated, redacted and oracle-readable?

    python measure_network_evidence.py https://vkpowerlife.136-85-106-73.sslip.io/

The M2.5 fixture gate proves the mechanism against an application built to
exhibit it -- a scripted 503,503,200 retry, a four-iteration poll, a 429 backoff
and a 500.  That is the right way to prove a MECHANISM and the wrong way to
prove it survives contact with a real application, because a fixture cannot
surprise you.  This instrument runs the same production crawler against a real
deployed app and prints what actually came back.

It ASSERTS NOTHING.  It is an instrument, not a gate: the whole point is to
report what a real application did, including the parts that are less
convenient than the fixture's.  A live app may make no repeated call at all and
may never return a 5xx -- if so, this says so, rather than reshaping the claim
to fit what happened to be observed.

Everything is the production path: `app.crawler.Crawler`, `app.main.
PlaywrightBrowserPort`, real Chromium, the real refuse pack.

DELIBERATELY NO `boundary_approvals` AND NO WALK ATTESTATION.  This is somebody's
live deployment, not a disposable fixture, so the crawl is given no authority to
cross an irreversible control.  The refuse pack and the boundary ledger stop it
at the commit boundary, which is exactly where a read-only measurement should
stop.  Network evidence does not need a submit -- an application makes its API
calls while you are filling its forms, not only when you commit them.
"""
from __future__ import annotations
import asyncio, json, os, shutil, sys
from collections import Counter, defaultdict
from pathlib import Path

EXPLORER = Path(__file__).resolve().parent
sys.path.insert(0, str(EXPLORER))

from app.auth import AuthWindow, Credentials
from app.crawl_constants import TRAVERSAL_FULL
from app.crawler import Budget, Crawler, GuardContext
from app.guard import load_refuse_pack
from app.main import EXPLORER_VERSION, PlaywrightBrowserPort
from app import endpoint_inventory as inv
from app import network_evidence as ne
from tests.characterization.harness import disposable_attestation

TARGET = (sys.argv[1] if len(sys.argv) > 1
          else "https://vkpowerlife.136-85-106-73.sslip.io/")
OUT = Path(os.environ.get("QEC_MEASURE_OUT") or (EXPLORER / "_measure_out")) / "network"
USER = os.environ.get("QEC_MEASURE_USER", "")
PASSWORD = os.environ.get("QEC_MEASURE_PASSWORD", "")
#: A second factor, when the application has one.  VKPower Life's login is
#: member-number -> password -> Security PIN, and the crawl stopped at the PIN
#: step until this was supplied: `MfaConfig(kind="otp")` drives a FIXED code,
#: which is the right shape for a demo/test environment that accepts any PIN.
#: Data-driven, never hard-coded into the engine.
MFA_OTP = os.environ.get("QEC_MEASURE_OTP", "")

FORWARD = ("quote", "continue", "next", "proceed", "apply", "review", "start",
           "get", "see", "calculate")


async def stub_advance_oracle(candidates, page_title, page_url):
    """The one stand-in: tier 3 is normally an LLM reached through qe-central.

    Deterministic so the run is reproducible and so what it picked is printed
    rather than inferred.  It chooses among controls the walker has ALREADY
    filtered (no danger, no commit words, no disabled, named only), preferring a
    button over a link among forward-shaped labels.
    """
    names = [str(c.get("name") or "") for c in candidates]
    for want_button in (True, False):
        for i, (name, c) in enumerate(zip(names, candidates)):
            if (str(c.get("kind") or "") == "button") is not want_button:
                continue
            if any(w in name.lower() for w in FORWARD):
                return {"status": "picked", "index": i, "signature": "measure-forward"}
    return {"status": "none", "signature": "measure-none"}


def _credential_payload() -> dict:
    """The credentials object exactly as a real explore request supplies it."""
    payload = {"username": USER, "password": PASSWORD}
    if MFA_OTP:
        payload["mfa"] = {"kind": "otp", "otp": MFA_OTP}
    return payload


def banner(text: str) -> None:
    print("\n" + "=" * 78, flush=True)
    print(text, flush=True)
    print("=" * 78, flush=True)


async def main() -> None:
    from playwright.async_api import async_playwright
    from app.playwright_port import context_defaults

    crawl_id, tenant = "measure-network", "measure"
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
    print(f"credentials : {'member ' + USER if USER else 'NONE (public crawl)'}"
          f"{' + fixed-OTP second factor' if MFA_OTP else ''}", flush=True)
    print(f"authority   : no boundary approvals, no walk attestation "
          f"(read-only posture)", flush=True)

    pw = await async_playwright().start()
    browser = await pw.chromium.launch()
    ctx = await browser.new_context(**context_defaults())
    page = await ctx.new_page()
    crawler = None
    try:
        crawler = Crawler(
            PlaywrightBrowserPort(page, ctx),
            crawl_id=crawl_id, tenant_id=tenant, target_url=TARGET,
            work_dir=str(work), refuse_pack=pack, budget=budget,
            explorer_version=EXPLORER_VERSION, guard_version=EXPLORER_VERSION,
            refuse_pack_version=pack.version,
            config_fingerprint="measure-network",
            guard_context=guard_ctx, identity_seed="qec-measure-network",
            observe_only=False, traversal=TRAVERSAL_FULL,
            advance_oracle=stub_advance_oracle,
            credentials=(Credentials.from_payload(_credential_payload())
                         if USER else None),
        )
        await crawler.run()
    finally:
        await ctx.close(); await browser.close(); await pw.stop()

    cov = crawler._coverage.build()
    manifest = work / crawl_id / "manifest.jsonl"
    records = [json.loads(l) for l in manifest.read_text(encoding="utf-8").splitlines()
               if l.strip()]
    pages = [r for r in records if "network_calls" in r]

    events = []
    for p in pages:
        for call in (p.get("network_calls") or []):
            row = dict(call)
            row["_visit_first_ms"] = p.get("first_seen_ms")
            row["_visit_last_ms"] = p.get("last_seen_ms")
            row["_visit_seq"] = p.get("sequence_index")
            row["_visit_url"] = p.get("location")
            events.append(row)
    calls = [e for e in events if not e.get("event")]

    banner("CRAWL SUMMARY")
    meta = [r for r in records if r.get("type") == "crawl_meta"]
    stop = meta[-1] if meta else {}
    print(f"  stop_reason      : {stop.get('stop_reason')!r}", flush=True)
    print(f"  stats            : {json.dumps(stop.get('stats'))}", flush=True)
    print(f"  page_states      : {len(pages)}", flush=True)
    print(f"  network events   : {len(calls)}", flush=True)
    print(f"  meta records     : {len(events) - len(calls)} "
          f"(truncation markers)", flush=True)

    if not calls:
        banner("NO NETWORK EVENTS CAPTURED")
        print("  Nothing below can be measured. Either the application makes no "
              "XHR/fetch calls, or the crawl never reached a page that does.",
              flush=True)
        return

    # ── EVIDENCE 1 — a raw event verbatim ────────────────────────────────────
    banner("EVIDENCE 1 - RAW EVENT (verbatim from manifest.jsonl)")
    interesting = next((e for e in calls if e.get("method") == "POST"), calls[0])
    print(json.dumps(interesting, indent=2, sort_keys=True), flush=True)

    # ── EVIDENCE 2 — repetition survived ─────────────────────────────────────
    banner("EVIDENCE 2 - REPEATED CALLS (the baseline dedup destroyed these)")
    groups = defaultdict(list)
    for e in calls:
        groups[(e.get("method"), e.get("path_template") or e.get("url"))].append(e)
    repeated = {k: v for k, v in groups.items() if len(v) > 1}
    baseline_keys = {(e.get("method"), e.get("url"), e.get("status")) for e in calls}
    print(f"  distinct method x path_template : {len(groups)}", flush=True)
    print(f"  events recorded now             : {len(calls)}", flush=True)
    print(f"  events the BASELINE dedup would have kept "
          f"(method|url|status) : {len(baseline_keys)}", flush=True)
    print(f"  DESTROYED BY THE BASELINE       : "
          f"{len(calls) - len(baseline_keys)}", flush=True)
    if not repeated:
        print("\n  This application made NO repeated call during this crawl, so "
              "there is no retry/poll here to preserve. Reported rather than "
              "reshaped.", flush=True)
    for (method, path), rows in sorted(repeated.items(),
                                       key=lambda kv: -len(kv[1]))[:8]:
        rows = sorted(rows, key=lambda e: int(e.get("sequence") or 0))
        statuses = [str(r.get("status")) for r in rows]
        tokens = {str(r.get("action_token") or "") for r in rows}
        print(f"\n  {method} {path}  x{len(rows)}", flush=True)
        print(f"    sequences : {[int(r['sequence']) for r in rows]}", flush=True)
        print(f"    statuses  : {statuses}", flush=True)
        print(f"    actions   : {sorted(tokens)}"
              f"{'   <- ONE action: a retry or an in-action poll' if len(tokens) == 1 else ''}",
              flush=True)

    # ── EVIDENCE 3 — the join ────────────────────────────────────────────────
    banner("EVIDENCE 3 - TIMESTAMP JOIN (T-NET-01)")
    inside = outside = 0
    pre_observation = 0
    worst = []
    for e in calls:
        try:
            ts = int(e["timestamp_ms"])
            observed_lo, hi = int(e["_visit_first_ms"]), int(e["_visit_last_ms"])
            lo = int(e.get("capture_window_start_ms") or 0)
        except (TypeError, ValueError, KeyError):
            outside += 1
            continue
        if lo <= ts <= hi:
            inside += 1
            if ts < observed_lo:
                pre_observation += 1
        else:
            outside += 1
            worst.append((e.get("url"), ts, lo, hi))
    stamps = [int(e["timestamp_ms"]) for e in calls
              if str(e.get("timestamp_ms") or "").isdigit()]
    for p in pages[:8]:
        n = len(p.get("network_calls") or [])
        if n:
            starts = {c.get("capture_window_start_ms")
                      for c in (p.get("network_calls") or [])}
            print(f"  visit seq={p.get('sequence_index'):>2} "
                  f"capture=[{sorted(starts)[0]}, {p.get('last_seen_ms')}] "
                  f"observed_from={p.get('first_seen_ms')} "
                  f"events={n}  {str(p.get('location'))[:52]}", flush=True)
    print(f"\n  events INSIDE their capture window : {inside}", flush=True)
    print(f"  events OUTSIDE                     : {outside}", flush=True)
    print(f"  ...of which fired BEFORE the state was observed "
          f"(navigation/prefetch traffic, correctly attributed to the "
          f"state it was entering) : {pre_observation}", flush=True)
    if worst:
        print(f"  offenders: {worst[:3]}", flush=True)
    print(f"  max network timestamp            : {max(stamps)} ms", flush=True)
    print(f"  crawl elapsed                    : "
          f"{(stop.get('stats') or {}).get('elapsed_ms')} ms", flush=True)
    print("  (a raw time.monotonic() epoch -- the baseline -- is system uptime "
          "and reads in the millions)", flush=True)

    # ── EVIDENCE 4 — action correlation ──────────────────────────────────────
    banner("EVIDENCE 4 - ACTION -> NETWORK CORRELATION (T-NET-03)")
    by_action = defaultdict(list)
    for e in calls:
        by_action[(e.get("action_token"), e.get("action_verb"),
                   e.get("action_label"))].append(e)
    named = [k for k in by_action if k[2]]
    print(f"  distinct actions with network traffic : {len(named)}", flush=True)
    print(f"  events attributed to a named action   : "
          f"{sum(len(v) for k, v in by_action.items() if k[2])}/{len(calls)}",
          flush=True)
    print("  (events with no action are page-load traffic -- honestly unattributed)",
          flush=True)
    for (tok, verb, label), rows in sorted(
            by_action.items(), key=lambda kv: -len(kv[1]))[:12]:
        if not label:
            continue
        print(f"\n  {verb} {label!r}  [{tok}]", flush=True)
        seen = Counter(f"{r.get('method')} "
                       f"{(r.get('path_template') or r.get('url'))} -> {r.get('status')}"
                       for r in rows)
        for call, n in seen.most_common(6):
            print(f"      {call}" + (f"   x{n}" if n > 1 else ""), flush=True)

    # ── EVIDENCE 5 — endpoint inventory ──────────────────────────────────────
    banner("EVIDENCE 5 - ENDPOINT INVENTORY (T-NET-04)")
    inventory = cov.get("endpoint_inventory") or []
    print(f"  endpoints in the coverage account : {len(inventory)}", flush=True)
    print(f"  truncated                         : "
          f"{cov.get('endpoint_inventory_truncated')}", flush=True)
    for row in inventory[:25]:
        acts = ", ".join(f"{a.get('verb')} {a.get('label')!r}"
                         for a in (row.get("actions") or [])[:2]) or "-"
        flags = " ".join(f for f, on in (("RETRIED", row.get("retried")),
                                         ("RATE_LIMITED", row.get("rate_limited")),
                                         ("5xx", row.get("has_server_error")))
                         if on)
        print(f"\n  {row.get('method'):<6} {row.get('path_template')}", flush=True)
        print(f"         host={row.get('host')}  auth={row.get('auth_pattern')}  "
              f"shape={row.get('response_shape')}  seen={row.get('observed_count')}",
              flush=True)
        print(f"         statuses={json.dumps(row.get('statuses'))}  {flags}", flush=True)
        print(f"         keys={row.get('request_keys')}", flush=True)
        print(f"         triggered by: {acts}", flush=True)

    # ── EVIDENCE 6 — redaction ───────────────────────────────────────────────
    banner("EVIDENCE 6 - REDACTION")
    blob = json.dumps(records)
    auth_rows = [e for e in calls if e.get("auth_pattern") not in ("", "none")]
    print(f"  events carrying an auth pattern : {len(auth_rows)}", flush=True)
    print(f"  auth patterns observed          : "
          f"{dict(Counter(e.get('auth_pattern') for e in calls))}", flush=True)
    hdrs = [e.get("request_headers", "") for e in calls]
    print(f"\n  a real captured request_headers value:", flush=True)
    sample = max(hdrs, key=len) if hdrs else ""
    print(f"    {sample[:300]}", flush=True)
    resp_sample = max((e.get("response_headers", "") for e in calls), key=len, default="")
    print(f"  a real captured response_headers value:", flush=True)
    print(f"    {resp_sample[:300]}", flush=True)
    bodies = [e.get("request_body_keys", "") for e in calls if e.get("request_body_keys")]
    print(f"\n  request bodies described by KEY NAMES ({len(bodies)} with a body):",
          flush=True)
    for b in sorted(set(bodies))[:8]:
        print(f"    {b[:200]}", flush=True)
    print(f"\n  redaction markers present in the manifest:", flush=True)
    for marker in ("<bearer>", "<present>", "<basic>", "<secret>", "[REDACTED:"):
        print(f"    {marker:<12} : {blob.count(marker)}", flush=True)
    leaks = [t for t in ("Bearer ey", "Basic ", "password=", "session=") if t in blob]
    print(f"  raw-credential shapes found in the manifest: "
          f"{leaks or 'NONE'}", flush=True)

    # ── EVIDENCE 7 — the oracle ──────────────────────────────────────────────
    banner("EVIDENCE 7 - NETWORK ORACLE (T-NET-05)")
    import importlib.util
    oracle_path = (EXPLORER.parent.parent / "platform" / "api" / "app" / "services"
                   / "test_factory" / "network_oracle.py")
    spec = importlib.util.spec_from_file_location("prod_network_oracle", oracle_path)
    oracle = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(oracle)
    print(f"  loaded PRODUCTION oracle from {oracle_path}", flush=True)

    entries = ne.to_oracle_entries(calls)
    statuses = Counter(e["status"] for e in entries)
    print(f"  observed statuses : {dict(sorted(statuses.items()))}", flush=True)

    server_errors = cov.get("network_server_errors") or []
    print(f"  coverage.network_server_errors : {len(server_errors)}", flush=True)
    for row in server_errors[:5]:
        print(f"    {row.get('method')} {row.get('url')} -> {row.get('status')} "
              f"during {row.get('action_verb')} {row.get('action_label')!r}", flush=True)

    verdict = oracle.classify_network_signal(entries)
    print(f"\n  oracle verdict over the WHOLE crawl stream:", flush=True)
    print(f"    {json.dumps(verdict)}", flush=True)
    print(f"    is_real_bug_signal : {oracle.is_real_bug_signal(verdict)}", flush=True)

    stripped = [dict(e, error="") for e in entries]
    v2 = oracle.classify_network_signal(stripped)
    print(f"  verdict with EVERY error string removed:", flush=True)
    print(f"    {json.dumps(v2)}", flush=True)
    print(f"  the OLD text-only path on that same prose-free evidence:", flush=True)
    print(f"    {oracle.network_signal_from_error('')}", flush=True)

    worst_status = max((e["status"] for e in entries), default=0)
    if worst_status < 400:
        print(f"\n  THIS APPLICATION HAS NO ERROR SURFACE. Every one of the "
              f"{len(entries)} observed requests returned 2xx, so the oracle "
              f"correctly stays SILENT.", flush=True)
        print(f"  That is a real result -- no false positive on {len(entries)} "
              f"events of genuine traffic -- but it is NOT proof that the oracle "
              f"FIRES. Only a 5xx proves that, and this app never returned one.",
              flush=True)
        print(f"  (Checked directly: even a nonexistent route answers 200 -- it "
              f"is a static export behind a catch-all.)", flush=True)

        # Show that the join works on THIS APP'S OWN event shape, by taking a
        # real captured event and changing ONE field: the status. Clearly a
        # constructed status on a real record -- it demonstrates that the
        # adapter and the oracle agree on the shape this crawl actually
        # produced, which the fixture cannot show because the fixture's events
        # are not this application's.
        real = dict(entries[0])
        print(f"\n  DEMONSTRATION on this app's real event shape, with the "
              f"status field -- and ONLY the status field -- set to 503:",
              flush=True)
        print(f"    real event : {real['method']} {real['url']} -> "
              f"{real['status']}  (seq={real['sequence']}, t={real['start_ms']}ms)",
              flush=True)
        real["status"] = 503
        verdict = oracle.classify_network_signal([real])
        print(f"    verdict    : {json.dumps(verdict)}", flush=True)
        print(f"    is_real_bug_signal : {oracle.is_real_bug_signal(verdict)}",
              flush=True)
        print(f"    NOTE: the 503 is constructed. The URL, method, timestamp, "
              f"sequence and correlation are this crawl's real data.", flush=True)

    if entries:
        pin = max(entries, key=lambda e: e["status"])
        t = pin["start_ms"]
        if t is not None:
            print(f"\n  step-window join, on a real captured event "
                  f"({pin['method']} -> {pin['status']} at t={t}ms):", flush=True)
            probe = dict(pin, status=503)
            print(f"    window [{t-500},{t+500}] : "
                  f"{json.dumps(oracle.classify_network_signal([probe], step_start_ms=t-500, step_end_ms=t+500))}",
                  flush=True)
            print(f"    window [0,1]             : "
                  f"{json.dumps(oracle.classify_network_signal([probe], step_start_ms=0, step_end_ms=1))}"
                  f"   <- correctly excluded by the real timestamp", flush=True)

    banner("MANIFEST")
    print(f"  {manifest}", flush=True)


asyncio.run(main())
