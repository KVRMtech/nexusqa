# Nexus QA — v2.0-visual-evidence-frozen (2026-06-03)

**HARD FREEZE.** The entire current working system is frozen as of commit
`f6a211b` on branch `feat/phase2-page-visits`. This builds on, and includes,
the canonical pipeline freeze `v1.0-gpu-validated` (`9f417e4`,
[FROZEN_RELEASE.md](FROZEN_RELEASE.md)).

## The rule (same as v1.0)

> **Do NOT modify any code in the frozen surface below.** It is validated and
> working end-to-end. All future capabilities must be built as **additive
> surfaces** (new tables, new services, new endpoints, new UI tabs) that read
> from or sit alongside the frozen code — never edits to it. Touching frozen
> code requires **explicit re-authorization** from the product owner.

## Frozen surface (do not touch)

| # | Capability | Where it lives |
|---|---|---|
| 1 | **Canonical Processing pipeline** | unchanged from `v1.0-gpu-validated` (`9f417e4`) — orchestrator, eyes, brain, backbone, spine, ears DAG |
| 2 | **Today's fixes (2026-06-03)** | storyboard derivation convergence (`650a19e`); working LLM/infra config (`f6a211b`); permanent JWT-secret hardening; eyes rate limit 120→3000 |
| 3 | **View Canonical Assets** | `platform/api/app/routers/artifacts.py` (artifacts, frames, scenes, transcript, evidence, cursor, workflows) + client asset views |
| 4 | **Visual Story** | storyboard composer + caption rewriter + panels |
| 5 | **Visual Flow → E2E Tests** | `platform/api/app/services/test_exporters/` (cypress, gherkin, playwright, preparation, base) |
| 6 | **Storyboard** | `platform/api/app/services/storyboard/` — scene_grouper, app_deduper, caption_rewriter, frame_annotator, action_extractor, composer + `routers/storyboard.py` |
| 7 | **3D Journey** | visual-evidence-graph endpoint + `client/src/pages/VisualFlowDiagramPage.tsx` (scene/app graph, captions, annotated frames) |
| 8 | **Pages & Forms** | `storyboard/page_visit_extractor.py`, `page_action_extractor.py`, `form_snapshot_extractor.py`, `page_schemas.py` + `page_visits/page_actions` tables + Pages & Forms UI panel |

Supporting frozen code: the 5-tier LLM router (`platform/api/app/services/llm/`
— router, providers, types), Alembic migrations `031`–`035`, and
`sdk/nexus-sdk` storyboard ORM models.

## Deployment state at freeze

| Item | Value |
|---|---|
| Frozen commit | `f6a211b` (branch `feat/phase2-page-visits`, tag `v2.0-visual-evidence-frozen`) |
| platform-api image | `nexus_power-platform-api:latest` — convergence fix baked in (survives `--force-recreate`) |
| LLM routing | `action` / `page_action` / `form_snapshot` / `page_visit_identity` → `tier_balanced` (GPT-4o); captions → `tier_fast`; page_visit inference → `tier_fast_cloud` |
| eyes frame rate limit | `NEXUS_RATE_LIMIT_RPM=3000` (was 120) |
| JWT secret | strong secret in VM `.env`, baked consistently across all services (fingerprint `0417a9aa8e76`) — `.env` is gitignored, NOT in this repo |
| Config in git | `docker-compose.override.action-extractor.yml` (== the VM's live `docker-compose.override.yml`) |

## Today's fixes — what they resolved

1. **Storyboard "not ready" 300s timeout + empty Pages panel** — `/storyboard`
   derivation committed only at end-of-request, so a client timeout rolled back
   ALL work and it never converged. Fix: per-step incremental commit (re-arm RLS),
   bounded page_visit step, internal vision-pass budget. → converges + persists.
2. **Logout-on-upload / stuck Canonicalize** — stale baked JWT secrets after
   partial container recreates. Fix: one strong `.env` secret + full-stack recreate
   (incl. the GPU-file `backbone` straggler).
3. **Empty form values / 0 actions / garbled page labels** — all 3 cloud LLM tiers
   were out of credit → fell back to weak local `llava`. Fix: OpenAI top-up + route
   the 4 vision/form/action tasks to GPT-4o + raise eyes rate limit. → real form
   values, 21 page actions, clean deep URLs.

## Known non-blocking limitations (acceptable at freeze)

- Homepage / shallow-URL pages still fragment into noisy `screen_name_ocr` rows
  (vision override only adopts URLs with ≥2 path segments). Cosmetic; fix is a
  small additive tweak when prioritized.
- GPT-4o < Claude Sonnet for dense form-field reading. If Anthropic credit is
  restored, flip the 4 tasks back to `tier_premium` for best quality (config-only).
- Inherited from v1.0: Milvus in-memory fallback; LLaVA-7B as the local vision
  fallback tier.
