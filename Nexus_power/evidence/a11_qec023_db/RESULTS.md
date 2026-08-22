# A11 / qec_023 — database verification against real PostgreSQL

**Date:** 2026-08-22 · **Commit:** `0731e31` · **Postgres:** 16.14 (throwaway container)

Closes the A11 open item *"the `qec_023` RLS coverage gate has not run — it is
`QEC_TEST_QEC_DATABASE_URL`-gated and skips without Postgres."* It had been
recorded as *"follows the qec_003 pattern exactly, but 'follows the pattern' is
not 'was verified against a database'.* This is the verification.

---

## What was run

A disposable Postgres 16 container (`a11-qec023-pg`, port 55423) — deliberately
separate from the three containers other concurrent sessions had running, so
nothing of theirs was touched.

```bash
docker run -d --name a11-qec023-pg -e POSTGRES_PASSWORD=… \
  -e POSTGRES_DB=qecentral -p 55423:5432 postgres:16-alpine

# roles from the PRODUCTION bootstrap script, not hand-written SQL
docker cp scripts/qec_db_bootstrap.sql a11-qec023-pg:/tmp/bootstrap.sql
docker exec … psql -U postgres -d qecentral -f /tmp/bootstrap.sql

export QEC_DATABASE_URL="postgresql+asyncpg://…@localhost:55423/qecentral"
python -m alembic -c alembic_qec/alembic.ini upgrade head     # → qec_023
```

`qec_023` applied cleanly from an empty database through the full 23-revision
chain. **This is the first time it has been executed against a real
PostgreSQL.**

---

## 1. RLS coverage gate — PASSES

```
QEC_TEST_QEC_DATABASE_URL=… QEC_REQUIRE_DB=1 \
  pytest tests/contract/test_rls_coverage_complete.py
→ 7 passed
```

The gate is self-discovering: it derives its subject set from the schema (*a
table with a `tenant_id` column must be isolated by the database*), so
`qec_023`'s three tenant tables came under contract automatically. All four
assertions hold for them:

| Table | ENABLE + FORCE RLS | policy covers 4 DML | policy is GUC-scoped |
| --- | --- | --- | --- |
| `env_provisioning_records` | ✅ | ✅ | ✅ |
| `attestation_revocations` | ✅ | ✅ | ✅ |
| `attestation_issuance_log` | ✅ | ✅ | ✅ |

`attestation_issuer_keys` is accepted as tenant-free via its declared
`_NO_TENANT_COLUMN` justification (fleet infrastructure; the issuer identity
belongs to the deployment, and the row's secret is protected by KMS envelope
encryption rather than by RLS). The gate checks that allowlist in **both**
directions, so the entry is not an escape hatch: a stale name fails, and a table
that later gains a `tenant_id` column fails until it is removed from the list.

The gate's own canary (`test_the_gate_actually_has_teeth`) passed in the same
run, so the discovery query is not silently returning zero tables.

---

## 2. Migration round-trip — UP → DOWN → UP

`qec_023.downgrade()` had never been executed. It was:

```bash
alembic downgrade qec_022      # → all four tables dropped
psql -c "SELECT count(*) FROM information_schema.tables WHERE table_name IN (…)"
→ 0
alembic upgrade head           # → qec_023 re-applied
pytest tests/contract/test_rls_coverage_complete.py tests/contract/test_index_contract.py
→ 12 passed, 2 skipped
```

RLS policies, the partial unique indexes and the CHECK constraints are all
re-established by the second upgrade — the round-trip is clean, and the policies
survive it. A downgrade that dropped a table while orphaning its policy would
have shown here.

---

## 3. Wider contract suite

```
pytest tests/contract  →  215 passed, 2 failed, 23 skipped
```

The two failures are `test_reaper_db.py`, and they are **not A11's**. Proven by
falsification rather than asserted: with the database downgraded to `qec_022` —
every A11 table absent — they fail identically.

```
at qec_023 (A11 present):  2 failed
at qec_022 (A11 absent):   2 failed   ← same two, same error
```

Both fail with `socket.gaierror: getaddrinfo failed`, i.e. they never reach the
database at all. It is an environment gap in this minimal bootstrap (the
substrate `nexus` chain was not built, so the substrate DSN is unset), not a
defect and not a regression. CI builds both databases and runs these green.

---

## What this does NOT prove

* **Not the `qec` least-privilege role path.** The bootstrap SQL aborts partway
  (`relation "sessions" does not exist`) because the substrate `nexus` chain was
  not built here. The coverage gate is catalog reads and runs correctly under
  either role, so its result stands — but the *behavioural* cross-tenant proof
  in `test_rls_isolation.py` needs the least-privilege role and was not run.
  CI runs it.
* **Not GCP KMS.** Separate item; see below.
* **Not a live deployment.** A throwaway container is not the production
  database.

---

## Related item, now a PROVEN blocker rather than an assumption

The A11 record carried *"GCP KMS unexercised — needs the VM."* That has been
narrowed to a specific, actionable cause:

```
gcloud kms keys list --keyring=verdict --location=asia-southeast1
→ kek   ENCRYPT_DECRYPT   ENABLED          # the key EXISTS and is reachable

gcloud kms encrypt --key=…/kek …
→ ERROR: IAM_PERMISSION_DENIED
```

The interactive `gcloud` account can **list** the KEK but not **use** it. That
is correct least privilege — `roles/cloudkms.cryptoKeyEncrypterDecrypter` is
held by the VM's service account, not by a human user. So the KMS custody path
cannot be exercised from a workstation by design, and the existing
`scripts/a37_verify_kms_decrypt.py` is written to run from the VM inside the
service container for exactly that reason.

**To close it:** run `a37_verify_kms_decrypt.py` on the VM, or grant the
encrypter/decrypter role to a human principal for a one-off verification. The
second weakens the least-privilege posture and is not recommended.

Not attempted from here: installing `google-cloud-kms` into the shared
interpreter (nine concurrent sessions use it) or running
`gcloud auth application-default login` (interactive, and it writes a credential
file to the user's machine).
