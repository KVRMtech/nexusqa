# Verification Platform — Client API Handover

Every endpoint is tenant-scoped (JWT bearer), deterministic by default ($0 LLM),
and returns machine-readable verdicts. Base: `/api/v1`.

## The one call that answers "can we trust this script?"

```bash
POST /test-factory/{artifact_id}/scripts/{test_id}/verify
# body {} for static+evidence verification, or {"base_url": "https://app.example.com"}
# to add the live readiness probe (reachability + per-locator preflight).
```
Response essentials:
- `overall_score` (0-10, min-gated) · `decision` (certified | repair | defect)
- `certification_level` — **CERTIFIED-EVIDENCED** (recording-grounded) vs
  **CERTIFIED-STATIC** (no evidence — the honest ceiling)
- `risk` — `{level, score, drivers[]}` (likelihood × blast_radius ÷ detectability).
  Certification and risk are **orthogonal on purpose**: certification says *the
  script is faithful to its evidence*; risk says *how much a failure could hide*
  (a certified script with many honest gaps is still HIGH risk — that is a
  feature, not a contradiction).
- `dimension_scores`, `findings` (active), `waived_findings`, `gaps` (honest
  UNPROVEN count), `lint`, `readiness` (READY/DEGRADED/BLOCKED + reasons)
- `verdict_event.chain_hash` — this verdict's position in the tamper-evident chain
- `dossier_saved` — the reproducible decision record was persisted

## Verdict history & audit

```bash
GET  /test-factory/{a}/scripts/{t}/verdicts            # timeline + regression trend
GET  /test-factory/{a}/scripts/{t}/dossiers/{verdict_id}
# dossier payload: inputs (spec/steps sha256, registry version), rules_applied,
# evidence (per-step verdicts, preflight), alternatives_considered (locator
# rungs), risk, final_rationale (template-rendered — replayable, never prose).
```
Every zip download also appends a `delivery-gate` event, so the timeline shows
what actually shipped.

## Governance

```bash
POST /test-factory/{a}/scripts/{t}/waivers   # {finding_match, reason, days<=90}
GET  /test-factory/{a}/scripts/{t}/waivers
```
Waivers **annotate** findings (shown as `waived_findings` with owner/reason/expiry);
they never delete them, and they expire automatically.

## Remediation & calibration

```bash
GET /test-factory/{a}/scripts/{t}/remediations
# findings mapped to SAFE compiler channels; apply via the existing
# POST .../scripts/{t}/audit/repair — repaired code is always compiler-emitted.
GET /verification/calibration
# Historian v0: verdict distribution per dimension/source. Honest note included:
# counts are not precision claims until outcomes are labeled.
```

## Third-party scripts (Copilot / hand-written)

```bash
POST /verification/import   # {"name": "...", "script": "<spec.ts source>"}
```
Runs the same rubric + lint. With no recording evidence the response states the
honest ceiling (`CERTIFIED-STATIC` max) and lists the unverifiable dimensions —
upload a recording to unlock `CERTIFIED-EVIDENCED`.

## Delivery gate (already enforced)

`GET /test-factory/{a}/playwright` is gated: `NEXUS_AUDITOR_GATE=block` +
`NEXUS_AUDITOR_MIN_SCORE=9` — a sub-threshold suite returns **HTTP 409 with the
findings** instead of a zip; every delivered zip contains
`vkpower-audit-report.json`, the POM (`pages/vkpower-pages.ts`), synthetic data
candidates (approval-gated), the advisory a11y lane and the diagnostics schema.

## Operational notes

- Decisions are deterministic and reproducible: same script + same registry
  version ⇒ identical verdict and chain-verifiable dossier.
- All bookkeeping (history/dossiers/waivers) is best-effort by contract — a
  storage fault can never fail a verification or block a delivery.
- Blueprints: `docs/AI_VERIFICATION_PLATFORM.md` (this platform) and
  `docs/PLAYWRIGHT_GENERATION_ENGINE.md` (the generation engine).
