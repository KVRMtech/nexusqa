/* ══════════════════════════════════════════════════════════════════════════
   VKPower Verdict — Test Studio factory client.

   The crawl portal's Test Studio (Test Cases + Playwright execution + Triage)
   is the SAME artifact-keyed factory surface the video portal already ships.
   Rather than rebuild it, we reuse its components verbatim — and this file is
   the method layer they call (`import { api } from './factoryApi'`), ported
   from client/src/services/api.ts with ONLY the transport rewired:

     • Base + auth come from the verdict portal, not the video portal. Requests
       go to `${QEC_API_URL}/api/v1/{test-factory,artifacts,test-runs,eyes,
       agentic}/...`, which qe-central's Phase-1 bridge (routers/factory_proxy.py)
       validates with the operator's verdict token and forwards to the factory
       with a tenant-scoped service token. The browser never holds a factory
       token; the factory's RLS is keyed on the authenticated tenant.

     • The <img>-src URL builders (frames + run screenshots) append the verdict
       token as `?token=` — qe-central's require_auth accepts a GET-only query
       token for browser-loaded resources, so screenshots load without a custom
       fetch, still through the bridge.

   The method bodies are otherwise an exact copy of the video portal's factory
   methods, so the ported panels behave identically. Every method that a crawl
   artifact does not populate (e.g. video-only storyboards) simply returns the
   factory's honest empty/absent response — never a fabricated value.
   ════════════════════════════════════════════════════════════════════════ */
import axios from 'axios';
import type { AxiosInstance, AxiosError } from 'axios';

import { QEC_API_URL } from '../lib/config';
import { getActiveTenantId, getAuthToken } from '../lib/auth';

/** Factory base: the bridge lives under `/api` on the QE-Central origin, and the
 *  ported method paths keep their original `/v1/...` prefix → `/api/v1/...`. */
const FACTORY_BASE = `${QEC_API_URL}/api`;

class FactoryApiClient {
  private client: AxiosInstance;

  constructor() {
    this.client = axios.create({
      baseURL: FACTORY_BASE,
      timeout: 30_000,
      headers: { 'Content-Type': 'application/json' },
    });

    // Attach the operator's verdict token + active tenant on every request —
    // read synchronously from the same sessionStorage the portal auth owns.
    this.client.interceptors.request.use((config) => {
      const token = getAuthToken();
      if (token) config.headers.Authorization = `Bearer ${token}`;
      const tenant = getActiveTenantId();
      if (tenant) config.headers['X-Tenant-Id'] = tenant;
      return config;
    });

    // On 401, drop the session and return to login — mirrors the verdict client.
    this.client.interceptors.response.use(
      (res) => res,
      (err: AxiosError) => {
        if (err.response?.status === 401) {
          try {
            sessionStorage.removeItem('verdict_token');
            sessionStorage.removeItem('verdict_tenant');
            if (typeof window !== 'undefined' && window.location.pathname !== '/login') {
              window.location.assign('/login');
            }
          } catch {
            /* non-browser / storage denied */
          }
        }
        return Promise.reject(err);
      },
    );
  }

  // ── Test case editing / controls ────────────────────────────────────────
  /** Inline-edit a Test Factory (Pages & Forms) case's step data. Stored as
   *  an override that survives regeneration; Playwright recompiles from it. */
  async editTestCase(
    artifactId: string,
    caseId: string,
    patch: { title?: string; steps?: Array<{ step_number: number; action?: string; expected_result?: string; verification?: string; value?: string; delete?: boolean; control?: { label: string; kind?: string; anchor?: string; anchor_kind?: string } }> },
  ): Promise<{ success: boolean; test_case_id: string; overridden_steps: number; repointed_steps?: number; survives_regenerate: boolean }> {
    const { data } = await this.client.patch(
      `/v1/test-factory/${artifactId}/test-cases/${caseId}`,
      patch,
    );
    return data;
  }

  /** The grounded menu of controls captured in this recording — the ONLY controls
   *  a step may be re-pointed to (the backend rejects anything not captured). */
  async getStepControls(artifactId: string, caseId: string): Promise<{ controls: Array<{ label: string; kind: string; page?: string }> }> {
    const { data } = await this.client.get(
      `/v1/test-factory/${artifactId}/test-cases/${caseId}/controls`);
    return data;
  }

  // ── <img>-src URL builders (verdict token + bridge base) ─────────────────
  /**
   * Build the absolute HTTP URL for a frame image given its asset path. The
   * verdict token is appended as `?token=` so <img> tags load through the
   * bridge (qe-central require_auth accepts a GET-only query token).
   */
  getFrameImageUrl(frameAssetPath: string): string {
    if (!frameAssetPath) return '';
    // Normalise separator: Windows paths from the engine use backslash
    const normalised = frameAssetPath.replace(/\\/g, '/');
    const token = getAuthToken() ?? '';
    return `${FACTORY_BASE}/v1/eyes/frames/${normalised}${token ? `?token=${encodeURIComponent(token)}` : ''}`;
  }

  /**
   * Resolve a run step's `screenshot_url` (the 'This run' actual) into a value
   * safe for an <img src>. The reporter stores a same-origin relative path
   * (/api/v1/test-runs/screenshot/{id}); we append the verdict token as a query
   * param so the <img> loads through the bridge. An absolute http(s) URL
   * (external CI-hosted shot) is returned unchanged.
   */
  getRunScreenshotUrl(urlOrPath: string): string {
    if (!urlOrPath) return '';
    if (/^https?:\/\//i.test(urlOrPath)) return urlOrPath;
    const token = getAuthToken() ?? '';
    if (!token) return urlOrPath;
    const sep = urlOrPath.includes('?') ? '&' : '?';
    return `${urlOrPath}${sep}token=${encodeURIComponent(token)}`;
  }

  // ── Visual evidence / storyboard (video-derived; empty for crawl) ────────
  async getVisualEvidenceGraph(
    artifactId: string,
    options?: { includeStoryboardOverlays?: boolean },
  ): Promise<any> {
    const params: Record<string, string> = {};
    if (options?.includeStoryboardOverlays) {
      params.include_storyboard_overlays = 'true';
    }
    const { data } = await this.client.get(
      `/v1/artifacts/${artifactId}/visual-evidence-graph`,
      { params },
    );
    return data;
  }

  async getStoryboard(
    artifactId: string,
  ): Promise<any> {
    // The first call for an artifact (or any call while a version is
    // stale) triggers the full composer derivation — scene grouping,
    // captions, action + page + form-snapshot extraction — which runs
    // the multimodal LLM and can take 1-3 minutes.  The default 30s
    // client timeout surfaced as "Storyboard not ready / timeout of
    // 30000ms exceeded" whenever the user opened the tab mid-derivation.
    // Give it a generous budget; subsequent cache-hit reads return in
    // a few seconds.
    const { data } = await this.client.get(
      `/v1/artifacts/${artifactId}/storyboard`,
      { timeout: 300_000 },
    );
    return data;
  }

  async regenerateStoryboard(
    artifactId: string,
  ): Promise<any> {
    const { data } = await this.client.post(
      `/v1/artifacts/${artifactId}/storyboard/regenerate`,
    );
    return data;
  }

  // ── Test Factory (Pages & Forms → grounded test cases) ──────────────
  /** Generate demonstrated + combination test cases from Pages & Forms. */
  async generateTestFactory(artifactId: string): Promise<any> {
    const { data } = await this.client.post(
      `/v1/test-factory/${artifactId}/generate`, {},
    );
    return data;
  }

  /** Aggregate counts for the Test Cases summary. */
  async getTestFactorySummary(artifactId: string): Promise<any> {
    const { data } = await this.client.get(
      `/v1/test-factory/${artifactId}/summary`,
    );
    return data;
  }

  /** Paginated list of generated test cases (full ProductionTestCase each). */
  async listTestFactoryCases(
    artifactId: string, page = 1, limit = 50, status = 'active', type?: string,
  ): Promise<any> {
    const params: Record<string, any> = { page, limit, status };
    if (type) params.type = type;
    const { data } = await this.client.get(
      `/v1/test-factory/${artifactId}/test-cases`, { params },
    );
    return data;
  }

  /** Co-Architect (repointed) — grounded QA-copilot chat over Pages & Forms. */
  async assistantTestFactory(
    artifactId: string, message: string, history: { role: string; content: string }[] = [],
  ): Promise<any> {
    const { data } = await this.client.post(
      `/v1/test-factory/${artifactId}/assistant`, { message, history },
      { timeout: 120_000 },
    );
    return data;
  }

  /** Draft a NEW grounded test case from a plain-English request (no write). */
  async proposeTestCase(
    artifactId: string, message: string, history: { role: string; content: string }[] = [],
  ): Promise<any> {
    const { data } = await this.client.post(
      `/v1/test-factory/${artifactId}/propose-case`, { message, history },
      { timeout: 120_000 },
    );
    return data;
  }

  /** Persist a reviewed proposed case (re-validated + grounded server-side). */
  async addTestCase(
    artifactId: string, payload: { name: string; message: string; steps: any[] },
  ): Promise<any> {
    const { data } = await this.client.post(
      `/v1/test-factory/${artifactId}/add-case`, payload,
      { timeout: 120_000 },
    );
    return data;
  }

  /** Transition a case through its sign-off lifecycle (draft -> in_review -> approved). */
  async reviewTestCase(
    artifactId: string, caseId: string,
    payload: { action: string; signature?: string; note?: string },
  ): Promise<any> {
    const { data } = await this.client.post(
      `/v1/test-factory/${artifactId}/test-cases/${caseId}/review`, payload,
    );
    return data;
  }

  /** On-demand generate a non-demonstrated category (negative|boundary|error_state). */
  async generateTestFactoryCategory(artifactId: string, category: string): Promise<any> {
    const { data } = await this.client.post(
      `/v1/test-factory/${artifactId}/generate/${category}`, {},
      { timeout: 120_000 },
    );
    return data;
  }

  /** Grounded triage board: per-failure verdict + baseline, + need-you tally. */
  async getTriage(artifactId: string): Promise<any> {
    const { data } = await this.client.get(`/v1/test-factory/${artifactId}/triage`);
    return data;
  }

  /** Grounded Oracle scorecard: verified-vs-assumed grounding, confidence rollup, heal integrity. */
  async getOracleScorecard(artifactId: string): Promise<any> {
    const { data } = await this.client.get(`/v1/test-factory/${artifactId}/oracle-scorecard`);
    return data;
  }

  /** Requirements Traceability Matrix: requirement(recorded)→step→assertion, grounded flags + unproven. */
  async getRtm(artifactId: string): Promise<any> {
    const { data } = await this.client.get(`/v1/test-factory/${artifactId}/rtm`);
    return data;
  }

  /** ADVISORY perceptual diff (baseline-vs-actual screenshot) — a badge, never a verdict. */
  async getVisualDiff(artifactId: string, scenarioId: string, stepNumber = 0): Promise<any> {
    const { data } = await this.client.get(
      `/v1/test-factory/${artifactId}/scenarios/${encodeURIComponent(scenarioId)}/visual-diff`,
      { params: { step_number: stepNumber } });
    return data;
  }

  /** ADVISORY VLM semantic check — does the screen still satisfy the recorded outcome? Signal only. */
  async semanticCheck(artifactId: string, scenarioId: string, stepNumber = 0): Promise<any> {
    const { data } = await this.client.post(
      `/v1/test-factory/${artifactId}/scenarios/${encodeURIComponent(scenarioId)}/semantic-check`,
      {}, { params: { step_number: stepNumber } });
    return data;
  }

  /** Human CONFIRMS or OVERRIDES the engine's grounded verdict — the direct oracle
   *  label. De-identified server-side (verdict enums only); flywheel capture gated. */
  async triageFeedback(
    artifactId: string, scenarioId: string,
    body: { verdict?: string; agrees: boolean; corrected_verdict?: string },
  ): Promise<any> {
    const { data } = await this.client.post(
      `/v1/test-factory/${artifactId}/triage/${encodeURIComponent(scenarioId)}/feedback`, body);
    return data;
  }

  /** Human resolves a value-conflict (typed vs committed vs other). De-identified
   *  server-side (choice enum only — never the value); flywheel capture gated. */
  async resolveValueConflict(
    artifactId: string, testId: string, choice: 'typed' | 'committed' | 'other',
  ): Promise<any> {
    const { data } = await this.client.post(
      `/v1/test-factory/${artifactId}/value-conflict/resolve`, { test_id: testId, choice });
    return data;
  }

  /** THIS RUN: per-scenario, per-step pass/fail timeline for the single latest run. */
  async getLatestRunTimeline(artifactId: string): Promise<any> {
    const { data } = await this.client.get(`/v1/test-factory/${artifactId}/runs/latest`);
    return data;
  }

  /** ONE flow's LAST run — full per-step evidence (step #, status, error/where,
   *  selector, screenshot) + drift + root-cause hints. Powers the Discovered
   *  Flows per-step drill-down so "why/where a step failed" is one click away. */
  async getScenarioLastRun(artifactId: string, scenarioId: string): Promise<any> {
    const { data } = await this.client.get(
      `/v1/e2e-architect/${artifactId}/scenarios/${encodeURIComponent(scenarioId)}/last-run`,
    );
    return data;
  }

  /** Auth profile status for an artifact (is a session stored + is a capture running). */
  async getAuthStatus(artifactId: string): Promise<any> {
    const { data } = await this.client.get(`/v1/test-factory/${artifactId}/playwright/auth`);
    return data;
  }

  /** Start an interactive login capture; returns { live_url } to embed for the operator to log in. */
  async startAuthCapture(artifactId: string, baseUrl = ''): Promise<any> {
    const { data } = await this.client.post(
      `/v1/test-factory/${artifactId}/playwright/auth/capture`, { base_url: baseUrl },
    );
    return data;
  }

  /** Save the captured session (encrypted server-side). */
  async saveAuthCapture(artifactId: string): Promise<any> {
    const { data } = await this.client.post(`/v1/test-factory/${artifactId}/playwright/auth/save`, {});
    return data;
  }

  /** RECORD + RUN — store the login just recorded, then immediately run with it.
   *
   *  One call on purpose: saving always "succeeds" even when the operator never
   *  completed the login (the recorder returns an empty cookie jar), and a run
   *  started on that would execute the whole suite logged out and report every
   *  failure against the application. The server judges the captured session and
   *  refuses with 422 `{ran:false, note}` rather than dispatch. `watch:true` gives
   *  the headed run you can watch; false runs headless. */
  async recordAndRun(artifactId: string, body: Record<string, unknown>): Promise<any> {
    const { data } = await this.client.post(
      `/v1/test-factory/${artifactId}/playwright/auth/save-and-run`, body);
    return data;
  }

  /** Cancel an in-progress capture. */
  async cancelAuthCapture(artifactId: string): Promise<any> {
    const { data } = await this.client.post(`/v1/test-factory/${artifactId}/playwright/auth/cancel`, {});
    return data;
  }

  /** Delete the stored auth profile (runs revert to unauthenticated). */
  async clearAuthProfile(artifactId: string): Promise<any> {
    const { data } = await this.client.delete(`/v1/test-factory/${artifactId}/playwright/auth`);
    return data;
  }

  /** Fidelity scorecard for ONE script (deep=1 adds the gpt-4o semantic review). */
  async getScriptFidelity(artifactId: string, testId: string, deep = false): Promise<any> {
    const { data } = await this.client.get(
      `/v1/test-factory/${artifactId}/scripts/${encodeURIComponent(testId)}/fidelity`,
      { params: deep ? { deep: 1 } : {} },
    );
    return data;
  }

  /** Agentic Playwright audit of ONE script: physical-possibility + grounding,
   *  5-dimension scorecard + per-step verdict. deep=1 adds the LLM reasoning;
   *  never green-wash (ungrounded "fix" → UNPROVEN). */
  async auditScriptAgentic(artifactId: string, testId: string, deep = true): Promise<any> {
    const { data } = await this.client.post(
      `/v1/test-factory/${artifactId}/scripts/${encodeURIComponent(testId)}/audit`,
      null, { params: deep ? { deep: 1 } : { deep: 0 } },
    );
    return data;
  }

  /** Apply the audit repair: re-derive from the recording with the grounded
   *  generator, recompile, save v+1. Returns before/after + the re-scored audit. */
  async repairScriptFromAudit(artifactId: string, testId: string): Promise<any> {
    const { data } = await this.client.post(
      `/v1/test-factory/${artifactId}/scripts/${encodeURIComponent(testId)}/audit/repair`,
    );
    return data;
  }

  /** Live Preflight — deterministic Playwright probe (no LLM): opens the live app
   *  and reports per-step locator resolution (1 good / 0 broken / >1 ambiguous). */
  async runScriptPreflight(artifactId: string, testId: string, baseUrl = ''): Promise<any> {
    const { data } = await this.client.post(
      `/v1/test-factory/${artifactId}/scripts/${encodeURIComponent(testId)}/preflight`,
      { base_url: baseUrl },
      { timeout: 180_000 },   // launches a real browser via the runner — well past the 30s default
    );
    return data;
  }

  /** Suite-level fidelity rollup (deterministic, all scripts). */
  async getSuiteFidelity(artifactId: string): Promise<any> {
    const { data } = await this.client.get(`/v1/test-factory/${artifactId}/fidelity`);
    return data;
  }

  /** Regenerate ONE script from its current test case → new immutable version. */
  async regenerateScript(artifactId: string, testId: string): Promise<any> {
    const { data } = await this.client.post(
      `/v1/test-factory/${artifactId}/scripts/${encodeURIComponent(testId)}/regenerate`, {});
    return data;
  }

  /** Make AI Review actionable: apply ONLY deterministic, grounded fixes
   *  (regenerate to resolve drift/stale oracles). Returns { fixed, escalations,
   *  version_no } — what was fixed vs. honestly left for a human. Never LLM code. */
  async fixScriptGaps(artifactId: string, testId: string): Promise<any> {
    const { data } = await this.client.post(
      `/v1/test-factory/${artifactId}/scripts/${encodeURIComponent(testId)}/fix-gaps`, {});
    return data;
  }

  /** Regenerate a group of scripts (test_ids/categories, or all) to new versions. */
  async regenerateAll(artifactId: string, body: { test_ids?: string[]; categories?: string[] }): Promise<any> {
    const { data } = await this.client.post(
      `/v1/test-factory/${artifactId}/scripts/regenerate-all`, body);
    return data;
  }

  /** Approve a PROPOSED (auto-healed / TrueFix) version → it becomes the active
   *  source for runs. The human gate on machine-written fixes. */
  async approveScriptVersion(artifactId: string, testId: string, versionNo: number): Promise<any> {
    const { data } = await this.client.post(
      `/v1/test-factory/${artifactId}/scripts/${encodeURIComponent(testId)}/versions/${versionNo}/approve`, {},
    );
    return data;
  }

  /** VKPower TrueFix — grounded root-cause diagnosis of one failed step. */
  async analyzeStep(artifactId: string, scenarioId: string, stepNumber: number): Promise<any> {
    const { data } = await this.client.get(
      `/v1/test-factory/${artifactId}/steps/${encodeURIComponent(scenarioId)}/${stepNumber}/analyze`,
    );
    return data;
  }

  /** TrueFix — apply the control-kind fix to one step + re-run it HEADED to prove
   *  it. Persists a new version only on a verified green. Returns { run_id, live_url };
   *  poll getNexusRunStatus for { healed, heal_version, heal_reason }. */
  async healStep(
    artifactId: string, scenarioId: string, stepNumber: number,
    body: { base_url?: string; data?: Record<string, string> },
  ): Promise<any> {
    const { data } = await this.client.post(
      `/v1/test-factory/${artifactId}/steps/${encodeURIComponent(scenarioId)}/${stepNumber}/heal`,
      body,
    );
    return data;
  }

  /** Phase B — re-run the failing test with a11y capture ON so TrueFix can find a
   *  renamed control. Returns { captured, nodes, reanchored, reanchor }. */
  async captureFailureState(
    artifactId: string, scenarioId: string, stepNumber: number,
    body: { base_url?: string; data?: Record<string, string> } = {},
  ): Promise<any> {
    const { data } = await this.client.post(
      `/v1/test-factory/${artifactId}/steps/${encodeURIComponent(scenarioId)}/${stepNumber}/capture-failure-state`,
      body,
    );
    return data;
  }

  /** Phase D — per-script run-history sparkline + flake + board Run Summary. */
  async getRunsSummary(artifactId: string, window = 10): Promise<any> {
    const { data } = await this.client.get(
      `/v1/test-factory/${artifactId}/runs/summary`, { params: { window } },
    );
    return data;
  }

  /** P2.8 — north-star quality metric: client-visible product-fault failures
   *  (target zero) vs. product faults caught by certification before any
   *  client run. */
  async getProductFaults(artifactId: string, windowDays = 30): Promise<any> {
    const { data } = await this.client.get(
      `/v1/test-factory/${artifactId}/quality/product-faults`,
      { params: { window_days: windowDays } },
    );
    return data;
  }

  /** Structured listing of the compiled Playwright suite (each script's source +
   *  spec path + category + per-step stats, project files, run commands) for the
   *  Execution view. Same compilation as the zip, returned as JSON. */
  async getPlaywrightManifest(artifactId: string): Promise<any> {
    const { data } = await this.client.get(
      `/v1/test-factory/${artifactId}/playwright/manifest`,
    );
    return data;
  }

  /** Compile a CONFIGURED, env+data-driven Playwright run bundle (zip) for a
   *  local run — selected scripts + chosen base URL + data overrides baked in. */
  async getRunBundle(
    artifactId: string,
    body: { categories?: string[]; test_ids?: string[]; base_url?: string; data?: Record<string, string>; data_by_test?: Record<string, Record<string, string>>; browsers?: string[]; headed?: boolean; workers?: number; retries?: number },
  ): Promise<Blob> {
    const resp = await this.client.post(
      `/v1/test-factory/${artifactId}/playwright/run-config`,
      body,
      { responseType: 'blob' },
    );
    return resp.data as Blob;
  }

  /** CI/CD bundle: the configured run bundle PLUS GitHub Actions / GitLab CI /
   *  Jenkins pipelines that run the suite and report to the triage board. */
  async getCiBundle(
    artifactId: string,
    body: { categories?: string[]; test_ids?: string[]; base_url?: string; data?: Record<string, string>; data_by_test?: Record<string, Record<string, string>>; browsers?: string[]; headed?: boolean; workers?: number; retries?: number },
  ): Promise<Blob> {
    const resp = await this.client.post(
      `/v1/test-factory/${artifactId}/playwright/ci-bundle`,
      body,
      { responseType: 'blob' },
    );
    return resp.data as Blob;
  }

  /** Start a server-side run on the VKPower runner. Returns { run_id, status }. */
  async startNexusRun(
    artifactId: string,
    body: { categories?: string[]; test_ids?: string[]; base_url?: string; data?: Record<string, string>; data_by_test?: Record<string, Record<string, string>>; browsers?: string[]; headed?: boolean; workers?: number; retries?: number },
  ): Promise<any> {
    const { data } = await this.client.post(
      `/v1/test-factory/${artifactId}/playwright/run`, body,
    );
    return data;
  }

  /** Start a HEADED, VNC-streamed run on the VKPower runner. Returns { run_id, live_url }. */
  async startNexusLiveRun(
    artifactId: string,
    body: { categories?: string[]; test_ids?: string[]; base_url?: string; data?: Record<string, string>; data_by_test?: Record<string, Record<string, string>>; browsers?: string[]; retries?: number },
  ): Promise<any> {
    const { data } = await this.client.post(
      `/v1/test-factory/${artifactId}/playwright/run-live`, body,
    );
    return data;
  }

  /** Auto-Heal Run — run headed and auto diagnose+fix+re-run+continue until the
   *  whole suite is green, then save a "Clean Run - V1". Returns { run_id, live_url };
   *  poll getNexusRunStatus for { heal_trace, terminal_state, clean_run_version }. */
  async autoHealRun(
    artifactId: string,
    body: { categories?: string[]; test_ids?: string[]; base_url?: string; data?: Record<string, string>; enable_agentic_heal?: boolean; agentic_min_confidence?: number },
  ): Promise<any> {
    const { data } = await this.client.post(
      `/v1/test-factory/${artifactId}/auto-heal/run-config`, body,
    );
    return data;
  }

  /** Poll a VKPower runner job's live status (running -> passed/failed/...). */
  async getNexusRunStatus(artifactId: string, runId: string): Promise<any> {
    const { data } = await this.client.get(
      `/v1/test-factory/${artifactId}/playwright/run/${runId}`,
    );
    return data;
  }

  /** Phase C — per-test script version history (newest first). */
  async listScriptVersions(artifactId: string, testId: string): Promise<any> {
    const { data } = await this.client.get(
      `/v1/test-factory/${artifactId}/scripts/${testId}/versions`,
    );
    return data;
  }

  /** Per-script GROUNDED VERDICT HISTORY across recent runs: proven-green vs
   *  regression/drift/flake, duration, the final-frame success screenshot, and any
   *  heal event that landed on that run. Read-only, $0 LLM. */
  async getVerdictHistory(artifactId: string, scenarioId: string, limit = 20): Promise<any> {
    const { data } = await this.client.get(
      `/v1/test-factory/${artifactId}/scenarios/${encodeURIComponent(scenarioId)}/verdict-history`,
      { params: { limit } },
    );
    return data;
  }

  /** Agentic-QE per-agent ON/OFF config + catalog (for the toggles panel). $0 agents
   *  default ON; LLM agents default OFF. */
  async getAgenticConfig(): Promise<{ agents: Record<string, boolean>; defaults: Record<string, boolean>; catalog: string[] }> {
    const { data } = await this.client.get(`/v1/agentic/config`);
    return data;
  }

  /** Turn each agent on/off for this tenant (a toggle only gates whether an agent runs). */
  async setAgenticConfig(agents: Record<string, boolean>): Promise<{ agents: Record<string, boolean> }> {
    const { data } = await this.client.put(`/v1/agentic/config`, { agents });
    return data;
  }

  /** Phase 5 — Proven Control Ledger KB: controls whose heals are oracle-PROVEN green
   *  and reused across scenarios + recordings, with provenance (label/page/fix/confidence/
   *  app scope) + lifecycle (quarantined). scope = recording | app | tenant. Read-only. */
  async getProvenControls(
    artifactId: string,
    scope: 'recording' | 'app' | 'tenant' = 'recording',
    limit = 200,
  ): Promise<any> {
    const { data } = await this.client.get(
      `/v1/test-factory/${artifactId}/proven-controls`,
      { params: { scope, limit } },
    );
    return data;
  }

  /** Editor seed: the edited (active or a specific) source, else the
   *  deterministic parametrized compiled source. `edited` says which. */
  async getScriptSource(artifactId: string, testId: string, versionNo?: number): Promise<any> {
    const { data } = await this.client.get(
      `/v1/test-factory/${artifactId}/scripts/${testId}/source`,
      { params: versionNo ? { version_no: versionNo } : {} },
    );
    return data;
  }

  /** Save edits as a new immutable version (vN+1). */
  async saveScriptVersion(
    artifactId: string,
    body: { test_id: string; script_source: string; data?: Record<string, string>; note?: string },
  ): Promise<any> {
    const { data } = await this.client.post(`/v1/test-factory/${artifactId}/scripts/save`, body);
    return data;
  }

  /** Restore version N by appending it as a new highest version. */
  async restoreScriptVersion(
    artifactId: string,
    body: { test_id: string; version_no: number },
  ): Promise<any> {
    const { data } = await this.client.post(`/v1/test-factory/${artifactId}/scripts/restore`, body);
    return data;
  }

  /** Download the deterministic, grounded Playwright project (zip).
   *  Empty opts = whole active suite; {category} = one category;
   *  {testCaseId} = one specific test case (takes precedence). */
  async getPlaywrightBundle(
    artifactId: string,
    opts: { category?: string; testCaseId?: string } = {},
  ): Promise<Blob> {
    const params: Record<string, string> = {};
    if (opts.testCaseId) params.test_case_id = opts.testCaseId;
    else if (opts.category) params.category = opts.category;
    const resp = await this.client.get(
      `/v1/test-factory/${artifactId}/playwright`,
      { params, responseType: 'blob' },
    );
    return resp.data as Blob;
  }

  /** One-click enrichment: options + anchors + outcomes (vision) + regenerate. */
  async enrichTestFactory(artifactId: string): Promise<any> {
    const { data } = await this.client.post(
      `/v1/test-factory/${artifactId}/enrich`, {},
      { timeout: 600_000 },
    );
    return data;
  }

  /** Download the suite as Excel / CSV / JSON (blob). */
  async exportTestFactory(artifactId: string, format = 'excel', details = false, redact = false): Promise<Blob> {
    const resp = await this.client.get(
      `/v1/test-factory/${artifactId}/export`,
      { params: { format, details, redact }, responseType: 'blob' },
    );
    return resp.data as Blob;
  }

  /** Push the suite into a test-management tool (qtest|testrail|zephyr|xray). */
  async pushTestFactory(artifactId: string, tool: string): Promise<any> {
    const { data } = await this.client.post(
      `/v1/test-factory/${artifactId}/push/${tool}`, {},
    );
    return data;
  }

  // ── Execution Evidence Report (Certificate of Execution) ─────────────────
  // Viewing is open to any authenticated tenant user; the EXPORT endpoints are
  // gated on the operator's role by the qe-central bridge, so a viewer gets a
  // 403 on the download links rather than a silent evidence egress.

  /** Recent runs the report can be pointed at, each with its own step counts. */
  async listEvidenceRuns(artifactId: string): Promise<any> {
    const { data } = await this.client.get(`/v1/test-factory/${artifactId}/report/runs`);
    return data;
  }

  /** The full structured report (Trust Block, flows, cases, steps, defects, diff). */
  async getEvidenceReport(artifactId: string, runId?: string): Promise<any> {
    const { data } = await this.client.get(
      `/v1/test-factory/${artifactId}/report`,
      { params: runId ? { run_id: runId } : {} },
    );
    return data;
  }

  /**
   * Open a Playwright trace in the SELF-HOSTED viewer at `/trace/`.
   *
   * The viewer is served from our own origin (its static assets are copied out
   * of the runner at deploy time), so an air-gapped on-prem install can replay
   * a failure step-by-step without reaching trace.playwright.dev or any CDN.
   * The trace itself is fetched same-origin with the operator's `?token=`.
   */
  getTraceViewerUrl(traceUrlOrPath: string): string {
    if (!traceUrlOrPath) return '';
    const abs = /^https?:\/\//i.test(traceUrlOrPath)
      ? traceUrlOrPath
      : `${window.location.origin}${this.getRunScreenshotUrl(traceUrlOrPath)}`;
    return `/trace/index.html?trace=${encodeURIComponent(abs)}`;
  }

  /** Personas defined for this artifact (+ the synthetic 'default' persona-0). */
  async listPersonas(artifactId: string, traits?: string): Promise<any> {
    const { data } = await this.client.get(
      `/v1/test-factory/${artifactId}/personas`,
      { params: traits ? { traits } : {} });
    return data;
  }

  // ── Persona × Environment matrix (management surface) ──────────────────────

  /** Define a persona (identity + behavior class + traits). */
  async createPersona(artifactId: string, body: {
    name: string; description?: string; traits?: string[]; behavior_class?: string;
  }): Promise<any> {
    const { data } = await this.client.post(`/v1/test-factory/${artifactId}/personas`, body);
    return data;
  }

  /** Retire a persona (status → retired; never a hard delete). */
  async retirePersona(artifactId: string, personaId: string): Promise<any> {
    const { data } = await this.client.post(
      `/v1/test-factory/${artifactId}/personas/${personaId}/retire`, {});
    return data;
  }

  /** WRITE-ONLY: store a persona's envelope-encrypted credential card for an
   *  environment. Slot VALUES are sent once and never returned. */
  async putCredentialCard(artifactId: string, personaId: string, environmentId: string,
    slot_values: Record<string, string>): Promise<any> {
    const { data } = await this.client.put(
      `/v1/test-factory/${artifactId}/personas/${personaId}/credentials/${environmentId}`,
      { slot_values });
    return data;
  }

  /** The NON-SECRET provisioning grid: persona × env cards, slot NAMES only. */
  async credentialsManifest(artifactId: string): Promise<any> {
    const { data } = await this.client.get(`/v1/test-factory/${artifactId}/credentials/manifest`);
    return data;
  }

  /** Environments + governance (posture, production flag, health). */
  async listEnvironments(artifactId: string): Promise<any> {
    const { data } = await this.client.get(`/v1/test-factory/${artifactId}/environments`);
    return data;
  }

  /** Register/update an environment's governance. */
  async putEnvironment(artifactId: string, environmentId: string, body: {
    label?: string; posture?: string; is_production?: boolean;
    write_authorized?: boolean; base_url?: string; data_epoch?: string;
  }): Promise<any> {
    const { data } = await this.client.put(
      `/v1/test-factory/${artifactId}/environments/${environmentId}`, body);
    return data;
  }

  /** Which cards for an environment need re-verification (staleness due-list). */
  async environmentStaleness(artifactId: string, environmentId: string): Promise<any> {
    const { data } = await this.client.get(
      `/v1/test-factory/${artifactId}/environments/${environmentId}/staleness`);
    return data;
  }

  /** Login recipes (versions + verify state). */
  async listRecipes(artifactId: string): Promise<any> {
    const { data } = await this.client.get(`/v1/test-factory/${artifactId}/recipes`);
    return data;
  }

  /** The member × environment grid: one verdict per cell, from the same derivation
   *  the dispatch gate applies. Slot NAMES only — no credential value is reachable. */
  async memberMatrix(artifactId: string, mutating = true): Promise<any> {
    const { data } = await this.client.get(
      `/v1/test-factory/${artifactId}/matrix`, { params: { mutating } });
    return data;
  }

  /** PROVE a card: replay the recorded login with this member's credentials
   *  against this environment, for real, and report what happened. Only reaching
   *  the recorded logged-in page counts as proven — steps that merely replayed do
   *  not, and neither does a form login submitted with nothing asserting it
   *  succeeded. Returns {verified, outcome, attribution, recipe_drift, note}. */
  async verifyRecipe(artifactId: string, personaId: string, environmentId: string,
    baseUrl = ''): Promise<any> {
    const { data } = await this.client.post(`/v1/test-factory/${artifactId}/recipes/verify`, {
      persona_id: personaId, environment_id: environmentId, base_url: baseUrl,
    });
    return data;
  }

  /** The fields a credential card must supply, DERIVED from the recorded login —
   *  `{has_recipe, fields:[{name,label,type}], reason}`. The card editor renders
   *  exactly this rather than asking anyone to retype slot names; the same
   *  derivation backs the server-side refusal, so form and contract cannot drift. */
  async loginContract(artifactId: string): Promise<any> {
    const { data } = await this.client.get(`/v1/test-factory/${artifactId}/login-contract`);
    return data;
  }

  /** RECORD AT ONBOARDING. Deliberately not artifact-scoped: a recipe lives
   *  against an artifact and an artifact is only minted by the first crawl, so at
   *  the Access step there is nothing to attach to. One pass yields the login
   *  choreography, the environment landed on, and the session that gets THIS
   *  crawl past the login. */
  async startRecording(url: string): Promise<any> {
    const { data } = await this.client.post('/v1/test-factory/recordings/start', { url });
    return data;
  }

  async recordingObservation(): Promise<any> {
    const { data } = await this.client.get('/v1/test-factory/recordings/observation');
    return data;
  }

  async saveRecording(): Promise<any> {
    const { data } = await this.client.post('/v1/test-factory/recordings/save', {});
    return data;
  }

  async cancelRecording(): Promise<any> {
    const { data } = await this.client.post('/v1/test-factory/recordings/cancel', {});
    return data;
  }

  /** Has this tenant already recorded a login for this host?
   *  At onboarding no form has been seen, so only the domain is sent and the
   *  server matches on that: one recorded login is a reuse, several are a
   *  question (a public login and a member portal are not the same), none is a
   *  fresh recording. Sending `fields` later tightens it to the exact form. */
  async reuseCheck(body: {
    domain: string; login_path?: string;
    fields?: Array<{ slot: string; type?: string }>; submit?: string;
  }): Promise<any> {
    const { data } = await this.client.post('/v1/test-factory/recipes/reuse-check', body);
    return data;
  }

  /** Derive a baseline login recipe from a configured form-login (idempotent). */
  async ensureBaselineRecipe(artifactId: string): Promise<any> {
    const { data } = await this.client.post(
      `/v1/test-factory/${artifactId}/recipes/ensure-baseline`, {});
    return data;
  }

  /** Ops rollup: personas / environments+postures / credentials / recipes. */
  async personaOpsSummary(artifactId: string): Promise<any> {
    const { data } = await this.client.get(`/v1/test-factory/${artifactId}/personas/ops/summary`);
    return data;
  }

  /** §2.18 — the Needs-Review QUEUE, joined with recorded dispositions. */
  async getReviewQueue(artifactId: string, includeResolved = false): Promise<any> {
    const { data } = await this.client.get(
      `/v1/test-factory/${artifactId}/report/review-queue`,
      { params: includeResolved ? { include_resolved: true } : {} },
    );
    return data;
  }

  /** Record a human disposition (with optional assignee + typed-name e-signature). */
  async recordReviewDisposition(artifactId: string, body: {
    scenario_id: string; step_number: number; disposition: string; reason: string;
    assignee?: string; reclassify_to?: string; signature_name?: string;
    defect_signature?: string;
  }): Promise<any> {
    const { data } = await this.client.post(
      `/v1/test-factory/${artifactId}/report/review`, body,
    );
    return data;
  }

  /** Dashboard metrics — rates over EXECUTED cases only. */
  async getEvidenceAnalytics(artifactId: string, runId?: string): Promise<any> {
    const { data } = await this.client.get(
      `/v1/test-factory/${artifactId}/report/analytics`,
      { params: runId ? { run_id: runId } : {} },
    );
    return data;
  }

  /**
   * Absolute URL for the self-contained HTML report / an export, carrying the
   * operator's verdict token as `?token=` (the bridge accepts a GET-only query
   * token for browser-loaded resources, exactly like frames and screenshots).
   * Used for `window.open` and download anchors, which cannot set a header.
   */
  getEvidenceUrl(artifactId: string, kind: 'html' | 'zip' | 'csv' | 'junit' | 'xlsx' | 'pdf',
                 runId?: string): string {
    const path = kind === 'html'
      ? `report.html`
      : kind === 'zip'
        ? `report.zip`
        : `report/export.${kind}`;
    const token = getAuthToken() ?? '';
    const qs = new URLSearchParams();
    if (runId) qs.set('run_id', runId);
    if (token) qs.set('token', token);
    const q = qs.toString();
    return `${FACTORY_BASE}/v1/test-factory/${artifactId}/${path}${q ? `?${q}` : ''}`;
  }
}

/** The shared singleton — the ported Studio panels import `{ api }` from here. */
export const api = new FactoryApiClient();
export default api;
