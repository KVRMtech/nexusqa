# Evidence Signing, Retention and E-Signature — Operator Guide

Closes the three configuration decisions left open by
`EXECUTION_EVIDENCE_REPORT_SPEC.md` §6. All three are **implemented and live**;
this document is what an operator needs to run them safely.

---

## 1. Evidence signing key (tamper-EVIDENT → tamper-PROOF)

Every export carries a SHA-256 `chain_root`. With a signing key configured, the
root also gets a **detached HMAC-SHA256 signature**, so an attacker who can
rewrite a package cannot also forge a root that verifies.

**Resolution order (file first, deliberately):**

1. `NEXUS_EVIDENCE_SIGNING_KEY_FILE` — path to a secret file
2. `/run/secrets/nexus_evidence_signing_key` — the default path
3. `NEXUS_EVIDENCE_SIGNING_KEY` — env var (fallback)

A file is preferred because it can be mounted, rotated and permission-controlled
**without recreating the container**, and it does not leak into `docker inspect`,
a process listing or a crash dump.

### ⚠ Durability — read this

The key provisioned on 2026-07-26 lives at `/run/secrets/nexus_evidence_signing_key`
inside `nexus-platform-api`, **in the container's writable layer** — that path is
NOT a mounted volume today. Consequences:

* `docker restart` — key survives (verified).
* `docker compose up --force-recreate`, an image rebuild, or moving hosts —
  **the key is lost** and exports silently revert to `signed: false`.

The package never lies about this (it states `signed: false` and explains the
difference), and an unsigned export now logs
`test_factory.evidence_export_UNSIGNED` at WARNING — but the only real fix is to
mount the secret. Add to the compose service:

```yaml
services:
  platform-api:
    secrets:
      - nexus_evidence_signing_key

secrets:
  nexus_evidence_signing_key:
    file: ./secrets/nexus_evidence_signing_key   # chmod 400, NEVER committed
```

Or bind-mount a host path to `/run/secrets/`.

### Generating / rotating

Generate **on the host**, never in a chat transcript or a shell history:

```bash
head -c 48 /dev/urandom | od -An -tx1 | tr -d ' \n' > ./secrets/nexus_evidence_signing_key
chmod 400 ./secrets/nexus_evidence_signing_key
```

**Rotation is not retroactive.** Signatures are computed at export time, so
packages exported under the old key verify only against the old key. Keep
retired keys for as long as the exports they signed must remain verifiable, and
record the rotation date alongside them.

**Confirm which key is in play** — never assume:

```
GET /api/v1/test-factory/{artifact_id}/report/audit-trail
  → signing_enabled, signing_key_source   # e.g. "file:/run/secrets/..."
```

---

## 2. Evidence retention (bounded storage, unbroken audit trail)

Traces are megabytes; screenshots are kilobytes. Each media class gets its own
hot window:

| Class | Default | Why |
|---|---|---|
| `application/zip` (traces) | 30 days | dominates storage |
| `video/webm`, `video/mp4` | 30 days | same |
| `application/json` (T3 diagnostics) | 90 days | small, queryable |
| `image/*` (screenshots) | 365 days | cheap, and what reviewers actually open |

Override per class:
`NEXUS_RETENTION_APPLICATION_ZIP_DAYS=60`. **`0` = keep forever.**

### Tombstoning, not deletion

Past its window an artifact's **bytes** are reclaimed, but the row survives
carrying its SHA-256, original size and reclaim date. Anyone holding an exported
copy can still prove it genuine by hashing it against the retained digest.
Deleting outright would silently rewrite history — a report that once linked a
trace would show nothing, and no one could tell whether it never existed or was
quietly removed.

**Run/step rows, statuses, attributions and verdicts are never touched.** The
report's numbers do not change when storage is reclaimed.

```
POST /api/v1/test-factory/{artifact_id}/evidence/retention          # DRY RUN
POST /api/v1/test-factory/{artifact_id}/evidence/retention?apply=true
```

Dry run is the default: an irreversible sweep must be a deliberate act. Admin or
manager only, and an applied pass is recorded on the audit chain.

Suggested cadence: a weekly dry run reviewed by an operator, then an applied
pass. There is deliberately **no automatic sweep** — evidence disposal should not
happen because a cron job existed.

---

## 3. E-signature method

`NEXUS_ESIGN_METHOD` selects what counts as a signature on a review disposition:

| Value | Meaning |
|---|---|
| `typed_name` *(default)* | the reviewer types their full name — one factor, and honest about being one |
| `sso_session` | the IdP-authenticated session itself: subject, email, issuer are recorded from the validated token; a reviewer cannot type someone else's identity |
| `both` | requires both — closest to a 21 CFR Part 11 two-component signature (an authenticated identity **plus** a deliberate act) |

**Recommendation for regulated tenants: `both`.** A typed name alone is
repudiable; an authenticated session alone lacks the deliberate act.

The disposition records `signature.method` and `signature.components`, so an
auditor sees not just *that* it was signed but *how*. An unmet requirement is
recorded as **unsigned** — a merely authenticated request is never upgraded to a
sign-off.

---

## Verified live (2026-07-26)

* signing: `signing_enabled=true`, `key_source=file:/run/secrets/…`, export
  header `X-Nexus-Signed: true`, manifest `signed=true`,
  `hmac-sha256-detached`, 64-hex signature;
* retention: dry run scanned 116 artifacts, 0 past window (all evidence is
  recent), viewer refused with 403;
* e-signature: typed name → signed with `components:[typed_name, sso_session]`;
  omitting the name → `signed=false` under the `typed_name` policy.
