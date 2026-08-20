# M2.5 — Network Evidence: results and proof

Companion to [`M2_5_NETWORK_EVIDENCE_BASELINE.md`](M2_5_NETWORK_EVIDENCE_BASELINE.md),
which records the schema and the seven defects **before** any change.

Every number below is read from the manifest a real Chromium crawl wrote, not
from a hand-built example. The crawl is
`tests/browser/test_network_stream_gate.py` against fixture
`30-network-retry-poll-ratelimit`, whose `/__net/` endpoints answer a scripted
`503,503,200` / `200×4` / `429,429,200` / `500`.

---

## The event schema, after

Nine fields became twenty-eight. The three that matter are the three joins.

| field | purpose | task |
|---|---|---|
| `sequence` | crawl-wide ordinal assigned **at capture** | T-NET-02 |
| `timestamp_ms` | the **crawl clock** (`emit.MonotonicClock`) | T-NET-01 |
| `action_token` / `action_label` / `action_verb` | the UI action in flight | T-NET-03 |
| `page_token` | which page (M1.5 registry) | join |
| `path_template` | normalized route | T-NET-04 |
| `request_headers` / `response_headers` | allow-listed, value-redacted | T-NET-02 |
| `request_body_bytes` / `_keys` / `_source` | body **shape**, never content | T-NET-02 |
| `auth_pattern`, `response_shape`, `shape_source` | catalog characteristics | T-NET-04 |
| `failed`, `error` | a request that got **no response** | T-NET-05 |

---

## Evidence 1 — a raw event, verbatim from `manifest.jsonl`

```json
{
  "action_label": "Get quote", "action_token": "a2", "action_verb": "click",
  "auth_pattern": "bearer", "error": "", "failed": "false",
  "has_query": "false", "method": "POST", "path_template": "/__net/quote",
  "request_body_bytes": "74",
  "request_body_keys": "age,state,coverage,<secret>",
  "request_body_source": "json",
  "request_headers": "authorization=<bearer>; content-type=application/json; x-request-id=req-quote-001",
  "request_mime": "application/json", "resource_type": "fetch",
  "response_bytes": "48",
  "response_headers": "cache-control=no-store, must-revalidate; content-length=48; content-type=application/json; x-request-id=srv-quote-1",
  "response_mime": "application/json", "response_shape": "json",
  "sequence": "1", "shape_source": "media_type",
  "status": "503", "timestamp_ms": "6016",
  "url": "http://127.0.0.1:56956/__net/quote"
}
```

## Evidence 2 — the retry sequence (three retries stayed three ordered events)

```
  seq   ts_ms  status  action_token  action_label
    1    6016     503            a2  Get quote
    2    6046     503            a2  Get quote
    3    6046     200            a2  Get quote
```

Poll — four requests identical in method, URL **and status**, the case a dedup
destroys completely because no field distinguishes them:

```
  seq=4 ts=7875 status=200      seq=6 ts=7875 status=200
  seq=5 ts=7875 status=200      seq=7 ts=7875 status=200
```

Rate limiting, with the server's own backoff headers preserved:

```
  seq=8  status=429  retry-after=1; x-ratelimit-limit=3; x-ratelimit-remaining=0
  seq=9  status=429  retry-after=1; x-ratelimit-limit=3; x-ratelimit-remaining=0
  seq=10 status=200
```

**11 events recorded where the baseline would have recorded 5.**

## Evidence 3 — timestamps join

```
visit sequence_index=0   window=[3000, 12000]   events=11
events inside their visit window : 11
events OUTSIDE                   : 0
max network timestamp            : 11375 ms
```

The maximum is bounded by the crawl's own duration. A raw `time.monotonic()`
reading — the baseline — is system uptime and reads in the millions, which is
why *no* baseline event could fall inside *any* visit window.

## Evidence 4 — action → network correlation

"Which click caused this POST?", answered from the evidence alone:

```
click 'Get quote'      [a2]   POST /quote   -> 503, 503, 200
click 'Check status'   [a3]   GET  /status  -> 200, 200, 200, 200
click 'Refresh limits' [a4]   GET  /limited -> 429, 429, 200
click 'Submit claim'   [a5]   POST /claim   -> 500
```

Each endpoint sits behind a distinctly-named button on purpose: a correlation
that were merely per-*page* would show one label on all four groups.

## Evidence 5 — endpoint inventory (`method × path_template`)

```json
{"method": "POST", "path_template": "/__net/quote",   "auth_pattern": "bearer",
 "response_shape": "json", "statuses": {"503": 2, "200": 1}, "observed_count": 3,
 "retried": true,  "rate_limited": false, "has_server_error": true,
 "actions": [{"verb": "click", "label": "Get quote", "action_token": "a2"}]}

{"method": "GET",  "path_template": "/__net/status",  "auth_pattern": "none",
 "statuses": {"200": 4}, "observed_count": 4, "retried": true}

{"method": "GET",  "path_template": "/__net/limited", "auth_pattern": "none",
 "statuses": {"429": 2, "200": 1}, "rate_limited": true}

{"method": "POST", "path_template": "/__net/claim",   "auth_pattern": "bearer",
 "statuses": {"500": 1}, "has_server_error": true}
```

Rides the coverage account as `endpoint_inventory`, beside `states`. It is
**not** `states[*].endpoints` (the M2.4 compiler map), which is deliberately 2xx-
only — compiling a 5xx into an assertion would freeze the application's bug into
the regression suite as expected behaviour.

## Evidence 6 — redaction

| sent by the fixture | recorded |
|---|---|
| `Authorization: Bearer meridian-test-token-do-not-log-abc123` | `authorization=<bearer>` |
| `Set-Cookie: session=abc123secret` | presence only |
| body `{"age":42,"state":"CA","coverage":500000,"password":"not-a-real-password"}` | `age,state,coverage,<secret>` |

Searched across the **whole manifest**:

```
'meridian-test-token-do-not-log-abc123' present: False
'abc123secret'                          present: False
'not-a-real-password'                   present: False
```

Redaction is by **allow-list**: an unlisted header is dropped, so a header a
future application invents cannot leak by default.

## Evidence 7 — the network oracle fires on structured evidence

The adapter output, and the verdict from the **production** `network_oracle.py`
loaded by file path (the deployed module, not a copy):

```json
{"url": "…/__net/claim", "method": "POST", "status": 500,
 "start_ms": 11375, "timestamp_ms": 11375, "failed": false, "error": "",
 "sequence": 11, "action_token": "a5", "action_label": "Submit claim"}
```

```
production oracle verdict     : {"kind":"server_error","status":500,"url":"…/__net/claim"}
is_real_bug_signal            : True
verdict with ALL prose stripped: {"kind":"server_error", …}   ← unchanged
text fallback on empty prose  : None                          ← the OLD only path
step window [11325,11425]     : fires
step window [0,1]             : None                          ← correctly excluded
```

It fires **because the status is 500**, not because a string matched — proven by
removing every error string and getting the same verdict, while the text
fallback given the same absence returns nothing.

## Evidence 8 — fixture crawl results

`tests/browser/test_network_stream_gate.py` — **19 assertions, all green**, run
6× consecutively for stability. `tests/test_network_evidence.py` — 38 green.
`platform/api/tests/test_network_oracle_crawl_contract.py` — 18 green.

---

## Defects closed, and two found while closing them

The seven baseline defects (D1–D7) are closed. Two more surfaced *during* the
work and are worth recording because both were found by looking at real output
rather than by reasoning:

- **A digit-and-separator path segment escaped templating.** `123-45-6789` — an
  SSN, a policy number, a phone — matched none of the id patterns (not all
  digits, not long enough to be opaque, not a date). Caught by the inventory's
  own redaction test, fixed by `_NUMERICISH_RE`.
- **`bool("false")` is `True`.** The manifest field is typed `dict[str, str]`, so
  a captured `failed: False` is re-read as the string `"false"`. Untreated, every
  successful call *and every 5xx* was reported to the oracle as a **connection
  failure** the moment the evidence had been through the manifest — the
  difference between "the server rejected this" and "the server was
  unreachable", which route to different remediations. Fixed by `_truthy`.

## What is deliberately NOT claimed

- **Response bodies are not read.** The `response` listener is synchronous by
  design (M1.5 — an awaiting listener races the action that produced it), and
  reading a body requires an await. `response_shape` is therefore derived from
  the media type and every event says so via `shape_source: "media_type"`. It is
  never presented as a parsed body.
- **Nothing here is deployed or live-proven.** Everything is proven in CI-shaped
  local runs against a fixture. No VM deploy, no crawl of a real carrier app.
- **Truncation is now reported, not silent** (`buffer_truncated` at the adapter,
  `stream_truncated` at the normalizer), so a clipped stream can no longer read
  as a complete one — but the caps themselves (500 per drain, 100 per visit, 200
  endpoints) still exist and still clip.

---

# PROVEN ON A LIVE APPLICATION — VKPower Life, 2026-08-20

Everything above is measured on a fixture built to exhibit the behaviour. This
section is the same production crawler run against
`https://vkpowerlife.136-85-106-73.sslip.io/` — a real deployed Next.js
application nobody wrote for this milestone.

Instrument: `measure_network_evidence.py` (asserts nothing; prints what came
back). Posture: **no boundary approvals, no walk attestation** — the crawl was
given no authority to cross an irreversible control on a live deployment.

```
credentials : member 25000001 + fixed-OTP second factor
stop_reason : 'completed'
stats       : {"actions": 83, "elapsed_ms": 124906, "requests": 39, "states": 15}
network events : 72
```

The app's login is member-number → password → Security PIN. It publishes its own
demo members and accepts any password/PIN; the second factor is driven by the
existing data-driven `MfaConfig(kind="otp")`, not by anything added to the engine.

## What the live app PROVED

**T-NET-02 — retention (the headline number).**

```
distinct method × path_template : 8
events recorded now             : 72
events the BASELINE dedup would have kept : 8
DESTROYED BY THE BASELINE       : 64
```

`GET /life-insurance/quote/start/index.txt` was observed **15 times**, all 200,
across 13 distinct actions. Under `method|url|status` every one of those
collapsed to a single record. On this application the baseline was discarding
**89% of the network evidence**.

**T-NET-01 — the join.** 72 inside their capture window, **0 outside**. Max
timestamp 103187 ms against a 124906 ms crawl — crawl-relative, not uptime.

**T-NET-03 / T-NET-04 — correlation and inventory.** 8 endpoints, each carrying
the UI action that fired it. The funnel is legible directly from the inventory:

```
GET /portal/dashboard/index.txt            seen=15   triggered by: click 'Verify & Sign In'
GET /portal/beneficiaries/index.txt        seen=15   triggered by: click 'Verify & Sign In'
GET /life-insurance/quote/coverage/index.txt      seen=2    triggered by: click 'Continue'
GET /life-insurance/quote/personal/index.txt      seen=2    triggered by: click 'Continue'
GET /life-insurance/quote/health-check/index.txt  seen=1    triggered by: click 'Continue'
```

## THE DEFECT THE LIVE APP FOUND — and the fixture could not

**5 of 13 events in the first (public) run fell OUTSIDE their visit window.**

```
offenders: ('…/login/index.txt', ts=3405, window [4936, 14186])
           ('…/index.txt',       ts=16108, window [17655, 23280])
```

Cause: a `page_state`'s `first_seen_ms` is stamped when the crawl **observes**
the state, but the requests attributed to that state include the ones the
browser fired while **navigating to it** — a Next.js route prefetch goes out
before the new page exists to be observed. The M2.5 fixture is a single page
that never navigates, so it could not produce this and the gate passed while the
defect was live.

Clamping the timestamp into the window — which is what the screenshot path does
— would have made the assertion pass by recording a time the request did not
happen. Instead each event now carries `capture_window_start_ms`, the moment its
capture window opened, so the window a reviewer checks against is the one that
actually corresponds to the events in it.

After the fix, on the authenticated run: **72 inside, 0 outside**, of which **36
fired before their state was observed** and are now legible as the navigation
traffic they are.

## What this app did NOT prove — stated, not worked around

- **No auth pattern.** All 72 events report `auth_pattern: none`. The crawl's own
  log explains why: *"this app drops the sign-in on every page load (its session
  lives in the page, not in a cookie)"*. There is no Authorization header and no
  session cookie on the wire, so the auth-pattern axis has nothing to show here.
- **No request bodies.** Every observed call is a GET route prefetch, so
  `request_body_keys` and the `<secret>` masking are unexercised on this app.
  (The 4 `[REDACTED:…]` markers in this manifest are the pre-existing form-value
  scrubber on `q_email` / `q_phone`, not network redaction.)
- **T-NET-05 IS NOT PROVEN HERE.** Every one of the 72 requests returned 200 —
  and a direct probe confirmed even a nonexistent route answers 200, because the
  app is a static export behind a catch-all. The oracle correctly stayed
  **silent**: a real no-false-positive result over 72 events of genuine traffic,
  but not evidence that it fires. Firing is proven on the fixture and on the
  frozen-data contract test, not here.

  What *was* shown on this app's data is that the shapes agree: taking a real
  captured event and changing **only** its status to 503 produces
  `{"kind": "server_error", …, "detail": "GET …/login/index.txt -> 503"}` with
  `is_real_bug_signal: True`, and the step window still excludes it correctly on
  the event's real timestamp. The 503 is constructed; the URL, method, timestamp,
  sequence and correlation are this crawl's real data.

To prove the oracle end-to-end on a live app, point this instrument at a
deployment with a real error surface — the machinery is identical.
