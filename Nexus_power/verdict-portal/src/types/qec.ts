/* ══════════════════════════════════════════════════════════════════════════
   VKPower Verdict — domain types for the QE-Central API.

   Every shape here is modeled from the REAL service source (the source route is
   named in comments so a reviewer can diff it):
     · platform/qe-central/app/routers/{apps,cycles,cost,scenario_gov,explorations,
       harness,webhooks}.py
     · platform/qe-central/app/db/{models,gov_models,controlplane_models}.py
     · platform/qe-central/app/services/{coverage,touch_meter,approval,tier_label}.py

   Where an endpoint returns an opaque JSONB blob (journey, evidence, detail,
   budgets, …) it is typed `JsonObject` rather than invented. A `TODO(source:…)`
   comment marks the one or two shapes the router does not fully pin down — the
   only place such a marker is allowed, per the build contract.
   ════════════════════════════════════════════════════════════════════════ */

// ── JSON primitives (for the opaque JSONB columns) ──────────────────────────
export type JsonPrimitive = string | number | boolean | null;
export type JsonValue = JsonPrimitive | JsonValue[] | { [key: string]: JsonValue };
export type JsonObject = { [key: string]: JsonValue };

/** ISO-8601 timestamp string (the API serialises every datetime with .isoformat()). */
export type IsoTimestamp = string;

// ── Enumerated vocabularies (mirrors of the server frozensets) ──────────────

/** client_apps.status (routers/apps.py:_SETTABLE_STATUSES + soft-delete). */
export type AppStatus = 'active' | 'paused' | 'deleted';

/** env_attestation.env_kind (routers/apps.py:_ENV_KINDS). */
export type EnvKind = 'prod' | 'staging' | 'disposable';

/** app_cycles.state — the full state machine (controlplane_models.py:CYCLE_STATES). */
export type CycleState =
  | 'pending'
  | 'probing'
  | 'selecting'
  | 'crawling'
  | 'generating'
  | 'running'
  | 'healing'
  | 'verifying'
  | 'done'
  | 'failed'
  | 'budget_stopped'
  | 'blackout_deferred';

/** The three HONEST terminal states (controlplane_models.py:TERMINAL_CYCLE_STATES). */
export const TERMINAL_CYCLE_STATES: readonly CycleState[] = [
  'done',
  'failed',
  'budget_stopped',
];

/** app_cycles.trigger (controlplane_models.py:CYCLE_TRIGGERS). */
export type CycleTrigger =
  | 'webhook_repo'
  | 'schedule'
  | 'probe_drift'
  | 'manual'
  | 'full_floor';

/** POST /apps/{id}/cycles mode (cycles.py:CycleCreate / driver.CYCLE_MODES). */
export type CycleMode = 'auto' | 'full' | 'probe_only';

/** Criticality bands (scenario_gov / coverage / touch_meter). Never averaged. */
export type CriticalityBand = 'P0' | 'P1' | 'P2' | 'P3';
export const BANDS: readonly CriticalityBand[] = ['P0', 'P1', 'P2', 'P3'];

/** qec_scenarios.diff_state (gov_models.py:ScenarioRow). */
export type DiffState = 'new' | 'changed' | 'unchanged' | 'missing';

/** scenario review lifecycle state (scenario_gov.py review block). */
export type ReviewState = 'draft' | 'in_review' | 'approved' | 'rejected';

/** scenario review actions a human may submit (scenario_gov.py:_SCENARIO_REVIEW_ACTIONS). */
export type ReviewAction = 'submit' | 'approve' | 'reject' | 'reopen';

/** RENDERS-vs-BEHAVES tier (tier_label.py:TIERS + unlabeled). */
export type Tier = 'renders' | 'behaves' | 'unlabeled';

/** coverage scorecard verdict (coverage.py:VERDICT_OK/VERDICT_BLOCKED). */
export type CoverageVerdict = 'ok' | 'blocked_on_p0_gaps';

/** qec_coverage_gaps.kind (gov_models.py:CoverageGapRow / coverage.py:GAP_KINDS). */
export type GapKind =
  | 'possible_deletion'
  | 'uncovered_atom'
  | 'tier_gap'
  | 'unclassified_fail_up';

/** qec_coverage_gaps.status (coverage.py). */
export type GapStatus = 'open' | 'adjudicated' | 'waived';

/** qec_coverage_atoms.kind (coverage.py:ATOM_KINDS). */
export type AtomKind = 'route' | 'api_endpoint' | 'form_field' | 'journey_edge' | 'validator';

/** qec_coverage_atoms.source (coverage.py:ATOM_SOURCES). */
export type AtomSource = 'crawl' | 'repo' | 'answer_key';

/** qec_coverage_atoms.provenance (coverage.py:PROVENANCES). */
export type Provenance = 'G_DETERMINISTIC' | 'G_LIVE_CONFIRMED' | 'G_INFERRED';

/** qec_touch_events.touch_type (touch_meter.py:TOUCH_TYPES). */
export type TouchType =
  | 'scenario_approve'
  | 'invariant_author'
  | 'gap_adjudicate'
  | 'waiver_create'
  | 'credential_provision'
  | 'answer_key_edit'
  | 'heal_approve'
  | 'case_review'
  | 'defect_confirm';

/** qec_touch_events.source (touch_meter.py). */
export type TouchSource = 'qec_direct' | 'vkpower_audit';

/** qe_explorations.status (models.py:QEExplorationRow). */
export type ExplorationStatus = 'pending' | 'writing' | 'completed' | 'failed' | 'refused';

/** qe_harness_runs.verdict (models.py:QEHarnessRunRow). */
export type HarnessVerdict =
  | 'REFUSED_CORRECTLY'
  | 'GREEN_WASH_DETECTED'
  | 'CHAIN_ERROR'
  | 'PASS_BASELINE';

/** The three verdict kinds the ledger streams (composed — see LedgerResponse). */
export type VerdictKind = 'certified' | 'refused' | 'healed';

// ── Registered client application (routers/apps.py:_public_view) ────────────

/** env_attestation.rules_of_engagement (security/prod_guard.py:_roe_signed). */
export interface RulesOfEngagement {
  signed?: boolean;
  signed_by?: string;
  note?: string;
}

/** env_attestation JSONB (models.py:ClientAppRow comment). The three fields the
 * crawl gate (security/prod_guard.py) is fail-closed on: a signed
 * rules_of_engagement, a non-prod attested + unexpired envelope (attested_by +
 * env_kind + expires_at), and a passed preflight. */
export interface EnvAttestation {
  attested_by?: string;
  attested_at?: IsoTimestamp;
  env_kind?: EnvKind;
  reset_procedure?: string;
  expires_at?: IsoTimestamp;
  rules_of_engagement?: RulesOfEngagement;
  preflight?: { passed?: boolean; note?: string };
  /** The operator's AUTHORIZATION attestation — they own or are permitted to test
   *  the target URL. prod_guard._authorized_to_test requires authorized===true AND
   *  a non-empty authorized_by before any crawl reaches a site (the liability gate). */
  authorization?: { authorized?: boolean; authorized_by?: string; authorized_at?: IsoTimestamp };
  [key: string]: unknown;
}

/** fences JSONB (models.py:ClientAppRow comment). */
export interface Fences {
  allowed_hosts?: string[];
  blocked_path_globs?: string[];
  irreversible_verbs_extra?: string[];
  max_rps?: number;
  max_crawl_workers?: number;
  blackout_windows?: JsonValue[];
  allow_submit?: boolean;
  [key: string]: unknown;
}

/**
 * One registered client app — the `_public_view` shape (credentials NEVER echoed;
 * only `has_credentials`). GET /apps, GET /apps/{id}, and the create/update
 * responses all return this.
 */
/** Crawl-gate lifecycle the app UI reads (routers/apps.py:_public_view →
 *  security/prod_guard.py:onboarding_status). draft = not yet safe to crawl;
 *  attested = signed + non-prod but preflight not passed; live = fully gated open. */
export type OnboardingStatus = 'draft' | 'attested' | 'live';

/** Crawl coverage (engines/qe-explorer _build_coverage), carried on the
 *  exploration `stats.coverage` and surfaced on GET /explorations/{id}. Turns
 *  "why did the crawl only reach N flows?" into a NAMED seed-remediation list. */
export interface ExplorationCoverage {
  forms_found: number;
  forms_submitted: number;
  fields_inferred: string[];
  fields_needing_seed: string[];
  submit_candidates: string[];
  summary?: string;
}

export interface ClientApp {
  app_id: string;
  tenant_id: string;
  name: string;
  base_url: string;
  canonical_host: string;
  answer_key: JsonObject;
  env_attestation: EnvAttestation;
  fences: Fences;
  repo_binding: JsonObject;
  schedule: JsonObject;
  budgets: JsonObject;
  latest_artifact_id: string;
  baseline_fingerprint_id: string;
  status: AppStatus;
  has_credentials: boolean;
  /** Crawl-gate status (routers/apps.py:_public_view). draft/attested/live. */
  onboarding_status?: OnboardingStatus;
  /** True when the app is fully gated open for a crawl (all reasons cleared). */
  onboarding_ready?: boolean;
  /** The unmet crawl-gate reasons when not ready (auditable refusal). */
  onboarding_reasons?: string[];
  /** env_attestation.expires_at echoed for a quick expiry read. */
  attestation_expires_at?: string;
  /** Multi-env: the Environment Profile name every cycle runs against ('' = the
   *  app base URL, single-env). Surfaced from schedule.run_environment. */
  run_environment: string;
  created_at: IsoTimestamp;
  updated_at: IsoTimestamp;
  /**
   * Live status of the app's most recent crawl (from GET /apps/{id}). Lets the app
   * view show "Crawling…" on load — server truth, not ephemeral client state — so a
   * long crawl never leaves an empty Test Studio looking broken/completed.
   */
  crawl?: AppCrawlStatus;
  /**
   * CLIENT-SIDE ENRICHMENT (not on the app row): the app's suite tier
   * (`behaves` | `renders`), joined from GET /artifacts/{latest_artifact_id}/
   * tier-label `.suite_tier`. Present only after the client resolves it.
   */
  tier?: Tier;
}

/** Live status of an app's most recent crawl (GET /apps/{id}.crawl). */
export interface AppCrawlStatus {
  status: 'none' | 'pending' | 'writing' | 'running' | 'dispatched' | 'queued' | 'claimed' | 'stalled' | 'completed' | 'failed' | 'refused' | 'unknown';
  /** True while the crawl is not yet terminal (still exploring). */
  active: boolean;
  exploration_id?: string;
  started_at?: string;
  finished_at?: string;
  artifact_id?: string;
  /** Pages captured (populated on completion; 0 while a fresh crawl is mid-flight). */
  pages?: number;
  /**
   * Typed, durable diagnosis (Phase 0 — legible failure). Always present on a
   * crawl status; the UI keys a persistent "what happened + what to do" card on
   * `code`/`severity` so a reloaded failed/empty crawl is never a blank Studio.
   */
  diagnosis?: CrawlDiagnosis;
}

/** The six data dispositions — see qe-central `dispositions.py`. */
export type Disposition = 'SYNTHESIZE' | 'PICK' | 'CARRY' | 'OBSERVE' | 'ASK' | 'APPROVE';

/** One classified field in the Seed Manifest. */
export interface SeedItem {
  label: string;
  disposition: Disposition;
  default: string | null;
  grounded: boolean;
  reason: string;
  options: string[];
  required: boolean;
  editable: boolean;
  /** Added by flow grouping: true when a value is already in the answer key. */
  provided?: boolean;
  /** A dropdown/choice whose OPTIONS the crawl could not read — a choice, not free text. */
  uncaptured_options?: boolean;
}

/** A group of fields belonging to one flow (or the shared login group). */
export interface SeedFlow {
  key: string;
  name: string;              // human name — "Transfer", "Sign in", "Fields to provide"
  kind: 'flow' | 'auth';
  primary?: boolean;         // the flow the app was onboarded for (lead with it)
  satisfied?: boolean;       // auth only: stored credentials cover the login group
  to_provide: number;        // actionable fields not yet provided
  actionable: number;        // total ASK + APPROVE fields in this flow
  uncaptured: number;        // dropdowns whose options weren't read — blocks "ready"
  total: number;             // all fields (incl. auto-handled)
  items: SeedItem[];
}

/** Set when the app's onboarded entry flow was NOT captured on the crawl. */
export interface MissingPrimary {
  key: string;
  name: string;
  reason: string;
}

export type CoverageDisposition = 'FULLY' | 'PARTIAL' | 'UNHANDLED' | 'OPAQUE';

/** One control in the Coverage Ledger — how fully it was captured, and why. */
export interface CoverageControl {
  label: string;
  type: string;
  disposition: CoverageDisposition;
  reason: string;
  depends_on?: string;
}

/** The Coverage Ledger — measured per-app UI-capture coverage + every non-fully-captured
 *  control, named. The honesty spine: "did we miss anything?" as a number, not a surprise. */
export interface Coverage {
  controls: CoverageControl[];
  fully: number;
  partial: number;
  unhandled: number;
  opaque: number;
  total: number;
  coverage_pct: number;
  gaps: CoverageControl[];
  measures_captured_only?: boolean;
}

/** GET /apps/{id}/seed-manifest — the discovery-first Seed Manifest. */
export interface SeedManifest {
  artifact_id: string;
  status: 'ready' | 'no_crawl' | 'no_fields';
  recommended: SeedItem[]; // only ASK + APPROVE — the human 1%
  full: SeedItem[];        // every field with its grounded default
  prefill: Record<string, string>;
  counts: Partial<Record<Disposition, number>>;
  ask_count: number;
  approve_count: number;
  autonomous_count: number;
  // Flow grouping (added server-side): lead with the primary flow, split off login.
  flows?: SeedFlow[];
  auth?: SeedFlow | null;
  primary_flow?: string | null;
  missing_primary?: MissingPrimary | null;
  uncaptured_choice_count?: number;
  has_credentials?: boolean;
  coverage?: Coverage;
  mode?: 'recommended' | 'full';
  items?: SeedItem[];
}

/** Machine-readable crawl outcome — see qe-central `crawl_diagnosis.py`. */
export interface CrawlDiagnosis {
  code:
    | 'COMPLETED_OK' | 'SEEDS_NEEDED' | 'NO_CASES' | 'EMPTY_SUBSTRATE'
    | 'LOGIN_FAILED' | 'STALLED' | 'REFUSED' | 'FAILED'
    | 'RUNNING' | 'QUEUED' | 'NONE' | 'UNCLASSIFIED';
  /** 'ok' | 'info' (in-progress) | 'action' (a human must act) | 'warn' (a problem). */
  severity: 'ok' | 'info' | 'action' | 'warn';
  title: string;
  human: string;
  remediation: string;
  /** Named fields blocking deeper coverage (SEEDS_NEEDED). */
  fields: string[];
  evidence: Record<string, unknown>;
}

export interface AppListResponse {
  apps: ClientApp[];
  total: number;
}

/** POST /apps request body (routers/apps.py:AppCreate). */
export interface AppCreatePayload {
  name: string;
  base_url: string;
  canonical_host?: string;
  /** Login credentials — envelope-encrypted at rest, never echoed back. */
  credentials?: Record<string, unknown> | null;
  /** A login RECORDED on the Access page: steps, slot NAMES and the reuse keys.
   *  Never credential values — the session from the same recording rides in
   *  `credentials.session`. Held on the app until the first crawl mints an
   *  artifact, then materialised into a replayable recipe. */
  login_recording?: Record<string, unknown> | null;
  answer_key?: Record<string, unknown>;
  env_attestation?: EnvAttestation;
  fences?: Fences;
  repo_binding?: Record<string, unknown>;
  schedule?: Record<string, unknown>;
  budgets?: Record<string, unknown>;
}

/**
 * The HARD proof a run landed on the intended environment — a DOM banner match
 * (needed when envs share one URL and only a cookie differs) and/or a URL pattern.
 * A mismatch fails the run CLOSED to RED (never a silent mis-pin).
 */
export interface EnvAssertion {
  selector?: string;
  expect_text?: string;
  url_pattern?: string;
  [key: string]: unknown;
}

/**
 * One Environment Profile (dev/test/uat/prod) — a run-time REBIND of the app's one
 * learned flow. The `_env_public_view` shape: credentials NEVER echoed (only
 * `has_credentials`); routing cookies/headers are the env selector.
 */
export interface EnvProfile {
  environment_id: string;
  tenant_id: string;
  app_id: string;
  name: string;
  base_url: string;
  canonical_host: string;
  cookies: JsonValue[];
  headers: JsonObject;
  data_overrides: JsonObject;
  fences: Fences;
  env_attestation: EnvAttestation;
  env_assertion: EnvAssertion;
  status: string;
  has_credentials: boolean;
  created_at: IsoTimestamp;
  updated_at: IsoTimestamp;
}

export interface EnvProfileListResponse {
  app_id: string;
  environments: EnvProfile[];
}

/** POST /apps/{id}/environments request body (routers/apps.py:EnvCreate). */
export interface EnvProfileCreatePayload {
  name: string;
  base_url?: string;
  /** Routing cookies [{name,value,domain,path}] — the env selector (e.g. a Gloo cookie). */
  cookies?: JsonValue[];
  headers?: Record<string, unknown>;
  /** Login + HTTP basic-auth + SECRET cookies/headers — sealed (AAD=environment_id), never echoed. */
  credentials?: Record<string, unknown> | null;
  data_overrides?: Record<string, unknown>;
  fences?: Fences;
  env_attestation?: EnvAttestation;
  env_assertion?: EnvAssertion;
}

/** PATCH /apps/{id} request body (routers/apps.py:AppUpdate) — every field optional. */
export interface AppUpdatePayload extends Partial<AppCreatePayload> {
  status?: Extract<AppStatus, 'active' | 'paused'>;
  /** Multi-env: bind the app's run environment (validated server-side against an
   *  existing Environment Profile; '' clears it). */
  run_environment?: string;
}

export interface DeleteAppResponse {
  app_id: string;
  status: 'deleted';
  credentials_zeroed: boolean;
}

// ── Cycles (routers/cycles.py) ──────────────────────────────────────────────

/** honest_gaps JSONB (controlplane_models.py:AppCycleRow comment). */
export interface HonestGaps {
  uncomputable_pages_treated_changed?: number | string[] | JsonValue;
  vanished_pages_possible_deletion?: boolean;
  [key: string]: unknown;
}

/**
 * One carried-forward case in a cycle's selection.
 * TODO(source: controlplane/cycle/selector.py — not read in this pass): each
 * carried case is documented to carry `verdict_run_id` + `verdict_age_cycles`
 * (cycles.py:197-198). Modeled permissively until the selector shape is pinned.
 */
export interface CarriedForwardCase {
  test_id?: string;
  verdict_run_id?: string;
  verdict_age_cycles?: number;
  reason?: string;
  [key: string]: unknown;
}

/** cost roll-up from the append-only cost_ledger (cycles.py:_cost_snapshot). */
export interface CostSnapshot {
  /** unit → quantity, as strings (exact-precision Decimal, never a float). */
  units: Record<string, string>;
  /** runs that could not be correlated to a real duration — a metering GAP,
   *  surfaced separately so it can never hide inside a total. */
  unmetered_runs: number;
  /** dollars — ONLY when every entry is priced, else null (never invented). */
  usd: string | null;
  entry_count: number;
}

/** Compact cycle view (cycles.py:_cycle_summary). */
export interface CycleSummary {
  cycle_id: string;
  app_id: string;
  trigger: CycleTrigger;
  state: CycleState;
  terminal: boolean;
  mode: CycleMode | null;
  selected_count: number;
  carried_count: number;
  /** Regression Agent (Phase 5): cases needing human review (GENUINE_REGRESSION /
   *  HONEST_UNPROVEN) in this cycle. 0 when all selected cases passed or self-healed. */
  regression_review_count: number;
  possible_deletion: boolean;
  created_at: IsoTimestamp | null;
  started_at: IsoTimestamp | null;
  finished_at: IsoTimestamp | null;
}

export interface CycleListResponse {
  app_id: string;
  cycles: CycleSummary[];
  total: number;
}

/** The explicit selected-vs-carried block (cycles.py:get_cycle → `selected`). */
export interface CycleSelection {
  mode: CycleMode | null;
  selected_test_ids: string[];
  carried_forward: CarriedForwardCase[];
  selection_reason: string;
  per_case_reasons: Record<string, string>;
}

/** GET /cycles/{id} — the base app_cycles row + the composed selection/cost. */
export interface CycleDetail {
  cycle_id: string;
  tenant_id: string;
  app_id: string;
  trigger: CycleTrigger;
  state: CycleState;
  selected_scope: JsonObject;
  honest_gaps: HonestGaps;
  result: JsonObject;
  error: string;
  started_at: IsoTimestamp | null;
  finished_at: IsoTimestamp | null;
  created_at: IsoTimestamp;
  updated_at: IsoTimestamp;
  // ── composed by the endpoint on top of the row ──
  selected: CycleSelection;
  coverage_verdict: CoverageVerdict | null;
  cost: CostSnapshot;
}

/** POST /apps/{id}/cycles response (cycles.py:create_cycle, 202). */
export interface CreateCycleResponse {
  cycle_id: string;
  app_id: string;
  mode: CycleMode;
  trigger: CycleTrigger;
  state: 'pending';
}

// ── Cost (routers/cost.py) ──────────────────────────────────────────────────

export interface CostAggregate {
  units: Record<string, string>;
  unmetered_runs: number;
  usd: string | null;
  entry_count: number;
}

export interface AppCostResponse {
  app_id: string;
  group_by: string;
  window_hours: number | null;
  groups: Record<string, CostAggregate>;
  group_count: number;
}

// ── Scenario governance (routers/scenario_gov.py) ───────────────────────────

/** One entry in a scenario's review history (scenario_gov.py:review_scenario). */
export interface ReviewHistoryEntry {
  action: ReviewAction | 'carry_forward';
  by: string;
  at: IsoTimestamp;
  chain_hash: string;
}

/** The review block persisted on a scenario row (scenario_gov.py). */
export interface ScenarioReview {
  state?: ReviewState;
  approved_by?: string;
  approved_email?: string;
  approved_at?: IsoTimestamp;
  signature?: string;
  chain_hash?: string;
  rejected_by?: string;
  rejected_at?: IsoTimestamp;
  history?: ReviewHistoryEntry[];
  [key: string]: unknown;
}

/** Compact scenario view for the approval queue (scenario_gov.py:_scenario_view). */
export interface ScenarioView {
  scenario_id: string;
  app_id: string;
  name: string;
  source_artifact_id: string;
  criticality_band: CriticalityBand;
  criticality_evidence: JsonObject;
  registry_version: string;
  fingerprint: string;
  diff_state: DiffState;
  review_state: ReviewState;
  tier: Tier;
  materialized_artifact_id: string;
  status: string;
  updated_at: IsoTimestamp | null;
}

export interface ScenarioListResponse {
  app_id: string;
  state: string;
  band: string;
  scenarios: ScenarioView[];
  total: number;
}

/** Full scenario (row_to_dict of ScenarioRow) — GET /scenarios/{id}. */
export interface Scenario extends Omit<ScenarioView, 'review_state'> {
  tenant_id: string;
  journey: JsonObject;
  review: ScenarioReview;
  approved_snapshot: JsonObject | null;
  created_at: IsoTimestamp;
}

/** One appended, hash-chained approval event (approval.py:append_event return). */
export interface ApprovalEvent {
  event_id: string;
  action: ReviewAction | 'carry_forward';
  prev_hash: string;
  chain_hash: string;
  is_touch: boolean;
  carry_forward: boolean;
  created_at: IsoTimestamp;
}

/** POST /scenarios/{id}/review request body (scenario_gov.py:ReviewRequest). */
export interface ScenarioReviewPayload {
  action: ReviewAction;
  /** typed full name — REQUIRED to approve (422 otherwise). */
  signature?: string;
  note?: string;
}

/** POST /scenarios/{id}/review response. */
export interface ScenarioReviewResponse {
  scenario: ScenarioView;
  event: ApprovalEvent;
  touch: TouchRecord | null;
}

// ── Coverage scorecard (services/coverage.py:compute_coverage) ──────────────

export interface CoverageAtomsSummary {
  count: number;
  by_kind: Partial<Record<AtomKind, number>>;
  by_source: Partial<Record<AtomSource, number>>;
  by_provenance: Partial<Record<Provenance, number>>;
  atoms_hash: string;
}

export interface InvariantSummary {
  total: number;
  by_status: Record<string, number>;
  linked_to_scenarios: number;
}

export interface Waiver {
  reason: string;
  actor: string;
  created_at: IsoTimestamp;
  expires_at: IsoTimestamp;
  max_days: number;
}

/** One named coverage gap (coverage.py:_gap_row_to_dict, +blocking on scorecard). */
export interface CoverageGap {
  gap_id: string;
  tenant_id: string;
  app_id: string;
  kind: GapKind;
  band: CriticalityBand;
  detail: JsonObject;
  status: GapStatus;
  waiver: Waiver | null;
  waiver_expires_at: IsoTimestamp | null;
  created_at: IsoTimestamp | null;
  updated_at: IsoTimestamp | null;
  /** Present on the coverage scorecard: does this gap currently BLOCK all-green? */
  blocking?: boolean;
}

export interface CoverageScorecard {
  model_version: string;
  app_id: string;
  generated_at: IsoTimestamp;
  verdict: CoverageVerdict;
  atoms: CoverageAtomsSummary;
  invariants: InvariantSummary;
  tier_distribution: Partial<Record<Tier, number>>;
  gaps: CoverageGap[];
  open_gaps: number;
  blocking_gaps: number;
}

export interface GapListResponse {
  app_id: string;
  gaps: CoverageGap[];
  total: number;
}

/** POST /apps/{id}/gaps/{gap_id}/waive body (scenario_gov.py:GapWaive). */
export interface GapWaivePayload {
  reason: string;
  requested_expires_at?: IsoTimestamp | null;
}

/** POST /apps/{id}/gaps/{gap_id}/adjudicate body (scenario_gov.py:GapAdjudicate). */
export interface GapAdjudicatePayload {
  note?: string;
}

// ── Certified invariants (the non-enumerable half of coverage) ──────────────

export interface CertifiedInvariant {
  invariant_id: string;
  tenant_id: string;
  app_id: string;
  statement: string;
  criticality_band: CriticalityBand;
  signature: string;
  signed_by: string;
  signed_at: IsoTimestamp | null;
  requires_disposable_env: boolean;
  linked_scenario_ids: string[];
  status: string;
  created_at: IsoTimestamp;
  updated_at: IsoTimestamp;
}

export interface InvariantListResponse {
  app_id: string;
  invariants: CertifiedInvariant[];
  total: number;
}

/** POST /apps/{id}/invariants body (scenario_gov.py:InvariantCreate). */
export interface InvariantCreatePayload {
  statement: string;
  /** e-signature (full name) — REQUIRED (422 if blank). */
  signature: string;
  criticality_band?: CriticalityBand;
  requires_disposable_env?: boolean;
  linked_scenario_ids?: string[];
}

export interface CreateInvariantResponse {
  invariant_id: string;
  app_id: string;
  band: CriticalityBand;
  status: string;
  touch: TouchRecord | null;
}

// ── Tier labels (services/tier_label.py) ────────────────────────────────────

export interface CaseTierLabel {
  test_id: string;
  name?: string;
  tier: Tier;
  evidence: JsonObject;
  computed_at?: IsoTimestamp;
}

export interface TierCounts {
  behaves: number;
  renders: number;
  total: number;
}

export interface TierLabelResult {
  artifact_id: string;
  cases: CaseTierLabel[];
  suite_tier: Tier;
  counts: TierCounts;
  model_version: string;
  persisted?: { inserted: number; updated: number; skipped: number; total: number };
}

// ── Autonomy KPI (services/touch_meter.py — NEVER a single averaged number) ──

export interface BandAutonomy {
  governed_scenarios: number;
  human_touches: number;
  /** null when there is no governed denominator — an honest "not computable",
   *  never a misleading 0 or 100. */
  autonomy_pct: number | null;
  by_type: Partial<Record<TouchType, number>>;
}

export interface AutonomyByBand {
  model_version: string;
  app_id: string;
  cycle_id: string;
  by_band: Partial<Record<CriticalityBand, BandAutonomy>>;
  unattributed: {
    human_touches: number;
    by_type: Partial<Record<TouchType, number>>;
    note: string;
  };
  totals: {
    governed_scenarios: number;
    human_touches: number;
  };
  note: string;
}

export interface CycleMeta {
  cycle_id: string;
  state: CycleState;
  trigger: CycleTrigger;
  created_at: IsoTimestamp | null;
}

export interface TrendPoint {
  cycle_id: string;
  governed_scenarios: number;
  human_touches: number;
  /** null for any cycle where the band has no governed denominator (never 100%). */
  autonomy_pct: number | null;
}

export interface AutonomyTrend {
  model_version: string;
  app_id: string;
  window: number;
  cycles: CycleMeta[];
  per_band: Partial<Record<CriticalityBand, TrendPoint[]>>;
  unattributed: Array<{ cycle_id: string; human_touches: number }>;
  note: string;
}

// ── Human-touch meter ───────────────────────────────────────────────────────

/** record_touch return (touch_meter.py) — also the `touch` field on review/invariant. */
export interface TouchRecord {
  touch_id: string;
  touch_type: TouchType;
  band?: CriticalityBand | '';
  app_id?: string;
  cycle_id?: string;
  source?: TouchSource;
  source_ref?: string | null;
  deduped?: boolean;
  created_at?: IsoTimestamp;
}

/** list_touches audit row (touch_meter.py:list_touches). */
export interface Touch {
  touch_id: string;
  touch_type: TouchType;
  band: CriticalityBand | '';
  app_id: string;
  cycle_id: string;
  source: TouchSource;
  source_ref: string | null;
  actor: string;
  created_at: IsoTimestamp | null;
}

export interface TouchListResponse {
  app_id: string;
  touches: Touch[];
  total: number;
}

// ── Criticality registry (routers/scenario_gov.py:get_registry) ─────────────

/** One criticality signal in a registry pack (gov_models.py:CriticalityRegistryRow). */
export interface CriticalitySignal {
  signal_id: string;
  band: CriticalityBand;
  applies_to?: string[];
  matchers?: JsonValue;
  rationale?: string;
  [key: string]: unknown;
}

export interface CriticalityRegistry {
  registry_version: string;
  signals: CriticalitySignal[];
  signal_count: number;
  is_seed_fallback: boolean;
  classifier: string;
}

// ── Explorations (routers/explorations.py) ──────────────────────────────────

export interface Exploration {
  exploration_id: string;
  tenant_id: string;
  app_id: string;
  artifact_id: string;
  session_id: string;
  status: ExplorationStatus;
  explorer_version: string;
  extractor_version: string;
  stats: JsonObject;
  /** honest failure / refusal reason (empty when completed). */
  error: string;
  started_at: IsoTimestamp | null;
  finished_at: IsoTimestamp | null;
  created_at: IsoTimestamp;
  updated_at: IsoTimestamp;
}

/** POST /explorations (Phase-1 dispatch) response — the crawl was accepted and
 * is running; poll GET /explorations/{id} for the terminal status + stats. */
export interface CreateExplorationResponse {
  exploration_id: string;
  app_id: string;
  crawl_id: string;
  extractor_version: string;
  status: string;
  accepted?: boolean;
}

// ── REFUSE harness (routers/harness.py) ─────────────────────────────────────

/**
 * One rule verdict from the REFUSE matrix.
 * TODO(source: harness/runner.py:run_refuse_matrix — not read in this pass): the
 * `verdicts[]` element shape is modeled from qe_harness_runs columns; extend if
 * the runner emits more fields.
 */
export interface HarnessRuleVerdict {
  rule_id: string;
  verdict: HarnessVerdict;
  expected?: string;
  observed?: string;
  harness_run_id?: string;
}

export interface HarnessRunResult {
  harness_run_ids: string[];
  verdicts: HarnessRuleVerdict[];
  green_wash_detected: boolean;
}

/** One persisted harness run (row_to_dict of QEHarnessRunRow). */
export interface HarnessRun {
  harness_run_id: string;
  tenant_id: string;
  fixture_name: string;
  rule_id: string;
  expected: string;
  observed: string;
  verdict: HarnessVerdict;
  report: JsonObject;
  created_at: IsoTimestamp;
}

export interface HarnessRunsResponse {
  runs: HarnessRun[];
  total: number;
}

// ── Health (GET /health) ────────────────────────────────────────────────────

export interface HealthStatus {
  status: 'healthy' | 'degraded';
  service: string;
  db_qec: 'connected' | 'disconnected';
  db_substrate: 'connected' | 'disconnected';
  storage_backend: string;
  guc: JsonObject;
  kek: JsonObject;
  harness_enabled: boolean;
}

// ══════════════════════════════════════════════════════════════════════════
//  COMPOSED types — the signature portal surfaces the API does not serve as a
//  single endpoint. The API client builds these from primitive calls and marks
//  them `composed: true` so the UI can be honest about their derivation.
// ══════════════════════════════════════════════════════════════════════════

/**
 * One entry in the VERDICT LEDGER — a hash-chained stream of certified / refused
 * / healed verdicts. There is no single `/ledger` endpoint; the client COMPOSES
 * this from the append-only chains + cycle/exploration/harness signals (see
 * `api.getLedger`). `chain_hash`/`prev_hash` are the tamper-evident chain links
 * for entries that carry one (approvals, baselines); composed signals that have
 * no chain hash leave them empty and set `verified: false`.
 */
export interface VerdictLedgerEntry {
  id: string;
  kind: VerdictKind;
  /** human subject line (scenario name, app name, invariant statement, rule id). */
  subject: string;
  subject_kind: 'scenario' | 'invariant' | 'universe' | 'cycle' | 'exploration' | 'harness' | 'app';
  chain_hash: string;
  prev_hash: string;
  ts: IsoTimestamp;
  app_id?: string;
  app_name?: string;
  actor?: string;
  signature?: string;
  band?: CriticalityBand;
  /** the honest reason (esp. for refused): why this verdict landed. */
  reason?: string;
  /** does this entry carry a verifiable chain link? */
  verified: boolean;
  /** where the client derived this entry from. */
  source: 'approval_chain' | 'universe_baseline' | 'cycle' | 'exploration' | 'harness' | 'touch';
}

export interface LedgerResponse {
  entries: VerdictLedgerEntry[];
  counts: Record<VerdictKind, number>;
  /** true — this stream is composed client-side, not a single server endpoint. */
  composed: boolean;
  note: string;
}

/**
 * A DOSSIER — the full evidence bundle for one subject (an app, here). No single
 * `/dossier` endpoint exists; `api.getDossier` composes it from the app row, the
 * coverage scorecard, per-band autonomy, recent cycles, certified invariants,
 * and the app-scoped verdict ledger. Marked `composed` for honesty.
 */
export interface Dossier {
  app: ClientApp;
  coverage: CoverageScorecard | null;
  autonomy: AutonomyByBand | null;
  cycles: CycleSummary[];
  invariants: CertifiedInvariant[];
  ledger: VerdictLedgerEntry[];
  tier: TierLabelResult | null;
  composed: boolean;
  generated_at: IsoTimestamp;
}
