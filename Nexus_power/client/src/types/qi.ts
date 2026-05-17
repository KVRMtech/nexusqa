// ═══════════════════════════════════════════════════════════════
//  QI ENGINEER PORTAL — Type System (Phase 7)
//  Persona-driven mission-based quality intelligence
// ═══════════════════════════════════════════════════════════════

// ── Stage Constants ─────────────────────────────────────────

export const STAGE_TYPES = ['capture', 'understand', 'strategize', 'generate', 'validate'] as const;
export type StageType = typeof STAGE_TYPES[number];

export const STAGE_LABELS: Record<number, string> = {
  1: 'Capture',
  2: 'Understand',
  3: 'Strategize',
  4: 'Generate',
  5: 'Validate',
};

export const STAGE_DESCRIPTIONS: Record<number, string> = {
  1: 'Gather requirements, domain knowledge, and source materials',
  2: 'Analyze rules, build knowledge graphs, detect contradictions',
  3: 'Plan test strategy, define coverage priorities',
  4: 'Create test cases, test data, and documentation',
  5: 'Execute tests, verify compliance, measure confidence',
};

// ── Persona ─────────────────────────────────────────────────

export interface Persona {
  persona_id: string;
  tenant_id: string;
  name: string;
  slug: string;
  description: string;
  avatar_icon: string;
  system_prompt: string;
  capabilities: string[];
  stage_config: Record<string, PersonaStageConfig>;
  specialty_domains: string[];
  is_system: boolean;
  is_active: boolean;
  sort_order: number;
  created_at: string;
  updated_at: string;
}

export interface PersonaStageConfig {
  engines: string[];
  auto_advance: boolean;
}

export interface CreatePersonaRequest {
  name: string;
  slug: string;
  description?: string;
  avatar_icon?: string;
  system_prompt?: string;
  capabilities?: string[];
  stage_config?: Record<string, PersonaStageConfig>;
  specialty_domains?: string[];
}

export interface UpdatePersonaRequest {
  name?: string;
  description?: string;
  avatar_icon?: string;
  system_prompt?: string;
  capabilities?: string[];
  stage_config?: Record<string, PersonaStageConfig>;
  specialty_domains?: string[];
  is_active?: boolean;
}

// ── Mission ─────────────────────────────────────────────────

export type MissionStatus = 'draft' | 'active' | 'paused' | 'completed' | 'failed' | 'cancelled';
export type MissionPriority = 'critical' | 'high' | 'medium' | 'low';
export type StageStatus = 'pending' | 'active' | 'completed' | 'skipped' | 'failed';

export interface Mission {
  mission_id: string;
  tenant_id: string;
  user_id?: string;
  persona_id?: string;
  title: string;
  description: string;
  objective: string;
  status: MissionStatus;
  current_stage: number;
  priority: MissionPriority;
  tags: string[];
  context: Record<string, unknown>;
  summary: string;
  progress_pct: number;
  created_at: string;
  updated_at: string;
  started_at?: string;
  completed_at?: string;
  // Populated in detail view
  persona?: Persona;
  stages?: MissionStage[];
  messages?: MissionMessage[];
}

export interface MissionSummary {
  mission_id: string;
  tenant_id: string;
  user_id?: string;
  persona_id?: string;
  title: string;
  description: string;
  status: MissionStatus;
  current_stage: number;
  priority: MissionPriority;
  tags: string[];
  progress_pct: number;
  created_at: string;
  updated_at: string;
  started_at?: string;
  completed_at?: string;
  stages: MissionStageSummary[];
}

export interface MissionStageSummary {
  stage_number: number;
  stage_type: StageType;
  status: StageStatus;
}

export interface CreateMissionRequest {
  title: string;
  description?: string;
  objective?: string;
  persona_id: string;
  priority?: MissionPriority;
  tags?: string[];
  artifact_id?: string;
  session_id?: string;
}

export interface UpdateMissionRequest {
  title?: string;
  description?: string;
  objective?: string;
  priority?: MissionPriority;
  tags?: string[];
  status?: MissionStatus;
}

// ── Mission Stage ───────────────────────────────────────────

export interface MissionStage {
  stage_id: string;
  mission_id: string;
  stage_number: number;
  stage_type: StageType;
  stage_label: string;
  status: StageStatus;
  inputs: Record<string, unknown>;
  outputs: Record<string, unknown>;
  engine_calls: EngineCall[];
  started_at?: string;
  completed_at?: string;
  duration_seconds: number;
  error_message?: string;
  artifacts?: MissionArtifact[];
}

export interface EngineCall {
  engine: string;
  endpoint: string;
  status: 'ok' | 'error' | 'timeout' | 'skipped';
  duration_ms: number;
  error?: string;
}

// ── Mission Artifact ────────────────────────────────────────

export type ArtifactType =
  | 'document'
  | 'transcript'
  | 'rules'
  | 'test_cases'
  | 'test_data'
  | 'report'
  | 'graph_snapshot'
  | 'strategy'
  | 'execution_results';

export interface MissionArtifact {
  artifact_id: string;
  mission_id: string;
  stage_id: string;
  artifact_type: ArtifactType;
  name: string;
  description: string;
  content_json: Record<string, unknown>;
  content_text: string;
  file_path: string;
  file_size_bytes: number;
  item_count: number;
  created_at: string;
}

export interface AddArtifactRequest {
  artifact_type: ArtifactType;
  name: string;
  description?: string;
  content_json?: Record<string, unknown>;
  content_text?: string;
  item_count?: number;
}

// ── Mission Message ─────────────────────────────────────────

export type MessageRole = 'user' | 'assistant' | 'system';

export interface MissionMessage {
  message_id: string;
  mission_id: string;
  role: MessageRole;
  content: string;
  stage_number: number;
  content_type: 'text' | 'markdown' | 'json' | 'action';
  action_data?: Record<string, unknown>;
  token_count: number;
  created_at: string;
}

export interface SendMessageRequest {
  content: string;
  content_type?: 'text' | 'markdown' | 'json' | 'action';
  action_data?: Record<string, unknown>;
}

export interface SendMessageResponse {
  user_message: MissionMessage;
  assistant_message: MissionMessage;
}

// ── Dashboard ───────────────────────────────────────────────

export interface MissionDashboard {
  total_missions: number;
  status_counts: Record<MissionStatus, number>;
  stage_distribution: Record<string, number>;
  total_artifacts: number;
  recent_missions: MissionSummary[];
}

// ── Paginated Response ──────────────────────────────────────

export interface MissionListResponse {
  missions: MissionSummary[];
  total: number;
  limit: number;
  offset: number;
}

// ── Stage Advance ───────────────────────────────────────────

export interface AdvanceStageRequest {
  skip_current?: boolean;
  stage_inputs?: Record<string, unknown>;
}

export interface AdvanceStageResponse {
  advanced: boolean;
  previous_stage?: number;
  current_stage?: number;
  stage_type?: StageType;
  stage_label?: string;
  mission_status?: MissionStatus;
  message?: string;
}
