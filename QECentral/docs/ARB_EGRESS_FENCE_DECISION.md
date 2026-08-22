# ARB decision record — the cross-tenant egress fence (T4 · task 11)

**Status: DECISION REQUIRED. This record does not make it.**
Prepared 2026-08-22 against `c40cf6c`. Author: A11/T2 session — **not** the
decision owner.

The run plan offers two paths: **(a) FIX** — make `_write_egress_allowlist`
per-crawl, then raise the `qec_022` capacity default above 1 — or **(b) ACCEPT**
capacity=1 as a shipped constraint.

**Path (a) as written does not close the hole.** That is this record's only new
finding, and it is load-bearing, because (a) is described as the option that
"unlocks real concurrency" and it would not.

---

## 1. Why (a) cannot work as specified

The fence has a producer and a consumer. The plan's (a) changes the producer.
The constraint is in the consumer.

```
producer   qe-central  _write_egress_allowlist(domains, allowlist_path)
                       — writes a file. Takes no crawl id today.

consumer   squid.conf  acl allowed_domains dstdomain \
                         "/etc/squid/allowlist/allowed_domains.txt"
                       — reads ONE fixed path. Per worker, per proxy.
```

`docker-compose.qec.yml` runs **one** `qec-egress-proxy` container, mounting the
shared `qec-egress-allowlist` volume at `/etc/squid/allowlist`, and its squid
matches a single `dstdomain` ACL against a single file.

So writing per-crawl files changes nothing on its own: squid still reads
`allowed_domains.txt` and still applies one ACL to every request reaching it. A
producer-side change would make the code *look* fixed — new files appearing per
crawl — while the browser remains fenced by whichever write happened last. **That
is strictly worse than the status quo**, because the current code is honestly
documented as broken above capacity 1 and the changed code would not be.

Closing it within one worker requires a consumer-side change: per-crawl squid
instances, or per-crawl ACL selection keyed on something in the request (proxy
credentials or source IP — neither exists per crawl today; both crawls share one
container and one identity). That is an architecture change on the explorer/proxy
side, not an edit to one function.

## 2. What the existing evidence actually says

A32's "zero fence violations" and T-FL-08's failure are both true, and they are
not in tension once the scope is named:

| Proof | Scope | Verdict |
| --- | --- | --- |
| A32 | 4 workers, 4 proxies, 4 files | fence HOLDS **across** workers |
| T-FL-08 | 1 worker, capacity 2 | fence BREAKS **within** a worker |

The isolation boundary is the **worker**, because the worker is what owns a squid
instance and a file. `explorer_pool` already encodes exactly this: each entry
pins its own `allowlist_path`, and `config.py` says so — *"each worker MUST have
its OWN squid egress allowlist file."*

## 3. The reframing this produces — and why (b) costs far less than it appears

The plan frames (b) as giving up concurrency. It does not.

**The system already has a working concurrency story: add workers, not capacity.**
`acquire_slot` is a single conditional UPDATE (`in_flight < capacity`) and the
scheduler already prefers the least-loaded eligible worker across the registry.
N workers at capacity 1 gives N concurrent crawls with the fence intact — the
configuration A32 proved.

So the real cost of (b) is not "no concurrency". It is:

* concurrency scales by **container count**, not by a number in a table — more
  memory per unit of concurrency, since each worker carries its own browser and
  proxy (which is already true: one heavy browser per container is the M0.5
  single-flight invariant);
* the `capacity` column stays a trap. It is settable, the scheduler honours it,
  and nothing refuses it.

That last point is the part of (b) that is currently unfinished. The plan
describes (b) as *"record capacity=1 as a shipped constraint with the two alarms
as the permanent guard"* — but **both alarms are tests**. Neither prevents an
operator setting `capacity=2` on a live registry row. The constraint is enforced
by a `server_default` and by two tests that fail in CI, not by anything in the
running system.

## 4. The three options, honestly costed

| | What it is | Real cost | Closes the hole? |
| --- | --- | --- | --- |
| **(a)** as written | per-crawl files, raise capacity | small — **and does not work** (§1) | **No** |
| **(a′)** done properly | per-crawl squid instances or per-crawl ACL selection, plus the explorer-side change | architecture work on the proxy topology; new failure modes around reload/lifecycle | Yes |
| **(b+)** accept, and *enforce* | ARB record + a runtime refusal so `capacity > 1` cannot be used while the fence is per-worker | small | Makes it **unreachable**, which is the property that matters |

**(b+) is (b) with the gap in it closed.** Today the accepted constraint is
guarded by tests; under (b+) the running system refuses to over-subscribe a
worker, so the leak stops being reachable by configuration error rather than
merely being documented as unsupported.

## 5. Recommendation

**(b+)**, unless intra-worker concurrency is a requirement someone can name. It
is not on any current gate's critical path, and inter-worker concurrency —
already proven by A32 — delivers the same throughput.

Whoever takes (b+) should implement the refusal in
`controlplane/scheduling/worker_registry.acquire_slot` (the one choke point every
dispatch passes) and at registration, log loudly, and cite this record. The two
existing alarms then stop being the only guard and become the regression test for
one.

If the ARB prefers (a′), it should be scheduled as its own task with the
explorer/proxy owner, **not** inside T4's window — and `qec_022`'s default must
stay at 1 until it lands.

## 6. Deliberately not done here

No code was changed. Implementing the refusal would *be* the decision, and the
run plan assigns that to the ARB. What this record adds is that one of the two
options on the table cannot deliver what it promises — which is the kind of thing
a decision should not be made without.

**Artifacts**

| | |
| --- | --- |
| fence writer (no crawl id) | `platform/qe-central/app/routers/explorations.py::_write_egress_allowlist` |
| the single consumer ACL | `engines/qe-explorer/squid.conf:22` |
| one proxy, shared volume | `docker-compose.qec.yml:226` (`qec-egress-proxy`) |
| capacity default | `alembic_qec/versions/qec_022_explorer_worker_registry.py` (`server_default="1"`) |
| the choke point to guard | `controlplane/scheduling/worker_registry.py::acquire_slot` |
| alarm 1 (records the hole) | `tests/fleet/test_t_fl_08_concurrency_redteam.py` (`xfail(strict=True)`, capacity 2) |
| alarm 2 (fails on raise) | `tests/contract/test_egress_fence_latent_to_live_tripwire.py` |
