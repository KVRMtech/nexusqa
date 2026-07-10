/* ══════════════════════════════════════════════════════════════════════════
   VKPower Verdict — mock dataset (VITE_QEC_MOCK=1).

   Representative, design-accurate data so the portal RUNS + demos before the
   backend is reachable. Shapes match src/types/qec.ts exactly. Hashes are
   deterministic pseudo-hashes (display only, NOT cryptographic) and the ledger
   entries are linked prev_hash → chain_hash so the hash-chain reads honestly.
   ════════════════════════════════════════════════════════════════════════ */
import type {
  AppCostResponse,
  AppListResponse,
  AutonomyByBand,
  AutonomyTrend,
  ClientApp,
  CoverageScorecard,
  CycleDetail,
  CycleListResponse,
  CycleSummary,
  Dossier,
  Exploration,
  GapListResponse,
  HarnessRunResult,
  HarnessRunsResponse,
  HealthStatus,
  InvariantListResponse,
  LedgerResponse,
  ScenarioListResponse,
  ScenarioView,
  TierLabelResult,
  TouchListResponse,
  VerdictKind,
  VerdictLedgerEntry,
} from '../types/qec';

// ── deterministic helpers ───────────────────────────────────────────────────

/** Deterministic 64-hex pseudo-hash for a seed (display only, not crypto). */
export function hex64(seed: string): string {
  let h = 2166136261 >>> 0;
  const out: string[] = [];
  const s = seed || 'x';
  for (let i = 0; i < 64; i++) {
    h ^= s.charCodeAt((i + s.length) % s.length);
    h = Math.imul(h, 16777619) >>> 0;
    h = (h + i * 2654435761) >>> 0;
    out.push((h & 0xf).toString(16));
  }
  return out.join('');
}

function iso(minutesAgo: number): string {
  return new Date(Date.now() - minutesAgo * 60_000).toISOString();
}

// ── the fleet ────────────────────────────────────────────────────────────────

interface AppFixture {
  app: ClientApp;
  tier: 'behaves' | 'renders';
  headline: VerdictKind;
  autonomy: Partial<Record<'P0' | 'P1' | 'P2' | 'P3', number | null>>;
  sparkline: number[];
  coverageBlocked: boolean;
}

function makeApp(
  id: string,
  name: string,
  host: string,
  status: ClientApp['status'],
  envKind: 'disposable' | 'staging' | 'prod',
  tier: 'behaves' | 'renders',
): ClientApp {
  return {
    app_id: id,
    tenant_id: 'acme-life',
    name,
    base_url: `https://${host}`,
    canonical_host: host,
    answer_key: {},
    env_attestation: {
      attested_by: 'founder@vkpower.demo',
      attested_at: iso(60 * 24 * 3),
      env_kind: envKind,
      reset_procedure: 'nightly snapshot restore',
    },
    fences: { allowed_hosts: [host], max_rps: 4, allow_submit: envKind === 'disposable' },
    repo_binding: { provider: 'gitlab', project: `acme/${id}` },
    schedule: { cadence: 'on_push' },
    budgets: { usd_per_cycle: 12 },
    latest_artifact_id: `art_${hex64(id).slice(0, 12)}`,
    baseline_fingerprint_id: `fp_${hex64(id + 'fp').slice(0, 12)}`,
    status,
    has_credentials: true,
    created_at: iso(60 * 24 * 30),
    updated_at: iso(35),
    tier,
  };
}

const FLEET: AppFixture[] = [
  {
    app: makeApp('app_acme_quote', 'ACME Life · Term Quote', 'quote.acmelife.example', 'active', 'disposable', 'behaves'),
    tier: 'behaves',
    headline: 'certified',
    autonomy: { P0: 100, P1: 96.4, P2: 100, P3: 100 },
    sparkline: [88, 91, 90, 94, 97, 96, 99, 100],
    coverageBlocked: false,
  },
  {
    app: makeApp('app_guardian_annuity', 'Guardian · Annuity Portal', 'annuity.guardian.example', 'active', 'staging', 'behaves'),
    tier: 'behaves',
    headline: 'healed',
    autonomy: { P0: 100, P1: 92.1, P2: 98.3, P3: 100 },
    sparkline: [95, 93, 89, 84, 88, 92, 90, 94],
    coverageBlocked: false,
  },
  {
    app: makeApp('app_northshore_claims', 'Northshore · Claims Intake', 'claims.northshore.example', 'active', 'staging', 'renders'),
    tier: 'renders',
    headline: 'refused',
    autonomy: { P0: 50, P1: 88.0, P2: 100, P3: 100 },
    sparkline: [82, 80, 76, 70, 62, 58, 54, 50],
    coverageBlocked: true,
  },
  {
    app: makeApp('app_beacon_uw', 'Beacon · Underwriting Rules', 'uw.beacon.example', 'active', 'disposable', 'behaves'),
    tier: 'behaves',
    headline: 'certified',
    autonomy: { P0: 100, P1: 100, P2: 100, P3: 100 },
    sparkline: [90, 92, 95, 96, 98, 99, 100, 100],
    coverageBlocked: false,
  },
  {
    app: makeApp('app_summit_polADMIN', 'Summit · Policy Admin', 'admin.summit.example', 'paused', 'prod', 'renders'),
    tier: 'renders',
    headline: 'refused',
    autonomy: { P0: null, P1: null, P2: null, P3: null },
    sparkline: [70, 68, 69, 67, 66, 65, 64, 64],
    coverageBlocked: false,
  },
  {
    app: makeApp('app_harbor_billing', 'Harbor · Billing Cycle', 'billing.harbor.example', 'active', 'disposable', 'behaves'),
    tier: 'behaves',
    headline: 'certified',
    autonomy: { P0: 100, P1: 97.9, P2: 100, P3: 100 },
    sparkline: [84, 88, 90, 93, 95, 97, 98, 98],
    coverageBlocked: false,
  },
];

function fixture(appId: string): AppFixture | undefined {
  return FLEET.find((f) => f.app.app_id === appId);
}

/** Extra fleet-tile signal the fleet grid consumes (tier + headline + spark). */
export interface FleetTileSignal {
  app_id: string;
  tier: 'behaves' | 'renders';
  headline: VerdictKind;
  autonomy: Partial<Record<'P0' | 'P1' | 'P2' | 'P3', number | null>>;
  sparkline: number[];
}

export function mockFleetSignals(): FleetTileSignal[] {
  return FLEET.map((f) => ({
    app_id: f.app.app_id,
    tier: f.tier,
    headline: f.headline,
    autonomy: f.autonomy,
    sparkline: f.sparkline,
  }));
}

// ── reads ────────────────────────────────────────────────────────────────────

export function mockListApps(tenantId: string): AppListResponse {
  const apps = FLEET.map((f) => ({ ...f.app, tenant_id: tenantId }));
  return { apps, total: apps.length };
}

export function mockGetApp(appId: string): ClientApp {
  const f = fixture(appId);
  if (!f) throw notFound('app', appId);
  return f.app;
}

export function mockGetCoverage(appId: string): CoverageScorecard {
  const f = fixture(appId);
  const blocked = f?.coverageBlocked ?? false;
  const gaps: CoverageScorecard['gaps'] = blocked
    ? [
        {
          gap_id: hex64(appId + 'gap').slice(0, 24),
          tenant_id: 'acme-life',
          app_id: appId,
          kind: 'possible_deletion',
          band: 'P0',
          detail: {
            canonical_key: 'route:/claims/{id}/appeal',
            baseline_id: hex64(appId + 'baseline').slice(0, 16),
            reason:
              'a previously-approved, covered atom is MISSING from the fresh universe — possible deletion; blocks "all green" until adjudicated or waived',
          },
          status: 'open',
          waiver: null,
          waiver_expires_at: null,
          created_at: iso(90),
          updated_at: iso(90),
          blocking: true,
        },
      ]
    : [];
  return {
    model_version: 'qec-coverage-v1',
    app_id: appId,
    generated_at: iso(2),
    verdict: blocked ? 'blocked_on_p0_gaps' : 'ok',
    atoms: {
      count: blocked ? 214 : 187,
      by_kind: { route: 41, api_endpoint: 33, form_field: 78, journey_edge: 24, validator: 11 },
      by_source: { crawl: 150, repo: 22, answer_key: 15 },
      by_provenance: { G_DETERMINISTIC: 96, G_LIVE_CONFIRMED: 61, G_INFERRED: 30 },
      atoms_hash: hex64(appId + 'atoms'),
    },
    invariants: { total: 4, by_status: { certified: 4 }, linked_to_scenarios: 3 },
    tier_distribution: f?.tier === 'behaves' ? { behaves: 22, renders: 3 } : { renders: 18, behaves: 6 },
    gaps,
    open_gaps: gaps.length,
    blocking_gaps: gaps.filter((g) => g.blocking).length,
  };
}

export function mockGetGaps(appId: string): GapListResponse {
  const cov = mockGetCoverage(appId);
  return { app_id: appId, gaps: cov.gaps, total: cov.gaps.length };
}

const CYCLE_STATES_DEMO: Array<CycleSummary['state']> = ['done', 'done', 'running', 'done', 'budget_stopped'];

export function mockListCycles(appId: string): CycleListResponse {
  const cycles: CycleSummary[] = CYCLE_STATES_DEMO.map((state, i) => ({
    cycle_id: `cyc_${hex64(appId + i).slice(0, 14)}`,
    app_id: appId,
    trigger: (['webhook_repo', 'schedule', 'manual', 'probe_drift', 'schedule'] as const)[i],
    state,
    terminal: state === 'done' || state === 'failed' || state === 'budget_stopped',
    mode: (['auto', 'auto', 'full', 'auto', 'auto'] as const)[i],
    selected_count: [12, 6, 25, 8, 19][i],
    carried_count: [40, 46, 27, 44, 33][i],
    possible_deletion: i === 2 && (fixture(appId)?.coverageBlocked ?? false),
    created_at: iso((i + 1) * 180),
    started_at: iso((i + 1) * 180 - 1),
    finished_at: state === 'running' ? null : iso((i + 1) * 180 - 6),
  }));
  return { app_id: appId, cycles, total: cycles.length };
}

export function mockGetCycle(cycleId: string): CycleDetail {
  const appId = 'app_acme_quote';
  return {
    cycle_id: cycleId,
    tenant_id: 'acme-life',
    app_id: appId,
    trigger: 'webhook_repo',
    state: 'done',
    selected_scope: { mode: 'auto' },
    honest_gaps: { vanished_pages_possible_deletion: false },
    result: { coverage_verdict: 'ok' },
    error: '',
    started_at: iso(181),
    finished_at: iso(174),
    created_at: iso(182),
    updated_at: iso(174),
    selected: {
      mode: 'auto',
      selected_test_ids: ['t_quote_happy', 't_quote_reject', 't_quote_esign'],
      carried_forward: [
        { test_id: 't_login', verdict_run_id: hex64('run1').slice(0, 12), verdict_age_cycles: 3, reason: 'unchanged fingerprint' },
        { test_id: 't_dashboard', verdict_run_id: hex64('run2').slice(0, 12), verdict_age_cycles: 1, reason: 'unchanged fingerprint' },
      ],
      selection_reason: 'incremental: 3 atoms changed on the quote journey; 40 unchanged carried',
      per_case_reasons: { t_quote_esign: 'validator changed on the e-sign step' },
    },
    coverage_verdict: 'ok',
    cost: {
      units: { browser_seconds: '412.500000', llm_tokens: '0', runner_runs: '3', substrate_rows: '128' },
      unmetered_runs: 0,
      usd: '2.480000',
      entry_count: 14,
    },
  };
}

export function mockGetAppCost(appId: string): AppCostResponse {
  return {
    app_id: appId,
    group_by: 'cycle_id',
    window_hours: null,
    groups: {
      [`cyc_${hex64(appId).slice(0, 14)}`]: {
        units: { browser_seconds: '412.5', runner_runs: '3' },
        unmetered_runs: 0,
        usd: '2.48',
        entry_count: 14,
      },
    },
    group_count: 1,
  };
}

const ALL_SCENARIOS: Record<string, ScenarioView[]> = {};
function scenariosFor(appId: string): ScenarioView[] {
  if (ALL_SCENARIOS[appId]) return ALL_SCENARIOS[appId];
  const defs: Array<[string, ScenarioView['criticality_band'], ScenarioView['diff_state'], ScenarioView['review_state'], ScenarioView['tier']]> = [
    ['New business term-life quote → e-sign', 'P0', 'changed', 'in_review', 'behaves'],
    ['Underwriting decline path with reason codes', 'P0', 'new', 'draft', 'behaves'],
    ['Beneficiary edit + save', 'P1', 'changed', 'draft', 'renders'],
    ['Quote PDF download', 'P2', 'unchanged', 'approved', 'behaves'],
    ['Marketing footer links', 'P3', 'unchanged', 'approved', 'renders'],
  ];
  const rows = defs.map((d, i) => ({
    scenario_id: `scn_${hex64(appId + i).slice(0, 14)}`,
    app_id: appId,
    name: d[0],
    source_artifact_id: `art_${hex64(appId).slice(0, 12)}`,
    criticality_band: d[1],
    criticality_evidence: { signal_id: 'money_movement', matched: d[1] === 'P0' },
    registry_version: 'crit-v1',
    fingerprint: hex64(appId + 'fp' + i).slice(0, 24),
    diff_state: d[2],
    review_state: d[3],
    tier: d[4],
    materialized_artifact_id: d[3] === 'approved' ? `art_${hex64(appId).slice(0, 12)}` : '',
    status: 'active',
    updated_at: iso(i * 20 + 5),
  }));
  ALL_SCENARIOS[appId] = rows;
  return rows;
}

export function mockListScenarios(appId: string, state: string, band: string | null): ScenarioListResponse {
  let rows = scenariosFor(appId);
  if (band) rows = rows.filter((r) => r.criticality_band === band.toUpperCase());
  if (state === 'needs_approval') {
    rows = rows.filter((r) => (r.diff_state === 'new' || r.diff_state === 'changed') && r.review_state !== 'approved');
  } else if (['new', 'changed', 'unchanged', 'missing'].includes(state)) {
    rows = rows.filter((r) => r.diff_state === state);
  }
  return { app_id: appId, state, band: band ?? '', scenarios: rows, total: rows.length };
}

export function mockGetAutonomy(appId: string): AutonomyByBand {
  const f = fixture(appId);
  const a = f?.autonomy ?? { P0: 100, P1: 100, P2: 100, P3: 100 };
  const by_band: AutonomyByBand['by_band'] = {};
  const gov: Record<string, number> = { P0: 6, P1: 14, P2: 9, P3: 4 };
  (['P0', 'P1', 'P2', 'P3'] as const).forEach((b) => {
    const pct = a[b];
    if (pct === undefined) return;
    const governed = gov[b];
    const touches = pct === null ? 0 : Math.round((governed * (100 - pct)) / 100);
    by_band[b] = {
      governed_scenarios: pct === null ? 0 : governed,
      human_touches: touches,
      autonomy_pct: pct,
      by_type: touches > 0 ? { scenario_approve: touches } : {},
    };
  });
  return {
    model_version: 'qec-touch-v1',
    app_id: appId,
    cycle_id: '',
    by_band,
    unattributed: { human_touches: 0, by_type: {}, note: 'ingested audit touches with no criticality band — counted, never folded into a real band' },
    totals: { governed_scenarios: 33, human_touches: 2 },
    note: 'autonomy is reported PER band only — there is deliberately no aggregate averaged autonomy figure',
  };
}

export function mockGetAutonomyTrend(appId: string): AutonomyTrend {
  const cyc = mockListCycles(appId).cycles.slice().reverse();
  const cycles = cyc.map((c) => ({ cycle_id: c.cycle_id, state: c.state, trigger: c.trigger, created_at: c.created_at }));
  const series = (base: number[], gov: number) =>
    cycles.map((c, i) => ({
      cycle_id: c.cycle_id,
      governed_scenarios: gov,
      human_touches: Math.round((gov * (100 - base[i])) / 100),
      autonomy_pct: base[i],
    }));
  return {
    model_version: 'qec-touch-v1',
    app_id: appId,
    window: cycles.length,
    cycles,
    per_band: {
      P0: series([100, 100, 83, 100, 100], 6),
      P1: series([92, 94, 90, 96, 96], 14),
      P2: series([100, 100, 100, 100, 100], 9),
    },
    unattributed: [],
    note: 'autonomy is reported PER band PER cycle — no single averaged figure; an empty band is null, never 100%',
  };
}

export function mockGetInvariants(appId: string): InvariantListResponse {
  const invariants = [
    ['Issued policy face amount can NEVER exceed the underwriting-approved ceiling', 'P0'],
    ['A declined applicant is NEVER charged a premium', 'P0'],
    ['Beneficiary allocation percentages MUST sum to exactly 100%', 'P1'],
    ['A quote older than 30 days MUST be re-rated before bind', 'P1'],
  ].map((d, i) => ({
    invariant_id: `inv_${hex64(appId + i).slice(0, 14)}`,
    tenant_id: 'acme-life',
    app_id: appId,
    statement: d[0],
    criticality_band: d[1] as 'P0' | 'P1',
    signature: 'Dana Whitfield, Chief Actuary',
    signed_by: 'founder@vkpower.demo',
    signed_at: iso(60 * 24 * 7 + i * 60),
    requires_disposable_env: true,
    linked_scenario_ids: [],
    status: 'certified',
    created_at: iso(60 * 24 * 7 + i * 60),
    updated_at: iso(60 * 24 * 7 + i * 60),
  }));
  return { app_id: appId, invariants, total: invariants.length };
}

export function mockGetTierLabel(artifactId: string): TierLabelResult {
  return {
    artifact_id: artifactId,
    cases: [
      { test_id: 't_quote_happy', name: 'Term quote happy path', tier: 'behaves', evidence: { reason: '2 grounded navigation assertions' } },
      { test_id: 't_beneficiary', name: 'Beneficiary edit', tier: 'renders', evidence: { reason: 'no grounded navigation/outcome-region oracle' } },
    ],
    suite_tier: 'renders',
    counts: { behaves: 1, renders: 1, total: 2 },
    model_version: 'qec-tier-v1',
  };
}

export function mockListTouches(appId: string): TouchListResponse {
  const touches = [
    { touch_type: 'scenario_approve' as const, band: 'P0' as const, actor: 'Dana Whitfield' },
    { touch_type: 'invariant_author' as const, band: 'P0' as const, actor: 'Dana Whitfield' },
    { touch_type: 'gap_adjudicate' as const, band: 'P0' as const, actor: 'founder@vkpower.demo' },
  ].map((t, i) => ({
    touch_id: `tch_${hex64(appId + i).slice(0, 12)}`,
    touch_type: t.touch_type,
    band: t.band,
    app_id: appId,
    cycle_id: '',
    source: 'qec_direct' as const,
    source_ref: null,
    actor: t.actor,
    created_at: iso(i * 45 + 10),
  }));
  return { app_id: appId, touches, total: touches.length };
}

// ── the composed VERDICT LEDGER (hash-chained) ───────────────────────────────

interface LedgerSeed {
  kind: VerdictKind;
  subject: string;
  subject_kind: VerdictLedgerEntry['subject_kind'];
  app_id: string;
  actor?: string;
  signature?: string;
  band?: 'P0' | 'P1' | 'P2' | 'P3';
  reason?: string;
  source: VerdictLedgerEntry['source'];
  minsAgo: number;
}

const LEDGER_SEEDS: LedgerSeed[] = [
  { kind: 'certified', subject: 'New business term-life quote → e-sign', subject_kind: 'scenario', app_id: 'app_acme_quote', actor: 'Dana Whitfield', signature: 'Dana Whitfield, Chief Actuary', band: 'P0', source: 'approval_chain', minsAgo: 8 },
  { kind: 'refused', subject: 'Northshore · Claims Intake — cycle', subject_kind: 'cycle', app_id: 'app_northshore_claims', band: 'P0', reason: 'blocked_on_p0_gaps: route:/claims/{id}/appeal is a possible deletion — refusing "all green"', source: 'cycle', minsAgo: 22 },
  { kind: 'healed', subject: 'Guardian · Annuity Portal — e-sign button re-anchored', subject_kind: 'cycle', app_id: 'app_guardian_annuity', reason: 'oracle-proven heal: navigation + outcome-region confirmed, 0 false-heals', source: 'cycle', minsAgo: 41 },
  { kind: 'certified', subject: 'Issued face amount ≤ underwriting ceiling', subject_kind: 'invariant', app_id: 'app_beacon_uw', actor: 'Dana Whitfield', signature: 'Dana Whitfield, Chief Actuary', band: 'P0', source: 'approval_chain', minsAgo: 63 },
  { kind: 'refused', subject: 'R4 — fabricated locator', subject_kind: 'harness', app_id: 'app_acme_quote', reason: 'REFUSED_CORRECTLY: the factory refused an ungrounded selector instead of green-washing', source: 'harness', minsAgo: 95 },
  { kind: 'certified', subject: 'Harbor · Billing Cycle — universe baseline', subject_kind: 'universe', app_id: 'app_harbor_billing', actor: 'founder@vkpower.demo', signature: 'K. Rao', source: 'universe_baseline', minsAgo: 120 },
  { kind: 'healed', subject: 'ACME Life · Term Quote — date-picker widget rebind', subject_kind: 'cycle', app_id: 'app_acme_quote', reason: 'oracle-proven heal on a custom widget, 16/16 steps green', source: 'cycle', minsAgo: 150 },
  { kind: 'refused', subject: 'ACME Life · Term Quote — exploration', subject_kind: 'exploration', app_id: 'app_acme_quote', reason: 'REFUSED: evidence rule R2 — screenshot digest missing on 2 visits; artifact refused, never a silent empty', source: 'exploration', minsAgo: 190 },
  { kind: 'certified', subject: 'Beneficiary allocation sums to 100%', subject_kind: 'invariant', app_id: 'app_beacon_uw', actor: 'Dana Whitfield', signature: 'Dana Whitfield, Chief Actuary', band: 'P1', source: 'approval_chain', minsAgo: 240 },
];

function buildLedger(seeds: LedgerSeed[]): VerdictLedgerEntry[] {
  // oldest → newest so prev_hash → chain_hash links forward, then reversed for display
  const ordered = seeds.slice().sort((a, b) => b.minsAgo - a.minsAgo);
  let prev = '';
  const chained: VerdictLedgerEntry[] = ordered.map((s, i) => {
    const app = fixture(s.app_id);
    const chain_hash = hex64(`${prev}|${s.kind}|${s.subject}|${i}`);
    const verified = s.source === 'approval_chain' || s.source === 'universe_baseline';
    const entry: VerdictLedgerEntry = {
      id: `led_${hex64(s.subject + i).slice(0, 14)}`,
      kind: s.kind,
      subject: s.subject,
      subject_kind: s.subject_kind,
      chain_hash: verified ? chain_hash : '',
      prev_hash: verified ? prev : '',
      ts: iso(s.minsAgo),
      app_id: s.app_id,
      app_name: app?.app.name,
      actor: s.actor,
      signature: s.signature,
      band: s.band,
      reason: s.reason,
      verified,
      source: s.source,
    };
    if (verified) prev = chain_hash;
    return entry;
  });
  return chained.reverse(); // newest first for the stream
}

export function mockGetLedger(appId?: string): LedgerResponse {
  const seeds = appId ? LEDGER_SEEDS.filter((s) => s.app_id === appId) : LEDGER_SEEDS;
  const entries = buildLedger(seeds);
  const counts: Record<VerdictKind, number> = { certified: 0, refused: 0, healed: 0 };
  entries.forEach((e) => (counts[e.kind] += 1));
  return {
    entries,
    counts,
    composed: true,
    note: 'Composed client-side from the append-only approval/baseline chains + cycle/exploration/harness verdict signals (no single /ledger endpoint exists).',
  };
}

export function mockGetHealth(): HealthStatus {
  return {
    status: 'healthy',
    service: 'qe-central',
    db_qec: 'connected',
    db_substrate: 'connected',
    storage_backend: 'local',
    guc: { ok: true },
    kek: { provider: 'local', ready: true },
    harness_enabled: false,
  };
}

export function mockRunHarness(): HarnessRunResult {
  const rules = ['R1', 'R2', 'R3', 'R4', 'R5', 'R6', 'R7', 'R8'];
  return {
    harness_run_ids: rules.map((r) => `hrn_${hex64(r).slice(0, 12)}`),
    verdicts: rules.map((r) => ({ rule_id: r, verdict: 'REFUSED_CORRECTLY' as const })),
    green_wash_detected: false,
  };
}

export function mockListHarnessRuns(): HarnessRunsResponse {
  const rules: Array<[string, 'REFUSED_CORRECTLY' | 'PASS_BASELINE']> = [
    ['R1', 'REFUSED_CORRECTLY'],
    ['R2', 'REFUSED_CORRECTLY'],
    ['R4', 'REFUSED_CORRECTLY'],
    ['baseline', 'PASS_BASELINE'],
  ];
  return {
    runs: rules.map(([rule, verdict], i) => ({
      harness_run_id: `hrn_${hex64(rule + i).slice(0, 12)}`,
      tenant_id: 'acme-life',
      fixture_name: 'golden_3page_form',
      rule_id: rule,
      expected: 'refuse',
      observed: 'refused',
      verdict,
      report: {},
      created_at: iso(i * 30 + 15),
    })),
    total: rules.length,
  };
}

export function mockGetExploration(explorationId: string): Exploration {
  return {
    exploration_id: explorationId,
    tenant_id: 'acme-life',
    app_id: 'app_acme_quote',
    artifact_id: `art_${hex64('acme').slice(0, 12)}`,
    session_id: `ses_${hex64('acme').slice(0, 12)}`,
    status: 'completed',
    explorer_version: 'qe_explorer_v1',
    extractor_version: 'qec_live_v1@abc123',
    stats: { visits: 7, actions: 21, screenshots: 21, redactions: 3 },
    error: '',
    started_at: iso(200),
    finished_at: iso(194),
    created_at: iso(201),
    updated_at: iso(194),
  };
}

export function mockGetDossier(appId: string): Dossier {
  const f = fixture(appId);
  if (!f) throw notFound('app', appId);
  return {
    app: f.app,
    coverage: mockGetCoverage(appId),
    autonomy: mockGetAutonomy(appId),
    cycles: mockListCycles(appId).cycles,
    invariants: mockGetInvariants(appId).invariants,
    ledger: mockGetLedger(appId).entries,
    tier: mockGetTierLabel(f.app.latest_artifact_id),
    composed: true,
    generated_at: new Date().toISOString(),
  };
}

// ── mutation stubs ───────────────────────────────────────────────────────────

export function notFound(kind: string, id: string): Error {
  const err = new Error(`${kind} not found: ${id}`) as Error & { status?: number };
  err.status = 404;
  return err;
}

export function mockCreateApp(name: string, baseUrl: string): ClientApp {
  const id = `app_${hex64(name + Date.now()).slice(0, 12)}`;
  const host = (() => {
    try {
      return new URL(baseUrl).hostname;
    } catch {
      return baseUrl;
    }
  })();
  return makeApp(id, name, host, 'active', 'disposable', 'renders');
}
