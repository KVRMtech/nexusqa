// ═══════════════════════════════════════════════════════════════
//  NEXUS AI ENGINE FACTORY — Complete Type System (12 Modules)
// ═══════════════════════════════════════════════════════════════

// ── User & Auth ─────────────────────────────────────────────

export interface User {
  user_id: string;
  tenant_id: string;
  email: string;
  name: string;
  role: string;
  permissions: string[];
}

export interface Tenant {
  tenant_id: string;
  name: string;
  domain: string;
  plan: string;
  status: string;
}

// ── Engine Health ───────────────────────────────────────────

export interface EngineHealth {
  engine: string;
  status: 'healthy' | 'degraded' | 'unhealthy' | 'unreachable';
  version?: string;
  uptime_seconds?: number;
  modes?: Record<string, string>;
  dependencies?: DependencyStatus[];
  error?: string;
}

export interface DependencyStatus {
  name: string;
  status: string;
  latency_ms?: number;
  message?: string;
}

// ── Knowledge Graph ─────────────────────────────────────────

export interface GraphNode {
  node_id: string;
  node_type: string;
  properties: Record<string, any>;
  tenant_id: string;
  source?: Record<string, any>;
  tags?: string[];
  created_at?: string;
}

export interface GraphRelation {
  relation_id: string;
  source_id: string;
  target_id: string;
  relation_type: string;
  properties?: Record<string, any>;
}

export interface GraphStats {
  total_nodes: number;
  total_relations: number;
  node_types: Record<string, number>;
}

// ══════════════════════════════════════════════════════════════
// ZONE 1 — KNOWLEDGE CAPTURE CENTER
// ══════════════════════════════════════════════════════════════

// ── Sessions (Module 1 & 2) ────────────────────────────────

export type SessionStatus = 'live' | 'processing' | 'completed' | 'failed' | 'needs_review';

export type UploadJobType = 'audio' | 'video';

export interface TrackedJob {
  job_id: string;
  type: UploadJobType;
  filename: string;
  status: string;
  progress_percent: number;
  current_stage: string;
  created_at: string;
  error?: string | null;
  artifact_id?: string | null;
  quality_gate_passed?: boolean | null;
  quality_gate_outcome?: string | null;
  degraded_stages?: string[] | null;
}

// ── Workflow Progress (SSE streaming from orchestrator) ─────

export type WorkflowProgressStatus =
  | 'created'
  | 'running'
  | 'completed'
  | 'degraded'
  | 'needs_review'
  | 'failed'
  | 'cancelled';

export interface WorkflowStageProgress {
  stage_id: string;
  status: string;
  duration_ms: number;
}

export interface WorkflowProgressEvent {
  workflow_id: string;
  status: WorkflowProgressStatus;
  current_stage: string | null;
  progress_percent: number;
  stages: WorkflowStageProgress[];
  error?: string | null;
}

export interface CanonicalProcessingResult {
  workflow_id: string;
  chain_id: string;
  chain_name: string;
  status: WorkflowProgressStatus;
  session_id: string;
}

export interface KTSession {
  session_id: string;
  title: string;
  description?: string;
  status: SessionStatus;
  participants: SessionParticipant[];
  recording_url?: string;
  screen_recording_url?: string;
  created_at: string;
  completed_at?: string;
  duration_seconds?: number;
  rules_extracted: number;
  tests_generated: number;
  contradictions_found: number;
  confidence_score: number;
  tenant_id: string;
}

export interface SessionParticipant {
  speaker_id: string;
  name: string;
  role?: string;
  speaking_time_seconds?: number;
  rules_contributed?: number;
}

export interface IntelligenceEvent {
  timestamp_seconds: number;
  event_type: 'rule_extracted' | 'contradiction' | 'system_link' | 'test_generated' | 'key_decision' | 'action_item';
  title: string;
  description: string;
  speaker?: string;
  confidence?: number;
  linked_nodes?: string[];
  severity?: 'critical' | 'high' | 'medium' | 'low';
  screenshot_url?: string;
}

export interface TranscriptSegment {
  start: number;
  end: number;
  speaker: string;
  text: string;
  confidence?: number;
}

// ── SME Profiles (Module 3) ────────────────────────────────

export interface SMEProfile {
  speaker_id: string;
  name: string;
  role?: string;
  department?: string;
  sessions_count: number;
  total_rules_contributed: number;
  expertise_areas: ExpertiseArea[];
  last_active: string;
  knowledge_timeline: KnowledgeTimelineEntry[];
  bus_factor_risks: BusFactorRisk[];
  voice_enrolled: boolean;
}

export interface ExpertiseArea {
  domain: string;
  rules_count: number;
  confidence_avg: number;
}

export interface KnowledgeTimelineEntry {
  date: string;
  session_title: string;
  rules_extracted: number;
}

export interface BusFactorRisk {
  domain: string;
  rules_count: number;
  sole_source: boolean;
  nearest_backup?: string;
}

// ══════════════════════════════════════════════════════════════
// ZONE 2 — INTELLIGENCE HUB
// ══════════════════════════════════════════════════════════════

// ── Business Rules ──────────────────────────────────────────

export interface BusinessRule {
  rule_id: string;
  description: string;
  condition: string;
  expected_result: string;
  domain?: string;
  priority?: 'critical' | 'high' | 'medium' | 'low';
  confidence?: number;
  source_session_id?: string;
  source_speaker?: string;
  source_timestamp?: number;
  test_coverage: number;
  tests_linked: number;
  status?: 'validated' | 'pending' | 'contradicted' | 'rejected';
  tags?: string[];
  created_at?: string;
}

// ── Contradictions (Module 5) ──────────────────────────────

export type ContradictionSeverity = 'critical' | 'high' | 'medium' | 'low';
export type ContradictionStatus = 'open' | 'resolved' | 'escalated' | 'dismissed';

export interface Contradiction {
  contradiction_id: string;
  severity: ContradictionSeverity;
  status: ContradictionStatus;
  title: string;
  claim_a: ContradictionClaim;
  claim_b: ContradictionClaim;
  ai_analysis: string;
  ai_recommendation?: string;
  impact: ContradictionImpact;
  created_at: string;
  resolved_at?: string;
  resolved_by?: string;
  resolution_note?: string;
}

export interface ContradictionClaim {
  speaker: string;
  session_id: string;
  session_title: string;
  timestamp_seconds: number;
  statement: string;
  date: string;
  confidence: number;
}

export interface ContradictionImpact {
  affected_tests: number;
  affected_rules: number;
  affected_tickets: number;
  domains: string[];
}

// ── AI Confidence (Module 6) ───────────────────────────────

export interface GuardrailPipelineStats {
  stage_name: string;
  passed: number;
  total: number;
  blocked_reasons?: string[];
}

export interface HumanReviewItem {
  item_id: string;
  item_type: 'rule' | 'test' | 'classification';
  content: string;
  confidence: number;
  reason: string;
  source_session?: string;
  source_audio_url?: string;
  created_at: string;
}

export interface TrustScoreTrend {
  month: string;
  score: number;
  note?: string;
}

// ══════════════════════════════════════════════════════════════
// ZONE 3 — TESTING POWERHOUSE
// ══════════════════════════════════════════════════════════════

// ── Test Cases & Execution (Modules 7, 8) ──────────────────

export interface TestCase {
  test_id: string;
  title: string;
  description: string;
  steps: TestStep[];
  priority?: 'critical' | 'high' | 'medium' | 'low';
  status?: 'passed' | 'failed' | 'pending' | 'running' | 'not_run';
  test_type?: 'web' | 'api' | 'mainframe' | 'data_driven';
  linked_rules?: string[];
  linked_jira?: string;
  last_execution?: string;
  pass_rate?: number;
}

export interface TestStep {
  step_number: number;
  action: string;
  expected_result: string;
  test_data?: Record<string, any>;
}

export interface ExecutionRun {
  run_id: string;
  title: string;
  status: 'running' | 'completed' | 'failed' | 'paused' | 'queued';
  total_tests: number;
  passed: number;
  failed: number;
  skipped: number;
  progress_pct: number;
  started_at: string;
  eta_seconds?: number;
  elapsed_seconds?: number;
  live_failures: ExecutionFailure[];
}

export interface ExecutionFailure {
  test_id: string;
  test_title: string;
  error_message: string;
  screenshot_url?: string;
  video_url?: string;
  log_url?: string;
  auto_jira_ticket?: string;
  timestamp: string;
}

export interface ExecutionResult {
  job_id: string;
  test_id: string;
  status: 'passed' | 'failed' | 'error' | 'running' | 'pending';
  duration_ms?: number;
  steps_passed?: number;
  steps_total?: number;
  error?: string;
  evidence?: string[];
}

// ── Traceability (Module 7) ────────────────────────────────

export interface TraceabilityRow {
  source_speaker: string;
  source_date: string;
  source_session: string;
  rule_id: string;
  rule_description: string;
  tests_total: number;
  tests_passed: number;
  jira_status: 'linked' | 'new' | 'none' | 'failed';
  jira_tickets: string[];
  has_source: boolean;
}

export interface TraceabilityGap {
  gap_type: 'no_tests' | 'partial_tests' | 'no_source';
  count: number;
  rules: string[];
}

// ── Test Data (Module 9) ───────────────────────────────────

export interface DataGenerationConfig {
  product_type: string;
  state?: string;
  count: number;
  strategy: 'boundary' | 'random' | 'pairwise' | 'combinatorial';
  include_edge_cases: boolean;
  include_negative: boolean;
  include_regulatory: boolean;
}

export interface DataGenerationResult {
  job_id: string;
  records_generated: number;
  unique_combinations: number;
  pairwise_coverage: number;
  full_coverage: number;
  preview: Record<string, any>[];
  download_url?: string;
}

// ══════════════════════════════════════════════════════════════
// ZONE 4 — OPERATIONS & GOVERNANCE
// ══════════════════════════════════════════════════════════════

// ── Compliance (Module 10) ──────────────────────────────────

export type ComplianceStatus = 'compliant' | 'at_risk' | 'non_compliant' | 'not_tested';

export interface JurisdictionCompliance {
  jurisdiction: string;
  jurisdiction_code: string;
  status: ComplianceStatus;
  issues_count: number;
  issues: ComplianceIssue[];
  last_checked: string;
}

export interface ComplianceIssue {
  issue_id: string;
  regulation_code: string;
  regulation_title: string;
  description: string;
  expected_value: string;
  actual_value: string;
  test_id?: string;
  test_status?: string;
  impact: string;
  severity: 'critical' | 'high' | 'medium' | 'low';
}

export interface ComplianceScoreTrend {
  month: string;
  score: number;
  note?: string;
}

// ── Executive Metrics (Module 11) ──────────────────────────

export interface ExecutiveMetrics {
  knowledge_captured: number;
  test_coverage_pct: number;
  defects_found: number;
  risk_score: string;
  risk_grade: string;
  roi: ROIMetrics;
  top_risks: RiskItem[];
}

export interface ROIMetrics {
  manual_hours_eliminated: number;
  cost_savings_monthly: number;
  defect_catch_rate: number;
  prev_defect_catch_rate: number;
  knowledge_retention_pct: number;
  onboarding_days: number;
  prev_onboarding_days: number;
}

export interface RiskItem {
  rank: number;
  title: string;
  description: string;
  severity: 'critical' | 'high' | 'medium' | 'low';
  category: 'bus_factor' | 'compliance' | 'contradiction' | 'coverage';
}

// ── Reports ─────────────────────────────────────────────────

export interface Report {
  report_id: string;
  report_type: string;
  title: string;
  format: string;
  created_at: string;
  created_by?: string;
}

// ── Connectors ──────────────────────────────────────────────

export interface Connector {
  connector: string;
  status: 'connected' | 'disconnected' | 'error' | 'not_configured' | 'rate_limited';
  configured: boolean;
  synced_items?: number;
  last_sync?: string;
  error_message?: string;
}

export interface ConnectorResult {
  connector: string;
  action: string;
  success: boolean;
  data: Record<string, any>;
  error?: string;
  execution_time_ms: number;
}

// ── Workflows ───────────────────────────────────────────────

export interface Workflow {
  workflow_id: string;
  chain_name: string;
  tenant_id: string;
  status: 'pending' | 'running' | 'completed' | 'failed';
  stages: WorkflowStage[];
  created_at: string;
  completed_at?: string;
  error?: string;
}

export interface WorkflowStage {
  name: string;
  engine: string;
  status: 'pending' | 'running' | 'completed' | 'failed' | 'skipped';
  started_at?: string;
  completed_at?: string;
  duration_ms?: number;
}

// ── System Admin (Module 12) ───────────────────────────────

export interface SystemResources {
  gpu_model?: string;
  gpu_utilization_pct: number;
  ram_used_gb: number;
  ram_total_gb: number;
  cpu_pct: number;
  storage_used_gb: number;
  storage_total_gb: number;
}

// ── Canonical Asset (Phase 3A) ────────────────────────────

export type { CanonicalArtifact, ArtifactCompletionStatus, ArtifactTranscript, WorkflowTimeline, CanonicalAssetViewModel, CanonicalLaunchAction, WorkflowTimelineEntry, QualityScoreBreakdown, StallEvent, VisualGraph, ArtifactProvenance, QualityOutcome, WorkflowSyncState, LaunchActionState } from './canonical';

export interface AuditLogEntry {
  event_id: string;
  action: string;
  user_email: string;
  timestamp: string;
  details?: string;
  ip_address?: string;
}

// ── Legacy Transcription (backwards compat) ─────────────────

export interface TranscriptionJob {
  job_id: string;
  status: 'queued' | 'processing' | 'completed' | 'error';
  progress?: number;
  result?: {
    segments: TranscriptSegment[];
    speakers: string[];
    duration_seconds: number;
  };
}

// ══════════════════════════════════════════════════════════════
// VISUALIZATION TYPES (Graph Canvas)
// ══════════════════════════════════════════════════════════════

export interface GraphVisNode {
  id: string;
  label: string;
  type: string;
  color: string;
  size: number;
  x: number;
  y: number;
}

export interface GraphVisEdge {
  source: string;
  target: string;
  label: string;
  type: string;
}

// ══════════════════════════════════════════════════════════════
// TEST SUITE AGGREGATE
// ══════════════════════════════════════════════════════════════

export interface TestSuite {
  id: string;
  name: string;
  type: string;
  total_tests: number;
  passed: number;
  failed: number;
  running: number;
  not_run: number;
  last_run?: string;
}

// ══════════════════════════════════════════════════════════════
// DATA FORGE — FIELD SCHEMA
// ══════════════════════════════════════════════════════════════

export interface ForgeField {
  name: string;
  type: string;
  constraints?: Record<string, unknown>;
}

// ══════════════════════════════════════════════════════════════
// ADMIN — ENRICHED ENGINE & INTEGRATION INFO
// ══════════════════════════════════════════════════════════════

export interface EngineInfo {
  name: string;
  code_name: string;
  status: 'healthy' | 'degraded' | 'unhealthy' | 'unreachable';
  mode: string;
  uptime: string;
  version: string;
  cpu_pct: number;
  memory_mb: number;
  requests_24h: number;
  errors_24h: number;
}

export interface IntegrationInfo {
  name: string;
  type: string;
  status: 'connected' | 'disconnected' | 'error' | 'not_configured';
  last_sync?: string;
  synced_items?: number;
}

// ══════════════════════════════════════════════════════════════
// TRACEABILITY — TEST REF
// ══════════════════════════════════════════════════════════════

export interface TraceTest {
  test_id: string;
  title: string;
  type: string;
  last_result: 'passed' | 'failed' | 'not_run';
  pass_rate: number;
}

// ── QI Engineer Portal ──────────────────────────────────────

export type {
  Persona,
  PersonaStageConfig,
  CreatePersonaRequest,
  UpdatePersonaRequest,
  Mission,
  MissionSummary,
  MissionStatus,
  MissionPriority,
  MissionStage,
  MissionStageSummary,
  StageType,
  StageStatus,
  MissionArtifact,
  ArtifactType,
  AddArtifactRequest,
  MissionMessage,
  MessageRole,
  SendMessageRequest,
  SendMessageResponse,
  MissionDashboard,
  MissionListResponse,
  CreateMissionRequest,
  UpdateMissionRequest,
  AdvanceStageRequest,
  AdvanceStageResponse,
  EngineCall,
} from './qi';

