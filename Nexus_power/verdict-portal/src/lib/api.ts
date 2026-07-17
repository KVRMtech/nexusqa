/* ══════════════════════════════════════════════════════════════════════════
   VKPower Verdict — the typed QE-Central API client.

   ONE client, not fetch-scattered. Every method is fully typed to src/types/qec.ts,
   accepts an optional AbortSignal (abortable requests), and surfaces a typed
   `QecApiError`. The bearer token + active tenant come SYNCHRONOUSLY from
   src/lib/auth.ts (its sessionStorage store), so the transport layer has no React
   coupling.

   Mock mode (VITE_QEC_MOCK=1) short-circuits EVERY method to representative
   design data (src/lib/mock.ts) with a small delay, so the portal runs + demos
   before the backend is reachable — the loading / empty / error states in the UI
   are all still exercised.

   Endpoints (base `/api/v1/qec`, plus public /health):
     apps · scenarios/coverage/gaps/invariants · autonomy · cycles/cost · tiers ·
     touches · explorations · harness · health. The VERDICT LEDGER + DOSSIER have
     no single endpoint — `getLedger` / `getDossier` COMPOSE them and flag
     `composed: true` (see the type docs).
   ════════════════════════════════════════════════════════════════════════ */
import axios from 'axios';
import type { AxiosError, AxiosInstance, AxiosRequestConfig } from 'axios';

import { QEC_API_URL, QEC_MOCK } from './config';
import { getActiveTenantId, getAuthToken } from './auth';
import * as mock from './mock';
import type {
  AppCostResponse,
  AppCreatePayload,
  EnvProfile,
  EnvProfileCreatePayload,
  EnvProfileListResponse,
  AppListResponse,
  AppUpdatePayload,
  AutonomyByBand,
  AutonomyTrend,
  ClientApp,
  SeedManifest,
  EnvAttestation,
  CoverageGap,
  CoverageScorecard,
  CreateCycleResponse,
  CreateInvariantResponse,
  CriticalityRegistry,
  CreateExplorationResponse,
  CycleDetail,
  CycleListResponse,
  CycleMode,
  DeleteAppResponse,
  Dossier,
  Exploration,
  GapAdjudicatePayload,
  GapListResponse,
  GapWaivePayload,
  HarnessRun,
  HarnessRunResult,
  HarnessRunsResponse,
  HealthStatus,
  InvariantCreatePayload,
  InvariantListResponse,
  LedgerResponse,
  Scenario,
  ScenarioListResponse,
  ScenarioReviewPayload,
  ScenarioReviewResponse,
  TierLabelResult,
  TouchListResponse,
  VerdictKind,
  VerdictLedgerEntry,
} from '../types/qec';

const QEC = '/api/v1/qec';

// ── Typed error ──────────────────────────────────────────────────────────────

export type QecErrorCode = 'http' | 'network' | 'timeout' | 'aborted' | 'unknown';

/** Every failure the client raises is a QecApiError — never a raw AxiosError. */
export class QecApiError extends Error {
  readonly status?: number;
  readonly code: QecErrorCode;
  /** the server's `detail` payload (string or a structured refusal object). */
  readonly detail?: unknown;

  constructor(message: string, opts: { status?: number; code?: QecErrorCode; detail?: unknown } = {}) {
    super(message);
    this.name = 'QecApiError';
    this.status = opts.status;
    this.code = opts.code ?? (opts.status ? 'http' : 'unknown');
    this.detail = opts.detail;
  }

  /** True when the request was cancelled via an AbortSignal (not a real error). */
  get isAborted(): boolean {
    return this.code === 'aborted';
  }
}

function abortError(): QecApiError {
  return new QecApiError('Request aborted', { code: 'aborted' });
}

function toQecError(err: unknown): QecApiError {
  if (err instanceof QecApiError) return err;
  const e = err as { status?: number; message?: string };
  return new QecApiError(e?.message ?? 'Request failed', { status: e?.status });
}

// ── options ──────────────────────────────────────────────────────────────────

export interface RequestOpts {
  signal?: AbortSignal;
}

// ── client ───────────────────────────────────────────────────────────────────

export class QecApiClient {
  private readonly http: AxiosInstance;
  readonly mock: boolean;

  constructor(baseURL: string = QEC_API_URL, useMock: boolean = QEC_MOCK) {
    this.mock = useMock;
    this.http = axios.create({
      baseURL,
      timeout: 30_000,
      headers: { 'Content-Type': 'application/json' },
    });

    // Attach the operator's bearer token + active-tenant hint on every request.
    this.http.interceptors.request.use((config) => {
      const token = getAuthToken();
      if (token) config.headers.Authorization = `Bearer ${token}`;
      const tenant = getActiveTenantId();
      if (tenant) config.headers['X-Tenant-Id'] = tenant;
      return config;
    });

    // Normalise EVERY failure into a QecApiError; on 401, drop the session.
    this.http.interceptors.response.use(
      (res) => res,
      (err: AxiosError) => {
        if (axios.isCancel(err) || err.code === 'ERR_CANCELED') throw abortError();
        const status = err.response?.status;
        if (status === 401) this.handleUnauthorized();
        const rawDetail = (err.response?.data as { detail?: unknown } | undefined)?.detail;
        const message =
          typeof rawDetail === 'string'
            ? rawDetail
            : rawDetail
              ? this.summariseDetail(rawDetail)
              : err.message || 'Request failed';
        const code: QecErrorCode = status
          ? 'http'
          : err.code === 'ECONNABORTED'
            ? 'timeout'
            : 'network';
        throw new QecApiError(message, { status, code, detail: rawDetail ?? err.message });
      },
    );
  }

  private summariseDetail(detail: unknown): string {
    if (detail && typeof detail === 'object') {
      const d = detail as { reason?: unknown; refused?: unknown };
      if (typeof d.reason === 'string') return d.reason;
    }
    try {
      return JSON.stringify(detail);
    } catch {
      return 'Request failed';
    }
  }

  private handleUnauthorized(): void {
    try {
      sessionStorage.removeItem('verdict_token');
      sessionStorage.removeItem('verdict_tenant');
      if (typeof window !== 'undefined' && window.location.pathname !== '/login') {
        window.location.assign('/login');
      }
    } catch {
      /* non-browser / storage denied — nothing to clear */
    }
  }

  // ── low-level verbs ──
  private async get<T>(url: string, opts?: RequestOpts, params?: Record<string, unknown>): Promise<T> {
    const config: AxiosRequestConfig = { signal: opts?.signal, params };
    const { data } = await this.http.get<T>(url, config);
    return data;
  }

  private async post<T>(url: string, body?: unknown, opts?: RequestOpts): Promise<T> {
    const { data } = await this.http.post<T>(url, body ?? {}, { signal: opts?.signal });
    return data;
  }

  private async patch<T>(url: string, body?: unknown, opts?: RequestOpts): Promise<T> {
    const { data } = await this.http.patch<T>(url, body ?? {}, { signal: opts?.signal });
    return data;
  }

  private async del<T>(url: string, opts?: RequestOpts): Promise<T> {
    const { data } = await this.http.delete<T>(url, { signal: opts?.signal });
    return data;
  }

  /** Resolve mock data after a small delay; honour an already/at-abort signal. */
  private mocked<T>(produce: () => T, signal?: AbortSignal): Promise<T> {
    return new Promise<T>((resolve, reject) => {
      if (signal?.aborted) return reject(abortError());
      const timer = setTimeout(() => {
        try {
          resolve(produce());
        } catch (e) {
          reject(toQecError(e));
        }
      }, 130);
      signal?.addEventListener(
        'abort',
        () => {
          clearTimeout(timer);
          reject(abortError());
        },
        { once: true },
      );
    });
  }

  // ══════════════════════ Apps ══════════════════════

  listApps(opts?: RequestOpts): Promise<AppListResponse> {
    if (this.mock) return this.mocked(() => mock.mockListApps(getActiveTenantId() ?? 'acme-life'), opts?.signal);
    return this.get<AppListResponse>(`${QEC}/apps`, opts);
  }

  getApp(appId: string, opts?: RequestOpts): Promise<ClientApp> {
    if (this.mock) return this.mocked(() => mock.mockGetApp(appId), opts?.signal);
    return this.get<ClientApp>(`${QEC}/apps/${encodeURIComponent(appId)}`, opts);
  }

  createApp(payload: AppCreatePayload, opts?: RequestOpts): Promise<ClientApp> {
    if (this.mock) return this.mocked(() => mock.mockCreateApp(payload.name, payload.base_url), opts?.signal);
    return this.post<ClientApp>(`${QEC}/apps`, payload, opts);
  }

  updateApp(appId: string, payload: AppUpdatePayload, opts?: RequestOpts): Promise<ClientApp> {
    if (this.mock) return this.mocked(() => ({ ...mock.mockGetApp(appId), ...payload }) as ClientApp, opts?.signal);
    return this.patch<ClientApp>(`${QEC}/apps/${encodeURIComponent(appId)}`, payload, opts);
  }

  deleteApp(appId: string, opts?: RequestOpts): Promise<DeleteAppResponse> {
    if (this.mock)
      return this.mocked(() => ({ app_id: appId, status: 'deleted' as const, credentials_zeroed: true }), opts?.signal);
    return this.del<DeleteAppResponse>(`${QEC}/apps/${encodeURIComponent(appId)}`, opts);
  }

  /**
   * Re-attest (or edit) the crawl-gate attestation. PATCH /apps is WHOLE-REPLACE
   * on env_attestation (routers/apps.py), so we MUST spread the current attestation
   * and overlay only the changed fields — otherwise re-setting the expiry would wipe
   * attested_by / rules_of_engagement / preflight and knock the app back to 'draft'.
   * One-click "extend N days" = reAttest(app, { expires_at }).
   */
  reAttest(app: ClientApp, patch: Partial<EnvAttestation>, opts?: RequestOpts): Promise<ClientApp> {
    const env_attestation: EnvAttestation = { ...(app.env_attestation || {}), ...patch };
    return this.updateApp(app.app_id, { env_attestation }, opts);
  }

  /**
   * Add answer-key seeds (post-crawl seed-confirm). PATCH is WHOLE-REPLACE on
   * answer_key too, so spread the existing answer_key AND its nested `fill` map
   * before overlaying the new field→value pairs — otherwise one new seed would
   * erase every existing fill/note/outcome.
   */
  addSeeds(app: ClientApp, newFills: Record<string, unknown>, opts?: RequestOpts): Promise<ClientApp> {
    const ak = (app.answer_key || {}) as Record<string, unknown>;
    const fill = { ...((ak.fill as Record<string, unknown> | undefined) || {}), ...newFills };
    const answer_key = { ...ak, fill };
    return this.updateApp(app.app_id, { answer_key }, opts);
  }

  /**
   * The discovery-first Seed Manifest (Phase 1): every observed field classified
   * into one of the six dispositions. `recommended` is the human 1% (ASK + APPROVE);
   * `full` is every field with its grounded auto-filled default.
   */
  getSeedManifest(appId: string, mode: 'recommended' | 'full' = 'full', opts?: RequestOpts): Promise<SeedManifest> {
    if (this.mock)
      return this.mocked(
        () => ({ artifact_id: '', status: 'no_crawl', recommended: [], full: [], prefill: {},
          counts: {}, ask_count: 0, approve_count: 0, autonomous_count: 0, mode }) as SeedManifest,
        opts?.signal,
      );
    return this.get<SeedManifest>(
      `${QEC}/apps/${encodeURIComponent(appId)}/seed-manifest?mode=${mode}`, opts);
  }

  // ═══════════ Environment Profiles (multi-env, crawl-once/run-many) ═══════════
  listEnvironments(appId: string, opts?: RequestOpts): Promise<EnvProfileListResponse> {
    if (this.mock) return this.mocked(() => ({ app_id: appId, environments: [] }), opts?.signal);
    return this.get<EnvProfileListResponse>(`${QEC}/apps/${encodeURIComponent(appId)}/environments`, opts);
  }

  createEnvironment(appId: string, payload: EnvProfileCreatePayload, opts?: RequestOpts): Promise<EnvProfile> {
    return this.post<EnvProfile>(`${QEC}/apps/${encodeURIComponent(appId)}/environments`, payload, opts);
  }

  getEnvironment(appId: string, envId: string, opts?: RequestOpts): Promise<EnvProfile> {
    return this.get<EnvProfile>(
      `${QEC}/apps/${encodeURIComponent(appId)}/environments/${encodeURIComponent(envId)}`, opts);
  }

  updateEnvironment(
    appId: string, envId: string,
    payload: Partial<EnvProfileCreatePayload> & { status?: string },
    opts?: RequestOpts,
  ): Promise<EnvProfile> {
    return this.patch<EnvProfile>(
      `${QEC}/apps/${encodeURIComponent(appId)}/environments/${encodeURIComponent(envId)}`, payload, opts);
  }

  deleteEnvironment(
    appId: string, envId: string, opts?: RequestOpts,
  ): Promise<{ app_id: string; environment_id: string; status: string }> {
    return this.del(
      `${QEC}/apps/${encodeURIComponent(appId)}/environments/${encodeURIComponent(envId)}`, opts);
  }

  // ══════════════════════ Scenarios (the 1%) ══════════════════════

  listScenarios(
    appId: string,
    query: { state?: string; band?: string } = {},
    opts?: RequestOpts,
  ): Promise<ScenarioListResponse> {
    if (this.mock)
      return this.mocked(() => mock.mockListScenarios(appId, query.state ?? 'all', query.band ?? null), opts?.signal);
    return this.get<ScenarioListResponse>(`${QEC}/apps/${encodeURIComponent(appId)}/scenarios`, opts, {
      state: query.state ?? 'all',
      band: query.band,
    });
  }

  getScenario(scenarioId: string, opts?: RequestOpts): Promise<Scenario> {
    if (this.mock)
      return this.mocked(() => {
        throw mock.notFound('scenario', scenarioId);
      }, opts?.signal);
    return this.get<Scenario>(`${QEC}/scenarios/${encodeURIComponent(scenarioId)}`, opts);
  }

  reviewScenario(scenarioId: string, payload: ScenarioReviewPayload, opts?: RequestOpts): Promise<ScenarioReviewResponse> {
    if (this.mock) return this.mocked(() => this.mockReview(scenarioId, payload), opts?.signal);
    return this.post<ScenarioReviewResponse>(`${QEC}/scenarios/${encodeURIComponent(scenarioId)}/review`, payload, opts);
  }

  /** Convenience: approve a scenario (requires a typed e-signature). */
  approveScenario(scenarioId: string, signature: string, note?: string, opts?: RequestOpts): Promise<ScenarioReviewResponse> {
    return this.reviewScenario(scenarioId, { action: 'approve', signature, note }, opts);
  }

  private mockReview(scenarioId: string, payload: ScenarioReviewPayload): ScenarioReviewResponse {
    const state =
      payload.action === 'approve'
        ? 'approved'
        : payload.action === 'reject'
          ? 'rejected'
          : payload.action === 'submit'
            ? 'in_review'
            : 'draft';
    const chain = mock.hex64(scenarioId + payload.action);
    return {
      scenario: {
        scenario_id: scenarioId,
        app_id: 'app_acme_quote',
        name: 'New business term-life quote → e-sign',
        source_artifact_id: 'art_demo',
        criticality_band: 'P0',
        criticality_evidence: {},
        registry_version: 'crit-v1',
        fingerprint: mock.hex64(scenarioId).slice(0, 24),
        diff_state: 'changed',
        review_state: state,
        tier: 'behaves',
        materialized_artifact_id: '',
        status: 'active',
        updated_at: new Date().toISOString(),
      },
      event: {
        event_id: mock.hex64(scenarioId + 'evt').slice(0, 16),
        action: payload.action,
        prev_hash: mock.hex64(scenarioId + 'prev'),
        chain_hash: chain,
        is_touch: payload.action !== 'submit',
        carry_forward: false,
        created_at: new Date().toISOString(),
      },
      touch:
        payload.action === 'approve'
          ? { touch_id: mock.hex64(scenarioId + 'tch').slice(0, 12), touch_type: 'scenario_approve', band: 'P0', deduped: false }
          : null,
    };
  }

  // ══════════════════════ Coverage / gaps / invariants ══════════════════════

  getCoverage(appId: string, opts?: RequestOpts): Promise<CoverageScorecard> {
    if (this.mock) return this.mocked(() => mock.mockGetCoverage(appId), opts?.signal);
    return this.get<CoverageScorecard>(`${QEC}/apps/${encodeURIComponent(appId)}/coverage`, opts);
  }

  listGaps(appId: string, query: { status?: string } = {}, opts?: RequestOpts): Promise<GapListResponse> {
    if (this.mock) return this.mocked(() => mock.mockGetGaps(appId), opts?.signal);
    return this.get<GapListResponse>(`${QEC}/apps/${encodeURIComponent(appId)}/gaps`, opts, { status: query.status });
  }

  waiveGap(appId: string, gapId: string, payload: GapWaivePayload, opts?: RequestOpts): Promise<CoverageGap> {
    if (this.mock)
      return this.mocked(() => ({ ...mock.mockGetGaps(appId).gaps[0], status: 'waived' as const }), opts?.signal);
    return this.post<CoverageGap>(
      `${QEC}/apps/${encodeURIComponent(appId)}/gaps/${encodeURIComponent(gapId)}/waive`,
      payload,
      opts,
    );
  }

  adjudicateGap(appId: string, gapId: string, payload: GapAdjudicatePayload, opts?: RequestOpts): Promise<CoverageGap> {
    if (this.mock)
      return this.mocked(() => ({ ...mock.mockGetGaps(appId).gaps[0], status: 'adjudicated' as const }), opts?.signal);
    return this.post<CoverageGap>(
      `${QEC}/apps/${encodeURIComponent(appId)}/gaps/${encodeURIComponent(gapId)}/adjudicate`,
      payload,
      opts,
    );
  }

  listInvariants(appId: string, opts?: RequestOpts): Promise<InvariantListResponse> {
    if (this.mock) return this.mocked(() => mock.mockGetInvariants(appId), opts?.signal);
    return this.get<InvariantListResponse>(`${QEC}/apps/${encodeURIComponent(appId)}/invariants`, opts);
  }

  createInvariant(appId: string, payload: InvariantCreatePayload, opts?: RequestOpts): Promise<CreateInvariantResponse> {
    if (this.mock)
      return this.mocked(
        () => ({
          invariant_id: mock.hex64(appId + payload.statement).slice(0, 14),
          app_id: appId,
          band: payload.criticality_band ?? 'P0',
          status: 'certified',
          touch: { touch_id: 'demo', touch_type: 'invariant_author', band: payload.criticality_band ?? 'P0', deduped: false },
        }),
        opts?.signal,
      );
    return this.post<CreateInvariantResponse>(`${QEC}/apps/${encodeURIComponent(appId)}/invariants`, payload, opts);
  }

  // ══════════════════════ Autonomy (per band — never averaged) ══════════════════════

  getAutonomy(appId: string, query: { cycleId?: string } = {}, opts?: RequestOpts): Promise<AutonomyByBand> {
    if (this.mock) return this.mocked(() => mock.mockGetAutonomy(appId), opts?.signal);
    return this.get<AutonomyByBand>(`${QEC}/apps/${encodeURIComponent(appId)}/autonomy`, opts, {
      cycle_id: query.cycleId,
    });
  }

  getAutonomyTrend(appId: string, query: { cycles?: number } = {}, opts?: RequestOpts): Promise<AutonomyTrend> {
    if (this.mock) return this.mocked(() => mock.mockGetAutonomyTrend(appId), opts?.signal);
    return this.get<AutonomyTrend>(`${QEC}/apps/${encodeURIComponent(appId)}/autonomy/trend`, opts, {
      cycles: query.cycles ?? 10,
    });
  }

  // ══════════════════════ Cycles / cost ══════════════════════

  listCycles(appId: string, query: { limit?: number } = {}, opts?: RequestOpts): Promise<CycleListResponse> {
    if (this.mock) return this.mocked(() => mock.mockListCycles(appId), opts?.signal);
    return this.get<CycleListResponse>(`${QEC}/apps/${encodeURIComponent(appId)}/cycles`, opts, {
      limit: query.limit ?? 25,
    });
  }

  getCycle(cycleId: string, opts?: RequestOpts): Promise<CycleDetail> {
    if (this.mock) return this.mocked(() => mock.mockGetCycle(cycleId), opts?.signal);
    return this.get<CycleDetail>(`${QEC}/cycles/${encodeURIComponent(cycleId)}`, opts);
  }

  triggerCycle(appId: string, query: { mode?: CycleMode } = {}, opts?: RequestOpts): Promise<CreateCycleResponse> {
    const mode = query.mode ?? 'auto';
    if (this.mock)
      return this.mocked(
        () => ({ cycle_id: mock.hex64(appId + Date.now()).slice(0, 14), app_id: appId, mode, trigger: 'manual', state: 'pending' as const }),
        opts?.signal,
      );
    return this.post<CreateCycleResponse>(`${QEC}/apps/${encodeURIComponent(appId)}/cycles`, { mode }, opts);
  }

  getAppCost(
    appId: string,
    query: { windowHours?: number; groupBy?: string } = {},
    opts?: RequestOpts,
  ): Promise<AppCostResponse> {
    if (this.mock) return this.mocked(() => mock.mockGetAppCost(appId), opts?.signal);
    return this.get<AppCostResponse>(`${QEC}/apps/${encodeURIComponent(appId)}/cost`, opts, {
      window_hours: query.windowHours,
      group_by: query.groupBy ?? 'cycle_id',
    });
  }

  // ══════════════════════ Tier labels (RENDERS vs BEHAVES) ══════════════════════

  getTierLabel(artifactId: string, opts?: RequestOpts): Promise<TierLabelResult> {
    if (this.mock) return this.mocked(() => mock.mockGetTierLabel(artifactId), opts?.signal);
    return this.get<TierLabelResult>(`${QEC}/artifacts/${encodeURIComponent(artifactId)}/tier-label`, opts);
  }

  computeTierLabel(artifactId: string, opts?: RequestOpts): Promise<TierLabelResult> {
    if (this.mock) return this.mocked(() => mock.mockGetTierLabel(artifactId), opts?.signal);
    return this.post<TierLabelResult>(`${QEC}/artifacts/${encodeURIComponent(artifactId)}/tier-label`, {}, opts);
  }

  // ══════════════════════ Touches ══════════════════════

  listTouches(query: { appId?: string; limit?: number } = {}, opts?: RequestOpts): Promise<TouchListResponse> {
    if (this.mock) return this.mocked(() => mock.mockListTouches(query.appId ?? ''), opts?.signal);
    return this.get<TouchListResponse>(`${QEC}/touches`, opts, { app_id: query.appId, limit: query.limit ?? 500 });
  }

  // ══════════════════════ Criticality registry ══════════════════════

  getCriticalityRegistry(opts?: RequestOpts): Promise<CriticalityRegistry> {
    if (this.mock)
      return this.mocked(
        () => ({ registry_version: 'crit-v1', signals: [], signal_count: 0, is_seed_fallback: true, classifier: 'qec-crit-v1' }),
        opts?.signal,
      );
    return this.get<CriticalityRegistry>(`${QEC}/registry/criticality`, opts);
  }

  // ══════════════════════ Explorations / harness / health ══════════════════════

  getExploration(explorationId: string, opts?: RequestOpts): Promise<Exploration> {
    if (this.mock) return this.mocked(() => mock.mockGetExploration(explorationId), opts?.signal);
    return this.get<Exploration>(`${QEC}/explorations/${encodeURIComponent(explorationId)}`, opts);
  }

  /**
   * Dispatch a live crawl (Phase-1 exploration) for a registered app — the step
   * that records the app and mints its artifact/substrate so a cycle has
   * something to run. The app must be onboarding-`live` (the crawl gate refuses
   * a draft app). Returns immediately (status `dispatched`); poll
   * `getExploration(exploration_id)` for the terminal status + stats.
   */
  triggerExploration(appId: string, opts?: RequestOpts): Promise<CreateExplorationResponse> {
    if (this.mock)
      return this.mocked(
        () => ({
          exploration_id: mock.hex64(appId + Date.now()).slice(0, 32),
          app_id: appId,
          crawl_id: mock.hex64(appId).slice(0, 32),
          extractor_version: 'mock',
          status: 'dispatched',
          accepted: true,
        }),
        opts?.signal,
      );
    return this.post<CreateExplorationResponse>(`${QEC}/explorations`, { app_id: appId }, opts);
  }

  runHarness(payload: { fixture_name?: string; rules?: string[] } = {}, opts?: RequestOpts): Promise<HarnessRunResult> {
    if (this.mock) return this.mocked(() => mock.mockRunHarness(), opts?.signal);
    return this.post<HarnessRunResult>(`${QEC}/harness/run`, payload, opts);
  }

  listHarnessRuns(query: { limit?: number } = {}, opts?: RequestOpts): Promise<HarnessRunsResponse> {
    if (this.mock) return this.mocked(() => mock.mockListHarnessRuns(), opts?.signal);
    return this.get<HarnessRunsResponse>(`${QEC}/harness/runs`, opts, { limit: query.limit ?? 50 });
  }

  getHealth(opts?: RequestOpts): Promise<HealthStatus> {
    if (this.mock) return this.mocked(() => mock.mockGetHealth(), opts?.signal);
    return this.get<HealthStatus>(`/health`, opts);
  }

  // ══════════════════════ COMPOSED: verdict ledger + dossier ══════════════════════

  /**
   * The VERDICT LEDGER — certified / refused / healed verdicts as one stream.
   * There is NO single `/ledger` endpoint; in mock mode this returns a fully
   * hash-chained fixture, and against the live API it COMPOSES the stream from
   * cycles (done → certified, budget_stopped/failed/possible-deletion → refused)
   * and the REFUSE harness (REFUSED_CORRECTLY → refused, PASS_BASELINE →
   * certified). Entries composed from signals that carry no chain link set
   * `verified: false`. Always flagged `composed: true`.
   */
  async getLedger(query: { appId?: string; limit?: number } = {}, opts?: RequestOpts): Promise<LedgerResponse> {
    if (this.mock) return this.mocked(() => mock.mockGetLedger(query.appId), opts?.signal);
    return this.composeLedger(query.appId, query.limit ?? 60, opts);
  }

  private async composeLedger(appId: string | undefined, limit: number, opts?: RequestOpts): Promise<LedgerResponse> {
    const apps: ClientApp[] = appId ? [await this.getApp(appId, opts)] : (await this.listApps(opts)).apps;
    const perApp = await Promise.allSettled(
      apps.slice(0, 12).map((a) => this.listCycles(a.app_id, { limit: 8 }, opts)),
    );
    const harness = await Promise.allSettled([this.listHarnessRuns({ limit: 30 }, opts)]);

    const entries: VerdictLedgerEntry[] = [];
    const nameOf = (id: string) => apps.find((a) => a.app_id === id)?.name ?? id;

    perApp.forEach((res) => {
      if (res.status !== 'fulfilled') return;
      for (const c of res.value.cycles) {
        const refused = c.state === 'budget_stopped' || c.state === 'failed' || c.possible_deletion;
        const running = !c.terminal;
        if (running) continue;
        entries.push({
          id: `led_cyc_${c.cycle_id}`,
          kind: refused ? 'refused' : 'certified',
          subject: `${nameOf(c.app_id)} — cycle`,
          subject_kind: 'cycle',
          chain_hash: '',
          prev_hash: '',
          ts: c.finished_at ?? c.created_at ?? new Date().toISOString(),
          app_id: c.app_id,
          app_name: nameOf(c.app_id),
          reason: refused
            ? c.possible_deletion
              ? 'possible deletion detected — refusing "all green"'
              : `cycle ended ${c.state}`
            : undefined,
          verified: false,
          source: 'cycle',
        });
      }
    });

    if (harness[0].status === 'fulfilled') {
      for (const run of harness[0].value.runs as HarnessRun[]) {
        const certified = run.verdict === 'PASS_BASELINE';
        entries.push({
          id: `led_hrn_${run.harness_run_id}`,
          kind: certified ? 'certified' : 'refused',
          subject: `REFUSE harness · ${run.rule_id}`,
          subject_kind: 'harness',
          chain_hash: '',
          prev_hash: '',
          ts: run.created_at,
          reason: run.verdict === 'GREEN_WASH_DETECTED' ? 'GREEN_WASH_DETECTED — deploy gate failure' : run.observed,
          verified: false,
          source: 'harness',
        });
      }
    }

    entries.sort((a, b) => (a.ts < b.ts ? 1 : -1));
    const trimmed = entries.slice(0, limit);
    const counts: Record<VerdictKind, number> = { certified: 0, refused: 0, healed: 0 };
    trimmed.forEach((e) => (counts[e.kind] += 1));
    return {
      entries: trimmed,
      counts,
      composed: true,
      note: 'Composed client-side from live cycle + REFUSE-harness verdict signals (no single /ledger endpoint). Chain-verified certified entries require the per-subject approval chain.',
    };
  }

  /**
   * A DOSSIER — the full evidence bundle for one app. No single endpoint; this
   * composes the app row, coverage scorecard, per-band autonomy, recent cycles,
   * certified invariants, tier label and the app-scoped ledger. Sub-calls that
   * fail degrade to null rather than failing the whole dossier.
   */
  async getDossier(appId: string, opts?: RequestOpts): Promise<Dossier> {
    if (this.mock) return this.mocked(() => mock.mockGetDossier(appId), opts?.signal);
    const app = await this.getApp(appId, opts);
    const [coverage, autonomy, cycles, invariants, ledger, tier] = await Promise.all([
      this.getCoverage(appId, opts).catch(() => null),
      this.getAutonomy(appId, {}, opts).catch(() => null),
      this.listCycles(appId, { limit: 10 }, opts).then((r) => r.cycles).catch(() => []),
      this.listInvariants(appId, opts).then((r) => r.invariants).catch(() => []),
      this.getLedger({ appId }, opts).then((r) => r.entries).catch(() => []),
      app.latest_artifact_id ? this.getTierLabel(app.latest_artifact_id, opts).catch(() => null) : Promise.resolve(null),
    ]);
    return {
      app,
      coverage,
      autonomy,
      cycles,
      invariants,
      ledger,
      tier,
      composed: true,
      generated_at: new Date().toISOString(),
    };
  }
}

/** The shared singleton — import `{ api }` everywhere. */
export const api = new QecApiClient();
export default api;
