# VKPower Verdict — Pilot Engagement: Scope & Readiness

*A supervised, single-tenant pilot on your own infrastructure.*

## What Verdict is

Verdict is an autonomous test-automation system that **generates real Playwright tests
you own** from your application, **runs them, and checks each ran as intended** — not
merely that the run was "green." It is built to catch genuine regressions and to refuse to
fabricate a pass. It deploys entirely on **your own infrastructure** (on-prem or
air-gapped), so your recordings, test evidence, and application data never leave your
environment.

## This engagement is a supervised, single-tenant pilot

- **Single-tenant.** One dedicated Verdict instance, installed on *your* cluster, testing
  *your* application. There is no shared or co-hosted infrastructure.
- **On-prem / in-your-environment.** The evidence Verdict produces *is* the product, and
  it stays on your infrastructure.
- **Supervised.** Our team drives the installation and the first runs together with you.

## What is proven today

- **Reproducible, verifiable install (control plane).** The Verdict control plane
  (`qe-central`) deploys from a clean build on a reference Kubernetes cluster, and the
  running component is verified **byte-for-byte identical to the reviewed source code**.
  This was proven in a development-mode reference install (see *what the pilot completes*
  for the production-hardened path). The remaining services — the crawler, repo-intel, and
  the portal — deploy through the **same Helm chart and the same code-equals-source
  verification gate**, exercised and verified when they are enabled on your pilot cluster.
- **The autonomous loop, demonstrated end-to-end.** On a representative Next.js
  application, Verdict crawled the app, generated Playwright tests, ran them (healthy app:
  9 of 9 green), a **broken route was flagged as a genuine regression needing review with
  no green-wash**, and restoring the route returned it to green. This regression detection
  is at the **flow level**. Per-step, value-level checks (e.g. "the premium equals the
  expected number") are built and their oracles were grounded on that app; **running those
  value assertions inside the generated tests is part of what the pilot completes on
  yours.**
- **A backup restore-drill that actually restores.** Verdict ships an automated
  restore-drill that dumps each database, **restores it into a throwaway database, and
  verifies the schema revision matches and that seeded rows survive** — not just "we have
  backups." This drill **passed a rehearsal against a real PostgreSQL** and is wired as a
  blocking CI gate. (It has not yet completed a green end-to-end CI run; we re-run and
  verify it against *your* database during the supervised install.)

## Configured and enabled during the supervised install

- **Fail-closed security.** Verdict is built to **refuse to start with a development-grade
  encryption key in a deployed (staging/production) environment**, requiring a KMS-backed
  key — this boot gate is wired into startup and covered by unit tests. (The reference kind
  install deliberately ran as *development*, where the gate warns rather than refuses; the
  refusal itself is verified by tests.) The chart externalizes every secret to your
  KMS / secret store (External Secrets), and restricts outbound network access via a
  default-deny NetworkPolicy plus an egress allowlist proxy — all enabled and verified on
  your cluster during the supervised install.
- **The full plane, including the crawler.** The crawler (`qe-explorer`) is enabled and
  verified on your cluster through the same install + code-equals-source gate.
- **Single-tenant by deployment.** Because the pilot runs as one dedicated instance on
  your cluster, no other tenant's data exists on it. (This is a property of single-tenant
  deployment, not an intra-tenant isolation control; cross-tenant isolation is out of
  scope below.)

## Explicitly out of scope for this pilot (stated plainly)

- **Multi-tenant co-hosting / cross-tenant isolation guarantees** — not part of a
  single-tenant pilot, and not claimed here.
- **General Availability (GA)** — this is a supervised pilot, not a self-serve production
  product.
- **Independent third-party security audit / penetration test** — recommended before broad
  production rollout; not yet performed.
- **High-scale load / soak certification** — the pilot targets your app at pilot volume, not
  a certified peak-load guarantee.

## What "the pilot worked" means (success criteria)

1. Verdict **installs cleanly** on your cluster (full plane), every component matching
   reviewed source, in the production-hardened configuration (KMS, external secrets,
   egress allowlist).
2. It **autonomously generates** owned Playwright tests for an agreed set of your key flows.
3. It **runs them and catches an introduced regression** (a broken route, and — with the
   value assertions running — a wrong result) **without a human wiring the check**.
4. Your evidence is **backed up and a restore is demonstrated on your database**.

## What we need from you

- A Kubernetes cluster in your environment (or a VM we stand one up on).
- Access to the target application (a test / UAT environment is ideal).
- Your KMS (or a mounted key) for production-grade encryption at rest.
- A technical point of contact for the supervised install and review.

## Honest posture

This pilot proves Verdict's **core value** — proof-of-behavior test automation, on your
infrastructure, on your application — under supervision. It is **not** a claim of GA,
multi-tenant operation, or independently-audited production readiness. We scope it this
deliberately narrowly for one reason: **so the pilot succeeds on what is real, and you can
trust every green we show you.**
