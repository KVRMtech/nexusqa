# R5′ — deploy the candidate branch SHA, not `develop`

**Status: PREPARED, NOT EXECUTED. Awaiting the human's scheduled window — the VM
serves live applications.**

The original R5 was refused as unsatisfiable: its acceptance was "rerun the M2.1
catalogue proof against the deployed service", and that proof
(`tests/browser/test_questionnaire_catalog_e2e.py`) builds a `Crawler` with **no
HTTP client and no service URL** and folds the catalogue in-process — its own
comment says *"the SAME derivation `journey_fold` performs, minus the DB"*. It
cannot exercise a deployed service before or after a deploy.

R5′ replaces that acceptance with one that is actually satisfiable, and defers
the rest explicitly rather than silently.

---

## 1 · Scope, and the boundary that is NOT crossed

| | |
| --- | --- |
| Deploy | the **candidate branch SHA** of `feat/qec-dynamic-catalog-p0-p6` |
| Do NOT deploy | `develop` |
| Reason | the evidence must come from the code being certified. Deploying `develop` would deploy `develop`'s state, not the candidate SHA |

**The 145-commit `develop` merge is explicitly OUT of scope** and is parked as a
separately-owned release-engineering decision. It is six sessions' work, and
`develop`'s reconciliation with `origin` is a long-standing open problem
(`CLAUDE.md §4`); entangling R5′ with it would make neither reviewable.

## 2 · Target state, measured

Read-only inspection during this run:

```
/home/srika/nexus-src   HEAD ede6bf26c68a…   branch develop   2026-08-18
GET https://136.85.106.73/health
  {"status":"healthy","service":"qe-central","db_qec":"connected",
   "db_substrate":"connected","kek":{"provider":"gcp_kms",
   "is_production_grade":true,"envelope_ready":true}}
containers: nexus-qe-central, nexus-qe-explorer, nexus-platform-api, verdict-caddy,
            nexus-postgres, nexus-redis, summitlife-*, nexus-verdict-portal  (all Up)
```

Healthy, production-grade KMS, **145 commits behind**, six migrations absent.

## 3 · Migrations — verified additive, not assumed

The six absent migrations were parsed and their `upgrade()` paths checked for
destructive operations (`drop_table`, `drop_column`, `drop_constraint`,
`drop_index`, `alter_column`, and raw `DROP`/`DELETE FROM`/`TRUNCATE` in
`op.execute`):

| migration | `upgrade()` | `downgrade()` |
| --- | --- | --- |
| `qec_018_business_rules` | **ADDITIVE** | drop_index, drop_table |
| `qec_019_catalog_evidence` | **ADDITIVE** | drop_column |
| `qec_020_catalog_retirement` | **ADDITIVE** | drop_column, execute:DROP |
| `qec_021_journey_endpoints` | **ADDITIVE** | drop_column |
| `qec_022_explorer_worker_registry` | **ADDITIVE** | drop_index, drop_table |
| `qec_023_attestation_issuer` | **ADDITIVE** | drop_index, drop_table, execute:DROP |

**Every destructive operation is confined to `downgrade()`, which a deploy never
runs.** The dump in §4 is therefore belt-and-braces, not the primary defence —
which is the right order, since a backup nobody has restored is not a proven
control.

## 4 · Order of operations

1. **🔒 DB dump BEFORE migrating** — on the VM:
   ```
   docker exec nexus-postgres pg_dump -U <user> qecentral \
     > ~/backups/qecentral_pre_qec023_$(date +%Y%m%d_%H%M%S).sql
   ```
   **GATE:** file exists, non-empty, and its size is sane against the live row
   counts. A zero-byte dump that nobody looked at is the classic version of this
   step.
2. **Deploy the candidate SHA** via `scripts/deploy.ps1` (build + `up -d
   --force-recreate`; data and KEK live on volumes so recreate preserves them —
   never `docker cp`).
   **GATE:** `docker ps` healthy; `/health` returns the same shape as §2 with
   `kek.is_production_grade: true`.
3. **Migrate** `qec_018 → qec_023`:
   ```
   docker compose -f docker-compose.qec.yml run --rm qe-central \
     alembic -c alembic_qec/alembic.ini upgrade head
   ```
   **GATE:** `alembic current` reports `qec_023`.
4. **Golden-crawl gate + manifest rollback ARMED** — not bypassed. This is the
   protection that decides whether the deploy stands.
5. **The thin deployed-service check** (§5).

## 5 · Acceptance — what R5′ actually claims

**Satisfiable, and narrow on purpose:** the deployed service answers for the
Phase-2 catalogue deliverable — the catalogue endpoint on the VM returns a
**non-empty** deliverable, served by the deployed build, with the deployed
version identified.

Evidence to capture:
* the deployed SHA, read from the VM (`git rev-parse HEAD` in `/home/srika/nexus-src`),
  matching the candidate SHA exactly;
* `alembic current` = `qec_023`;
* the catalogue response, non-empty, with row/question counts;
* `/health` before and after.

**A falsification control is required**, per this repository's standard: an
assertion that something is *present* is weak unless it can be absent. Query the
same endpoint for a tenant/app with no catalogue and require an empty result — a
check that returns "non-empty" for everything proves nothing about the deploy.

## 6 · What R5′ explicitly DEFERS

**The full deployed-service M2.1 proof is deferred to Phase 5's install
milestone.** It requires an artefact that does not exist: a variant of
`test_questionnaire_catalog_e2e.py` that drives **deployed** services over HTTP
instead of constructing an in-process `Crawler`.

| | |
| --- | --- |
| Deferred item | deployed-services variant of the M2.1 catalogue proof |
| Why | the current proof is structurally in-process (verified, not accepted) |
| Trigger | Phase 5 install milestone |
| Owner | **VACANT — to be named.** No `CODEOWNERS` exists in this repository, and per the Gate 5 rule that no agent writes a human name into a record, this is left to be appointed |

Deferring it in writing, with a trigger and an owner slot, is the honest form.
Letting R5′'s narrow check stand in for it silently would be the green-wash.

## 7 · Not claimed

* Not claimed that this deploy has happened — it has not.
* Not claimed that the thin check proves Phase 2 end-to-end. It proves the
  deployed build serves a non-empty catalogue; that is less than M2.1 and is
  labelled as such.
* Not claimed that `develop` and the VM are reconciled — they are not, and that
  is somebody else's decision.
