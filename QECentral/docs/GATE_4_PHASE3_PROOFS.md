# Gate 4 — Phase 3 proofs on real infrastructure

**Scope executed: A28–A35.** A26/A27 belong to another squad and were already
implemented when this gate opened; A36 is closed and was not touched.

| # | Milestone | Verdict |
|---|---|---|
| A28 | vision-operate caller | **CODE COMPLETE, live proof blocked by A29** |
| A29 | real multimodal prediction | **BLOCKED — provider returns HTTP 401** |
| A30 | signed vision attestation rung | **NOT STARTED — blocked by A29, plus a design conflict (§A30)** |
| A31 | KEDA on a real cluster | **PASS** — reproduced twice |
| A32 | T-FL-08 vs real Chromium | **PASS** — 36 cross-fence attempts, 0 violations |
| A33 | live Squid fence reload | **PASS** — flipped live, no restart |
| A34 | `_scan_fleet` scalability | **DECIDED** — bound measured and formally accepted |
| A35 | crossing journal recovery | **PASS** — both crash shapes, 0 double-submits |

Evidence: `Nexus_power/evidence/gate4/*.json`. Branch `gate4/phase3-proofs`
on `origin` (`1e7c5fd` → `3f1fa63`).

---

## §0 · The three results that were green and wrong

All three were caught, all three are kept in the record, and each one is the
reason a guard now exists. The third was found *after* the gate had been signed
off by an independent verifier — which is the honest reason it is listed here
rather than quietly repaired.

**A35 reported PASS with a ledger of zero binds in both runs.** The verdict said
"zero double-submits". True, and worthless: the crawl had never submitted at
all, so "at most one" was satisfied by nothing ever happening. A fault-injection
test whose subject never performs the action cannot fail. A mandatory control
phase now runs an *unkilled* crawl first and the run is INVALID unless it binds.

**A34's first sweep produced a per-tenant cost falling from 32,175 ms to
13.6 ms.** A plausible amortisation curve, entirely artefact: an earlier run
killed by a Docker outage never ran its cleanup, so 1 024 tenants stayed in the
registry and every step secretly measured 1 024 while being labelled 1, 8, 32…
The guard asserted only `apps_returned >= n` — it caught under-reaching and was
blind to over-reaching. It now purges first and asserts the enumerated fleet is
exactly `baseline + n`.

**A32/A33 passed against config bytes production never loads** — a Windows
checkout's CRLF `squid.conf`, not the LF blob in git. Detail in §A32; the file
is now pinned to `eol=lf` and the harness refuses to run on a CR byte.

There was also a **near-miss in the other direction**: A35's control crawl bound
two policies against a `max_crossings: 1` grant, which looked exactly like a
serious product defect. It was my fixture — it re-served the application form
for `GET /bind`, inventing a second boundary. Fixed to post-redirect-get; the
control now binds exactly once. **No defect was reported, because there was
none.**

---

## §A31 · KEDA on a real cluster — PASS

Reproducer: `bash scripts/gate4_a31_keda_cluster_proof.sh --recreate`

Real kind cluster (kubelet v1.32.2, containerd 2.0.3), real KEDA operator
`ghcr.io/kedacore/keda:2.20.2`. **The repository's own ScaledObject is applied
UNEDITED** — the scaffolding is built to satisfy the names the production
manifest already assumes (`nexus/qe-explorer`,
`monitoring/prometheus-operated:9090`, secret `qec-prometheus`), so a wrong name
fails the proof rather than being papered over.

```
queue depth 0  ->  1 replica   (negative control: the fleet is at the floor)
queue depth 8  ->  4 replicas in 30s
4 pods Running on gate4-keda-control-plane
HPA reason: "New size: 4; reason: external metric s0-prometheus ... above target"
queue back to 0 -> still 4 after 60s (scaleDown stabilisation is 300s)
```

Run twice, identical both times.

**Finding — two documented fields are inert.** KEDA's admission webhook warns:

> PollingInterval is configured but is not relevant … only relevant when
> minReplicaCount = 0 · CooldownPeriod is configured but is not relevant …

`minReplicaCount` is 1, so `pollingInterval: 15` and `cooldownPeriod: 300` do
nothing. The manifest's most carefully-argued comment — that a 300 s cooldown
protects a pod holding a live crawl — describes behaviour the cluster does not
implement. The protection is real but comes from
`behavior.scaleDown.stabilizationWindowSeconds: 300`, which IS honoured (the
fleet stayed at 4 after the queue emptied).

**Actioned:** the manifest's comments are corrected in this branch to say the
two fields are inert and to attribute the protection to the HPA `behavior`
block. The **configuration is deliberately unchanged** — removing the fields, or
setting `minReplicaCount: 0` to make them take effect, is a scale-to-zero
behaviour decision belonging to the fleet owner, and the manifest's cold-start
argument against scale-to-zero still stands. What was wrong was the
explanation, not the configuration.

**Boundaries.** The queue gauge is published by a stand-in exporter, not by
`queue_drainer._publish_fleet_metrics`. The scaled container is a sleeping
busybox, not the 3 GB Playwright image — the loop from *queue depth* to
*scheduled pods* is fully real; the metric's publisher and the container's
payload sit outside it. Single-node cluster: placement is observed, multi-node
spreading is not.

---

## §A32 · The egress red team against real Chromium — PASS

Reproducer:
`python platform/qe-central/tests/fleet/gate4_a32_a33_chromium_egress.py --workers 4 --rounds 3`

`test_t_fl_08_concurrency_redteam.py` states plainly that its explorer is "a
coroutine that … reads the fence it was given". A coroutine has no network
stack, no DNS, no TLS and no proxy setting, so **the security property was
asserted about an object structurally incapable of violating it.**

Now real: the production `ubuntu/squid:latest` running this repository's own
`engines/qe-explorer/squid.conf` bytes, started by the same entrypoint and
mtime→`kill -HUP` watcher `docker-compose.qec.yml` uses; Playwright Chromium,
one context per worker, each proxied through its own worker's Squid; separate
origin containers with distinct DNS names and distinct response bodies.

```
negative control : all 4 workers REACHED their OWN origin (http_200)
48 attempts, 36 of them cross-fence
VIOLATIONS: 0
```

The negative control is load-bearing. A harness that only ever observes
"denied" passes just as well when egress is broken everywhere — a wrong proxy
port, a dead origin, or a Chromium that never left the machine all produce a
perfect score. Each worker must first reach its own origin and read back that
origin's unique marker; only then does a denial mean the fence caused it. The
readiness probe additionally requires a *fresh* Squid to answer 403 to a real
internet host, so fail-closed is verified before the first measurement.

Also reproduced at 3 workers × 2 rounds (18 attempts, 12 cross-fence, 0
violations).

### A third false pass, found after the gate was signed off

The first green A32/A33 runs shipped **the wrong config bytes into the
container**, and passed anyway.

`squid.conf` carried no `eol` attribute, so with `core.autocrlf=true` the
working copy held **71 CR bytes** while the committed blob is LF. The harness
`docker cp`s the *working copy*, so Squid was loading CRLF config that no Linux
deployment has ever seen — while this document claimed the run used "the
repository's own `squid.conf` bytes".

It passed, and that is precisely the problem: a security proof executed against
different bytes than production runs is not a proof about production, and
nothing anywhere said so. Found by applying to my own work a defect class the
Gate 1 squad hit independently (`sha256sum -c` treats a trailing CR as part of
the filename, so a CRLF checkout of a digest manifest reports "sources have
drifted" when nothing has).

Closed two ways:

* `.gitattributes` now pins `squid.conf` and `squid_allowed_domains.txt` to
  `text eol=lf` — config consumed *inside a Linux container* must not depend on
  the developer's platform.
* `assert_config_is_production_bytes()` refuses to start the run if the file
  contains a single CR, and records the file's sha256 in the evidence. An
  attribute can be missed again; a silently-passing proof is the failure mode.

Re-run against the corrected bytes: **4 workers × 3 rounds, 36 cross-fence
attempts, 0 violations**, and the evidence's recorded digest
`6b5bcc7505e82ed321ca167b5a0e60e631e581e5b3ce0fbb65ceec373cd77805` **equals the
committed blob's digest** — so the proof now demonstrably ran the bytes in git.

---

## §A33 · Squid re-reads a rewritten fence, live — PASS

Same harness. Not a timestamp, not a config parse, not a restart:

```
before rewrite : own=REACHED   other=REFUSED
   << fence file rewritten inside the running container >>
after  rewrite : own=REFUSED   other=REACHED
squid restarted: False
```

The *same already-open* Chromium context performs all four navigations. Squid's
container `StartedAt` and PID-1 start time are captured before and after and
asserted **unchanged**, so "it reloaded" cannot be satisfied by a restart.

---

## §A34 · `_scan_fleet` — DECIDED (Option B)

Full record: **`QECentral/docs/A34_SCAN_FLEET_DECISION.md`**.

Measured under the production RLS posture (`qec`, NOSUPERUSER, NOBYPASSRLS,
FORCE RLS — the harness refuses to record a timing otherwise): **linear at
≈ 11.7 ms per tenant**, 11.98 s at 1 032 tenants.

**Decision: accept the bound**, because the consumer is switched off
(`QEC_CYCLE_TICK_SECONDS` is set in no compose file, env template or deployment
in this repository), the current registry holds 8 tenants, and the named fix is
a `SECURITY DEFINER` function that reads across tenants — the exact shape whose
absence caused T-FL-05. Triggers that make batching mandatory: **250 tenants**,
or a scan exceeding **25 % of the tick**, or the daemon being enabled in
production with >100 tenants.

**Finding — the documented bound understates the risk.** §6.5 describes round
trips. The sharper problem is materialisation: the `LIMIT` is applied *per
tenant* (`limit*4` apps, `limit*20` change events) while the fleet-wide cap is
applied only afterwards in `_discover_due_work` as `out[:limit]`. At 1 000
tenants that is up to ~1.2 M rows accumulated to emit 50 work items. Memory
fails before round trips do, and whoever implements batching must push the
fleet-wide cap into the query.

**Finding — a fail-soft that is silent to its caller.** `fleet_tenant_ids()`
returns `[PLATFORM_SCOPE]` when the registry is unreadable. `_scan_fleet` then
completes normally, scans one scope and reports success. This happened during
this milestone and produced plausible timings for a "fleet" of one; it was
caught only because the harness cross-checks the count. Recommend a metric on
enumerated tenant count.

---

## §A35 · Exactly-once across a real SIGKILL — PASS

Reproducer:
`python engines/qe-explorer/tests/browser/gate4_a35_crossing_recovery.py`

The existing M3.4 test simulates the crash by truncating the manifest,
in-process, against a fixture whose submit button does nothing. Three things
were therefore unproven: nothing was killed, the "application" could not count
submissions, and the crash shape was the one the author expected.

New ground: `proving-grounds/crossing-ledger` — a real HTTP server that records
every bind and **deliberately does not deduplicate**, so the count measures the
crawler and not the application's own idempotency. A real Chromium crawl runs in
a child process and is SIGKILLed.

```
control (no kill)  : binds = 1        <- the boundary is reachable and countable
                                         (without this the run is vacuous)

scenario A — killed before the request left the browser
  reserved record durable after SIGKILL : True
  binds after kill  = 0     binds after resume = 0     delta = 0

scenario B — killed once the SERVER had recorded the bind, response in flight
  binds after kill  = 1     binds after resume = 1     delta = 0
```

Scenario B is the one that matters. The irreversible effect had already landed
and the crawler never learned that it did — the exact ambiguity a naive "retry
what did not complete" resolves by binding a second policy. It did not.

### What the resume actually did — and the half this does NOT prove

The mechanism is directly observable in the resumed run's log, and it is worth
stating precisely because it is easy to describe more flatteringly than the
evidence supports:

```
qec.crawler.crossings_restored crawl_id=gate4-a35-crossing journalled=1 flows=1
  - this run INHERITS the irreversible actions the killed run took;
    it will not repeat them
qec.inventory.built controls=4 ... dangerous=1
qec.crawler.completed stop_reason=completed states=0 actions=0
```

So: the write-ahead record was **read back off disk and restored into the
ledger** (`journalled=1 flows=1`), and the resumed crawl **did load the page and
inventory the boundary control** (`controls=4 dangerous=1`). It then completed
with **0 new states**, because the entry state was already in the restored
visited set.

In both scenarios the manifest ends holding exactly one crossing record, status
`reserved` — it never becomes `crossed` or `refused`, and `refusals` is `[]`.

**Therefore, stated exactly:**

* **PROVEN** — the reservation survives a real `SIGKILL`; a resumed process
  inherits it; and the application's non-deduplicating ledger shows no second
  bind (delta 0 in both scenarios; total 1 in scenario B).
* **NOT PROVEN HERE** — the explicit `CROSSING_REFUSED` path. The resumed crawl
  never re-attempted the boundary, so nothing refused it. That path is covered
  by `tests/test_resume_crossing_journal_m34.py`, where a scripted browser does
  re-reach the boundary and is refused — but A35 did **not** reproduce that
  live.

A reviewer described this as "the crossing is refused on resume". It is not:
**it is inherited as already-spent and never re-attempted.** The exactly-once
outcome is the same and the durability claim stands, but the two are different
mechanisms and only the first is evidenced here.

**Operator-visible consequence.** In scenario A the boundary is spent in the
journal but was never actuated at the application (`binds = 0`). The resumed
crawl does not retry it, so the funnel stays honestly *uncrossed* and the
journey is not completed. That is the correct trade — a missing outcome
milestone is recoverable, a duplicate irreversible action is not — but it means
a killed crawl can leave a boundary permanently unspendable for that crawl id,
and an operator seeing "reserved, never crossed" should read it as *deliberate
refusal to guess*, not as a stuck crawl.

---

## §A28 · The orphaned endpoint now has a caller — code complete

`/internal/vision-operate` was live, HMAC-authenticated, server-side flag-gated,
tested on the server side — and **no code in the engine called it.**
`OracleGateway.operate` raises `NotImplementedError`; the ladder's medic rung
calls `/internal/operate-control` (the *text* medic) and stops. The only vision
the crawler ever performed went through `/internal/perceive-controls`, which
answers a different question. Per the decision taken at the start of this gate,
the endpoint was **wired, not deleted**.

* `app/main.py::_make_vision_medic_oracle` — same HMAC + fleet token as its
  siblings, same `VisionBudget` double gate, never raises.
* `app/playwright_port.py::_vision_medic_rung` — runs after the text medic is
  exhausted; refuses a point outside the element's box; refuses to send an image
  whose redaction cannot be proven.
* `app/metrics.py` — `vision_medic` added to `ORACLE_KINDS`, or `_enum` folds
  the new rung's spend into `other`.

12 new tests, weighted towards the ways it fails *quietly*. The headline one
pins the M3.1 defect class: `bounding_box()` is viewport-relative, `click_at`
takes page coordinates, and the endpoint returns bbox-relative offsets — three
spaces, and conflating two of them silently mis-aims every vision click.

**Full explorer suite: 2016 passed, 0 failed.**

The live half of A28's acceptance ("a live Explorer vision path calls the
endpoint") is **not claimed** — it requires A29's provider.

---

## §A29 · Real multimodal prediction — BLOCKED

Everything is built and proven working up to the provider:

```
real canvas page   fixture 23, 0 DOM controls collected
real screenshot    1280x900, 15,067 bytes
real redaction     production T-VIS-05 path, 1 region masked,
                   receipt sha256 b14c93a4…bfb76d
real prompt        qe-central vision_medic.SYSTEM (authoritative table)
real router        platform-api build_router() + openai_compat provider
real request       dispatched to api.openai.com
PROVIDER           HTTP 401 "Incorrect API key provided"
```

`OPENAI_API_KEY` in this environment is rejected. Verified **independently of
our code**: a plain `curl https://api.openai.com/v1/models` with the same key
also returns 401. The key is 164 chars with a well-formed `sk-proj-` prefix, so
it is invalid or revoked, not mangled in transit.

I did not route around it. A stubbed prediction is exactly what A29 exists to
abolish, so the milestone is reported as **not proven**. Supply a working
credential and the committed harness produces the evidence or fails honestly.

---

## §A30 · Signed vision rung — NOT STARTED, and a design conflict to resolve first

A11 **was** independently certified during this gate (by the Gate 1 squad, not
its author), so the stated dependency cleared. A30 is nonetheless not started,
for two reasons — the second matters more than the first:

1. **It depends on A29**, which is blocked. There is no successful vision
   operation to promote onto a trusted rung.

2. **The obvious implementation would invalidate A11's certification — and
   that certification is now real.** When this gate opened, A11 existed in no
   commit and its certification pinned digests of working-tree files, so the
   constraint was theoretical. It is not any more: A11 was committed, the six
   CRLF-mismatched sources were normalised, the manifest regenerated, and the
   result verified **from a clean detached checkout** — 9/9 digests OK, 131
   independent checks, 143 tests passing. Breaking it now breaks something that
   actually holds.
   `app/attest.py::ProofClaims` is `extra="forbid"`, so a vision rung cannot
   simply be added to the signed claims — it is a two-sided change to a
   red-teamed verifier plus a `CLAIMS_VERSION` bump. And A11's certification
   record binds **nine files pinned by SHA-256**; the record lapses and its
   reproducer refuses to run if any of them change. Adding a claims field would
   therefore de-certify the very attestation A30 is trying to build on.

   The shape that avoids this already exists in the codebase: `walk_attested` is
   **derived on the Explorer from its own verification verdict**, and qe-central
   has no way to set it (there is a test that tokenises every qe-central source
   file and fails if the name appears). A30's vision rung should be built the
   same way — attach bytes, let the verifier decide — rather than by widening
   the signed claims.

A11's author and its independent certifier have both since confirmed this
independently, and sharpened it: the verifier checks integrity over the **raw**
claims *before* the typed parse, so an unknown field is refused at schema
validation rather than merely ignored. `attest.py` is one of the nine pinned
files, so touching it fails the drift gate, de-certifies A11, and re-blocks A12
— which is now unblocked. The same applies to the open IPv6 finding
(CERT-FINDING-2): its fix touches `attest.py` and `walk_attestation.py`, both
pinned, so it needs a re-certified follow-up rather than a quiet patch.

Recorded here so whoever picks A30 up starts from the constraint rather than
discovering it after de-certifying A11.

---

## §1 · What could not be proven, and why

* **A29 / A30** — above.
* **CI execution of this branch.** `ci.yml` triggers on `push` only for `main`,
  `develop` and `feat/qec-dynamic-catalog-p0-p6`, its `pull_request` trigger
  targets only `main`/`develop`, and it has no `workflow_dispatch`. A
  `gate4/*` branch therefore cannot run it. Widening the trigger is a cost
  decision the workflow's own comment says should be taken deliberately, and
  merging into the shared integration branch is a bigger step than this gate was
  authorised to take — so I did neither, and validated locally instead (2016
  explorer tests green). **This is a decision for the branch owner.**
* **Multi-node scheduling** (A31) and **the production database's per-round-trip
  constant** (A34) — both need infrastructure this machine does not have. A34's
  numbers are laptop-Docker numbers; the *shape* transfers, the milliseconds do
  not.

## §2 · An incident I caused

Creating the kind cluster on this 8 GB machine crashed Docker Desktop (WSL VHDX
unmount timeout), killing **every** container on the box — including other
squads' MinIO and Postgres. I recovered Docker, restarted what could be
restarted, and told the affected sessions immediately; `pg-acme` and `pg-vk` had
been created with `--rm` and were **destroyed, not stopped**. One squad
confirmed the outage had inflated a timing measurement they were about to write
up as a code defect. Heavy infrastructure was serialised afterwards. Recorded
because a shared-machine outage that silently corrupts someone else's evidence
is exactly the failure Gate 0 §0 exists to name.

## §3 · Independent reproduction

Every proof is one command and leaves a JSON artefact under
`Nexus_power/evidence/gate4/`. Each harness refuses to produce numbers when its
preconditions are unmet — wrong DB role, unmasked screenshot, fleet size that
does not match its label, a control that never reached its own origin, a crawl
that never submitted. Every one of those refusals exists because the
corresponding mistake was actually made while producing this document.
