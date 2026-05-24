// ═══════════════════════════════════════════════════════════════
//  Canonical Asset View Model — unified contract for all
//  canonical result consumers (pages, adapters, downstream cards).
// ═══════════════════════════════════════════════════════════════

// ── Raw API response types (match backend response shapes) ──

/** Matches the full CanonicalArtifactRow shape returned by GET /v1/artifacts/{id}. */
export interface CanonicalArtifact {
  artifact_id: string;
  tenant_id: string;
  session_id: string;
  media_fingerprint: string | null;
  status: string;
  workflow_id: string | null;
  source_type: string | null;
  source_filename: string | null;
  created_by: string | null;
  duration_seconds: number;
  scene_count: number;
  frame_count: number;
  safe_transcript_text: string;
  visual_summary: string;
  application_types_seen: string[];
  brain_quality_score: number | null;
  quality_gate_passed: boolean;
  quality_gate_outcome: string | null;
  has_real_transcript: boolean;
  has_visual_semantics: boolean;
  /** True only when visual_scenes rows are actually persisted in the DB. */
  visual_e2e_ready?: boolean;
  semantic_completeness_score: number | null;
  full_artifact_json: Record<string, unknown> | null;
  processing_time_seconds: number;
  created_at: string;
  completed_at: string | null;
  error: string | null;
}

/** Matches the response from GET /v1/artifacts/{id}/status. */
export interface ArtifactCompletionStatus {
  artifact_id: string;
  workflow_id: string | null;
  session_id: string;
  tenant_id: string;
  status: string;
  quality_gate_passed: boolean;
  quality_gate_outcome: string | null;
  brain_quality_score: number | null;
  has_real_transcript: boolean;
  has_visual_semantics: boolean;
  /** True only when visual_scenes rows are actually persisted in the DB. */
  visual_e2e_ready?: boolean;
  visual_scene_count_persisted?: number;
  semantic_completeness_score: number | null;
  review_reasons: string[];
  model_provenance: Record<string, string>;
  source_type: string | null;
  source_filename: string | null;
  processing_time_seconds: number | null;
  created_at: string | null;
  completed_at: string | null;
  error: string | null;
}

/** Matches the response from GET /v1/artifacts/{id}/transcript. */
export interface ArtifactTranscript {
  artifact_id: string;
  session_id: string;
  safe_transcript_text: string;
}

/** Single timeline entry produced by the orchestrator during chain execution. */
export interface WorkflowTimelineEntry {
  timestamp: string;
  event: string;
  detail: string;
}

/** Matches the response from GET /v1/workflows/{id}/timeline. */
export interface WorkflowTimeline {
  workflow_id: string;
  chain_name: string;
  status: string;
  timeline: WorkflowTimelineEntry[];
  started_at: string;
  completed_at: string | null;
}

/** Quality score breakdown by dimension. */
export interface QualityScoreBreakdown {
  transcript: number | null;
  visual: number | null;
  pii: number | null;
  completeness: number | null;
}

/** A stage that stalled beyond expected duration during workflow execution. */
export interface StallEvent {
  stage: string;
  duration_seconds: number;
}

/** Visual graph structure embedded in the canonical artifact. */
export interface VisualGraph {
  nodes: Array<Record<string, unknown>>;
  edges: Array<Record<string, unknown>>;
}

// ── Downstream Launch Actions ──────────────────────────────

export type LaunchActionState =
  | 'ready'
  | 'preview_only'
  | 'requires_mapping'
  | 'coming_next';

export interface CanonicalLaunchAction {
  id: string;
  label: string;
  description: string;
  icon: string;
  route: string;
  state: LaunchActionState;
  reason?: string;
  requires: string[];
}

// ── Cache Provenance ───────────────────────────────────────

export type ArtifactProvenance = 'fresh' | 'cache_reuse' | 'alias';

// ── Workflow Sync State ────────────────────────────────────

export type WorkflowSyncState = 'synced' | 'fallback' | 'stale';

// ── Quality Outcome ────────────────────────────────────────

export type QualityOutcome = 'pass' | 'fail' | 'needs_review';

// ═══════════════════════════════════════════════════════════════
//  CanonicalAssetViewModel — the single normalized shape that
//  every consumer page binds to.
// ═══════════════════════════════════════════════════════════════

export interface CanonicalAssetViewModel {
  // ── Identity ──────────────────────────────────────────────
  artifact_id: string;
  session_id: string;
  workflow_id: string | null;
  tenant_id: string;

  // ── Source ────────────────────────────────────────────────
  source_filename: string | null;
  source_type: string | null;
  duration_seconds: number;
  created_by: string | null;
  created_at: string;
  completed_at: string | null;

  // ── Provenance (architect correction #4) ─────────────────
  provenance: ArtifactProvenance;
  original_session_id: string | null;
  produced_at: string;
  cache_hit: boolean;

  // ── Quality ───────────────────────────────────────────────
  quality_score: number | null;
  quality_outcome: QualityOutcome;
  quality_gate_passed: boolean;
  score_breakdown: QualityScoreBreakdown;
  review_reasons: string[];

  // ── Semantic flags ────────────────────────────────────────
  has_real_transcript: boolean;
  has_visual_semantics: boolean;
  semantic_completeness_score: number | null;

  // ── Content ───────────────────────────────────────────────
  safe_transcript_preview: string;
  transcript_segment_count: number;
  transcript_word_count: number;
  speaker_count: number;
  visual_summary: string;
  frame_count: number;
  scene_count: number;
  application_types_seen: string[];
  visual_graph: VisualGraph | null;

  // ── Model provenance ──────────────────────────────────────
  model_provenance: Record<string, string>;

  // ── Processing ────────────────────────────────────────────
  processing_time_seconds: number;
  timeline: WorkflowTimelineEntry[];

  // ── Operator truth (architect correction #6) ──────────────
  degraded_stages: string[];
  skipped_stages: string[];
  retry_count: number;
  stall_events: StallEvent[];
  workflow_sync_state: WorkflowSyncState;

  // ── Downstream readiness (architect correction #5) ────────
  launch_actions: CanonicalLaunchAction[];
}


// ═══════════════════════════════════════════════════════════════
//  Process Oracle — Persona Draft Generation
// ═══════════════════════════════════════════════════════════════

/** A single piece of evidence grounding a persona claim. */
export interface EvidenceCitation {
  text: string;
  source_modality: 'transcript' | 'visual' | 'graph' | 'inferred';
  timestamp_range?: string | null;
  confidence: number;
  /** Visual-strict only: which scene grounds this citation. */
  scene_id?: string;
  /** Visual-strict only: which control grounds this citation. */
  control_id?: string;
  /** Visual-strict only: which flow edge grounds this citation. */
  edge_id?: string;
}

/** An actor/role identified in the domain. */
export interface DomainActor {
  name: string;
  role: string;
  evidence: EvidenceCitation[];
}

/** A system/application identified in the domain. */
export interface DomainSystem {
  name: string;
  purpose: string;
  evidence: EvidenceCitation[];
}

/** A workflow/process step identified in the domain. */
export interface DomainWorkflow {
  step_number: number;
  name: string;
  description: string;
  actors: string[];
  systems: string[];
  decisions: string[];
  evidence: EvidenceCitation[];
}

/** A risk or unknown identified in the domain. */
export interface DomainRisk {
  description: string;
  severity: 'low' | 'medium' | 'high' | 'critical';
  evidence: EvidenceCitation[];
}

/** Structured domain knowledge extracted from the canonical artifact. */
export interface DomainMap {
  actors: DomainActor[];
  systems: DomainSystem[];
  entities: string[];
  workflows: DomainWorkflow[];
  decisions: string[];
  risks: DomainRisk[];
  unknowns: string[];
}

/** The generated persona profile. */
export interface PersonaDraftProfile {
  name: string;
  description: string;
  system_prompt: string;
  capabilities: string[];
  specialty_domains: string[];
  avatar_icon: string;
  stage_config: Record<string, { engines: string[]; auto_advance: boolean }>;
}

/** Grounding summary — how trustworthy is the generated content. */
export interface GroundingContract {
  total_evidence_count: number;
  modality_distribution: {
    transcript: number;
    visual: number;
    graph: number;
    inferred: number;
  };
  avg_confidence: number;
  open_questions: string[];
}

/** Provenance — exactly how and from what the draft was generated. */
export interface PersonaDraftProvenance {
  artifact_id: string;
  session_id: string;
  workflow_id: string;
  generated_at: string;
  model_used: string;
  model_backend: string;
  quality_score_threshold: number;
  artifact_quality_score: number;
  artifact_status?: string;
  has_real_transcript?: boolean;
  has_visual_semantics?: boolean;
  platform_processing_ms?: number;
}

/** Full response from POST /v1/personas/generate-draft. */
export interface PersonaDraftResponse {
  success: boolean;
  artifact_id: string;
  session_id: string;
  persona: PersonaDraftProfile;
  domain_map: DomainMap;
  grounding_contract: GroundingContract;
  provenance: PersonaDraftProvenance;
  processing_time_ms: number;
  cached?: boolean;
  cache_hit_ms?: number;
}


// ═══════════════════════════════════════════════════════════════
//  Test Architect — Test Strategy Generation
// ═══════════════════════════════════════════════════════════════

/** A single step within a test case. */
export interface TestStep {
  step_number: number;
  action: string;
  input_data: string;
  expected_behavior: string;
  /** Visual-strict only: the scene_id whose frame proves this step. */
  evidence_scene_id?: string;
  /** Visual-strict only: the control_id whose selector implements this step. */
  evidence_control_id?: string;
  /** Visual-strict only: the flow edge whose transition is asserted by this step. */
  evidence_edge_id?: string;
  /** Visual-strict only: 0..1 confidence that all three groundings hold. */
  proof_confidence?: number;
  /** Visual-strict only: the target element label/selector (e.g. button text). */
  target_element?: string;
}

/** A single test case with full KT traceability. */
export interface TestCase {
  case_id: string;
  title: string;
  category: 'happy_path' | 'negative' | 'boundary' | 'edge_case' | 'security' | 'performance';
  priority: 'P0_critical' | 'P1_high' | 'P2_medium' | 'P3_low';
  preconditions: string[];
  steps: TestStep[];
  expected_result: string;
  test_data: Record<string, string>;
  tags: string[];
  evidence_trace: EvidenceCitation[];
}

/** A group of related test cases for a workflow step. */
export interface TestScenario {
  scenario_id: string;
  workflow_step_number: number;
  workflow_step_name: string;
  description: string;
  test_cases: TestCase[];
}

/** Maps a requirement/step to its test coverage. */
export interface TraceabilityEntry {
  requirement: string;
  workflow_step_number: number;
  test_case_ids: string[];
  coverage_status: 'covered' | 'partial' | 'gap';
  evidence_count: number;
}

/** Test coverage statistics. */
export interface CoverageBreakdown {
  total_scenarios: number;
  total_cases: number;
  by_category: Record<string, number>;
  by_priority: Record<string, number>;
  coverage_percentage: number;
  gap_areas: string[];
}

/** High-level test plan metadata. */
export interface TestPlanSummary {
  name: string;
  objective: string;
  scope: string;
  approach: string;
  source_persona: string;
  source_artifact_id: string;
}

/** Provenance for test strategy generation. */
export interface TestStrategyProvenance {
  artifact_id: string;
  session_id: string;
  persona_name: string;
  generated_at: string;
  model_used: string;
  model_backend: string;
  generation_time_ms: number;
  workflow_steps_analysed: number;
  risks_considered: number;
  platform_processing_ms?: number;
  source_persona_generated_at?: string;
  source_persona_quality?: string;
}

/** Full response from POST /v1/test-strategy/generate. */
export interface TestStrategyResponse {
  success: boolean;
  artifact_id: string;
  session_id: string;
  test_plan: TestPlanSummary;
  test_scenarios: TestScenario[];
  coverage: CoverageBreakdown;
  traceability: TraceabilityEntry[];
  provenance: TestStrategyProvenance;
  processing_time_ms: number;
  cached?: boolean;
  cache_hit_ms?: number;
}

// ─── E2E Architect Types ──────────────────────────────────────

/** A testable variable extracted from multimodal evidence. */
export interface E2EVariable {
  name: string;
  type: string;
  observed_values: string[];
  inferred_values: string[];
  source: string;
  impacts: string[];
}

/** A branching decision point in the workflow. */
export interface DecisionPoint {
  step_number: number;
  condition: string;
  observed_path: string;
  alternative_path: string;
  source: string;
}

/** A critical E2E test scenario with data combinations. */
export interface E2EScenario {
  scenario_id: string;
  title: string;
  category: 'observed' | 'inferred_high_risk' | 'boundary_assumption';
  priority: 'P0_critical' | 'P1_high' | 'P2_medium' | 'P3_low';
  rationale: string;
  evidence_sources: EvidenceCitation[];
  preconditions: string[];
  steps: TestStep[];
  expected_outcome: string;
  data_matrix: Record<string, string>[];
  workflow_steps_covered: number[];
  risk_areas_addressed: string[];
  /** Which generation strategy produced this scenario.
   *  LLM-driven: happy_path, variant, negative, boundary.
   *  Deterministic: state_explorer, cross_app, error_state.
   *  P4 Co-Architect: co_architect (agent-proposed, manually added). */
  strategy?:
    | 'happy_path'
    | 'variant'
    | 'negative'
    | 'boundary'
    | 'state_explorer'
    | 'cross_app'
    | 'error_state'
    | 'co_architect';
  /** Visual-strict only: how many steps are grounded in scene + control. */
  visual_proven_steps?: number;
  /** Visual-strict only: total step count (for proven/total display). */
  visual_total_steps?: number;
  /** P3 Lifecycle: current state of this scenario. */
  state?: ScenarioLifecycleState;
  /** P3 Lifecycle: when state last changed (ISO timestamp). */
  state_changed_at?: string | null;
  /** P3 Lifecycle: user_id who made the last transition. */
  state_changed_by?: string;
  /** P3 Lifecycle: email of who made the last transition. */
  state_changed_by_email?: string;
  /** P3 Lifecycle: comments thread (ordered oldest → newest). */
  comments?: ScenarioComment[];
  /** P3 Lifecycle: state transition history. */
  audit_log?: ScenarioAuditEntry[];
  /** P6 Execution Feedback: last-run summary, or null if never run. */
  last_run?: ScenarioLastRunSummary | null;
}

// ─── P6 Execution Feedback ────────────────────────────────────────

/** Run-level status. */
export type TestRunStatus =
  | 'running'
  | 'passed'
  | 'failed'
  | 'error'
  | 'cancelled';

/** Step-level status (worst-of, rolled up per scenario). */
export type TestStepRunStatus =
  | 'passed'
  | 'failed'
  | 'skipped'
  | 'timed_out'
  | 'broken';

/** Compact last-run summary attached to every E2EScenario. */
export interface ScenarioLastRunSummary {
  last_run_id: string;
  last_run_status: TestStepRunStatus;
  last_run_at: string | null;
  last_duration_ms: number;
  last_error_message: string;
  ci_commit_sha: string;
  ci_pipeline_url: string;
  flake_rate_pct: number;
  is_flaky: boolean;
  consecutive_failures: number;
  runs_in_window: number;
  selector_drift_observed: boolean;
}

/** Full test_run row returned by the runs list endpoint. */
export interface TestRunDetail {
  run_id: string;
  artifact_id: string;
  tenant_id: string;
  ci_run_id: string;
  ci_commit_sha: string;
  ci_pipeline_url: string;
  environment: string;
  status: TestRunStatus;
  total_steps: number;
  passed_steps: number;
  failed_steps: number;
  skipped_steps: number;
  duration_ms: number;
  started_at: string | null;
  completed_at: string | null;
  ingested_at: string | null;
  metadata_json: Record<string, unknown>;
}

/** Drift report returned per-step in the last-run detail endpoint. */
export interface SelectorDriftReport {
  selector_drifted: boolean;
  bbox_drifted: boolean;
  selector_diff: string;
  bbox_pixel_distance: number | null;
}

/** One step in the last-run detail. */
export interface TestRunStepDetail {
  step_run_id: string;
  run_id: string;
  artifact_id: string;
  tenant_id: string;
  scenario_id: string;
  step_number: number;
  status: TestStepRunStatus;
  duration_ms: number;
  expected_selector: string;
  resolved_selector: string;
  resolved_bbox_json: Record<string, number>;
  evidence_scene_id: string;
  evidence_control_id: string;
  evidence_edge_id: string;
  error_message: string;
  screenshot_url: string;
  metadata_json: Record<string, unknown>;
  created_at: string | null;
  drift: SelectorDriftReport;
  root_cause_hints: string[];
  last_passing_at: string | null;
}

/** Response from /scenarios/{id}/last-run. */
export interface ScenarioLastRunDetailResponse {
  artifact_id: string;
  scenario_id: string;
  has_runs: boolean;
  run?: TestRunDetail;
  steps?: TestRunStepDetail[];
}

// ─── P7 Demo Diff + Self-Healing ─────────────────────────────────

/** One control's change inside a scene diff. */
export interface ControlDiff {
  label: string;
  baseline_control_id: string;
  current_control_id: string;
  change_kind:
    | 'added' | 'removed' | 'renamed' | 'selector_changed'
    | 'moved' | 'automation_lost' | 'automation_gained' | 'unchanged';
  detail: string;
}

/** One scene's diff. */
export interface SceneDiff {
  page_key: string;
  baseline_scene_id: string;
  current_scene_id: string;
  change_kind: 'added' | 'removed' | 'ocr_changed' | 'controls_changed' | 'unchanged';
  title: string;
  baseline_ocr_preview: string;
  current_ocr_preview: string;
  control_diffs: ControlDiff[];
}

/** One flow edge's diff. */
export interface EdgeDiff {
  from_page_key: string;
  to_page_key: string;
  action_kind: string;
  baseline_edge_id: string;
  current_edge_id: string;
  change_kind: 'added' | 'removed';
}

/** Full diff between two visual evidence graphs. */
export interface ArtifactDiffReport {
  baseline_artifact_id: string;
  current_artifact_id: string;
  scenes: SceneDiff[];
  edges: EdgeDiff[];
  app_instances_added: string[];
  app_instances_removed: string[];
  summary: Record<string, number>;
}

/** A scenario whose steps were affected by a diff. */
export interface AffectedScenario {
  scenario_id: string;
  title: string;
  strategy?: string;
  state?: string;
  affected_step_numbers: number[];
  reasons: string[];
}

/** Response from /artifacts/diff. */
export interface ArtifactDiffResponse {
  success: boolean;
  diff: ArtifactDiffReport;
  affected_scenarios: AffectedScenario[];
}

/** One healing suggestion for a stale step. */
export interface HealSuggestion {
  scenario_id: string;
  step_number: number;
  old_control_id: string;
  old_label: string;
  old_scene_id: string;
  new_control_id: string;
  new_label: string;
  new_scene_id: string;
  new_selector: string;
  match_kind:
    | 'exact_in_same_scene' | 'label_match_other_scene'
    | 'bbox_nearest' | 'no_candidate';
  match_confidence: number;
  reason: string;
}

/** A heal plan returned by /heal-suggestions. */
export interface HealPlan {
  scenarios_scanned: number;
  suggestions: HealSuggestion[];
  unhealable_steps: Array<{ scenario_id: string; step_number: number; reason: string }>;
}

/** Response from /heal-suggestions. */
export interface HealSuggestionsResponse {
  success: boolean;
  artifact_id: string;
  plan: HealPlan;
}

/** Response from /apply-healing. */
export interface HealApplyResponse {
  success: boolean;
  artifact_id: string;
  result: {
    applied_count: number;
    rejected_count: number;
    audit_entries: Array<{
      scenario_id: string;
      step_number: number;
      outcome: 'applied' | 'rejected';
      reason?: string;
      old_control_id?: string;
      new_control_id?: string;
      old_scene_id?: string;
      new_scene_id?: string;
      match_kind?: string;
      match_confidence?: number;
    }>;
  };
}

/** P3 Lifecycle states for an E2E scenario. */
export type ScenarioLifecycleState =
  | 'draft'
  | 'reviewed'
  | 'approved'
  | 'rejected'
  | 'automated'
  | 'live'
  | 'stable'
  | 'failing';

/** A comment on an E2E scenario (P3). */
export interface ScenarioComment {
  comment_id: string;
  user_id: string;
  email: string;
  body: string;
  created_at: string;
}

/** A state transition entry in the audit log (P3). */
export interface ScenarioAuditEntry {
  from_state: ScenarioLifecycleState;
  to_state: ScenarioLifecycleState;
  user_id: string;
  email: string;
  at: string;
  note: string;
}

/** Response from the per-scenario state endpoint (P3). */
export interface ScenarioStateResponse {
  state_id: string;
  artifact_id: string;
  scenario_id: string;
  tenant_id: string;
  session_id: string;
  state: ScenarioLifecycleState;
  state_changed_at: string | null;
  state_changed_by: string;
  state_changed_by_email: string;
  comments_json: ScenarioComment[];
  audit_log_json: ScenarioAuditEntry[];
  created_at: string | null;
  updated_at: string | null;
}

// ─── P4 Co-Architect ─────────────────────────────────────────────

/** A single message in a Co-Architect chat conversation. */
export interface CoArchitectMessage {
  role: 'user' | 'assistant';
  content: string;
}

/** A step inside a Co-Architect proposed scenario. Every field except
 *  evidence_edge_id is mandatory; steps without scene+control are dropped
 *  by the backend before they ever reach the client. */
export interface CoArchitectProposedStep {
  step_number: number;
  action: string;
  input_data: string;
  expected_output: string;
  evidence_scene_id: string;
  evidence_control_id: string;
  evidence_edge_id: string;
  proof_confidence: number;
}

/** A scenario proposed by the Co-Architect agent. */
export interface CoArchitectProposedScenario {
  title: string;
  rationale: string;
  strategy: string;
  steps: CoArchitectProposedStep[];
}

/** Heart's /co-architect/chat response shape (also returned by the
 *  Platform API shim). */
export interface CoArchitectChatResponse {
  success: boolean;
  artifact_id: string;
  session_id: string;
  response: string;
  proposed_scenarios: CoArchitectProposedScenario[];
  parse_warning: string | null;
  model_used: string;
  latency_ms: number;
  graph_summary: {
    scene_count: number;
    control_count: number;
    edge_count: number;
  };
}

/** Response from /co-architect/commit. */
export interface CoArchitectCommitResponse {
  success: boolean;
  artifact_id: string;
  committed: Array<{
    scenario_id: string;
    title: string;
    step_count: number;
    dropped_steps: number;
  }>;
  dropped: Array<{ title: string; reason: string }>;
}

/** Complete E2E Architect analysis output. */
export interface E2EArchitectOutput {
  variables: E2EVariable[];
  decision_points: DecisionPoint[];
  critical_combinations: E2EScenario[];
  coverage_analysis: E2ECoverageAnalysis;
}

/** Coverage metrics for E2E Architect. */
export interface E2ECoverageAnalysis {
  total_scenarios: number;
  scenarios_before_dedup: number;
  duplicates_removed: number;
  by_category: Record<string, number>;
  by_priority: Record<string, number>;
  /** Visual-strict only: count of scenarios per generation strategy (P2). */
  by_strategy?: Record<string, number>;
  /** Visual-strict only: which strategies were requested in this run. */
  strategies_requested?: string[];
  /** Visual-strict only: strategies that produced zero output (G9 verification). */
  strategies_empty?: string[];
  /** Visual-strict only: per-strategy test counts. */
  strategy_counts?: Record<string, number>;
  /** Visual-strict only: error from the LLM call, if any (G8 honest surfacing). */
  llm_error?: string | null;
  /** Visual-strict only: total grounded steps across all scenarios. */
  visual_total_steps?: number;
  /** P3 Lifecycle: counts of scenarios per lifecycle state. */
  by_state?: Record<string, number>;
  /** P6 Execution Feedback: counts per last-run status (or "never_run"). */
  by_last_run?: Record<string, number>;
  /** P6: how many scenarios are currently flaky in the last-N window. */
  flaky_scenarios?: number;
  /** P6: how many scenarios failed in their most recent run. */
  failing_scenarios?: number;
  workflow_steps_covered: number[];
  workflow_coverage_pct: number;
  variables_tested: number;
  decision_points_found: number;
  pairwise_combinations_generated: number;
  pairwise_coverage_pct: number;
  risk_areas_addressed: string[];
}

/** Visual substrate quality assessment included in E2E Architect response. */
export interface VisualSubstrate {
  quality: 'multimodal' | 'deep' | 'fast' | 'minimal';
  frame_count: number;
  has_ocr: boolean;
  recommendation: string | null;
}

/** Full response from POST /v1/e2e-architect/generate. */
export interface E2EArchitectResponse {
  success: boolean;
  artifact_id: string;
  session_id: string;
  e2e_architect: E2EArchitectOutput;
  provenance: TestStrategyProvenance;
  processing_time_ms: number;
  cached?: boolean;
  cache_hit_ms?: number;
  visual_substrate?: VisualSubstrate;
}

// ═══════════════════════════════════════════════════════════════
//  Visual Evidence Types — Phases 3, 5, 6, 7
// ═══════════════════════════════════════════════════════════════

/** A logical scene (Phase 3): a group of visually similar consecutive frames. */
export interface SceneStateSummary {
  screen_type: 'form' | 'results' | 'confirmation' | 'listing' | 'detail' | 'landing' | 'unknown';
  screen_title: string;
  step_label: string;
  application_label: string;
  domain: string;
  page_key: string;
  state_key: string;
  state_signals: string[];
  state_confidence: number;
  state_quality: 'strong' | 'degraded' | 'weak';
  low_confidence_reasons: string[];
  /** Phase 9: per-frame intra-scene action records produced by the
   *  step extractor before evidence_steps becomes the canonical store.
   *  When evidence_steps is populated for this scene, prefer that over
   *  this field for rendering. */
  frame_actions?: FrameAction[];
  frame_action_count?: number;
  /** Number of times this exact state_key was visited in the artifact
   *  (set by the state_key merge pass when > 1). */
  visit_count?: number;
}

/** Intra-scene per-frame action record produced by the SDK step extractor.
 *  Always emitted into ``scene_state_summary.frame_actions`` whenever the
 *  scene has 2+ frames; replaced by first-class ``evidence_steps`` rows
 *  after the persistence layer is wired up. */
export interface FrameAction {
  from_frame_index: number;
  to_frame_index: number;
  from_timestamp_s: number;
  to_timestamp_s: number;
  action_kind:
    | 'enter_text' | 'select_option' | 'click' | 'click_cta'
    | 'open_overlay' | 'close_overlay' | 'scroll_or_repaint'
    | 'state_change' | 'user_interaction' | 'submit_form'
    | 'navigate' | 'review' | 'scroll';
  delta_text: string;
  target_label: string;
  confidence: number;
  evidence_signals: string[];
}

/** Phase C — first-class user-action row sourced from the evidence_steps
 *  table (Alembic 017).  Populated by the triangulated action classifier
 *  combining OCR, cursor, audio-intent, and LLaVA delta signals. */
export interface EvidenceStep {
  step_id: string;
  scene_id: string;
  /** scene_index injected by the platform API for chronological ordering. */
  scene_index?: number;
  step_index: number;
  action_kind: string;
  target_label: string;
  observed_value: string;
  trigger_control_id: string | null;
  before_frame_id: string | null;
  after_frame_id: string | null;
  start_ms: number;
  end_ms: number;
  confidence: number;
  agreement_score: number;
  evidence_signals: string[];
  cursor_x: number | null;
  cursor_y: number | null;
  audio_intent_text: string;
  audio_intent_ts_ms: number | null;
  metadata_json: Record<string, unknown>;
}

/** Phase A.3 — per-frame cursor reading + click/drag flags. */
/** detection_method values used by the cursor pipeline:
 *    motion              — real, motion-detected cursor (CursorTracker SDK)
 *    template / ml       — alternative real-cursor detectors
 *    url_delta           — synthesized: URL changed between adjacent frames
 *    ocr_new_block       — synthesized: new OCR token cluster appeared
 *    ocr_removed_block   — synthesized: OCR cluster disappeared
 *    ui_element_change   — synthesized: button/link visible-then-gone
 *    control_value       — synthesized: evidence_control has observed_value
 *    frame_action_promotion — synthesized: pipeline's own frame_action
 *                             with action_kind in click/open_overlay/etc.
 *
 *  Synthesized events have cursor_x = cursor_y = 0 and carry rich
 *  metadata under metadata_json describing what triggered the inference.
 */
export type CursorEventDetectionMethod =
  | 'motion' | 'template' | 'ml'
  | 'url_delta' | 'ocr_new_block' | 'ocr_removed_block'
  | 'ui_element_change' | 'control_value' | 'frame_action_promotion'
  | 'form_value_inferred'    // insurance-vocabulary regex (Strategy 6)
  | 'placeholder_transition' // GENERIC placeholder→value first fill (Strategy 7 Case A)
  | 'field_re_edit'          // value changed again after first fill (Strategy 7 Case B)
  | 'labelled_value_change'  // label learned by punctuation, no placeholder (Strategy 7 Case C / Strategy 8)
  | 'field_interaction_unreadable' // captcha / masked / blurred (Strategy 11)
  | 'audio_intent'           // SME narration anchored to a frame (Strategy 9)
  | 'viewport_shift';        // scroll / focus / pane change (Strategy 10)

export interface CursorEvent {
  event_id: string;
  frame_id: string | null;
  frame_index: number;
  timestamp_ms: number;
  cursor_x: number;
  cursor_y: number;
  velocity: number;
  is_click: boolean;
  is_drag: boolean;
  detection_method: CursorEventDetectionMethod;
  confidence: number;
  metadata_json: Record<string, unknown>;
}

/** A logical scene (Phase 3): a group of visually similar consecutive frames. */
export interface VisualScene {
  scene_id: string;
  artifact_id: string;
  session_id: string;
  tenant_id: string;
  representative_frame_id: string | null;
  /** Relative asset path for the representative frame — pass to getFrameImageUrl(). */
  representative_frame_asset_path: string;
  scene_index: number;
  screen_name: string | null;
  ocr_text: string | null;
  detected_url: string | null;
  start_ms: number | null;
  end_ms: number | null;
  app_instance_id: string | null;
  flow_id: string | null;
  completeness_confidence: number;
  /** Phase 8: structured, confidence-aware scene state — prefer over screen_name for rendering. */
  scene_state_summary?: SceneStateSummary | null;
  /** Explicit duration in ms; may differ from end_ms - start_ms for merged scenes. */
  duration_ms?: number | null;
  /** exact | estimated | unknown */
  duration_quality?: 'exact' | 'estimated' | 'unknown';
  /** strong | degraded | weak — top-level rendering gate. */
  scene_quality?: 'strong' | 'degraded' | 'weak';
}

/** A single captured frame (Phase 2): raw visual data before scene grouping. */
export interface VisualFrame {
  frame_id: string;
  frame_index: number;
  timestamp_ms: number;
  frame_asset_path: string;
  extracted_text: string;
  ocr_confidence: number;
  application_type: string;
  llava_description: string;
  scene_id: string | null;
}

/** A single UI element detected on a frame (Phase 1.7 — surfaced from
 *  visual_frames.ui_elements_json so the 3D Journey detail panel can show
 *  real button/text-field/dropdown affordances instead of OCR deltas). */
export interface FrameUiElement {
  element_type: string | null;
  text: string | null;
  confidence: number;
  properties?: {
    label?: string;
    entity_id?: string;
    action_kind?: string;
    inferred_from?: string;
    observed_value?: string;
    persistence_count?: number;
  };
  bbox?: number[];
}

/** Phase 1.7 — extended frame row exposed by /api/v1/artifacts/{id}/frames.
 *  The backend already returns these columns via row_to_dict; this type
 *  matches the actual wire shape so the frontend can render per-frame
 *  evidence (OCR, URL, UI elements) without a separate request. */
export interface FrameRowExtended {
  frame_id: string;
  frame_index: number;
  frame_asset_path: string;
  timestamp_seconds: number;
  is_keyframe: boolean;
  extracted_text?: string;
  url_or_path?: string;
  ui_elements_json?: FrameUiElement[];
  scene_id?: string | null;
  ocr_confidence?: number;
}

/** An app instance (Phase 5): a contiguous range of scenes in the same application. */
export interface AppInstance {
  instance_id: string;
  artifact_id: string;
  tenant_id: string;
  app_type: string | null;
  app_name: string | null;
  detected_url: string | null;
  window_title: string | null;
  /** What triggered this boundary: app_type_change | domain_change | window_title_change | no_boundary */
  segmentation_basis: string;
  segmentation_confidence: number;
  first_scene_index: number;
  last_scene_index: number;
  scene_count: number;
}

/** Pixel-coordinate bounding box.  Canonical form is ``{x1, y1, x2, y2}``
 *  (top-left + bottom-right); legacy rows used ``{x, y, width, height}``.
 *  ``{}`` is a valid empty box meaning "no geometry available".
 */
export interface BoundingBox {
  x1?: number;
  y1?: number;
  x2?: number;
  y2?: number;
  x?: number;
  y?: number;
  width?: number;
  height?: number;
}

/** A UI control extracted from a scene's representative frame (Phase 6). */
export interface EvidenceControl {
  control_id: string;
  scene_id: string;
  artifact_id: string;
  tenant_id: string;
  /** Nullable FK to the specific frame this control was extracted from. */
  frame_id: string | null;
  element_type: string | null;
  label_text: string | null;
  /** Captured text value of the control (e.g. input value, button label). */
  value_text: string | null;
  /** Bounding box in source-frame pixels, as written by the orchestrator's
   *  ControlExtractor.  Shape is ``{x1, y1, x2, y2}`` (the canonical form
   *  from Phase 6+); legacy rows may still carry ``{x, y, width, height}``.
   *  Empty object ``{}`` means no geometry was extracted (typical for
   *  vision-only controls and the mainframe / SAP surfaces). */
  bounding_box: BoundingBox;
  /** ocr | hybrid | vision | unknown */
  selector_source: string;
  playwright_selector: string | null;
  selector_confidence: number;
  /** True only when grounded in OCR evidence. Vision-only controls are never automation_ready. */
  automation_ready: boolean;
  /** Interaction type: click | enter_text | select_option | check | navigate | review | interact */
  action_kind?: string;
  /** Pre-composed human-readable action phrase, e.g. "Fill: Life Insurance = Get a quote" */
  display_label?: string;
  /** Cleaned OCR-evidence value that was entered or selected. */
  observed_value?: string;
}

/** A directed edge in the visual flow graph (Phase 7). */
export interface PrimaryActionSummary {
  action_kind: 'open_page' | 'click_cta' | 'select_option' | 'enter_text' | 'submit_form' | 'navigate' | 'review' | 'unknown';
  action_label: string;
  action_subject: string;
  action_value: string;
  target_control_type: string;
  target_control_label: string;
  trigger_control_id: string | null;
  source_scene_id: string;
  target_scene_id: string;
  resulting_state_key: string;
  evidence_sources: string[];
  action_confidence: number;
  action_quality: 'strong' | 'degraded' | 'weak';
  low_confidence_reasons: string[];
  fallback_used: boolean;
}

/** A directed edge in the visual flow graph (Phase 7). */
export interface VisualFlowEdge {
  edge_id: string;
  artifact_id: string;
  tenant_id: string;
  from_scene_id: string;
  to_scene_id: string;
  /** observed_transition | action_confirmed_transition | app_switch */
  edge_type: string;
  trigger_control_id: string | null;
  action_type: string | null;
  action_value: string | null;
  transition_ms: number | null;
  intra_app: boolean;
  evidence_confidence: number;
  flow_id: string | null;
  /** Phase 8: structured action summary — prefer over action_value for rendering. */
  primary_action_summary?: PrimaryActionSummary | null;
  /** strong | degraded | weak */
  action_quality?: 'strong' | 'degraded' | 'weak';
  /** 0.0–1.0 numeric confidence */
  action_confidence?: number;
}

/** A business-process flow grouping scenes (Phase 5b). */
export interface VisualFlow {
  flow_id: string;
  artifact_id: string;
  session_id: string;
  tenant_id: string;
  flow_index: number;
  flow_label: string;
  domain: string;
  app_type: string;
  first_scene_index: number;
  last_scene_index: number;
  scene_count: number;
  is_noise: boolean;
  confidence: number;
  /** True when user returned to this app after visiting another (interleaved session). */
  is_interleaved?: boolean;
  /** Number of distinct time-contiguous visit segments in this flow. */
  visit_count?: number;
}

/** Unified visual evidence graph (Phase 7) — single API response. */
export interface VisualEvidenceGraph {
  artifact_id: string;
  flows: VisualFlow[];
  app_instances: AppInstance[];
  scenes: VisualScene[];
  /** Controls indexed by scene_id for O(1) lookups. */
  controls_by_scene: Record<string, EvidenceControl[]>;
  edges: VisualFlowEdge[];
  summary: {
    total_flows: number;
    total_scenes: number;
    total_edges: number;
    action_confirmed_edges: number;
    automation_ready_controls: number;
    avg_completeness_confidence: number;
    /** Phase 8: coverage of strong/degraded scene states (0–1). */
    scene_state_coverage?: number;
    /** Phase 8: coverage of strong/degraded action summaries (0–1). */
    action_summary_coverage?: number;
  };
  /** Phase 8: four-level readiness (not_ready | preview_only | ready | strong_ready). */
  visual_e2e_status?: 'not_ready' | 'preview_only' | 'ready' | 'strong_ready';

  // Phase 1.7 storyboard overlays — populated only when the page
  // requests ``?include_storyboard_overlays=true``.  Older endpoint
  // versions or backends without storyboard data return empty
  // objects/arrays, so callers can read them unconditionally.

  /** Dedup'd app groupings (1 entry per real app, not per OCR boundary). */
  app_groupings?: StoryboardApp[];
  /** Scene IDs flagged as noise by the storyboard composer (hide by default). */
  noise_scene_ids?: string[];
  /** scene_id → short LLM caption + narrative + quality + model. */
  scene_captions?: Record<
    string,
    { short: string; narrative: string; quality: string; model: string }
  >;
  /** scene_id → annotated PNG endpoint URL (auth-gated, JWT required). */
  annotated_frame_urls?: Record<string, string>;
  /** scene_id → app_grouping_id (replaces the raw AppInstance lookup). */
  scene_to_app_grouping?: Record<string, string>;
}

/** Gate result for the visual flow approval surface (Phase 8). */
export interface VisualFlowGateResult {
  can_generate: boolean;
  reasons: string[];
  action_confirmed_count: number;
  automation_ready_count: number;
}

/** Request body for POST /v1/heart/generate-tests-from-visual (Phase 9). */
export interface GenerateTestsFromVisualRequest {
  artifact_id: string;
  session_id: string;
  /** visual_strict skips multimodal matching; multimodal uses all signals */
  evidence_mode: 'visual_strict' | 'multimodal';
  approved_edge_ids?: string[];
}

/** A single Heart-generated test case step (Phase 9). */
export interface VisualTestStep {
  action: string;
  target_element: string;
  expected_output: string;
  evidence_scene_id: string;
  evidence_control_id: string;
  evidence_edge_id: string;
  proof_confidence: number;
}

/** A Heart-generated test case (Phase 9). */
export interface VisualTestCase {
  title: string;
  steps: VisualTestStep[];
}

/** An unproven step that couldn't be grounded in visual evidence (Phase 9). */
export interface VisualUnprovenStep {
  description: string;
  reason: string;
  edge_id?: string;
  evidence_confidence?: number;
}

/** Coverage report returned alongside visual test cases (Phase 9). */
export interface VisualCoverageReport {
  total_edges: number;
  proven_steps: number;
  unproven_steps: number;
  automation_ready_pct: number;
}

/** Response from Heart's generate-tests-from-visual endpoint (Phase 9). */
export interface VisualTestGenerationResponse {
  success: boolean;
  artifact_id: string;
  session_id: string;
  evidence_mode: string;
  test_cases: VisualTestCase[];
  unproven_steps: VisualUnprovenStep[];
  coverage_report: VisualCoverageReport;
  processing_time_ms: number;
}

/** Error body returned with HTTP 422 when Playwright export gates fail (Phase 10). */
export interface PlaywrightExportError {
  error: 'gate_failed' | 'no_visual_evidence' | 'no_test_cases';
  message: string;
  unmet_gates: string[];
}

// ─── Storyboard Phase 1 — picture-first visual evidence ────────
// Matches the Pydantic models in
// ``platform/api/app/routers/storyboard.py``.  Don't add fields
// here without also updating the router — the frontend treats
// these as the source of truth.

/** One canonical (dedup'd) application detected in the recording. */
export interface StoryboardApp {
  grouping_id: string;
  display_label: string;
  canonical_name: string;
  canonical_domain: string;
  app_type: string;
  scene_count: number;
  first_scene_index: number;
  last_scene_index: number;
  confidence: number;
  /** How the deduper grouped this app instance. */
  dedup_basis: string;
}

/** One comic-strip panel — the UI's primary rendering unit. */
export interface StoryboardPanel {
  panel_id: string;
  panel_index: number;
  scene_count: number;
  in_scene_action_count: number;
  start_ms: number;
  end_ms: number;
  duration_ms: number;
  /** 5-8 word imperative caption.  May be empty before LLM rewriter runs. */
  caption_short: string;
  /** Longer narrative caption for the drill-down detail view. */
  caption_long: string;
  /** strong | degraded | weak — how confident we are in the caption. */
  caption_quality: string;
  /** strong | degraded | weak — overall panel quality. */
  panel_quality: string;
  completeness_confidence: number;
  is_noise: boolean;
  noise_reason: string;
  representative_frame_id: string | null;
  representative_frame_path: string;
  /** GET this URL to fetch the annotated PNG.  Null when no frame. */
  annotated_frame_url: string | null;
  annotated_frame_available: boolean;
  app: StoryboardApp | null;
}

/** Per-step run flags for observability — exposed to the UI for transparency. */
export interface StoryboardDerivation {
  scene_grouper_version: string;
  app_deduper_version: string;
  caption_rewriter_version: string;
  frame_annotator_version: string;
  ran_scene_grouper: boolean;
  ran_app_deduper: boolean;
  ran_caption_rewriter: boolean;
  ran_frame_annotator: boolean;
  derivation_elapsed_ms: number;
  storyboard_total_ms: number;
}

/** Top-level payload returned by ``GET /api/v1/artifacts/{id}/storyboard``. */
export interface StoryboardPayload {
  artifact_id: string;
  visual_e2e_status: string | null;
  panels: StoryboardPanel[];
  apps: StoryboardApp[];
  summary: {
    panel_count?: number;
    non_noise_panel_count?: number;
    noise_panel_count?: number;
    app_count?: number;
    total_in_scene_actions?: number;
    duration_ms?: number;
    strong_panels?: number;
    degraded_panels?: number;
    weak_panels?: number;
    source_filename?: string;
    brain_quality_score?: number | null;
    [k: string]: unknown;
  };
  derivation: StoryboardDerivation;
}
