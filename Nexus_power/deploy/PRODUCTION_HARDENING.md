# QE-Central — Production Hardening Runbook

The reproducible, secure-deploy checklist a regulated-buyer handover requires. Every
item here closes a specific gap the 2026-07-20 readiness audit flagged as living
*outside* the reproducible deploy. Work top to bottom; nothing here changes the
frozen VKPower factory code.

> **The enforcement is real, the recipe was missing.** `app/security/boot_validator.py`
> already **refuses to boot** in a deployed env (`NEXUS_ENV` ∈ {staging, production})
> if a KEK is `local` or any signing/DB secret is still a dev default. This runbook is
> how you satisfy that gate instead of accidentally running the dev posture.

---

## 1. Secrets & environment (fail-closed at boot)

- [ ] `cp deploy/.env.prod.example deploy/.env.prod` and fill **every** `__CHANGE_ME__`.
- [ ] Set `NEXUS_ENV=production` — this flips the boot gate from *warn* to *hard-fail*.
- [ ] Replace all dev defaults: `QEC_DB_PASSWORD`, `QEC_SUBSTRATE_DB_PASSWORD`,
      `NEXUS_JWT_SECRET`, `QEC_EXPLORER_TOKEN` (must match the qe-explorer side).
- [ ] Set a **real KMS KEK**: `NEXUS_KEK_PROVIDER=gcp|aws` + the key ref. `local` is
      refused outside dev — credentials are envelope-encrypted at rest, and a plaintext
      credential write already returns HTTP 503 rather than store it.
- [ ] Keep `.env.prod` **out of git** and off shared disks; source it from your secret
      manager at deploy time.
- [ ] **Verify:** boot the stack and confirm qe-central reaches `healthy`. If a secret
      is still a dev default, the container exits with the offending setting named —
      that is the gate working.

## 2. Tenant-isolation RLS on the audit-evidence tables

The QE-Central tables (23) already ENABLE+FORCE RLS via `qec_001`. The **evidence**
tables — `verdict_events`, `decision_dossiers`, `verification_waivers`,
`finding_labels` — are created out-of-band on the SDK Base, so that loop never reached
them; today their isolation leans on the app WHERE-clause alone.

- [ ] Apply the idempotent, reversible RLS script against the **nexus** DB:
      `psql "$NEXUS_DATABASE_URL" -f scripts/apply_evidence_rls.sql`
- [ ] **Before** applying in a shared factory+QEC deployment, confirm every writer of
      these tables sets the `nexus.current_tenant_id` GUC (qe-central does, via
      `tenant_scoped_session`). FORCE RLS makes an un-scoped read return zero rows —
      intended for isolation, but verify no factory path reads them without the GUC.
      *(This is why it is a deliberate ops step, not a boot-time auto-apply.)*
- [ ] **Verify:** `SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class
      WHERE relname = ANY(ARRAY['verdict_events','decision_dossiers',
      'verification_waivers','finding_labels']);` — all should show `t, t`.

## 3. Backups + restore drill (no silent data loss)

- [ ] Schedule `scripts/verdict_pg_backup.sh` (pg_dump) on a fixed cadence with
      **off-box** retention (object storage, not the DB host).
- [ ] Wire staleness alerting: page if the newest backup is older than the SLA.
- [ ] **Do a restore drill** into a scratch database and confirm row counts — an
      untested backup is not a backup.

## 4. Network isolation of the explorer (crawler)

The operator-authorization gate (`prod_guard.assert_crawlable`) lives in qe-central.
The explorer's own `POST /api/v1/explore` is bearer-token-only and does **not** re-check
it, so a caller reaching the explorer directly could crawl an arbitrary URL.

- [ ] Place `qe-explorer` on an internal network reachable **only** from qe-central's
      dispatch path. Do not expose `:8210` to any client- or internet-reachable network.
- [ ] Confirm the per-fleet `QEC_EXPLORER_TOKEN` is set and identical on both services.

## 5. Source & reproducibility

- [ ] Push `develop` to a durable, access-controlled remote (the full product must not
      live only on a laptop + the VM). Keep an off-box repo bundle as a cold backup.
- [ ] Build a **tagged image** from that commit for the deploy — do not rely on
      docker-cp overlays; a container *recreate* must reproduce the running system.
- [ ] Record the deployed git SHA and image digest with the release.

## 6. Post-deploy verification

- [ ] qe-central `healthy`; a real crawl completes end-to-end
      (`register app → authorize → crawl → cases`; verify via `stats.generate`).
- [ ] The four safety gates fire: authorization refusal on an un-attested URL,
      the destructive-verb actuation gate, the disposable+approval submit gate,
      verified-not-assumed login.
- [ ] Spot-check tenant isolation: a request scoped to tenant A cannot read tenant B's
      cycles/evidence.

---

*Additive only — no frozen-factory code changes. Pairs with `deploy/.env.prod.example`.*
