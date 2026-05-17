// ═══════════════════════════════════════════════════════════════
//  Canonical Asset Adapter — single normalization layer that
//  merges raw API responses into one CanonicalAssetViewModel.
//
//  Every page that consumes canonical results calls this adapter
//  instead of doing ad-hoc merging inline.
// ═══════════════════════════════════════════════════════════════

import api from '../services/api';
import type {
  CanonicalArtifact,
  ArtifactCompletionStatus,
  WorkflowTimeline,
  CanonicalAssetViewModel,
  CanonicalLaunchAction,
  ArtifactProvenance,
  QualityOutcome,
  QualityScoreBreakdown,
  StallEvent,
  VisualGraph,
  WorkflowSyncState,
  WorkflowTimelineEntry,
} from '../types/canonical';

// ── Internal aliases for the full_artifact_json sub-objects ──

interface FullArtifactJsonBlob {
  media_probe?: Record<string, unknown>;
  transcript?: { segments?: Array<{ text?: string; speaker?: string }>; transcript_text?: string; model_version?: string };
  safe_transcript?: Record<string, unknown>;
  visual_analysis?: { frames?: Array<Record<string, unknown>>; scenes?: Array<Record<string, unknown>>; model_version?: string; application_types_seen?: string[] };
  visual_graph?: { nodes?: unknown[]; edges?: unknown[] };
  model_provenance?: Record<string, string>;
  review_reasons?: string[];
  score_breakdown?: { transcript?: number | null; visual?: number | null; pii?: number | null; completeness?: number | null };
}

interface RawWorkflowListItem {
  workflow_id: string;
  chain_name?: string;
  session_id?: string;
  status?: string;
  started_at?: string;
  completed_at?: string;
}

interface RawSession {
  session_id: string;
  tenant_id: string;
  title?: string;
  status?: string;
  created_at?: string;
  completed_at?: string;
}

// ── Helpers ────────────────────────────────────────────────

const TRANSCRIPT_PREVIEW_LIMIT = 500;

/** Safely cast the opaque full_artifact_json blob to the known sub-shape. */
function asBlob(artifact: CanonicalArtifact): FullArtifactJsonBlob {
  return (artifact.full_artifact_json ?? {}) as FullArtifactJsonBlob;
}

function deriveProvenance(
  artifact: CanonicalArtifact,
  session: RawSession | null,
): { provenance: ArtifactProvenance; original_session_id: string | null; cache_hit: boolean } {
  // If the artifact's session_id doesn't match the session we asked for,
  // it was served via cache dedup / alias resolution.
  if (session && artifact.session_id !== session.session_id) {
    return {
      provenance: 'alias',
      original_session_id: artifact.session_id,
      cache_hit: true,
    };
  }

  // If there is a media_fingerprint and the artifact has no workflow_id,
  // it was a cache reuse from a previous upload with the same fingerprint.
  if (artifact.media_fingerprint && !artifact.workflow_id) {
    return {
      provenance: 'cache_reuse',
      original_session_id: null,
      cache_hit: true,
    };
  }

  return { provenance: 'fresh', original_session_id: null, cache_hit: false };
}

function deriveQualityOutcome(artifact: CanonicalArtifact, status: ArtifactCompletionStatus | null): QualityOutcome {
  const outcome = status?.quality_gate_outcome ?? artifact.quality_gate_outcome;
  if (outcome === 'pass') return 'pass';
  if (outcome === 'fail') return 'fail';
  if (outcome === 'needs_review') return 'needs_review';
  // Infer from boolean flag
  const passed = status?.quality_gate_passed ?? artifact.quality_gate_passed ?? false;
  return passed ? 'pass' : 'fail';
}

function extractScoreBreakdown(artifact: CanonicalArtifact, status: ArtifactCompletionStatus | null): QualityScoreBreakdown {
  const blob = asBlob(artifact).score_breakdown;
  return {
    transcript: blob?.transcript ?? null,
    visual: blob?.visual ?? null,
    pii: blob?.pii ?? null,
    completeness: blob?.completeness
      ?? status?.semantic_completeness_score
      ?? artifact.semantic_completeness_score
      ?? null,
  };
}

function extractTranscriptStats(artifact: CanonicalArtifact): {
  preview: string;
  segment_count: number;
  word_count: number;
  speaker_count: number;
} {
  const safeText = artifact.safe_transcript_text || '';
  const preview = safeText.slice(0, TRANSCRIPT_PREVIEW_LIMIT);

  const segments = asBlob(artifact).transcript?.segments ?? [];
  const segment_count = segments.length;

  // Word count from safe text (authoritative, PII-free)
  const word_count = safeText ? safeText.split(/\s+/).filter(Boolean).length : 0;

  // Speaker count from unique speakers in segments
  const speakers = new Set<string>();
  for (const seg of segments) {
    if (seg.speaker) speakers.add(seg.speaker);
  }

  return { preview, segment_count, word_count, speaker_count: speakers.size };
}

function extractVisualGraph(artifact: CanonicalArtifact): VisualGraph | null {
  const raw = asBlob(artifact).visual_graph;
  if (!raw || (!raw.nodes?.length && !raw.edges?.length)) return null;
  return {
    nodes: (raw.nodes ?? []) as Array<Record<string, unknown>>,
    edges: (raw.edges ?? []) as Array<Record<string, unknown>>,
  };
}

function deriveOperatorTruth(
  timeline: WorkflowTimelineEntry[],
  workflowStatus: string | null,
  workflowFromDb: boolean,
): {
  degraded_stages: string[];
  skipped_stages: string[];
  retry_count: number;
  stall_events: StallEvent[];
  workflow_sync_state: WorkflowSyncState;
} {
  const degraded_stages: string[] = [];
  const skipped_stages: string[] = [];
  const stall_events: StallEvent[] = [];
  let retry_count = 0;

  // Scan timeline events for operator-relevant signals
  for (const entry of timeline) {
    const ev = entry.event?.toLowerCase() ?? '';
    const det = entry.detail?.toLowerCase() ?? '';

    if (ev.includes('degraded') || det.includes('degraded')) {
      // Extract stage name from detail if possible
      const stageMatch = entry.detail.match(/stage[:\s]+(\w+)/i);
      const stageName = stageMatch?.[1] ?? entry.event;
      if (!degraded_stages.includes(stageName)) degraded_stages.push(stageName);
    }

    if (ev.includes('skip') || det.includes('skipped')) {
      const stageMatch = entry.detail.match(/stage[:\s]+(\w+)/i);
      const stageName = stageMatch?.[1] ?? entry.event;
      if (!skipped_stages.includes(stageName)) skipped_stages.push(stageName);
    }

    if (ev.includes('retry') || det.includes('retry') || det.includes('retrying')) {
      retry_count++;
    }

    if (ev.includes('stall') || det.includes('stall')) {
      const durMatch = entry.detail.match(/(\d+(?:\.\d+)?)\s*s/i);
      stall_events.push({
        stage: entry.event,
        duration_seconds: durMatch ? parseFloat(durMatch[1]) : 0,
      });
    }
  }

  // Workflow sync state: did we get timeline from DB or fallback?
  let workflow_sync_state: WorkflowSyncState = 'synced';
  if (!workflowFromDb) {
    workflow_sync_state = workflowStatus === 'completed' ? 'stale' : 'fallback';
  }

  return { degraded_stages, skipped_stages, retry_count, stall_events, workflow_sync_state };
}

// ── Launch Action Computation ──────────────────────────────

function computeLaunchActions(vm: {
  artifact_id: string;
  session_id: string;
  has_real_transcript: boolean;
  has_visual_semantics: boolean;
  quality_gate_passed: boolean;
  has_persona_draft_with_workflows: boolean;
}): CanonicalLaunchAction[] {
  return [
    {
      id: 'explore-knowledge-graph',
      label: 'Explore Knowledge Graph',
      description: 'Navigate extracted entities and relationships in the knowledge graph',
      icon: 'share-2',
      route: `/knowledge-graph?session_id=${vm.session_id}&artifact_id=${vm.artifact_id}`,
      state: 'preview_only',
      reason: 'Page wired to load canonical visual graph and merge with backbone data',
      requires: ['has_real_transcript'],
    },
    {
      id: 'launch-qi-mission',
      label: 'Launch QI Mission',
      description: 'Start an intelligent investigation mission seeded by this artifact',
      icon: 'target',
      route: `/qi/missions?artifact_id=${vm.artifact_id}&session_id=${vm.session_id}`,
      state: 'preview_only',
      reason: 'createMission() accepts artifact_id/session_id, modal auto-opens with artifact context',
      requires: ['has_real_transcript', 'quality_gate_passed'],
    },
    {
      id: 'build-traceability-matrix',
      label: 'Build Traceability Matrix',
      description: 'Map extracted rules to test cases and requirements',
      icon: 'git-branch',
      route: `/traceability?session_id=${vm.session_id}`,
      state: 'preview_only',
      reason: 'Page exists, no artifact-to-traceability wiring',
      requires: ['has_real_transcript'],
    },
    {
      id: 'generate-regression-suite',
      label: 'Generate Regression Suite',
      description: 'Auto-generate regression test cases from extracted business rules',
      icon: 'check-square',
      route: `/test-center?artifact_id=${vm.artifact_id}`,
      state: 'coming_next',
      reason: 'TestExecutionCenter exists but no canonical-to-test generation contract',
      requires: ['has_real_transcript', 'quality_gate_passed'],
    },
    {
      id: 'generate-e2e-test-cases',
      label: 'Generate E2E Test Cases',
      description: 'Multimodal E2E Test Architect — pairwise data combinations with evidence-backed critical scenarios',
      icon: 'play-circle',
      route: `/sessions/${vm.session_id}/e2e-architect?artifact_id=${vm.artifact_id}&ready=${vm.has_persona_draft_with_workflows ? '1' : '0'}`,
      state: vm.has_persona_draft_with_workflows ? 'ready' : 'preview_only',
      reason: vm.has_persona_draft_with_workflows
        ? 'E2E Architect powered by two-pass Brain LLM with pairwise analysis'
        : 'Requires persona draft with workflow steps for E2E scenario generation',
      requires: ['has_real_transcript', 'quality_gate_passed'],
    },
    {
      id: 'visual-flow-e2e',
      label: 'Visual Flow → E2E Tests',
      description: 'Inspect scene-by-scene visual evidence, approve observed transitions, and generate Playwright tests grounded in screenshot proof',
      icon: 'monitor',
      route: `/sessions/${vm.session_id}/visual-flow?artifact_id=${vm.artifact_id}`,
      state: vm.has_visual_semantics ? 'ready' : 'preview_only',
      reason: vm.has_visual_semantics
        ? 'Visual evidence pipeline active — scenes, controls, and flow graph available'
        : 'Requires visual analysis (Eyes engine) to have processed the recording',
      requires: ['has_visual_semantics'],
    },
    {
      id: 'generate-sme-persona',
      label: 'Generate SME Persona',
      description: 'Create an AI persona from the knowledge captured in this session',
      icon: 'user-plus',
      route: `/sessions/${vm.session_id}/persona-workspace?artifact_id=${vm.artifact_id}`,
      state: vm.has_real_transcript ? 'ready' : 'preview_only',
      reason: vm.has_real_transcript
        ? 'Process Oracle generation powered by Brain LLM'
        : 'Requires real transcript for grounded persona generation',
      requires: ['has_real_transcript'],
    },
    {
      id: 'generate-test-strategy',
      label: 'Generate Test Strategy',
      description: 'Create a complete test plan with traceability from persona knowledge',
      icon: 'flask-conical',
      route: `/sessions/${vm.session_id}/test-strategy?artifact_id=${vm.artifact_id}`,
      state: vm.has_persona_draft_with_workflows ? 'ready' : 'preview_only',
      reason: vm.has_persona_draft_with_workflows
        ? 'Test Architect — generates test cases grounded in KT evidence'
        : 'Requires SME persona with workflow steps',
      requires: ['has_real_transcript'],
    },
    {
      id: 'create-review-queue',
      label: 'Create Review Queue',
      description: 'Queue low-confidence extractions for human review',
      icon: 'clipboard-check',
      route: `/compliance?artifact_id=${vm.artifact_id}`,
      state: 'coming_next',
      reason: 'Compliance page exists but no review queue API',
      requires: ['quality_gate_passed'],
    },
    {
      id: 'build-knowledge-pack',
      label: 'Build Knowledge Pack',
      description: 'Package session knowledge into a portable, shareable bundle',
      icon: 'package',
      route: `/knowledge-pack?artifact_id=${vm.artifact_id}`,
      state: 'coming_next',
      reason: 'No knowledge pack concept in current backend',
      requires: ['has_real_transcript', 'quality_gate_passed'],
    },
  ];
}

// ═══════════════════════════════════════════════════════════════
//  Public API
// ═══════════════════════════════════════════════════════════════

/**
 * Fetch all raw data sources for a canonical artifact and merge them
 * into a single `CanonicalAssetViewModel`.
 *
 * Callers provide the artifact_id and session_id; the adapter calls
 * the 5 API endpoints in parallel and normalizes the result.
 */
export async function buildCanonicalAssetViewModel(
  artifactId: string,
  sessionId: string,
  tenantId: string,
): Promise<CanonicalAssetViewModel> {
  // ── Fetch all data sources in parallel ───────────────────
  const [artifact, status, sessionWorkflows, session] = await Promise.all([
    api.getArtifact(artifactId),
    api.getArtifactStatus(artifactId),
    api.listSessionWorkflows(sessionId, tenantId).catch((): RawWorkflowListItem[] => []),
    api.getSession(sessionId).catch((): RawSession | null => null) as Promise<RawSession | null>,
  ]);

  // ── Resolve workflow ────────────────────────────────────
  const workflowId = artifact.workflow_id ?? status?.workflow_id ?? null;
  let timeline: WorkflowTimelineEntry[] = [];
  let workflowFromDb = false;
  let workflowStatus: string | null = null;

  if (workflowId) {
    try {
      const timelineResp = await api.getWorkflowTimeline(workflowId);
      timeline = timelineResp.timeline ?? [];
      workflowStatus = timelineResp.status ?? null;
      // If we got a non-empty timeline, the data came from the persisted read model
      workflowFromDb = timeline.length > 0;
    } catch {
      // Timeline not available — fallback state
      workflowFromDb = false;
    }
  } else {
    // No workflow associated — try to find one from session workflows
    const wfList = sessionWorkflows as RawWorkflowListItem[];
    if (wfList.length > 0) {
      // Use the most recent workflow for this session
      const wf = wfList[0];
      try {
        const timelineResp = await api.getWorkflowTimeline(wf.workflow_id);
        timeline = timelineResp.timeline ?? [];
        workflowStatus = timelineResp.status ?? null;
        workflowFromDb = timeline.length > 0;
      } catch {
        workflowFromDb = false;
      }
    }
  }

  const resolvedWorkflowId = workflowId
    ?? ((sessionWorkflows as RawWorkflowListItem[])[0]?.workflow_id ?? null);

  // ── Provenance ─────────────────────────────────────────
  const { provenance, original_session_id: rawOriginalSessionId, cache_hit } = deriveProvenance(artifact, session);
  // For alias/reuse, original_session_id is the artifact's producing session.
  // If the artifact's session_id differs from the requested sessionId, surface that.
  const original_session_id = (artifact.session_id !== sessionId)
    ? artifact.session_id
    : rawOriginalSessionId;

  // ── Quality ────────────────────────────────────────────
  const quality_outcome = deriveQualityOutcome(artifact, status);
  const score_breakdown = extractScoreBreakdown(artifact, status);
  const review_reasons: string[] = [
    ...(status?.review_reasons ?? []),
    ...(asBlob(artifact).review_reasons ?? []),
  ];
  // Deduplicate
  const uniqueReviewReasons = [...new Set(review_reasons)];

  // ── Transcript stats ───────────────────────────────────
  const transcriptStats = extractTranscriptStats(artifact);

  // ── Visual ─────────────────────────────────────────────
  const blob = asBlob(artifact);
  const visualGraph = extractVisualGraph(artifact);
  const visualFrames = blob.visual_analysis?.frames ?? [];
  const visualScenes = blob.visual_analysis?.scenes ?? [];
  const applicationTypes = artifact.application_types_seen
    ?? blob.visual_analysis?.application_types_seen
    ?? [];

  // ── Model provenance ───────────────────────────────────
  const model_provenance: Record<string, string> = {
    ...(blob.model_provenance ?? {}),
    ...(status?.model_provenance ?? {}),
  };

  // ── Operator truth ─────────────────────────────────────
  const operatorTruth = deriveOperatorTruth(timeline, workflowStatus, workflowFromDb);

  // ── Semantic flags ─────────────────────────────────────
  const has_real_transcript = status?.has_real_transcript ?? artifact.has_real_transcript ?? false;
  const has_visual_semantics = status?.has_visual_semantics ?? artifact.has_visual_semantics ?? false;
  const quality_gate_passed = status?.quality_gate_passed ?? artifact.quality_gate_passed ?? false;

  // ── Session identity ───────────────────────────────────
  // CRITICAL: Use the *requested* sessionId, not artifact.session_id.
  // For cache-reuse or alias cases, artifact.session_id is the original
  // producing session, not the current session the operator is viewing.
  // artifact.session_id is preserved in original_session_id for provenance.
  const viewingSessionId = sessionId;

  // ── Persona draft readiness (for Test Architect gating) ──
  const fullJson = artifact.full_artifact_json as Record<string, unknown> | null;
  const personaDraftCache = fullJson?.persona_draft_cache as Record<string, unknown> | undefined;
  const personaDomainMap = personaDraftCache?.domain_map as Record<string, unknown> | undefined;
  const has_persona_draft_with_workflows = !!(
    personaDraftCache &&
    personaDomainMap &&
    Array.isArray(personaDomainMap.workflows) &&
    personaDomainMap.workflows.length > 0
  );

  // ── Launch actions ─────────────────────────────────────
  const launch_actions = computeLaunchActions({
    artifact_id: artifact.artifact_id,
    session_id: viewingSessionId,
    has_real_transcript,
    has_visual_semantics,
    quality_gate_passed,
    has_persona_draft_with_workflows,
  });

  // ── Assemble ───────────────────────────────────────────
  const vm: CanonicalAssetViewModel = {
    // Identity
    artifact_id: artifact.artifact_id,
    session_id: viewingSessionId,
    workflow_id: resolvedWorkflowId,
    tenant_id: artifact.tenant_id,

    // Source
    source_filename: artifact.source_filename ?? status?.source_filename ?? null,
    source_type: artifact.source_type ?? status?.source_type ?? null,
    duration_seconds: artifact.duration_seconds ?? 0,
    created_by: artifact.created_by ?? null,
    created_at: artifact.created_at ?? status?.created_at ?? new Date().toISOString(),
    completed_at: artifact.completed_at ?? status?.completed_at ?? null,

    // Provenance
    provenance,
    original_session_id,
    produced_at: artifact.created_at ?? new Date().toISOString(),
    cache_hit,

    // Quality
    quality_score: status?.brain_quality_score ?? artifact.brain_quality_score ?? null,
    quality_outcome,
    quality_gate_passed,
    score_breakdown,
    review_reasons: uniqueReviewReasons,

    // Semantic flags
    has_real_transcript,
    has_visual_semantics,
    semantic_completeness_score:
      status?.semantic_completeness_score ?? artifact.semantic_completeness_score ?? null,

    // Content
    safe_transcript_preview: transcriptStats.preview,
    transcript_segment_count: transcriptStats.segment_count,
    transcript_word_count: transcriptStats.word_count,
    speaker_count: transcriptStats.speaker_count,
    visual_summary: artifact.visual_summary ?? '',
    frame_count: artifact.frame_count ?? visualFrames.length,
    scene_count: artifact.scene_count ?? visualScenes.length,
    application_types_seen: applicationTypes,
    visual_graph: visualGraph,

    // Model provenance
    model_provenance,

    // Processing
    processing_time_seconds:
      status?.processing_time_seconds ?? artifact.processing_time_seconds ?? 0,
    timeline,

    // Operator truth
    ...operatorTruth,

    // Downstream readiness
    launch_actions,
  };

  return vm;
}
