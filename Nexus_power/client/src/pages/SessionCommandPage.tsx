// ═══════════════════════════════════════════════════════════════
//  MODULE 1 — CANONICAL PROCESSING
//  "Where organizational knowledge enters the system"
// ═══════════════════════════════════════════════════════════════

import { useState, useEffect, useCallback, useRef } from 'react';
import { useNavigate } from 'react-router-dom';
import { useAuth } from '../contexts/AuthContext';
import api from '../services/api';
import { useApiData } from '../hooks/useApiData';
import { useSSE } from '../hooks/useSSE';
import { PageHeader } from '../components/PageHeader';
import { StatCard } from '../components/StatCard';
import { StatusBadge } from '../components/StatusBadge';
import { EmptyState } from '../components/EmptyState';
import type {
  KTSession,
  SessionStatus,
  SessionParticipant,
  TrackedJob,
  UploadJobType,
  WorkflowProgressEvent,
} from '../types';
import {
  Radio,
  Upload,
  Mic,
  Video,
  Users,
  Brain,
  AlertTriangle,
  ChevronRight,
  Clock,
  CheckCircle2,
  XCircle,
  Loader2,
  Sparkles,
  FileAudio,
  BarChart3,
  Copy,
  Search,
  Trash2,
  Ban,
  Zap,
  Timer,
  Activity,
  ArrowRight,
  Award,
  Eye,
  ShieldCheck,
} from 'lucide-react';
import clsx from 'clsx';

// ── Empty fallback (production) ─────────────────────────────
const EMPTY_SESSIONS: KTSession[] = [];

function formatDuration(seconds: number): string {
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  if (h > 0) return `${h}h ${m}m`;
  return `${m}m`;
}

function formatDate(dateStr: string): string {
  return new Date(dateStr).toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' });
}

function StatusIcon({ status }: { status: SessionStatus }) {
  switch (status) {
    case 'live':
      return (
        <span className="relative flex h-3 w-3">
          <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-red-400 opacity-75" />
          <span className="relative inline-flex h-3 w-3 rounded-full bg-red-500" />
        </span>
      );
    case 'processing':
      return <Loader2 className="h-4 w-4 text-yellow-400 animate-spin" />;
    case 'completed':
      return <CheckCircle2 className="h-4 w-4 text-green-400" />;
    case 'failed':
      return <XCircle className="h-4 w-4 text-red-400" />;
    case 'needs_review':
      return <AlertTriangle className="h-4 w-4 text-orange-400" />;
  }
}

function StatusLabel({ status }: { status: SessionStatus }) {
  const variant = {
    live: 'red' as const,
    processing: 'yellow' as const,
    completed: 'green' as const,
    failed: 'red' as const,
    needs_review: 'orange' as const,
  }[status];
  const label = status === 'live' ? 'LIVE NOW' : status === 'needs_review' ? 'Needs Review' : status.charAt(0).toUpperCase() + status.slice(1);
  return <StatusBadge label={label} variant={variant} pulse={status === 'live'} />;
}

function isAudioFile(file: File): boolean {
  return file.type.startsWith('audio/') || /\.(wav|mp3|ogg|flac|m4a|wma|aac|webm)$/i.test(file.name);
}

function isVideoFile(file: File): boolean {
  return file.type.startsWith('video/') || /\.(mp4|webm|mkv|avi|mov)$/i.test(file.name);
}

// ── Canonical Stage Metadata ──────────────────────────────────
// Phase A — stage IDs match the Phase 12 canonical_pipeline_plan.
// The SSE stream emits `stage_id` as the bare step name (engine
// prefix stripped, e.g. "probe_media" not "spine.probe_media").
// Order here drives the progress rail rendering; META names are
// the user-facing labels.
//
// Phase 3 — persist_minimal_artifact replaces the legacy
// persist_artifact, and update_artifact_enriched is a new step
// that fires after the eyes branch finishes. Keep persist_artifact
// in the list so legacy workflows still render correctly.
const CANONICAL_STAGE_ORDER = [
  'probe_media',
  'redact_audio', 'redact_video',
  'preprocess', 'diarize', 'transcribe_segments', 'align',
  'redact_text',
  'extract_frames', 'detect_scenes',
  'persist_minimal_artifact',           // Phase 3 — artifact appears here
  'ocr_frames',
  'analyze_scenes', 'analyze_transitions', 'build_evidence',
  'build_visual_graph',
  'update_artifact_enriched',           // Phase 3 — enrichment fold-in
  'persist_artifact',                   // legacy (single-stage workflows)
  'persist_visual_evidence', 'persist_action_evidence',
  'canonicalize_multimodal', 'canonicalize',
] as const;

/**
 * Clamp a progress percent value into a CSS-safe [min, 100] range.
 * Defends against:
 *   - NaN / undefined (SSE drop, race during init) → falls back to `min`
 *   - Values > 100 from server bugs (rounding) that would otherwise
 *     overflow the parent container and visually break the bar
 *   - Negative values
 */
function clampPct(value: unknown, min: number = 0): number {
  const n = Number(value);
  if (!Number.isFinite(n)) return min;
  return Math.min(100, Math.max(min, n));
}

const CANONICAL_STAGE_META: Record<string, string> = {
  probe_media: 'Media Probe',
  redact_audio: 'PII (Audio)',
  redact_video: 'PII (Video)',
  preprocess: 'Audio Preprocess',
  diarize: 'Speaker Diarize',
  transcribe_segments: 'Transcribe',
  align: 'Align',
  redact_text: 'PII (Text)',
  extract_frames: 'Extract Frames',
  detect_scenes: 'Scene Detect',
  // Phase 3: artifact is visible in the UI after this step.
  persist_minimal_artifact: 'Save Partial',
  ocr_frames: 'OCR',
  analyze_scenes: 'Scene Analysis',
  analyze_transitions: 'Transitions',
  build_evidence: 'Build Evidence',
  build_visual_graph: 'Visual Graph',
  // Phase 3: enrichment fold-in promotes minimal → full artifact.
  update_artifact_enriched: 'Save Enriched',
  persist_artifact: 'Persist Artifact',
  persist_visual_evidence: 'Visual Evidence',
  persist_action_evidence: 'Action Evidence',
  canonicalize_multimodal: 'Canonicalize',
  canonicalize: 'Canonicalize',
  // Pseudo-stages emitted by the UI lifecycle, not the workflow.
  enriching: 'Enriching',
  finalizing: 'Finalizing',
  starting: 'Starting',
  cancelled: 'Cancelled',
  failed: 'Failed',
  completed: 'Completed',
  needs_review: 'Needs Review',
};

function computeConfidence(stages: { stage_id: string; status: string }[]): {
  transcript: number; visual: number; safety: number; overall: number;
  partial: boolean;
} {
  const m = new Map(stages.map(s => [s.stage_id, s.status]));
  const done = (id: string) => m.get(id) === 'completed';
  // Transcript confidence — grows as each audio stage completes so
  // the user sees progress during a long Whisper run, not a 0% jump
  // at the very end.
  let transcript = 0;
  if (done('preprocess')) transcript += 10;
  if (done('diarize')) transcript += 25;
  if (done('transcribe_segments')) transcript += 35;
  if (done('align')) transcript += 30;
  // Visual confidence — same incremental approach. The eyes branch is
  // long; users were seeing 0% for tens of minutes despite real work
  // happening. Each step now contributes its share.
  let visual = 0;
  if (done('extract_frames')) visual += 5;
  if (done('detect_scenes')) visual += 5;
  if (done('ocr_frames')) visual += 20;
  if (done('analyze_scenes')) visual += 25;
  if (done('analyze_transitions')) visual += 15;
  if (done('build_evidence')) visual += 10;
  if (done('build_visual_graph')) visual += 10;
  if (done('persist_visual_evidence')) visual += 10;
  // Safety = both shield audio + text redaction done.
  const safety = (done('redact_audio') || done('redact_video')) && done('redact_text') ? 100 : 0;
  // Phase 3: artifact is "partial" once persist_minimal_artifact runs
  // but enrichment hasn't completed. The UI uses this to show a
  // "Partial · enriching" badge instead of "Complete".
  const partial = done('persist_minimal_artifact') && !done('update_artifact_enriched') && !done('persist_artifact');
  const total = CANONICAL_STAGE_ORDER.length;
  const completed = CANONICAL_STAGE_ORDER.filter(id => done(id) || m.get(id) === 'skipped').length;
  const overall = Math.round((completed / total) * 100);
  return { transcript, visual, safety, overall, partial };
}

function qualityBadgeProps(
  score: number | null,
  outcome: string | null,
): { label: string; variant: 'green' | 'blue' | 'orange' | 'red' | 'gray' } {
  if (outcome === 'fail') return { label: 'FAILED', variant: 'red' };
  if (outcome === 'needs_review') return { label: 'NEEDS REVIEW', variant: 'orange' };
  if (score == null) return { label: 'PENDING', variant: 'gray' };
  if (score >= 0.9) return { label: 'EXCELLENT', variant: 'green' };
  if (score >= 0.7) return { label: 'GOOD', variant: 'blue' };
  return { label: 'NEEDS REVIEW', variant: 'orange' };
}

function provenanceBadgeProps(
  artifact: { media_fingerprint?: string | null; workflow_id?: string | null },
): { label: string; variant: 'nexus' | 'purple' } {
  if (artifact.media_fingerprint && !artifact.workflow_id) return { label: 'Cached', variant: 'purple' };
  return { label: 'Fresh', variant: 'nexus' };
}

function formatElapsed(ms: number | null | undefined): string {
  if (ms == null) return '—';
  // Sub-100ms (shield gates, scene_detect, etc.) — show as "<0.1s" instead
  // of "0.0s" so the user can tell the step actually ran (vs. didn't).
  if (ms > 0 && ms < 100) return '<0.1s';
  const s = ms / 1000;
  if (s < 60) return `${s.toFixed(1)}s`;
  return `${Math.floor(s / 60)}m ${Math.round(s % 60)}s`;
}

function formatProcessingTime(seconds: number): string {
  if (seconds < 60) return `${seconds.toFixed(1)}s`;
  const m = Math.floor(seconds / 60);
  const s = Math.round(seconds % 60);
  return `${m}m ${s}s`;
}

export default function SessionCommandPage() {
  const { user } = useAuth();
  const navigate = useNavigate();
  const tenantId = user?.tenant_id || '';

  const isTerminalJobStatus = useCallback((status: string) => {
    return status === 'completed' || status === 'failed' || status === 'needs_review' || status === 'degraded';
  }, []);

  const resolveWorkflowDisplayState = useCallback(async (
    workflowId: string,
    fallback: Pick<TrackedJob, 'status' | 'progress_percent' | 'current_stage' | 'created_at' | 'error' | 'artifact_id'>,
    workflowData?: any,
  ) => {
    const data = workflowData ?? await api.getWorkflowStatus(workflowId);
    const stagesCompleted = Object.values(data.stages || {}).filter(
      (stage: any) => stage.status === 'completed' || stage.status === 'skipped',
    ).length;
    const stagesTotal = Object.keys(data.stages || {}).length;
    const pct = stagesTotal > 0
      ? Math.round((stagesCompleted / stagesTotal) * 100)
      : (data.progress_percent ?? fallback.progress_percent);
    const workflowStatus = data.status || fallback.status;
    // Phase 12 DAG exposes artifact_id at the top level. Phase 3 also
    // writes it under persist_minimal_artifact / update_artifact_enriched
    // step outputs. The legacy chain engine wrote it under
    // artifact_persistence. Read all three so Track-Job-By-ID renders
    // a completed workflow as "completed" instead of falling through
    // to the "running / finalizing" branch.
    const artifactId =
      fallback.artifact_id
      || data.artifact_id
      || data.stages?.update_artifact_enriched?.output?.artifact_id
      || data.stages?.persist_minimal_artifact?.output?.artifact_id
      || data.stages?.artifact_persistence?.output?.artifact_id
      || null;
    const baseState = {
      created_at: data.created_at || fallback.created_at,
      error: data.error || fallback.error || null,
      artifact_id: artifactId,
      degraded_stages: data.input_data?._degraded_stages || null,
      _stagesFromPoll: data.stages || null,
    };

    if (workflowStatus === 'failed' || workflowStatus === 'cancelled') {
      return {
        ...baseState,
        status: 'failed',
        progress_percent: pct,
        current_stage: workflowStatus === 'cancelled' ? 'cancelled' : (data.current_stage || 'failed'),
        quality_gate_passed: null,
        quality_gate_outcome: null,
      };
    }

    if (workflowStatus === 'completed' || workflowStatus === 'needs_review' || workflowStatus === 'degraded') {
      if (artifactId) {
        try {
          const artStatus = await api.getArtifactStatus(artifactId);
          let finalStatus = artStatus.status;
          if (finalStatus === 'completed' && artStatus.quality_gate_passed === false) {
            finalStatus = 'needs_review';
          }
          if (finalStatus === 'degraded') {
            finalStatus = 'needs_review';
          }
          // Phase 3: artifact persisted at status="minimal" (basic
          // data only, enrichment still running) is intentionally
          // shown as "running" — the user can browse the partial
          // artifact, but the cinema/widget keeps polling until
          // enrichment flips status to "persisted".
          if (finalStatus === 'minimal') {
            return {
              ...baseState,
              status: 'running',
              progress_percent: Math.max(pct, 60),
              current_stage: 'enriching',
              quality_gate_passed: null,
              quality_gate_outcome: null,
            };
          }
          const TERMINAL_ART = new Set([
            'completed', 'completed_degraded', 'needs_review',
            'failed', 'persisted',
          ]);
          if (TERMINAL_ART.has(finalStatus)) {
            // Phase 3 / quality-gate terminal mapping:
            //   persisted          → completed (enrichment done, gate clean)
            //   completed_degraded → completed (enrichment done, gate flagged
            //                         pass_with_warnings; UI surfaces the
            //                         warning via quality_gate_outcome).
            //   completed          → completed (legacy)
            //   needs_review       → needs_review (gate refused, red banner)
            //   failed             → failed
            const isFailLike = finalStatus === 'failed';
            const isReviewLike = finalStatus === 'needs_review';
            const displayStatus = (
              finalStatus === 'persisted' || finalStatus === 'completed_degraded'
            ) ? 'completed' : finalStatus;
            return {
              ...baseState,
              status: displayStatus,
              progress_percent: isFailLike ? pct : 100,
              current_stage: isReviewLike
                ? 'needs_review'
                : isFailLike
                  ? 'failed'
                  : 'completed',
              quality_gate_passed: artStatus.quality_gate_passed,
              quality_gate_outcome: artStatus.quality_gate_outcome,
            };
          }
        } catch {
          // Keep the UI in a non-terminal finalizing state until artifact readiness is confirmed.
        }
      }

      return {
        ...baseState,
        status: 'running',
        progress_percent: Math.max(pct, 95),
        current_stage: 'finalizing',
        quality_gate_passed: null,
        quality_gate_outcome: null,
      };
    }

    return {
      ...baseState,
      status: workflowStatus,
      progress_percent: pct,
      current_stage: data.current_stage || fallback.current_stage,
      quality_gate_passed: null,
      quality_gate_outcome: null,
    };
  }, []);

  // ── Read sessions from the Platform API (backed by PostgreSQL) ──
  const { data: rawOrchSessions, isLive, refetch: refetchSessions } = useApiData(
    () => api.listSessions(tenantId),
    EMPTY_SESSIONS,
    !!tenantId,
  );

  // Auto-refresh sessions every 10 seconds
  useEffect(() => {
    const interval = setInterval(refetchSessions, 30_000);
    return () => clearInterval(interval);
  }, [refetchSessions]);

  // Map Platform API SessionRow → UI KTSession shape
  const sessions: KTSession[] = (rawOrchSessions || []).map((s: any) => {
    // Platform API returns `status` directly (scheduled | processing | completed | failed)
    const rawStatus: string = s.status || s.pipeline_stage || '';
    let status: SessionStatus;
    if (rawStatus === 'completed') status = 'completed';
    else if (rawStatus === 'failed') status = 'failed';
    else if (rawStatus === 'needs_review' || rawStatus === 'degraded') status = 'needs_review';
    else if (rawStatus === 'processing' || rawStatus === 'running' || rawStatus === 'scheduled' || rawStatus === 'created') status = 'processing';
    else status = 'processing';
    return {
      session_id: s.session_id,
      title: s.title || s.name || 'Untitled',
      description: s.description || '',
      status,
      participants: [],
      created_at: s.created_at || '',
      duration_seconds: 0,
      rules_extracted: s.rules_extracted || 0,
      tests_generated: s.tests_generated || 0,
      contradictions_found: 0,
      confidence_score: 0,
      tenant_id: s.tenant_id || tenantId,
    } satisfies KTSession;
  });

  const [uploading, setUploading] = useState(false);
  const [dragOver, setDragOver] = useState(false);
  const [uploadError, setUploadError] = useState<string | null>(null);
  const [processingProfile, setProcessingProfile] = useState<string>('multimodal');

  // ── Job Tracking ──────────────────────────────────────────
  const [trackedJobs, setTrackedJobs] = useState<TrackedJob[]>([]);
  const [copiedJobId, setCopiedJobId] = useState<string | null>(null);
  const [lookupJobId, setLookupJobId] = useState('');
  const [lookupResult, setLookupResult] = useState<TrackedJob | null>(null);
  const [lookupError, setLookupError] = useState<string | null>(null);
  const [lookupLoading, setLookupLoading] = useState(false);
  const pollingRef = useRef<ReturnType<typeof setInterval> | null>(null);

  // ── Workflow SSE Progress (Phase 3) ───────────────────────
  const [activeWorkflowId, setActiveWorkflowId] = useState<string | null>(null);
  const [workflowProgress, setWorkflowProgress] = useState<WorkflowProgressEvent | null>(null);

  const { status: sseStatus } = useSSE({
    path: activeWorkflowId ? api.getWorkflowStreamPath(activeWorkflowId) : '',
    enabled: !!activeWorkflowId,
    autoReconnect: true,
    maxReconnectAttempts: 5,
    eventHandlers: {
      progress: (data: unknown) => {
        const event = data as WorkflowProgressEvent;
        setWorkflowProgress(event);
        // Track stage changes for stall detection
        if (event.current_stage !== lastStageId.current) {
          lastStageId.current = event.current_stage;
          stageEnteredAt.current = Date.now();
          setStallWarning(null);
        }
        // Mirror into trackedJobs for the existing UI
        if (activeWorkflowId) {
          setTrackedJobs((prev) => {
            const idx = prev.findIndex((j) => j.job_id === activeWorkflowId);
            if (idx === -1) return prev;
            const updated = [...prev];
            updated[idx] = {
              ...updated[idx],
              status: event.status,
              progress_percent: event.progress_percent,
              current_stage: event.current_stage || updated[idx].current_stage,
            };
            return updated;
          });
        }
      },
      complete: (data: unknown) => {
        const event = data as WorkflowProgressEvent;
        setWorkflowProgress(event);
        const workflowId = activeWorkflowId;
        if (workflowId) {
          const fallbackJob = trackedJobs.find((job) => job.job_id === workflowId);

          setTrackedJobs((prev) =>
            prev.map((job) =>
              job.job_id === workflowId
                ? {
                    ...job,
                    status: event.status === 'failed' || event.status === 'cancelled' ? 'failed' : 'running',
                    progress_percent: event.status === 'failed' || event.status === 'cancelled'
                      ? job.progress_percent
                      : Math.max(event.progress_percent, 95),
                    current_stage: event.status === 'failed' || event.status === 'cancelled' ? 'failed' : 'finalizing',
                    error: event.error || null,
                  }
                : job,
            ),
          );

          void (async () => {
            try {
              const resolvedState = await resolveWorkflowDisplayState(workflowId, {
                status: fallbackJob?.status || event.status,
                progress_percent: fallbackJob?.progress_percent ?? event.progress_percent,
                current_stage: fallbackJob?.current_stage || event.current_stage || 'finalizing',
                created_at: fallbackJob?.created_at || new Date().toISOString(),
                error: fallbackJob?.error || event.error || null,
                artifact_id: fallbackJob?.artifact_id || null,
              });
              setTrackedJobs((prev) => prev.map((job) => (
                job.job_id === workflowId ? { ...job, ...resolvedState } : job
              )));
            } finally {
              refetchSessions();
            }
          })();
        }

        setActiveWorkflowId(null);
      },
      error: () => {
        setActiveWorkflowId(null);
      },
    },
  });

  const allSessions = sessions;
  const liveSessions = allSessions.filter((s) => s.status === 'live');
  const processingSessions = allSessions.filter((s) => s.status === 'processing');
  const completedSessions = allSessions.filter((s) => s.status === 'completed');

  const handleCancelSession = useCallback(async (e: React.MouseEvent, sessionId: string) => {
    e.stopPropagation();
    if (!confirm('Cancel this processing session?')) return;
    try {
      // Find the workflow for this session and cancel it too
      const job = trackedJobs.find((j) => (j as any).session_id === sessionId);
      if (job) {
        try { await api.cancelWorkflow(job.job_id); } catch { /* best-effort */ }
        setTrackedJobs((prev) => prev.map((j) =>
          j.job_id === job.job_id ? { ...j, status: 'failed', error: 'Cancelled by user', current_stage: 'cancelled' } : j
        ));
        setActiveWorkflowId(null);
      }
      await api.cancelSession(sessionId);
      refetchSessions();
    } catch (err: any) {
      console.error('Cancel failed:', err?.message || err);
    }
  }, [refetchSessions, trackedJobs]);

  const handleDeleteSession = useCallback(async (e: React.MouseEvent, sessionId: string) => {
    e.stopPropagation();
    if (!confirm('Permanently delete this session? This cannot be undone.')) return;
    try {
      await api.deleteSession(sessionId);
      refetchSessions();
    } catch (err: any) {
      console.error('Delete failed:', err?.message || err);
    }
  }, [refetchSessions]);

  const totalRules = allSessions.reduce((sum, s) => sum + s.rules_extracted, 0);
  const totalTests = allSessions.reduce((sum, s) => sum + s.tests_generated, 0);

  // ── Session artifacts (Mode 3) ────────────────────────────
  const [sessionArtifacts, setSessionArtifacts] = useState<Record<string, any>>({});

  useEffect(() => {
    const done = sessions.filter(s => s.status === 'completed' || s.status === 'needs_review');
    const toFetch = done.filter(s => !(s.session_id in sessionArtifacts));
    if (toFetch.length === 0) return;
    let cancelled = false;
    (async () => {
      const batch: Record<string, any> = {};
      for (const s of toFetch.slice(0, 20)) {
        if (cancelled) break;
        try {
          const arts = await api.listSessionArtifacts(s.session_id, s.tenant_id);
          if (arts.length > 0) batch[s.session_id] = arts[0];
        } catch { /* skip */ }
      }
      if (!cancelled) setSessionArtifacts(prev => ({ ...prev, ...batch }));
    })();
    return () => { cancelled = true; };
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessions.length]);

  // ── Stall detection ─────────────────────────────────────
  const stageEnteredAt = useRef(Date.now());
  const lastStageId = useRef<string | null>(null);
  const [stallWarning, setStallWarning] = useState<string | null>(null);

  useEffect(() => {
    if (!activeWorkflowId) { setStallWarning(null); return; }
    const timer = setInterval(() => {
      if (Date.now() - stageEnteredAt.current > 60_000 && lastStageId.current) {
        setStallWarning(lastStageId.current);
      }
    }, 5_000);
    return () => clearInterval(timer);
  }, [activeWorkflowId]);

  // ── Poll active jobs ──────────────────────────────────────
  // Jobs tracked here hold workflow IDs (from startCanonicalProcessing).
  // Poll orchestrator workflow status for progress, then verify artifact status
  // as the official completion signal (Phase 1.4).
  const pollJobs = useCallback(async () => {
    setTrackedJobs((prev) => {
      const activeJobs = prev.filter(
        (j) => j.status !== 'completed' && j.status !== 'failed' && j.status !== 'needs_review' && j.status !== 'degraded',
      );
      if (activeJobs.length === 0) return prev;

      // Fire poll requests (side effect outside setState return)
      Promise.all(
        activeJobs.map(async (job) => {
          try {
            const resolvedState = await resolveWorkflowDisplayState(job.job_id, job);
            return {
              ...job,
              ...resolvedState,
            } as TrackedJob;
          } catch {
            return job;
          }
        }),
      ).then((updated) => {
        setTrackedJobs((current) => {
          const map = new Map(current.map((j) => [j.job_id, j]));
          for (const u of updated) map.set(u.job_id, u);
          return Array.from(map.values());
        });
      });

      return prev;
    });
  }, []);

  useEffect(() => {
    const hasActive = trackedJobs.some(
      (j) => !isTerminalJobStatus(j.status),
    );
    if (hasActive && !pollingRef.current) {
        pollJobs();
      pollingRef.current = setInterval(pollJobs, 5000);
    } else if (!hasActive && pollingRef.current) {
      clearInterval(pollingRef.current);
      pollingRef.current = null;
    }
  }, [trackedJobs, pollJobs, isTerminalJobStatus]);

    useEffect(() => {
      return () => {
        if (pollingRef.current) {
          clearInterval(pollingRef.current);
          pollingRef.current = null;
        }
      };
    }, []);

  // ── Copy job ID helper ────────────────────────────────────
  const copyJobId = useCallback((jobId: string) => {
    navigator.clipboard.writeText(jobId).then(() => {
      setCopiedJobId(jobId);
      setTimeout(() => setCopiedJobId(null), 2000);
    });
  }, []);

  // ── Job Lookup ────────────────────────────────────────────
  const handleJobLookup = useCallback(async () => {
    const id = lookupJobId.trim();
    if (!id) return;
    setLookupLoading(true);
    setLookupError(null);
    setLookupResult(null);

    // All ingestion goes through canonical processing — look up via orchestrator workflow status,
    // then verify artifact status as the authoritative completion signal (Phase 1.4).
    try {
      const data = await api.getWorkflowStatus(id);
      const resolvedState = await resolveWorkflowDisplayState(id, {
        status: 'running',
        progress_percent: 0,
        current_stage: 'starting',
        created_at: '',
        error: null,
        artifact_id: null,
      }, data);

      setLookupResult({
        job_id: id,
        type: data.chain_name?.includes('video') ? 'video' : 'audio',
        filename: data.chain_name || 'Workflow',
        status: resolvedState.status,
        progress_percent: resolvedState.progress_percent,
        current_stage: resolvedState.current_stage || 'unknown',
        created_at: resolvedState.created_at || '',
        error: resolvedState.error || null,
      });
    } catch (err: any) {
      // Distinguish access-denied (workflow exists but belongs to a
      // different tenant) from genuine-not-found. The previous catch
      // collapsed both into "Workflow not found", so users hitting a
      // permission boundary thought their ID was wrong.
      const httpStatus = err?.response?.status;
      if (httpStatus === 403) {
        setLookupError(
          'Access denied. This workflow belongs to another tenant — '
          + 'switch tenants or ask a platform-admin for visibility.',
        );
      } else if (httpStatus === 404) {
        setLookupError('Workflow not found. Verify the ID — note this field accepts workflow_id, not artifact_id or session_id.');
      } else {
        setLookupError('Lookup failed. Verify the ID and ensure services are online.');
      }
    }
    setLookupLoading(false);
  }, [lookupJobId, resolveWorkflowDisplayState]);

  // ── Bulk upload helpers ────────────────────────────────────
  // Group a multi-file selection into upload "units". A unit is either
  // a single audio file, a single video file, OR a paired
  // (audio + video) when the two files share a basename. This means
  // dragging 20 zoom recordings (each a single .mp4) yields 20
  // independent workflows; dragging 5 pairs of (.mp4, .m4a) with
  // matching basenames yields 5 multimodal workflows.
  const groupFilesIntoUnits = useCallback((files: File[]): Array<{
    audio?: File;
    video?: File;
    label: string;
  }> => {
    const stem = (f: File) => f.name.replace(/\.[^.]+$/, '');
    const audios = files.filter(isAudioFile);
    const videos = files.filter(isVideoFile);
    const audioByStem = new Map<string, File>();
    audios.forEach((a) => audioByStem.set(stem(a), a));
    const usedAudio = new Set<string>();
    const units: Array<{ audio?: File; video?: File; label: string }> = [];
    for (const v of videos) {
      const matched = audioByStem.get(stem(v));
      if (matched) {
        usedAudio.add(stem(v));
        units.push({
          audio: matched, video: v,
          label: `${v.name} + ${matched.name}`,
        });
      } else {
        units.push({ video: v, label: v.name });
      }
    }
    for (const a of audios) {
      if (!usedAudio.has(stem(a))) {
        units.push({ audio: a, label: a.name });
      }
    }
    return units;
  }, []);

  // Submit a single unit. Extracted from the legacy handler so the
  // bulk dispatcher can call it in parallel with bounded concurrency.
  const submitOneUnit = useCallback(
    async (unit: { audio?: File; video?: File; label: string }) => {
      if (!user) return;
      const audioFile = unit.audio;
      const videoFile = unit.video;
      const jobType: UploadJobType = videoFile ? 'video' : 'audio';
      const primaryFile = videoFile || audioFile!;
      const sessionName = primaryFile.name.replace(/\.[^.]+$/, '');

      // Pre-create a per-unit Idempotency-Key so a transient network
      // retry inside the api client does not create two workflows.
      const idempotencyKey =
        (typeof crypto !== 'undefined' && (crypto as any).randomUUID)
          ? (crypto as any).randomUUID()
          : `idem-${Date.now()}-${Math.random().toString(36).slice(2)}`;

      try {
        const session = await api.createSession(user.tenant_id, sessionName);
        const sessionId: string = session.session_id;

        const result = await api.startCanonicalProcessing({
          sessionId,
          audioFile: audioFile || undefined,
          videoFile: videoFile || undefined,
          processingProfile,
          idempotencyKey,
        });

        const workflowId = result.workflow_id;
        const isCached = result.status === 'completed';
        const canonicalId = result.artifact_id || null;

        const provisionalJob = {
          job_id: workflowId,
          type: jobType,
          filename: unit.label,
          status: 'running',
          progress_percent: isCached ? 95 : 0,
          current_stage: isCached ? 'finalizing' : 'starting',
          created_at: new Date().toISOString(),
          error: null,
          artifact_id: canonicalId,
          session_id: sessionId,
          idempotency_key: idempotencyKey,
        } as TrackedJob;
        const initialJob = isCached
          ? {
              ...provisionalJob,
              ...(await resolveWorkflowDisplayState(workflowId, provisionalJob)),
            }
          : provisionalJob;

        setTrackedJobs((prev) => [initialJob, ...prev]);

        if (!isCached) {
          setActiveWorkflowId(workflowId);
        } else if (isTerminalJobStatus(initialJob.status)) {
          refetchSessions();
        }
      } catch (err: any) {
        const status = err?.response?.status;
        const detail = err?.response?.data?.detail;

        // 429 — rate limited. Surface the Retry-After value so the
        // user sees how long to wait, not a generic "upload failed".
        if (status === 429) {
          const retryAfter = err?.response?.headers?.['retry-after'] || '60';
          const tenantClass = err?.response?.headers?.['x-tenant-class'] || 'unknown';
          setUploadError(
            `Rate limit reached (tier: ${tenantClass}). Try again in ${retryAfter}s. ` +
            `Failed file: ${unit.label}`,
          );
          throw err;
        }

        // 503 — queue saturation guard. This is transient backpressure,
        // NOT a failure of the workflow. Previous behaviour rendered
        // the upload as a permanent "Failed" row with the long
        // saturation message; the user thought their workflow crashed
        // when in fact the system was protecting itself. Surface as a
        // retry banner instead of polluting the tracked-jobs list.
        if (status === 503) {
          const retryAfter = err?.response?.headers?.['retry-after']
            || detail?.retry_after_seconds || '30';
          setUploadError(
            `System is busy — pipeline lanes saturated. Try again in ${retryAfter}s. ` +
            `(No data was lost; this is automatic backpressure protecting in-flight work.)`,
          );
          throw err;
        }

        // 409 — server already has a workflow for this idempotency
        // key. Attach to it so the UI doesn't lose the running job.
        if (status === 409 && detail?.existing_workflow_id) {
          const existingWfId = detail.existing_workflow_id;
          setTrackedJobs((prev) => {
            if (prev.some((j) => j.job_id === existingWfId)) return prev;
            return [
              {
                job_id: existingWfId,
                type: jobType,
                filename: `${unit.label} (resuming)`,
                status: 'running',
                progress_percent: 0,
                current_stage: 'reconnecting',
                created_at: new Date().toISOString(),
                error: null,
              } as TrackedJob,
              ...prev,
            ];
          });
          setActiveWorkflowId(existingWfId);
          return;
        }

        const msg = typeof detail === 'string' ? detail
          : detail?.message || err?.message || 'Upload failed';
        // Add a failed-job entry so the retry button has something to
        // bind to. file_ref is preserved so the user can re-submit
        // without re-picking the file.
        setTrackedJobs((prev) => [
          {
            job_id: `failed-${Date.now()}`,
            type: jobType,
            filename: unit.label,
            status: 'failed',
            progress_percent: 0,
            current_stage: 'failed',
            created_at: new Date().toISOString(),
            error: msg,
            _retry_unit: unit,
          } as TrackedJob,
          ...prev,
        ]);
        throw err;
      }
    },
    [user, processingProfile, refetchSessions, resolveWorkflowDisplayState, isTerminalJobStatus],
  );

  // ── Upload handler (bulk-capable) ──────────────────────────
  const handleFileUpload = useCallback(
    async (files: FileList | null) => {
      if (!files?.length || !user) return;
      setUploading(true);
      setUploadError(null);
      try {
        const units = groupFilesIntoUnits(Array.from(files));
        if (units.length === 0) {
          setUploadError('Please select at least one audio or video file.');
          setUploading(false);
          return;
        }

        // Bounded parallelism: 3 simultaneous submissions. Higher
        // concurrency would flood the gateway and trigger the
        // per-tenant rate limiter; lower would needlessly serialize
        // a bulk drop.
        const concurrency = 3;
        let idx = 0;
        const workers = Array(Math.min(concurrency, units.length))
          .fill(null)
          .map(async () => {
            while (true) {
              const i = idx++;
              if (i >= units.length) return;
              try {
                await submitOneUnit(units[i]);
              } catch {
                // submitOneUnit already records a failed TrackedJob
                // and surfaces the error string; continue to the next.
              }
            }
          });
        await Promise.all(workers);
      } finally {
        setUploading(false);
      }
    },
    [user, groupFilesIntoUnits, submitOneUnit],
  );

  // ── Retry a failed upload ──────────────────────────────────
  const handleRetryJob = useCallback(
    async (job: TrackedJob) => {
      const unit = (job as any)._retry_unit as
        | { audio?: File; video?: File; label: string }
        | undefined;
      if (!unit) return;
      // Drop the failed entry; submitOneUnit will add a fresh one
      // (with a new idempotency key, so the server treats it as a
      // genuine retry rather than an idempotency-cache replay).
      setTrackedJobs((prev) => prev.filter((j) => j.job_id !== job.job_id));
      setUploading(true);
      setUploadError(null);
      try {
        await submitOneUnit(unit);
      } catch {
        /* error already recorded in trackedJobs */
      } finally {
        setUploading(false);
      }
    },
    [submitOneUnit],
  );

  return (
    <div className="space-y-6 animate-fade-in">
      {/* Header */}
      <PageHeader
        zone="CANONICAL PROCESSING"
        title="Knowledge Processing"
        subtitle="Upload and process knowledge transfer recordings through the canonical pipeline."
        isLive={isLive}
      />

      {/* Summary Cards */}
      <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
        <StatCard
          label="Live Now"
          value={liveSessions.length}
          icon={<Radio className="h-5 w-5 text-red-400" />}
          description={liveSessions.length > 0 ? `${liveSessions[0].participants.length} participants active` : undefined}
        />
        <StatCard
          label="Processing"
          value={processingSessions.length}
          icon={<Loader2 className={clsx("h-5 w-5 text-yellow-400", processingSessions.length > 0 && "animate-spin")} />}
        />
        <StatCard
          label="Completed"
          value={completedSessions.length}
          icon={<CheckCircle2 className="h-5 w-5 text-green-400" />}
        />
        <StatCard
          label="Insights Extracted"
          value={totalRules + totalTests}
          icon={<Sparkles className="h-5 w-5 text-nexus-400" />}
          description={completedSessions.length > 0 ? `from ${completedSessions.length} sessions` : undefined}
        />
      </div>

      {/* Upload Zone */}
      <div
        className={clsx(
          'card p-6 border-2 border-dashed transition-all duration-300 cursor-pointer',
          dragOver
            ? 'border-nexus-500 bg-nexus-500/5 shadow-lg shadow-nexus-500/10'
            : 'border-gray-200 hover:border-white/20',
        )}
        onDragOver={(e) => { e.preventDefault(); setDragOver(true); }}
        onDragLeave={() => setDragOver(false)}
        onDrop={(e) => { e.preventDefault(); setDragOver(false); handleFileUpload(e.dataTransfer.files); }}
        onClick={() => {
          const input = document.createElement('input');
          input.type = 'file';
          input.accept = 'audio/*,video/*';
          input.multiple = true;
          input.onchange = (e) => handleFileUpload((e.target as HTMLInputElement).files);
          input.click();
        }}
      >
        <div className="flex flex-col items-center text-center sm:flex-row sm:text-left sm:gap-6">
          <div className="flex h-16 w-16 items-center justify-center rounded-2xl bg-gradient-to-br from-nexus-500/20 to-purple-500/20 mb-3 sm:mb-0 shrink-0">
            {uploading ? (
              <Loader2 className="h-7 w-7 text-nexus-400 animate-spin" />
            ) : (
              <Upload className="h-7 w-7 text-nexus-400" />
            )}
          </div>
          <div className="flex-1">
            <p className="text-sm font-semibold text-[#0a2540]">
              {uploading ? 'Uploading & processing...' : 'Upload Recording or Start Live Session'}
            </p>
            <p className="text-xs text-slate-400 mt-1">
              Drag & drop one or many files, or click to browse. Each file
              becomes its own session; matching audio+video pairs (by
              basename) are processed as a single multimodal workflow.
              Supports WAV, MP3, MP4, WebM.
            </p>
          </div>
          <div className="flex flex-wrap gap-2 mt-4 sm:mt-0 sm:shrink-0">
            {/* Primary action — sized + colored to stand out from the
                neutral card background. The button itself doesn't need
                its own onClick because the parent drop-zone div catches
                the click; we keep it as a `<button>` for keyboard
                accessibility and screen readers. */}
            <button
              type="button"
              className="btn-primary text-sm py-2.5 px-5 font-semibold shadow-md shadow-nexus-500/30 ring-1 ring-white/10"
            >
              <FileAudio className="h-4 w-4" /> Browse files
            </button>
            {/* Record + Zoom are placeholders for upcoming features;
                disabled until wired so the user isn't confused about
                what actually triggers an upload. */}
            <button
              type="button"
              className="btn-secondary text-xs py-2 px-3 opacity-60 cursor-not-allowed"
              disabled
              title="Live recording — coming soon"
              onClick={(e) => e.stopPropagation()}
            >
              <Mic className="h-3.5 w-3.5" /> Record <span className="text-[9px] text-slate-400 ml-1">(soon)</span>
            </button>
            <button
              type="button"
              className="btn-secondary text-xs py-2 px-3 opacity-60 cursor-not-allowed"
              disabled
              title="Zoom integration — coming soon"
              onClick={(e) => e.stopPropagation()}
            >
              <Video className="h-3.5 w-3.5" /> Connect Zoom <span className="text-[9px] text-slate-400 ml-1">(soon)</span>
            </button>
          </div>
        </div>
        {/* Processing Profile Selector */}
        <div className="mt-3 pt-3 border-t border-gray-100 flex items-center gap-3">
          <span className="text-[11px] text-slate-400">Visual Processing:</span>
          {(['fast', 'multimodal', 'deep'] as const).map((p) => (
            <button
              key={p}
              type="button"
              onClick={(e) => { e.stopPropagation(); setProcessingProfile(p); }}
              className={clsx(
                'text-[11px] px-2.5 py-1 rounded-full border transition-colors',
                processingProfile === p
                  ? p === 'multimodal'
                    ? 'border-nexus-500/50 bg-nexus-500/15 text-nexus-400'
                    : 'border-white/20 bg-white/10 text-white'
                  : 'border-gray-100 text-slate-400 hover:text-slate-600 hover:border-gray-200',
              )}
            >
              {p === 'fast' ? 'Fast' : p === 'multimodal' ? 'Multimodal (default)' : 'Deep'}
            </button>
          ))}
          {processingProfile === 'fast' && (
            <span className="text-[10px] text-nexus-400/70">Optimized for speed — fewer enrichment calls, all scenes still preserved</span>
          )}
          {processingProfile === 'multimodal' && (
            <span className="text-[10px] text-nexus-400/70">60 frames · 40 scenes · full OCR — best for E2E Architect</span>
          )}
          {processingProfile === 'deep' && (
            <span className="text-[10px] text-nexus-400/70">Maximum fidelity — all frames, all scenes, deep vision analysis</span>
          )}
        </div>
      </div>

      {/* Upload Error Banner */}
      {uploadError && (
        <div className="card border border-red-500/30 bg-red-500/5 p-4 flex items-center gap-3">
          <AlertTriangle className="h-5 w-5 text-red-400 shrink-0" />
          <div className="flex-1">
            <p className="text-sm font-medium text-red-300">Upload Failed</p>
            <p className="text-xs text-red-400/80 mt-0.5">{uploadError}</p>
          </div>
          <button
            className="text-xs text-slate-8000 hover:text-[#0a2540]"
            onClick={(e) => { e.stopPropagation(); setUploadError(null); }}
          >
            Dismiss
          </button>
        </div>
      )}

      {/* ── Processing Cinema (Mode 2) ─────────────────────── */}
      {trackedJobs.length > 0 && (
        <div className="space-y-3">
          <div className="flex items-center justify-between">
            <h2 className="text-sm font-semibold text-slate-600">Processing</h2>
            <button
              className="text-xs text-slate-400 hover:text-red-400 transition-colors"
              onClick={() => setTrackedJobs([])}
            >
              Clear All
            </button>
          </div>
          {trackedJobs.map((job) => {
            const isActive = job.status !== 'completed' && job.status !== 'failed' && job.status !== 'needs_review' && job.status !== 'degraded';
            const barColor =
              job.status === 'completed' || job.status === 'degraded' ? 'bg-green-500' :
              job.status === 'failed' ? 'bg-red-500' :
              job.status === 'needs_review' ? 'bg-orange-500' : 'bg-nexus-500';

            // Build stage data from SSE (preferred) or polling fallback
            const hasSSEData = job.job_id === activeWorkflowId && workflowProgress;
            const pollStages = (job as any)._stagesFromPoll;
            let cinemaStages: { stage_id: string; status: string; duration_ms: number }[] | null = null;
            if (hasSSEData && workflowProgress) {
              cinemaStages = workflowProgress.stages;
            } else if (pollStages && typeof pollStages === 'object') {
              // Convert orchestrator stage object → stage array for cinema
              cinemaStages = Object.entries(pollStages).map(([sid, s]: [string, any]) => ({
                stage_id: sid,
                status: s.status || 'pending',
                duration_ms: s.duration_ms || 0,
              }));
            }
            const showCinema = isActive && cinemaStages && cinemaStages.length > 0;

            if (showCinema && cinemaStages) {
              const stageMap = new Map(cinemaStages.map(s => [s.stage_id, s]));
              const confidence = computeConfidence(cinemaStages);
              const isCached = job.current_stage === 'cached';

              return (
                <div key={job.job_id} className="card p-5 space-y-4 ring-1 ring-nexus-500/30">
                  {/* Header */}
                  <div className="flex items-center gap-3">
                    <Loader2 className="h-5 w-5 text-nexus-400 animate-spin shrink-0" />
                    <div className="flex-1 min-w-0">
                      <p className="text-sm font-semibold text-[#0a2540] truncate">{job.filename}</p>
                      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-[10px] text-slate-400 mt-0.5">
                        <span>{job.type === 'audio' ? 'Audio' : 'Video'}</span>
                        <span className="inline-flex items-center gap-1">
                          Workflow:{' '}
                          <code className="text-nexus-400 font-mono break-all select-all">{job.job_id}</code>
                        </span>
                        {job.artifact_id && (
                          <span className="inline-flex items-center gap-1">
                            Canonical:{' '}
                            <code className="text-blue-400 font-mono break-all select-all">{job.artifact_id}</code>
                          </span>
                        )}
                        {sseStatus === 'connected' && hasSSEData && <span className="text-green-400">&bull; SSE Live</span>}
                        {!hasSSEData && isActive && <span className="text-yellow-400">&bull; Polling</span>}
                      </div>
                    </div>
                    <div className="flex items-center gap-2 shrink-0">
                      <button className="text-slate-400 hover:text-[#0a2540] transition-colors p-1" title="Copy Workflow ID" onClick={() => copyJobId(job.job_id)}>
                        {copiedJobId === job.job_id ? <CheckCircle2 className="h-3.5 w-3.5 text-green-400" /> : <Copy className="h-3.5 w-3.5" />}
                      </button>
                      {job.artifact_id && (
                        <button
                          className="text-slate-400 hover:text-blue-400 transition-colors p-1"
                          title="Copy Canonical ID"
                          onClick={() => copyJobId(job.artifact_id!)}
                        >
                          {copiedJobId === job.artifact_id ? <CheckCircle2 className="h-3.5 w-3.5 text-green-400" /> : <Copy className="h-3.5 w-3.5" />}
                        </button>
                      )}
                      <button className="text-slate-400 hover:text-yellow-400 transition-colors p-1" title="Cancel" onClick={() => {
                        if (!confirm('Cancel this workflow?')) return;
                        const sid = (job as any).session_id;
                        if (sid) { api.cancelWorkflow(job.job_id).catch(() => {}); api.cancelSession(sid).catch(() => {}); }
                        setTrackedJobs(prev => prev.map(j => j.job_id === job.job_id ? { ...j, status: 'failed', error: 'Cancelled', current_stage: 'cancelled' } : j));
                        setActiveWorkflowId(null);
                        refetchSessions();
                      }}>
                        <Ban className="h-3.5 w-3.5" />
                      </button>
                    </div>
                  </div>

                  {/* Stage Rail
                       Highlighting precedence: running > current > status-based.
                       The `running` case wraps the cell in a pulsing nexus-blue
                       border so users always see at a glance which step is
                       actively executing \u2014 fixes "I can't tell which one is
                       processing" feedback. */}
                  <div className="flex items-stretch gap-0.5 overflow-x-auto pb-1">
                    {CANONICAL_STAGE_ORDER.map((stageId, idx) => {
                      const stage = stageMap.get(stageId);
                      const label = CANONICAL_STAGE_META[stageId] || stageId;
                      const isCurrent = stageId === (workflowProgress?.current_stage ?? job.current_stage);
                      const st = stage?.status || 'pending';
                      const isRunning = st === 'running' || (isCurrent && st !== 'completed' && st !== 'failed' && st !== 'skipped');
                      return (
                        <div key={stageId} className="flex items-center">
                          {idx > 0 && <div className={clsx('w-3 h-px shrink-0', st === 'completed' || st === 'skipped' ? 'bg-green-500/50' : 'bg-gray-100')} />}
                          <div className={clsx(
                            'flex flex-col items-center gap-0.5 px-2.5 py-2 rounded-lg text-center min-w-[76px] transition-all border-2',
                            isRunning ? 'border-nexus-500 bg-nexus-500/15 shadow-md shadow-nexus-500/20 animate-pulse' :
                            st === 'completed' ? 'border-green-500/40 bg-green-500/5' :
                            st === 'failed' ? 'border-red-500/40 bg-red-500/5' :
                            st === 'skipped' ? 'border-yellow-500/40 bg-yellow-500/5' :
                            'border-gray-200 bg-gray-50',
                          )}>
                            <div className="h-4 flex items-center">
                              {isRunning ? <Loader2 className="h-3.5 w-3.5 text-nexus-500 animate-spin" /> :
                               st === 'completed' ? <CheckCircle2 className="h-3.5 w-3.5 text-green-500" /> :
                               st === 'failed' ? <XCircle className="h-3.5 w-3.5 text-red-500" /> :
                               st === 'skipped' ? <AlertTriangle className="h-3.5 w-3.5 text-yellow-500" /> :
                               <div className="h-2 w-2 rounded-full bg-gray-400" />}
                            </div>
                            <span className={clsx('text-[10px] font-medium leading-tight', isRunning ? 'text-nexus-700 font-semibold' : 'text-slate-700')}>{label}</span>
                            <span className={clsx('text-[9px]', isRunning ? 'text-nexus-600 font-medium' : 'text-slate-500')}>
                              {isRunning ? 'running\u2026' : (stage && stage.duration_ms ? formatElapsed(stage.duration_ms) : '\u2014')}
                            </span>
                          </div>
                        </div>
                      );
                    })}
                  </div>

                  {/* Phase 3: partial-artifact banner. Shows when
                       the minimal artifact has been persisted but
                       heavy enrichment (OCR + LLaVA) is still
                       running. Tells the user their result is
                       already viewable; enrichment improves it. */}
                  {confidence.partial && (
                    <div className="flex items-center gap-2 rounded-md bg-amber-500/10 border border-amber-500/30 px-3 py-2 text-[11px]">
                      <Loader2 className="h-3.5 w-3.5 text-amber-500 animate-spin shrink-0" />
                      <span className="text-amber-700 font-medium">
                        Artifact ready (partial) — enrichment in progress.
                      </span>
                      <span className="text-amber-600/70">
                        Visual analysis will fill in as it completes.
                      </span>
                      {job.artifact_id && (
                        <a
                          href={`/artifacts/${job.artifact_id}`}
                          className="ml-auto text-amber-700 hover:text-amber-900 underline"
                          onClick={(e) => e.stopPropagation()}
                        >
                          View partial result →
                        </a>
                      )}
                    </div>
                  )}

                  {/* Confidence Meters */}
                  <div className="grid grid-cols-4 gap-3">
                    {(['transcript', 'visual', 'safety', 'overall'] as const).map(key => {
                      const val = confidence[key];
                      const label = key.charAt(0).toUpperCase() + key.slice(1);
                      const barCol = val === 100 ? 'bg-green-500' : val > 0 ? 'bg-nexus-500' : 'bg-gray-100';
                      return (
                        <div key={key}>
                          <div className="flex justify-between text-[10px] mb-1">
                            <span className="text-slate-8000">{label}</span>
                            <span className="text-slate-400">{val}%</span>
                          </div>
                          <div className="w-full bg-gray-100 rounded-full h-1.5 overflow-hidden">
                            <div className={clsx(barCol, 'h-1.5 rounded-full transition-all duration-500')} style={{ width: `${clampPct(val)}%` }} />
                          </div>
                        </div>
                      );
                    })}
                  </div>

                  {/* Operator Truth Panel */}
                  <div className="bg-white rounded-lg px-3 py-2 flex flex-wrap items-center gap-x-4 gap-y-1 text-[10px]">
                    <span className={clsx('flex items-center gap-1', isCached ? 'text-purple-400' : 'text-nexus-400')}>
                      <Zap className="h-3 w-3" /> {isCached ? 'Cache Hit' : 'Fresh Run'}
                    </span>
                    <span className={clsx('flex items-center gap-1', sseStatus === 'connected' ? 'text-green-400' : 'text-yellow-400')}>
                      <Activity className="h-3 w-3" /> {sseStatus === 'connected' ? 'Live' : 'Polling'}
                    </span>
                    {job.degraded_stages && job.degraded_stages.length > 0 && (
                      <span className="flex items-center gap-1 text-orange-400">
                        <AlertTriangle className="h-3 w-3" /> Degraded: {job.degraded_stages.map(d => CANONICAL_STAGE_META[d] || d).join(', ')}
                      </span>
                    )}
                    {cinemaStages.some(s => s.status === 'skipped') && (
                      <span className="flex items-center gap-1 text-yellow-400">
                        <AlertTriangle className="h-3 w-3" /> Skipped: {cinemaStages.filter(s => s.status === 'skipped').map(s => CANONICAL_STAGE_META[s.stage_id] || s.stage_id).join(', ')}
                      </span>
                    )}
                    {stallWarning && (
                      <span className="flex items-center gap-1 text-red-400 animate-pulse">
                        <Timer className="h-3 w-3" /> Stall: {CANONICAL_STAGE_META[stallWarning] || stallWarning} &gt;60s
                      </span>
                    )}
                  </div>

                  {/* Progress bar — clamped to [2, 100] so it stays visible
                       at the start and never overflows the container at 100%. */}
                  <div className="w-full bg-gray-100 rounded-full h-1.5 overflow-hidden">
                    <div className={clsx(barColor, 'h-1.5 rounded-full transition-all duration-500')} style={{ width: `${clampPct(job.progress_percent, 2)}%` }} />
                  </div>
                </div>
              );
            }

            {/* Condensed view for non-cinema jobs */}
            const statusColor =
              job.status === 'completed' || job.status === 'degraded' ? 'text-green-400' :
              job.status === 'failed' ? 'text-red-400' :
              job.status === 'needs_review' ? 'text-orange-400' : 'text-yellow-400';
            return (
              <div key={job.job_id} className="card px-5 py-4">
                <div className="flex items-center gap-3 mb-2">
                  {isActive ? (
                    <Loader2 className="h-4 w-4 text-yellow-400 animate-spin shrink-0" />
                  ) : job.status === 'completed' || job.status === 'degraded' ? (
                    <CheckCircle2 className="h-4 w-4 text-green-400 shrink-0" />
                  ) : job.status === 'needs_review' ? (
                    <AlertTriangle className="h-4 w-4 text-orange-400 shrink-0" />
                  ) : (
                    <XCircle className="h-4 w-4 text-red-400 shrink-0" />
                  )}
                  <div className="flex-1 min-w-0">
                    <p className="text-sm font-medium text-[#0a2540] truncate">{job.filename}</p>
                    <p className="text-[10px] text-slate-400 mt-0.5 flex flex-wrap items-center gap-x-1.5 gap-y-1">
                      <span>{job.type === 'audio' ? 'Audio' : 'Video'}</span>
                      <span>&middot;</span>
                      <span>Stage: <span className={statusColor}>{CANONICAL_STAGE_META[job.current_stage] || job.current_stage}</span></span>
                      {job.artifact_id && (
                        <>
                          <span>&middot;</span>
                          <span className="inline-flex items-center gap-1">
                            Canonical:{' '}
                            <code className="text-blue-400 font-mono break-all select-all">{job.artifact_id}</code>
                          </span>
                        </>
                      )}
                    </p>
                  </div>
                  <div className="flex items-center gap-2 shrink-0">
                    <button className="text-slate-400 hover:text-[#0a2540] transition-colors p-1" title="Copy Workflow ID" onClick={() => copyJobId(job.job_id)}>
                      {copiedJobId === job.job_id ? <CheckCircle2 className="h-3.5 w-3.5 text-green-400" /> : <Copy className="h-3.5 w-3.5" />}
                    </button>
                    {job.artifact_id && (
                      <button
                        className="text-slate-400 hover:text-blue-400 transition-colors p-1"
                        title="Copy Canonical ID"
                        onClick={() => copyJobId(job.artifact_id!)}
                      >
                        {copiedJobId === job.artifact_id ? <CheckCircle2 className="h-3.5 w-3.5 text-green-400" /> : <Copy className="h-3.5 w-3.5" />}
                      </button>
                    )}
                    {job.status === 'failed' && (job as any)._retry_unit && (
                      <button
                        className="text-slate-400 hover:text-nexus-400 transition-colors p-1"
                        title="Retry this upload"
                        onClick={() => handleRetryJob(job)}
                      >
                        <Loader2 className="h-3.5 w-3.5" />
                      </button>
                    )}
                    <button className="text-slate-400 hover:text-red-400 transition-colors p-1" title="Dismiss" onClick={() => setTrackedJobs(prev => prev.filter(j => j.job_id !== job.job_id))}>
                      <XCircle className="h-3.5 w-3.5" />
                    </button>
                  </div>
                </div>
                <div className="w-full bg-gray-100 rounded-full h-1.5 overflow-hidden">
                  <div className={clsx(barColor, 'h-1.5 rounded-full transition-all duration-500')} style={{ width: `${clampPct(job.progress_percent, isActive ? 2 : 0)}%` }} />
                </div>
                <div className="flex justify-between mt-1">
                  <span className="text-[10px] text-slate-400">
                    {job.status === 'failed' && job.error
                      ? `Error: ${job.error.slice(0, 80)}`
                      : job.quality_gate_passed === false
                        ? `Quality gate: ${job.quality_gate_outcome || 'failed'} \u2014 processing degraded`
                        : job.degraded_stages && job.degraded_stages.length > 0
                          ? `Needs review \u2014 skipped: ${job.degraded_stages.map(d => CANONICAL_STAGE_META[d] || d).join(', ')}`
                          : `${Math.round(job.progress_percent)}% complete`}
                  </span>
                  {job.status === 'failed' && (job as any)._retry_unit && (
                    <button
                      className="text-[10px] text-nexus-400 hover:text-nexus-300 underline"
                      onClick={() => handleRetryJob(job)}
                    >
                      Retry
                    </button>
                  )}
                </div>
              </div>
            );
          })}
        </div>
      )}

      {/* ── Job Lookup ────────────────────────────────────── */}
      <div className="card px-5 py-4">
        <h2 className="text-sm font-semibold text-slate-600 mb-3">Track Job by ID</h2>
        <div className="flex gap-2">
          <div className="relative flex-1">
            <Search className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-slate-400" />
            <input
              type="text"
              className="w-full bg-gray-100 border border-gray-200 text-sm text-[#0a2540] rounded-lg pl-9 pr-3 py-2 placeholder-slate-400 focus:outline-none focus:border-nexus-500"
              placeholder="Paste Job ID to check status..."
              value={lookupJobId}
              onChange={(e) => setLookupJobId(e.target.value)}
              onKeyDown={(e) => e.key === 'Enter' && handleJobLookup()}
            />
          </div>
          <button
            className="btn-primary text-xs py-2 px-4 shrink-0"
            onClick={handleJobLookup}
            disabled={lookupLoading || !lookupJobId.trim()}
          >
            {lookupLoading ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : 'Look Up'}
          </button>
        </div>
        {lookupError && (
          <p className="text-xs text-red-400 mt-2">{lookupError}</p>
        )}
        {lookupResult && (
          <div className="mt-3 p-3 bg-white rounded-lg border border-gray-100">
            <div className="flex items-center gap-2 mb-2">
              {lookupResult.status === 'completed' ? (
                <CheckCircle2 className="h-4 w-4 text-green-400" />
              ) : lookupResult.status === 'failed' ? (
                <XCircle className="h-4 w-4 text-red-400" />
              ) : lookupResult.status === 'needs_review' ? (
                <AlertTriangle className="h-4 w-4 text-orange-400" />
              ) : (
                <Loader2 className="h-4 w-4 text-yellow-400 animate-spin" />
              )}
              <span className="text-sm font-medium text-[#0a2540]">{lookupResult.filename}</span>
              <StatusBadge
                label={lookupResult.status === 'needs_review' ? 'NEEDS REVIEW' : lookupResult.status.toUpperCase()}
                variant={
                  lookupResult.status === 'completed' ? 'green' :
                  lookupResult.status === 'failed' ? 'red' :
                  lookupResult.status === 'needs_review' ? 'orange' : 'yellow'
                }
              />
            </div>
            <div className="grid grid-cols-2 gap-x-4 gap-y-1 text-xs">
              <div><span className="text-slate-400">Job ID:</span> <span className="text-slate-600 font-mono">{lookupResult.job_id}</span></div>
              <div><span className="text-slate-400">Type:</span> <span className="text-slate-600">{lookupResult.type}</span></div>
              <div><span className="text-slate-400">Stage:</span> <span className="text-slate-600">{lookupResult.current_stage}</span></div>
              <div><span className="text-slate-400">Progress:</span> <span className="text-slate-600">{Math.round(lookupResult.progress_percent)}%</span></div>
              {lookupResult.created_at && (
                <div><span className="text-slate-400">Started:</span> <span className="text-slate-600">{new Date(lookupResult.created_at).toLocaleString()}</span></div>
              )}
              {lookupResult.error && (
                <div className="col-span-2"><span className="text-slate-400">Error:</span> <span className="text-red-400">{lookupResult.error}</span></div>
              )}
            </div>
            {lookupResult.status !== 'completed' && lookupResult.status !== 'failed' && lookupResult.status !== 'needs_review' && (
              <div className="w-full bg-gray-100 rounded-full h-1.5 mt-2">
                <div
                  className="bg-nexus-500 h-1.5 rounded-full transition-all duration-500"
                  style={{ width: `${Math.max(lookupResult.progress_percent, 2)}%` }}
                />
              </div>
            )}
          </div>
        )}
      </div>

      {/* Live Session Card (if any) */}
      {liveSessions.map((session) => (
        <div
          key={session.session_id}
          className="card overflow-hidden ring-red-500/30 animate-pulse-glow cursor-pointer"
          onClick={() => navigate(`/sessions/${session.session_id}/replay`)}
        >
          <div className="bg-gradient-to-r from-red-500/10 to-orange-500/10 px-6 py-4">
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-3">
                <StatusIcon status="live" />
                <span className="badge-red">LIVE NOW</span>
                <h3 className="text-lg font-semibold text-[#0a2540]">{session.title}</h3>
              </div>
              <div className="flex items-center gap-2 text-xs text-slate-8000">
                <Clock className="h-3.5 w-3.5" />
                {formatDuration(session.duration_seconds ?? 0)}
              </div>
            </div>

            <div className="mt-4 flex flex-wrap items-center gap-6">
              <div className="flex items-center gap-2">
                <Users className="h-4 w-4 text-slate-8000" />
                <span className="text-sm text-slate-600">{session.participants.length} participants</span>
                <div className="flex -space-x-2 ml-1">
                  {session.participants.slice(0, 5).map((p: SessionParticipant) => (
                    <div
                      key={p.speaker_id}
                      className="flex h-7 w-7 items-center justify-center rounded-full bg-gradient-to-br from-nexus-500 to-purple-600 text-[10px] font-bold text-[#0a2540] ring-2 ring-gray-900"
                      title={p.name}
                    >
                      {p.name.charAt(0)}
                    </div>
                  ))}
                  {session.participants.length > 5 && (
                    <div className="flex h-7 w-7 items-center justify-center rounded-full bg-gray-100 text-[10px] font-bold text-slate-600 ring-2 ring-gray-900">
                      +{session.participants.length - 5}
                    </div>
                  )}
                </div>
              </div>

              <div className="flex gap-5 text-sm">
                <div className="flex items-center gap-1.5 text-yellow-400">
                  <Brain className="h-4 w-4" />
                  <span className="font-semibold">{session.rules_extracted}</span>
                  <span className="text-slate-400">insights</span>
                </div>
                <div className="flex items-center gap-1.5 text-blue-400">
                  <BarChart3 className="h-4 w-4" />
                  <span className="font-semibold">{session.confidence_score}%</span>
                  <span className="text-slate-400">confidence</span>
                </div>
              </div>
            </div>
          </div>

          {/* Live participant bars */}
          <div className="px-6 py-3 bg-white/90 flex items-center justify-between text-xs">
            <div className="flex gap-3">
              {session.participants.slice(0, 3).map((p: SessionParticipant) => (
                <span key={p.speaker_id} className="text-slate-8000">
                  <span className="text-slate-700 font-medium">{p.name.split(' ')[0]}</span> — {p.rules_contributed || 0} insights
                </span>
              ))}
            </div>
            <div className="flex items-center gap-1 text-nexus-400 font-medium">
              Open Replay <ChevronRight className="h-3.5 w-3.5" />
            </div>
          </div>
        </div>
      ))}

      {/* ── Sessions List (Mode 3) ───────────────────────── */}
      {allSessions.length === 0 ? (
        <EmptyState
          icon={<FileAudio className="h-7 w-7 text-slate-400" />}
          title="No sessions yet"
          description="Upload a recording or start a live session to begin extracting knowledge."
        />
      ) : (
      <div>
        <h2 className="text-sm font-semibold text-slate-600 mb-3">Recent Sessions</h2>
        <div className="space-y-2">
          {sessions
            .filter((s) => s.status !== 'live')
            .map((session) => {
              const artifact = sessionArtifacts[session.session_id];
              const hasArtifact = !!(artifact && (session.status === 'completed' || session.status === 'needs_review'));
              const qBadge = hasArtifact ? qualityBadgeProps(artifact.brain_quality_score ?? null, artifact.quality_gate_outcome ?? null) : null;
              const pBadge = hasArtifact ? provenanceBadgeProps(artifact) : null;

              return (
                <div
                  key={session.session_id}
                  className="card px-5 py-4 flex items-center gap-4 cursor-pointer hover:ring-white/10 transition-all group"
                  onClick={() => navigate(`/sessions/${session.session_id}/replay`)}
                >
                  <StatusIcon status={session.status} />

                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-2 flex-wrap">
                      <p className="text-sm font-medium text-[#0a2540] truncate group-hover:text-nexus-400 transition-colors">
                        {session.title}
                      </p>
                      <StatusLabel status={session.status} />
                      {qBadge && (
                        <StatusBadge label={qBadge.label} variant={qBadge.variant} />
                      )}
                      {pBadge && (
                        <StatusBadge label={pBadge.label} variant={pBadge.variant} />
                      )}
                    </div>
                    <div className="mt-1 flex items-center gap-4 text-xs text-slate-400">
                      <span>{formatDate(session.created_at)}</span>
                      {hasArtifact && artifact.source_filename ? (
                        <span className="flex items-center gap-1 text-slate-8000">
                          <FileAudio className="h-3 w-3" />
                          {artifact.source_filename}
                        </span>
                      ) : (
                        <span className="flex items-center gap-1">
                          <Users className="h-3 w-3" />
                          {session.participants.map((p: SessionParticipant) => p.name.split(' ')[0]).join(', ')}
                        </span>
                      )}
                      {hasArtifact && artifact.processing_time_seconds != null ? (
                        <span className="flex items-center gap-1">
                          <Timer className="h-3 w-3" />
                          {formatProcessingTime(artifact.processing_time_seconds)}
                        </span>
                      ) : (session.duration_seconds ?? 0) > 0 ? (
                        <span className="flex items-center gap-1">
                          <Clock className="h-3 w-3" />
                          {formatDuration(session.duration_seconds ?? 0)}
                        </span>
                      ) : null}
                    </div>
                  </div>

                  <div className="hidden sm:flex gap-6 text-center shrink-0">
                    {hasArtifact && artifact.brain_quality_score != null ? (
                      <div>
                        <p className="text-lg font-bold text-nexus-400">{(artifact.brain_quality_score * 100).toFixed(0)}%</p>
                        <p className="text-[10px] text-slate-400">Quality</p>
                      </div>
                    ) : (
                      <div>
                        <p className="text-lg font-bold text-[#0a2540]">{session.rules_extracted}</p>
                        <p className="text-[10px] text-slate-400">Insights</p>
                      </div>
                    )}
                    <div>
                      <p className="text-lg font-bold text-[#0a2540]">{session.tests_generated}</p>
                      <p className="text-[10px] text-slate-400">Documents</p>
                    </div>
                    {hasArtifact && artifact.processing_time_seconds != null ? (
                      <div>
                        <p className="text-lg font-bold text-slate-600">{formatProcessingTime(artifact.processing_time_seconds)}</p>
                        <p className="text-[10px] text-slate-400">Processing</p>
                      </div>
                    ) : (
                      <div>
                        <p className="text-lg font-bold text-nexus-400">{session.confidence_score}%</p>
                        <p className="text-[10px] text-slate-400">Confidence</p>
                      </div>
                    )}
                  </div>

                  <div className="flex items-center gap-1 shrink-0" onClick={(e) => e.stopPropagation()}>
                    {hasArtifact && (
                      <button
                        onClick={(e) => { e.stopPropagation(); navigate(`/sessions/${session.session_id}/canonical`); }}
                        className="flex items-center gap-1 px-2.5 py-1.5 rounded-md text-xs font-medium text-nexus-400 hover:bg-nexus-500/10 transition-colors"
                        title="View Canonical Asset"
                      >
                        View Asset <ArrowRight className="h-3 w-3" />
                      </button>
                    )}
                    {session.status === 'processing' && (
                      <button
                        onClick={(e) => handleCancelSession(e, session.session_id)}
                        className="p-1.5 rounded hover:bg-yellow-500/20 text-slate-400 hover:text-yellow-400 transition-colors"
                        title="Cancel processing"
                      >
                        <Ban className="h-4 w-4" />
                      </button>
                    )}
                    <button
                      onClick={(e) => handleDeleteSession(e, session.session_id)}
                      className="p-1.5 rounded hover:bg-red-500/20 text-slate-400 hover:text-red-400 transition-colors"
                      title="Delete session"
                    >
                      <Trash2 className="h-4 w-4" />
                    </button>
                  </div>

                  <ChevronRight className="h-4 w-4 text-slate-8000 group-hover:text-nexus-400 transition-colors shrink-0" />
                </div>
              );
            })}
        </div>
      </div>
      )}
    </div>
  );
}
