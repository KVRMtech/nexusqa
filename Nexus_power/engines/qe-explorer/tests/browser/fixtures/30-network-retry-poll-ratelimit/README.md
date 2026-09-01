# 30 — network: retries, polling, rate limiting and an observed 5xx

## Purpose

Isolate the four network behaviours that the **baseline capture destroyed**, so
that M2.5's network evidence stream can be proven on an application that really
produces them rather than on a hand-written event list.

Every other fixture in this library is about what the crawl can *read* from a
page. This one is about what the crawl can *record about the traffic that page
causes* — the ORDER, the COUNT and the CAUSE of real HTTP requests.

Four controls, four distinct behaviours, one endpoint each:

| Control | Call | Scripted answers | What it isolates |
|---|---|---|---|
| `Get quote` | `POST /__net/quote` | 503, 503, 200 | a **retry** that recovers |
| `Check status` | `GET /__net/status` | 200, 200, 200, 200 | a **poll** — nothing varies at all |
| `Refresh limits` | `GET /__net/limited` | 429, 429, 200 | **rate limiting** + `Retry-After` |
| `Submit claim` | `POST /__net/claim` | 500 | an **observed 5xx** that never recovers |

The poll is the sharpest case: four requests with the same method, the same URL
and the same status. There is no field that distinguishes them, so any
deduplication keyed on the request collapses all four into one record. Only an
ordinal assigned at capture time keeps the cadence visible.

The quote request deliberately carries a real `Authorization: Bearer` header and
a body containing both contract-shaped keys (`age`, `state`, `coverage`) and a
secret-shaped one (`password`), so the redaction evidence has something genuine
to reduce rather than a synthetic example. The rate-limited response carries
`Retry-After` and `X-RateLimit-*`, and every response sets a `Set-Cookie`.

## Expected controls

Four buttons, each with a distinct accessible name — distinct on purpose, so a
correlation that were merely per-PAGE rather than per-ACTION would show the same
label on all four network groups and be caught:

- `Get quote` (button)
- `Check status` (button)
- `Refresh limits` (button)
- `Submit claim` (button)

## Expected manifest

The `page_state` record's `network_calls` must contain **11 call events**, not 4:
3 + 4 + 3 + 1. Each one carries `sequence`, a crawl-relative `timestamp_ms`
inside the visit window, `action_token` / `action_label` / `action_verb`,
allow-listed `request_headers` / `response_headers`, and `request_body_keys`
with `password` masked to `<secret>`.

The coverage account must carry an `endpoint_inventory` of **4 endpoints** keyed
by `method × path_template`, with `/__net/quote` showing `{"503": 2, "200": 1}`
and `retried: true`, `/__net/limited` showing `rate_limited: true`, and
`/__net/claim` showing `has_server_error: true`.

## Targeted defect

Regression guard — for four defects found by tracing the capture lifecycle in
M2.5, each of which lived in a seam between two individually-correct components
and was therefore invisible to every unit test:

- **T-NET-01** network events were stamped with raw `time.monotonic()` while
  visit windows used the crawl clock — two epochs, and a join that silently
  produced nothing;
- **T-NET-02** `_network_calls` deduplicated on `method|url|status`, so three
  retries became one record;
- **T-NET-03** nothing on an event named the UI action that caused it;
- **T-NET-05** the oracle read `start_ms` and the crawler wrote `timestamp_ms`,
  so the step window did not raise — it switched itself off.

## Running this fixture alone

```bash
cd Nexus_power/engines/qe-explorer
python -m pytest tests/browser/test_network_stream_gate.py -q
```

The `/__net/` endpoints are served by the harness (`_harness.py`,
`_FixtureHandler._NET_SCRIPT`), not by a static file, so the sequences are
deterministic on a laptop and in CI alike. To open the page by hand you need the
harness server — a plain `file://` open renders the buttons but every call 404s.

This fixture declares the **playwright lane only**. jsdom has no network stack,
so a fixture whose entire subject is real HTTP ordering cannot be measured
there; declaring both lanes would report a lane that never ran as one that
passed.
