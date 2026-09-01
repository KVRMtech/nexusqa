# A11 — Independent Certification Checklist

**Status: NOT CERTIFIED. This document is unsigned.**

The ARB rule: *A12 (Walk Persistence / T-WP-01) must not begin until A11 has
been independently certified by a different engineering squad. Implementation
alone is insufficient.*

A11 was implemented by one squad. A self-signed certification would defeat the
entire purpose of that rule, so none is offered. This is the package for the
**non-author** squad who performs it.

You do not need to have read the implementation. You need to be able to break
it, and to fail to.

---

## Before you start

Do not skip this: **the point of an independent review is that you do not
inherit the author's assumptions.** Read §8 of `A11_ATTESTATION_ISSUER.md`
("What this milestone does NOT guarantee") *last*, not first — form your own
view of the limits before reading the author's, then compare. A limit the author
found and you did not is fine. A limit you found and the author did not is the
finding that matters.

---

## Step 1 — Reproduce the suite

```bash
cd platform/qe-central
python -m pytest tests/security/test_a11_attestation_redteam.py \
                 tests/security/test_a11_api_authorization.py \
                 tests/security/test_a11_dispatch_integration.py \
                 tests/contract/test_gate1_walk_attestation_contract.py \
                 -v -p no:randomly
```

Expected: **143 passed**.

> `-p no:randomly` works around a pre-existing environment defect on this
> workstation (`pytest-randomly` × `thinc`/numpy seeding: `ValueError: Seed must
> be between 0 and 2**32 - 1`). It is unrelated to A11 and predates it — confirm
> that for yourself on `git stash`, and record it as an environment finding, not
> an A11 one.

Also confirm no regression:

```bash
python -m pytest tests/ -q -p no:randomly
cd ../../engines/qe-explorer
python -m pytest tests/ -q -p no:randomly --ignore=tests/browser
```

Assert **zero failures**, not a fixed count: this checkout is shared with
several concurrent sessions landing other gates, so the totals move under you.
At the time of writing they were 2421 and 2006. If either suite is red, confirm
the failure is not A11's before treating it as one — `git stash` the A11 files
and re-run.

- [ ] 143 A11 tests pass (this number IS fixed — the A11 suite is self-contained)
- [ ] qe-central suite: zero failures
- [ ] qe-explorer suite: zero failures

---

## Step 2 — Verify the suite is testing the real thing

A red-team suite that verifies against its own idea of a verifier certifies
nothing. **Check this yourself rather than taking §6.1 on trust.**

```bash
# The suite loads the SHIPPING verifier by path. Prove it by breaking it:
cd engines/qe-explorer
cp app/attest.py /tmp/attest.py.bak
# Neuter the signature check:
python - <<'EOF'
p='app/attest.py'; s=open(p,encoding='utf-8').read()
s=s.replace('def _verify_ed25519(public_key_b64: str, payload: Any, signature_b64: str) -> bool:\n    """',
            'def _verify_ed25519(public_key_b64: str, payload: Any, signature_b64: str) -> bool:\n    return True\n    """',1)
open(p,'w',encoding='utf-8').write(s)
EOF
cd ../../platform/qe-central
python -m pytest tests/security/test_a11_attestation_redteam.py -q -p no:randomly
# EXPECT FAILURES (the forged-signature tests must go red).
cp /tmp/attest.py.bak ../../engines/qe-explorer/app/attest.py
```

- [ ] Neutering `attest.py` turns the forgery tests **red** — the suite really
      exercises production code, not a copy
- [ ] `app/attest.py` restored byte-for-byte (`git diff` is clean)

Then check the *other* direction — that the suite is not merely a machine that
refuses everything:

- [ ] `test_a_genuinely_provisioned_disposable_environment_enables_walk` passes,
      and asserts `authorized is True` with a bounded budget
- [ ] `test_a_proof_is_still_valid_one_millisecond_before_expiry` passes
- [ ] `test_a_platform_admin_token_does_reach_the_certification_route` passes

---

## Step 3 — The five required scenarios

Confirm each rejects, and confirm the **reason code** — a refusal for the wrong
reason is a coincidence, not a control.

| # | Scenario | Expected | Test |
| --- | --- | --- | --- |
| 1 | Forged signature | REJECTED `unknown_key_id` | `test_forged_signature_is_rejected` |
| 2 | Expired proof | REJECTED `expired` | `test_expired_proof_is_rejected` |
| 3 | Revoked proof | REJECTED `revoked` | `test_revoked_proof_is_rejected` |
| 4 | Replay | REJECTED `crawl_binding_mismatch` | `test_replaying_a_proof_into_a_different_crawl_is_rejected` |
| 5 | Tenant self-attestation | REJECTED `no_provisioning_record` | `test_a_tenant_cannot_certify_its_own_environment` |

- [ ] 1 — forged signature rejected, correct code
- [ ] 2 — expired proof rejected, correct code
- [ ] 3 — revoked proof rejected, correct code
- [ ] 4 — replay rejected, correct code
- [ ] 5 — tenant self-attestation rejected, correct code

---

## Step 4 — Read these four things and disagree with them

The tests prove the code does what the author intended. These are the places
where the *intention* could be wrong.

### 4.1 `env_provisioning_records` is the whole milestone

`app/services/attestation_issuer.py` reads `env_kind` from **one** table.
Satisfy yourself that:

- [ ] no code path falls back to `app_environments.env_attestation` (grep
      `env_attestation` across `app/services/attestation_*.py` — expect only
      comments explaining why not)
- [ ] the only writer of `env_provisioning_records` is guarded by
      `require_platform_admin` (grep `EnvProvisioningRecordRow(` — expect one
      construction site, in `routers/attestation.py`)
- [ ] a tenant-scoped JWT **cannot** carry `platform_admin` — read
      `app/fleet/rbac.py` and decide for yourself whether that is structural or
      merely conventional

### 4.2 The origin pin (gate 3)

- [ ] read `_check_origin_has_not_moved`. Convince yourself that a tenant who
      certifies a throwaway host and then `PATCH`es `base_url` to production
      cannot obtain a proof
- [ ] confirm the signed claims are built from the **record's pin**, not from
      the environment row
- [ ] consider: is there any *other* tenant-writable field that reaches the
      claims? (Author's answer: `reset_procedure` — from the record, not the
      row — and it is not consulted by any gate.)

### 4.3 Fail-closed on unreadable revocation state

- [ ] read `current_revocations`. It must **raise**, never return an empty
      `RevocationState`, when the read fails
- [ ] read `RevocationCache`. Confirm no code path populates or serves it on a
      failed read
- [ ] read §4.3 of the architecture doc on when revocation takes effect. Decide
      whether "enforced at admission, not continuously" is acceptable for A12,
      **or whether it blocks A12** — this is a judgement call the author has
      deliberately left to you

### 4.4 Key custody

- [ ] read §2.2 of the architecture doc — the claim that KMS-native Ed25519
      signing is unavailable. **Verify it independently** (GCP KMS asymmetric
      sign algorithms); if it is wrong, the custody model should change
- [ ] confirm nothing returns a plaintext private key (`grep -rn
      "private_key" app/services/attestation_keys.py` — expect only the sealed
      blob and the scoped `Signer` internals)
- [ ] decide whether "plaintext in heap during a signature" is an acceptable
      residual risk for this threat model

---

## Step 5 — Try to break it yourself

The author's tests encode the author's imagination. Spend an hour on yours.
Suggested starting points that the suite does **not** cover:

- [ ] concurrency: two simultaneous rotations; issuance racing a revocation
- [ ] clock: issuer and verifier skewed past `QEC_ATTESTATION_SKEW_MS`
- [ ] a provisioning record whose `target_origin` differs from the environment's
      `base_url` only by case, trailing slash, or IDN homograph
- [ ] `POST /provisioning-proof` with a `crawl_id` belonging to another tenant's
      in-flight crawl
- [ ] whether `attestation_issuance_log` growth is bounded, and what happens
      when it is not
- [ ] the `__platform__` KEK tenant id — can any real tenant be provisioned with
      it? (This checklist originally asserted "double underscores are not a
      legal tenant slug". **That was false** — `provision_tenant` takes
      `tenant_id` verbatim. Writing this line is what found it. Now enforced by
      `fleet.provisioning.RESERVED_TENANT_IDS`, pinned by
      `test_the_platform_kek_tenant_id_cannot_be_assigned_to_a_customer`.
      Re-verify the enforcement rather than the original claim.)

Record anything you find here, whether or not it changes the verdict.

---

## Step 6 — What CANNOT be certified from a workstation

These are open by construction, not oversights. Note them; do not sign them off.

- [ ] **GCP KMS is unexercised.** Custody is proven against `LocalKekProvider`
      with real AES-GCM. `GcpKmsProvider` changes where the KEK lives, not the
      envelope format — but that substitution is unproven. Requires a VM with a
      real KMS key. (Per `M0_CLOSURE`, prod deploy is separately blocked on a
      missing GCP KMS key.)
- [ ] **The RLS coverage gate for `qec_023` has not run.** It is
      `QEC_TEST_QEC_DATABASE_URL`-gated and skips without Postgres. Run it
      against a migrated database.
- [ ] **Migration round-trip (upgrade → downgrade → upgrade) unverified.**
- [ ] **No live end-to-end proof.** No crawl has enabled `Phase.WALK` against a
      real application under a real KMS-sealed key. Deliverable #9 of the brief
      is satisfied *in test* (`test_a_genuinely_provisioned_disposable_
      environment_enables_walk` and `test_dispatch_attaches_a_proof_the_real_
      verifier_accepts`), **not in production.**

---

## Verdict

| | |
| --- | --- |
| Reviewing squad | ______________________ |
| Reviewer(s) | ______________________ |
| Date | ______________________ |
| Commit reviewed | ______________________ |
| Suite result | ______ passed / ______ failed |
| Findings raised | ______________________ |

**Verdict** (circle one):

- **CERTIFIED** — A11 is complete; A12 (T-WP-01) may begin.
- **CERTIFIED WITH CONDITIONS** — conditions: ______________________
- **NOT CERTIFIED** — A12 must not begin. Reasons: ______________________

Signature: ______________________

---

Until this page is signed by a squad that did not write the implementation,
**A11 is incomplete and A12 must not begin.**
