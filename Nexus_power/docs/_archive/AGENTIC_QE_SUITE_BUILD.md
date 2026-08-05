# Agentic-QE Suite — Build Log (what shipped)

> Built 2026-06-27 on branch `feat/agentic-qe-suite` (7 commits). **Additive, generic,
> never-green-wash, fail-open.** The existing engine (`diff_and_heal/self_heal.diagnose`,
> `agentic_heal`, `defect_report`, the oracle) is **imported and reused — never modified.**
> Implements the agents from `AGENTIC_QE_MASTER_PLAN.md`.

## What shipped

| Agent | File | What it does | Default |
|---|---|---|---|
| **Governor** | `services/agentic/governor.py` | per-agent on/off + per-run `BudgetGuard` (cap + dedupe-by-fingerprint) + provenance stamping (deterministic / live-confirmed / inferred) | — |
| **Eyes** | `services/agentic/live_options.py` | harvest the LIVE option set + prior sibling field values from the a11y capture (the grounding spine) | — |
| **Context** ⭐ | `services/agentic/semantic_diagnosis.py` | cross-field reasoner (modeled on `agentic_heal.propose`): INERT unless live options exist AND the recorded value is genuinely absent from them; any suggested value must be verbatim-in-options or dropped; never auto-applies | OFF (LLM) |
| **Triage + Verdict** | `services/agentic/triage.py` | deterministic PRODUCT / SCRIPT / ENVIRONMENT source + fix/build/flag route (5xx→product, infra→environment) | ON ($0) |
| **Sentinel** | `services/agentic/auto_diagnosis.py` | reuses `self_heal.analyze_step` (so auto == on-click), runs Triage + optional Context, attaches to the timeline | ON ($0) |
| **Intent** | `services/agentic/requirement_oracle.py` | P3 scaffold — cites the violated requirement on a real regression (inert by default) | OFF (LLM) |
| **Toggles** | `services/agentic/agentic_prefs.py` + `scripts/apply_agentic_prefs.sql` | per-tenant agent on/off (mirrors `surface_prefs`; fail-open to Governor defaults pre-migration) | — |

**Wiring (additive, fail-open):** `routers/test_factory.py` — both `/runs/latest` and
`/runs/{run_id}` attach the diagnosis via `_attach_agentic_diagnosis` (gated by the
Governor, wrapped in try/except). New `GET`/`PUT /api/v1/agentic/config` for the toggles.
The timeline builders + `analyze_step` are untouched.

**UI (additive):** `StepTimeline.tsx` renders an `AutoDiagnosis` card under each failed
step (source/route chips + headline + Context explanation + provenance footer); a self-
contained `AgenticSettings.tsx` "🤖 Agents" toggle panel in the Playwright header;
`api.ts` `getAgenticConfig`/`setAgenticConfig`.

**Tests:** `tests/test_agentic_suite.py` pins toggles/budget/provenance, option-harvest,
triage routing (incl. 5xx→product, infra→environment), and the two Context inert paths
(no LLM call). 18/18 local logic checks pass; whole API tree compiles.

## The never-green-wash invariants (held in code)
1. **No agent turns a step green.** Agents diagnose, route, and *suggest*; the orthogonal
   oracle + `heal_policy` + `assert_assertions_unchanged` remain the only thing that passes a step.
2. **Context is inert unless grounded** — no live option set, or the recorded value is
   already valid → returns `None` (no LLM call, no claim). A suggested value must appear
   verbatim in the live options or it's dropped.
3. **Budget + dedupe** — a 50-scenario regression can't fan out into 50 LLM calls.
4. **Provenance** — every claim is labelled deterministic / live-confirmed / inferred.
5. **Generic** — zero domain hardcoding; works on any form (Florida/Canada was only the example).

## To deploy (you do this — it's safety-gated; I can't deploy autonomously)
1. **API:** rebuild/redeploy the platform-api with this branch's `services/agentic/` +
   `routers/test_factory.py`. Sentinel + Triage + Verdict are then live ($0, default-on),
   auto-diagnosing every failure. **No migration required** for these (timeline-attached,
   not persisted).
2. **Toggles persistence (optional):** apply `scripts/apply_agentic_prefs.sql` (idempotent,
   tenant-RLS) so toggles persist per-tenant; until then they fail-open to defaults.
3. **Client:** rebuild the client (the AutoDiagnosis card + 🤖 Agents panel).
4. **Context (the flagship, opt-in):** turn **Context** ON in the 🤖 Agents panel. For it
   to fire it needs a **live option set captured at the failure** — today Eyes harvests
   option-bearing a11y nodes from the existing heal-capture; the strongest grounding is to
   extend `NEXUS_HEAL_CAPTURE` to **open the failing chooser** at failure time (the one
   runner-side enhancement noted in the master plan). Until then Context stays inert on
   controls whose options weren't captured — which is correct (no options → no claim).

## Not done (honest)
- The runner-side "open the chooser to capture its real options" enhancement (Eyes' strongest
  grounding) — needs a runner change + live verification; Context is safe (inert) without it.
- Persisting the diagnosis at ingest (vs recompute-on-read) — a perf optimization; current
  read-path enrichment is `$0` deterministic + gated, so it's fine for normal runs.
- Live end-to-end verification (the build is local + committed; deploy is gated).
