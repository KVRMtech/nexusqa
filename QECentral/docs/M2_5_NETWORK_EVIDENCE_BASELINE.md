# M2.5 — Network Capture: the BASELINE (schema as it exists BEFORE any change)

Recorded before touching a line, so every later claim is measured against a
written starting point rather than against memory.

Trace date: 2026-08-19 · branch `feat/qec-dynamic-catalog-p0-p6`

---

## 1. The lifecycle, end to end

| # | Stage | Location |
|---|---|---|
| 1 | Listener attach | `app/playwright_port.py` `_PAGE_OBSERVERS` → `("response", "_on_response", "network")`, `("websocket", "_on_websocket", "websocket")`. Attached to **every** page the journey touches (M1.5 `PageRegistry`), re-attached on adoption. |
| 2 | Capture | `_on_response` (sync, defensive, never awaits) / `_on_websocket` |
| 3 | Buffer | `_record_net` → `self._net_buffer`, capped `_NET_BUFFER_MAX = 500` |
| 4 | Drain | `PlaywrightBrowserPort.drain_network()` — return + clear |
| 5 | Port protocol | `app/browser.py:455` (optional verb, reached by `getattr`) |
| 6 | Transport | `app/emitter.py:58` `MetaEmitter.drain_network` → `app/crawler.py:1030` `_drain_network` |
| 7 | Drain sites | `app/discovery.py:509` (once per visit, after fills + discovery clicks) · `app/walker.py:1768` (once per walk step) |
| 8 | Normalize | `app/state_identity.py:518` `_network_calls()` — re-scrub, **dedup**, cap `_MAX_NETWORK_CALLS = 100`, stringify |
| 9 | Record | `app/emit.py:305` `PageStateRecord.network_calls: list[dict[str,str]]` → manifest `page_state` line |
| 10 | Substrate | qe-central `clients/manifest_mapper.py:275` → `substrate/schema.py:276` `PageState.network_calls` → `substrate/writer.py:258` |
| 11 | Catalog | **nothing.** No endpoint inventory exists. `app/coverage.py` has no network axis. |
| 12 | Oracle | `platform/api/app/services/test_factory/network_oracle.py` — called ONLY from `routers/test_factory.py:3240`, on a **runner** failure record. **No crawl evidence ever reaches it.** |

## 2. The event schema as captured (v0)

`_on_response` emits exactly nine keys:

| field | type | derivation |
|---|---|---|
| `method` | str | `request.method`, upper |
| `url` | str | `scheme://netloc/path` — **query string dropped**, path `emit.scrub_value`d |
| `has_query` | bool | `bool(parts.query)` (the honest fact a query existed, without its values) |
| `status` | str | `response.status` |
| `resource_type` | str | `xhr` \| `fetch` \| `sse` \| `websocket` |
| `request_mime` | str | request `content-type`, before `;` |
| `response_mime` | str | response `content-type`, before `;` |
| `response_bytes` | str | response `content-length` **header only** |
| `timestamp_ms` | int | `int(time.monotonic() * 1000)` |

Filter: `resource_type ∈ {xhr, fetch, eventsource}` **or** `response_mime == text/event-stream`.
Scheme must be `http`/`https` (`ws`/`wss` for the WebSocket listener).

After `_network_calls()` every value is a **string**, plus dedup and a 100-item cap.

## 3. The three clocks in play

| producer | epoch | used by |
|---|---|---|
| `emit.MonotonicClock.now_ms()` | **ms since crawl start** (offset-seeded on resume) | `PageStateRecord.first_seen_ms` / `last_seen_ms`, `ActionRecord.timestamp_ms` — i.e. every visit window and every UI step |
| `int(time.monotonic()*1000)` | **ms since an arbitrary reference (system boot on Linux)** | `_on_response`, `_on_websocket` — i.e. every network event |
| `int(time.time()*1000)` | **Unix wall clock** | `_record_event` buffer-truncation marker |

## 4. The oracle contract vs. what the crawler produces

`classify_network_signal(entries, step_start_ms=, step_end_ms=, base_host=)` reads:

| oracle reads | crawler writes | result |
|---|---|---|
| `e["start_ms"]` | `timestamp_ms` | **window filter silently disabled** — `t is None` skips the range check, so every event in the crawl is "in window" |
| `e["status"]` int-able | `"503"` (str) | works (`int("503")`) |
| `e["url"]`, `e["method"]` | present | works |
| `e["failed"]` / `e["error"]` | **never written** | a connection-level failure can never be classified — the listener only fires on a *response* |
| `failure_record["network"]` | **no producer** | the structured path is dead code for crawl evidence |

## 5. Defects the trace found (all pre-existing, all in the baseline)

- **D1 — wrong epoch (T-NET-01).** Network `timestamp_ms` is raw `time.monotonic()`; visit windows are ms-since-crawl-start. On any machine with more than a few seconds of uptime a network event's timestamp is orders of magnitude larger than the visit window that contains it. **No network event can be joined to a visit or a step.**
- **D2 — retries destroyed (T-NET-02).** `_network_calls` dedups on `method|url|status`. Three identical retries collapse to one record; a poll that fires forty times reports once. Ordering is not recorded at all, so it cannot be recovered downstream.
- **D3 — no causality (T-NET-03).** Nothing on the event names the UI action. The drain is per-visit, so at best an event is attributed to a whole page visit. "Which click caused this POST?" is unanswerable.
- **D4 — no endpoint inventory (T-NET-04).** Raw per-visit URLs only. No path templating, no aggregation, no auth pattern, no application-level API surface.
- **D5 — oracle disconnected (T-NET-05).** Field-name mismatch (`start_ms` vs `timestamp_ms`) plus no producer at all. The oracle's only live path is regex-matching arbitrary error strings.
- **D6 — silent truncation.** `_record_net` drops silently at 500, unlike `_record_event`, which records a `buffer_truncated` marker. A truncated stream reads as a complete one.
- **D7 — headers/bodies never captured.** Only `content-type` and `content-length` survive. No request headers, no response headers, no request body, no body metadata — so neither an auth pattern nor a response shape can be derived.

## 6. Redaction posture inherited (must be preserved)

- Query strings are dropped at source AND again in the normalizer (belt and braces).
- The query-stripped URL is `emit.scrub_value`d for path-embedded PII on both passes.
- WebSocket **frame payloads are deliberately not captured** — the endpoint is the evidence.
- `scrub_value` fails **open and counted** (`errors=1`), never silently dropping evidence.
- Everything added by M2.5 must sit inside this posture, not beside it.
