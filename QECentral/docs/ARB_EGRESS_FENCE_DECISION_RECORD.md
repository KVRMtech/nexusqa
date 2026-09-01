# ARB decision record — cross-tenant egress fence: **DECISION MADE**

**Decision: (B) ACCEPT — `capacity = 1` is a shipped security constraint.**

| | |
| --- | --- |
| Decided at | HEAD `8c443f2941364b37ae48f95fba5f6bd596ee925d`, working tree CLEAN |
| Supersedes | `ARB_EGRESS_FENCE_DECISION.md` (status "DECISION REQUIRED") |
| Option rejected | (A) FIX — make the fence per-crawl and raise capacity above 1 |
| Option taken | (B) ACCEPT — retain capacity 1; scale concurrency by worker count |
| Owner seat for any future capacity increase | **VACANT — must be appointed** (see §5) |

This record makes the decision the prior record deliberately declined to make.
It does not re-argue the analysis; it adopts it, adds two measurements the prior
record did not have, and names what is now true.

---

## 1 · Why (A) is rejected

Rejected on the prior record's §1 finding, which this session re-verified against
the tree rather than accepting:

* the **producer** `_write_egress_allowlist` takes no crawl id;
* the **consumer** `squid.conf:22` reads ONE fixed path,
  `/etc/squid/allowlist/allowed_domains.txt`;
* `docker-compose.qec.yml` runs ONE `qec-egress-proxy` mounting one shared volume.

Changing the producer per-crawl therefore moves nothing: squid still applies a
single ACL from a single file. (A) as specified would make the code *look* fixed
while the browser stayed fenced by whichever write happened last — **strictly
worse than the status quo**, because the present code is honestly documented as
unsupported above capacity 1 and the changed code would not be.

Closing it *within* one worker requires a consumer-side change (per-crawl squid
instances, or per-crawl ACL selection keyed on proxy credentials or source IP —
neither of which exists per crawl today). That is proxy-topology architecture
work, not an edit to one function, and it is not on any gate's critical path.

## 2 · Why (B) costs less than it appears

The isolation boundary is the **worker**, because the worker owns a squid
instance and a file. `explorer_pool` already encodes this: each entry pins its
own `allowlist_path`.

**N workers at capacity 1 gives N concurrent crawls with the fence intact** —
exactly the configuration A32 proved (4 workers / 4 proxies / 4 files, zero fence
violations). `acquire_slot` is a single conditional UPDATE and the scheduler
already prefers the least-loaded eligible worker. So (B) does not forfeit
concurrency; it changes the unit of scaling from a number in a table to a
container.

## 3 · Blast radius, stated plainly

**If `capacity > 1` is ever set on a live worker registry row**, two crawls share
one squid instance and one allowlist file. The second crawl's
`_write_egress_allowlist` overwrites the first's. Consequence:

* the browser of crawl A is fenced by the allowlist of crawl B;
* **tenant A's browser may egress to tenant B's approved domains** — a
  cross-tenant egress leak, which is the exact property the fence exists to hold;
* it is silent: nothing in the running system logs or refuses it.

**Reachability.** The leak is reachable **only** by configuration — an operator
or a migration setting `capacity` above its `server_default="1"`
(`alembic_qec/versions/qec_022_explorer_worker_registry.py:83`). It is not
reachable by crawl input, by tenant action, or by scheduler behaviour at the
shipped default.

**Bound.** Within one worker only. A32 proves the fence holds *across* workers,
so the blast radius of a mis-set row is that worker's concurrent crawls, not the
fleet.

## 4 · Permanent tripwire coverage — measured, not asserted

Both alarms were run at the deciding SHA. **Their status is not identical and the
difference is recorded rather than smoothed over.**

| Alarm | Path | Run at `8c443f2` | Result |
| --- | --- | --- | --- |
| Fence tripwire (fails if capacity default is raised) | `tests/contract/test_egress_fence_latent_to_live_tripwire.py` | locally | **1 passed** |
| Concurrency red-team (records the hole at capacity 2) | `tests/fleet/test_t_fl_08_concurrency_redteam.py` | locally | **7 skipped** — infrastructure-gated |

The T-FL-08 skip reason, quoted from the run:

```
SKIPPED tests/fleet/test_t_fl_08_concurrency_redteam.py:212:
  QEC_TEST_QEC_DATABASE_URL / QEC_TEST_SUBSTRATE_DATABASE_URL not set
  — T-FL-08 needs the qecentral + substrate test DSNs
```

This is a declared infrastructure skip, not a silent hole: `.github/workflows/ci.yml:727`
sets `QEC_TEST_QEC_DATABASE_URL`, so **CI is the lane that adjudicates T-FL-08**, and
`tests/_infra_gate.py` turns a missing-infrastructure skip into a failure when
`QEC_REQUIRE_*` is set. Nexus QA CI is green at this SHA.

**What this means for (B):** the constraint is guarded by a `server_default` and
by two tests. **Neither prevents an operator setting `capacity = 2` on a live
registry row.** That gap is real, is not closed by this decision, and is the
subject of §5.

## 5 · What is NOT closed, and the owner seat

The prior record recommends **(b+)** — (B) plus a *runtime refusal* so that
`capacity > 1` cannot be used while the fence is per-worker, implemented at
`app/controlplane/scheduling/worker_registry.py::acquire_slot` and at
registration. **(b+) is endorsed by this decision and is NOT implemented here.**

Two reasons, both concrete:

1. **It needs an owner with authority over the scheduler.** No `CODEOWNERS` file
   exists in this repository, so there is no in-repo convention that can name
   one. Per the Gate 5 doctrine that no agent may write a human name into a
   record, this seat is left **VACANT and must be appointed**.
2. **A hazard this session measured that the prior record does not mention.**
   `test_t_fl_08_concurrency_redteam.py` is `xfail(strict=True)` at capacity 2.
   A runtime refusal in `acquire_slot` changes what that test observes; under
   `strict=True`, an xfail that starts passing becomes a **CI failure**. So (b+)
   must land together with a rewrite of T-FL-08 from "records the hole" to
   "proves the refusal", in one change. Landing the refusal alone would turn CI
   red at the next push.

**Path correction for whoever takes it:** the prior record cites
`controlplane/scheduling/worker_registry.acquire_slot`; the file is actually at
`app/controlplane/scheduling/worker_registry.py`.

### Trigger for revisiting

This decision is revisited if, and only if, **intra-worker concurrency becomes a
named requirement**. Until then `qec_022`'s `server_default` stays at `1`, and
option (a′) — per-crawl squid instances or per-crawl ACL selection — is the only
path that may raise it.

## 6 · What this record does not claim

* It does not claim the fence is enforced at runtime above capacity 1 — it is not (§4).
* It does not claim (b+) is done — it is endorsed and unimplemented (§5).
* It does not appoint anyone.
* It does not re-prove A32 or T-FL-08; it cites them and records that one of the
  two did not run in this lane.
