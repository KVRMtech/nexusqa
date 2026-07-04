# End-to-End Production Test Report — 2026-06-30

**Method:** started the deployed VM, exercised the **real deployed pipeline** against the
**10 existing recordings** in production, validated every layer like a human against the
actual DB rows + run history, then stopped the VM. This is the *as-shipped* truth, not a
code reading. **Important:** tonight's local trust fixes (branch `feat/pipeline-trust-track-ab`)
are **NOT deployed** — so these ratings reflect the **currently-deployed** system.

---

## ⚠️ The headline (as-found): the deployed Autopilot was CRASHING, not honest-stopping
- `auto-heal error: cannot import name 'agentic_heal'` — the agentic analyst module wasn't in the running image.
- `auto-heal error: list index out of range` (×3) — the `failures[0]` crash.
- **58 jobs stuck in `running`** forever (oldest **18 days**) — no timeout/cleanup.

Root cause: the running container was a **stale image** built *before* `agentic_heal.py` and the `if not failures` guard landed in the VM repo. Not missing code — an un-rebuilt image.

## ✅ FIXED & DEPLOYED TONIGHT (2026-06-30, verified in the running container)
- **H1 `agentic_heal` ImportError → FIXED.** Rebuilt platform-api from the VM repo. `agentic_heal import OK: True` at `/app/service/app/services/test_factory/agentic_heal.py`. **0** ImportError occurrences in the live logs since rebuild.
- **H2 `list index out of range` → FIXED.** The `if not failures` guard is confirmed present in the running router (`inspect.getsource` check).
- **H3 58 zombie `running` jobs → REAPED.** Board now `failed 76 / passed 16 / error 15`, **0 running**.
- platform-api `Up (healthy)`, `/health` 200, no crash signatures. Rollback image available.

**Net effect:** the deployed Autopilot goes from **crashing (~2/10)** to **running + honest-stopping (~3–4/10)** — the embarrassing failure mode is gone. The *structural* limits below remain (they are the real, multi-week work).

**Honestly DEFERRED tonight (too risky to do unattended right before a review):**
- **C4 quality-gate green-wash** — `brain_quality_score` is computed by the separate **brain service**; the artifact *status* is already honest (`completed_degraded`), so only the numeric `1.00` is inconsistent. A supervised fix.
- **Track A trust fixes (A1–A4)** + the disambiguator green-pass — a branch-merge onto the divergent VM repo; modest deployed value vs. merge risk. A supervised deploy.

The deploy divergence is now *smaller* but not gone: the VM repo (`af81e87`) is still behind the local trust branch (`6bfcbad`).

---

## 📊 Honest per-layer ratings — NOW, empirical (deployed)

| Layer | Rating | Evidence from the live data |
|---|---|---|
| 1. Canonical | **4/10** | Phantom pages from recording-chrome ("Video Call Interface", "My First Project — Google"); OCR-URL error fabricated a duplicate page (`checkout-step-to.html` vs real `checkout-step-two.html`); host:port mangled (`34.21.232.80.8096`); **quality gate green-washes** (a 0-action recording reports `brain_quality_score=1.00`). |
| 2. Extraction | **4/10** (bimodal) | Clean owned app (Aegis) ≈6; messy real recording (saucedemo) ≈3. Fabricated `navigate@0.55` actions confirmed; login captured but `conf 0.50 / automation_ready=false` → dropped; **2 of 10 recordings extracted 0 actions** (vision/LLM was unavailable → placeholder visits). |
| 3. Test cases | **4/10** | Honestly flags most over-inference `review` (skipped), but high-confidence **wrong** steps slip through (a duplicate "Sort order" step placed *after checkout-complete*). |
| 4. Playwright/compile | **4/10** | Over-qualified locator name (`Add to cart - Sauce Labs Onesie` used verbatim as the accessible name → 0 matches); no validator on the default path. |
| 5. Self-heal / **Autopilot (deployed)** | **2/10** | **Crashes** on the VM (agentic_heal ImportError + list-index). The sound local version isn't deployed. When it *doesn't* crash it honest-stops (good), but in production it mostly errors. |
| 6. Defect / Env | **6/10** | Defect-builder real + wired; env-triage absent; `is_base_host` still dead on the VM (my A4 fix not deployed). |

**Honesty engineering ~8/10; deployed accuracy/reliability ~3–4/10 (Autopilot ~2).** The gap is, again, the whole story — plus a fresh one: **the best code isn't the deployed code.**

---

## 🎬 Worked walkthrough — video → test case → Playwright (saucedemo, `9cef3242`)

**The video:** a real saucedemo checkout session, recorded through a screen-share (a "Video Call Interface" is visible), 12 scenes / 42 frames.

**→ Canonical (9 page-visits):** 3 of the first 4 "pages" are wrong — a video-call UI, a Google Cloud console tab, then the real app. The real pages (`/inventory`, `/cart`, `/checkout-step-one/two/complete`) are correct (`url_scene`, conf 0.85) **except** a phantom `checkout-step-to.html` from an OCR misread of "two".

**→ Extraction (25 actions):** the login **is** captured (`type 'visual_user'`, conf 0.50, `automation_ready=false`), three `Add to cart - Sauce Labs {Bolt,Fleece,Onesie}` clicks (product baked into the label), two fabricated `navigate@0.55` rows. One `Remove` action even has a real vision **anchor** (`{kind:row, label:'Sauce Labs Fleece Jacket'}`).

**→ Test case (17 steps):** starts at `/inventory` (**login dropped**); step 3 `Click 'Add to cart - Sauce Labs Onesie'` carries `next_url=/cart.html` (**impossible transition**) → correctly flagged `conf=review`; steps 12-13 are the **OCR-phantom** `checkout-step-to.html` (review); **step 16 repeats the "Sort order" step after checkout-complete at `conf=high`** — a wrong step that would actually run. 7 of 17 steps are `review` (honestly skipped); the leak is the high-confidence over-inferred ones.

**→ Playwright:** the `Add to cart - Sauce Labs Onesie` step compiles to `getByRole('button',{name:'Add to cart - Sauce Labs Onesie'})` → **0 matches** (the real button's name is just "Add to cart") → RED. (This refines the earlier "6-way ambiguity" hypothesis: on the live data the product is *concatenated into the name*, so it 0-matches rather than 6-matches — same root cause, the product context isn't used as a *scope*.)

**The clean contrast (Aegis insurance `7a0b36a6`):** a 26-step "Apply with Country Canada" test case that is **genuinely good** — real grounded values (REDDY, KARNA, Canada, phone, DOB, SSN), real navigations (`Next → /apply/coverage`, `confirm → /apply/review`), rich selects. Our **owned, clean-URL** app produces a strong test; the messy third-party recording produces a defective one. **The pipeline's quality is bimodal, and the quality gate can't tell the difference.**

---

## 🅰️🅱️🅲 Studio vs Autopilot vs Batch (empirical)

- **A — Studio (~5/10 deployed):** generates test cases (9 across recordings), runs them. `live passed` 13 / `failed` 8. Honestly skips `review` steps; fails on the high-confidence over-inferred ones. Clean apps → good, messy → defective. The most usable mode today.
- **B — Autopilot (~2/10 deployed):** **broken on the VM** — `agentic_heal` ImportError + `list index out of range`. The advanced, fixed loop exists locally but isn't deployed. Zero green autonomous passes; mostly *errors*, not even honest stops.
- **C — Batch (~3/10 deployed):** plumbing works (1 schedule-run: `status=failed, proven=0, stopped=1`). The `heal_trace` shows an honest sequence: step 1 passed → step 2 `LOCATOR_NOT_FOUND` → reanchor refused → select-content-fallback → honest stop. Inherits B's ceiling **and** B's deployed crashes.

## 🤖 How the agents are working (in production: mostly **not**)
- The **agentic analyst** (`agentic_heal`) is the keystone agent — and it **fails to import on the VM**, so in production the agent isn't running at all. Locally it's deployed + sound.
- The diagnosis/triage/semantic reasoners are **display-only** (decorate the timeline; not in the decision path) — confirmed earlier in code review.
- Net: the **never-green-wash discipline is real and holds** (refusals, honest stops, the batch trace), but the **AI agents are not actually doing work in production** because of the deploy gap.

---

## 🐞 All bugs found (this run), by layer

**Canonical**
- C1 Recording-chrome promoted to pages (video-call UI, Google console tab) — 2 phantom pages.
- C2 OCR-URL typo → phantom duplicate page (`checkout-step-to.html`).
- C3 OCR-URL host:port mangled (`34.21.232.80.8096`).
- C4 **Quality gate green-wash**: 0-action degraded recording reports `brain_quality_score=1.00`, `semantic_completeness=1.00`.

**Extraction**
- E1 Fabricated `navigate@0.55` actions (the verb=none→navigate rule).
- E2 Login captured but low-conf / not automation-ready → dropped downstream.
- E3 Vision/LLM unavailable → "(visual analysis unavailable)" placeholder visits + **0 actions** (2/10 recordings, no test case produced).
- E4 Over-qualified labels (product concatenated into `target_label`).

**Test case**
- T1 Login dropped (test starts mid-flow).
- T2 Impossible transition (Add-to-cart → /cart) — flagged review (honest).
- T3 OCR-phantom page → nonsense steps (review).
- T4 **Duplicate/misplaced "Sort order" step after checkout-complete at conf=high** (would run + fail).
- T5 Values present in the test but not clearly in the action stream (e.g. zip `78006`).

**Playwright**
- P1 Over-qualified locator name → 0-match RED.
- P2 No validation gate on the default generate/compile path.

**Self-heal / Autopilot (deployed — critical)**
- H1 `cannot import name 'agentic_heal'` — agent module missing on the VM.
- H2 `list index out of range` — crash fixed locally, not deployed.
- H3 **58 zombie `running` jobs** (oldest 18 days) — no timeout/cleanup/reaper.

**Batch**
- B1 Works but inherits B's ceiling + crashes; only honest-stops, no green.

---

## 🛠️ Do we need to build more? — NOT first.

**The order is: DEPLOY → REAP → then BUILD.**

1. **Deploy what's already built + fixed (supervised).** This single step fixes H1 (agentic_heal), H2 (list-index), wires A4 (env-outage), and ships tonight's trust fixes (A1/A2/A3 + the harness). The best code is sitting un-deployed. **Highest leverage by far.**
2. **Add a job reaper / timeout** (H3) — 58 zombie jobs is a real reliability bug at 1,000 apps × 100 clients. Cheap.
3. **Then the structural trust work (Milestone 1 plan):** the *bimodal quality* + the *quality-gate green-wash* (C4) are exactly what the M1 accuracy harness + the canonical fixes target. The harness is built (Track B); the next move is labeling the 10 existing recordings and producing the first real baseline number.
4. **Genuinely new build (later):** the vision cross-check oracle (kills C1/C2/E1 at the source) and the env-log analyzer. These are the real agentic white space — but they come *after* deploy + measurement.

**One-line truth:** you don't need more AI — you need to **deploy the AI and fixes you already have**, add a job-reaper, and stand up the measurement harness. The clean-app result (Aegis) proves the pipeline *can* be good; the messy-app result (saucedemo) + the deploy gap are what's holding the rating at ~4 (and Autopilot at ~2).
