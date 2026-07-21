# Reproducible deploy — "what runs == a clean build from git"

Phase 0's finish line: every running instance is provably the git checkout, with no
hand-applied `docker cp` overlays. This is achieved by two halves — **build from git**
and **prove what runs matches git** — plus one discipline: **never patch a live box by
hand.**

## Half 1 — build from git (already exists)

`.github/workflows/cd.yml` is the reproducible build+deploy pipeline:

```
test → metadata(tag) → build-base → build-services (matrix, push to GHCR)
     → build-client → deploy-staging (Helm/K8s) → load-test-gate → deploy-production
```

Every image is built from a clean checkout and pushed to GHCR by tag; deploys pull
those tags via Helm. Nothing is copied into a running container. This is the correct
production path.

> **Honest status:** the pipeline is well-formed but its end-to-end operation is
> **unverified** (the visible CI history shows a failure; the `verdict-box` VM — a
> docker-compose *dev/eval* box — is the de-facto environment we have been using).
> Validating the pipeline for real (GHCR auth, Helm charts apply, the K8s clusters
> exist, the load-test gate passes) is tracked infra work, not done here.

## Half 2 — prove what runs == git (the missing piece, now built)

`verify_deployment.py` content-hashes every runtime file in a git tree and in the
deployed copy and fails on any missing / extra / different file. Line endings are
normalized, so a CRLF checkout vs an LF container is **not** a false diff (the trap
that made an earlier by-hand check misread `compiler.py`). Tested by
`test_verify_deployment.py`.

**Run it as the last step of any deploy — mandatory gate.**

Kubernetes (per deployed pod):
```bash
kubectl exec deploy/<service> -- tar -C /app/service/app -cf - . | tar -C /tmp/dep -xf -
python ci/reproducibility/verify_deployment.py \
    --git-root platform/<service>/app --deployed-root /tmp/dep
```

Docker / the dev VM (per container):
```bash
python ci/reproducibility/verify_deployment.py \
    --git-root platform/api/app \
    --container nexus-platform-api --container-path /app/service/app
```

Exit non-zero ⇒ the running instance is **not** this git tree — the deploy is not
reproducible; fix the deploy, never silence the check.

## Cutting the dev VM over to reproducible deploys

Retire `docker cp` on `verdict-box`:

1. Ensure the branch is pushed so the canonical remote has everything.
2. On the VM, `git -C <src> pull` to the deployed commit.
3. Rebuild + recreate from that source (no cp overlays):
   `docker compose build <service> && docker compose up -d --force-recreate <service>`
   (or `docker pull` the GHCR tag the pipeline built, then `up -d`).
4. **Run `verify_deployment.py` against every recreated container.** Green = the box
   is provably the git checkout. Any red is a real drift to resolve before calling it
   reproducible.
5. From then on, deploy only by rebuild/pull + verify — never by `docker cp`.

## Why this closes Phase 0

Build-from-git (Half 1) + a passing deploy-verify (Half 2) + no hand patching =
"a clean build" and "what runs" are the **same artifact, proven on every deploy.**
