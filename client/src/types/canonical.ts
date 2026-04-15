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
