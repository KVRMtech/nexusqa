# Acme Life — life-insurance proving ground

A synthetic, self-contained life-insurance web app used as the **beachhead-representative crawl target** for VKPower Verdict (Phase 6.5). It mirrors the Aegis/Skyward proving-ground pattern: an accessible SPA served by `nginx:alpine`, driven by any credentials.

## The flow
`#/login → #/quote → #/apply → #/review → Bind policy`

Every input carries a real `<label for>` (the crawler inventories by accessible name); the terminal action is named **"Bind policy"** so the explorer's irreversible-verb refuse-pack catches it; a modal confirms the bind. Synthetic data only (SSNs in the `900-xx-xxxx` range, fake names).

## The business rule (the invariant Verdict certifies)
> **An applicant over 60 cannot bind coverage above $500,000.**

Enforced in `enforceOver60Rule()` — checked both when Bind is opened *and* at confirm.

## The MUST-REFUSE probe (the break-mode)
Append **`?break_over60=1`** to the URL (or set `localStorage acme_break_over60=1`). The rule is disabled, so an over-60 $600k bind *wrongly succeeds*.

- A **certified Verdict suite for this invariant must go RED under the probe.**
- If it stays green under the probe → that's green-wash → the run **fails**.

This is exactly what the Phase-0 REFUSE-matrix logic proves, now on a real insurance-shaped app.

## Run it
```bash
docker build -t acme-life .
docker run -p 8097:80 acme-life
# → http://localhost:8097
```
Or via compose (profile-gated so the default `up` doesn't build it):
```bash
docker compose -f docker-compose.qec.yml --profile grounds up -d acme-life
```

## Benchmark
`answer_key.json` is the measure-first artifact: the enumerable universe (routes + forms), the critical scenarios, the P0 over-60 invariant + its break-mode, and the grading rules (discovery recall, criticality precision, refuse-proof, behavioral tier). Verdict grades its live crawl against this key.
