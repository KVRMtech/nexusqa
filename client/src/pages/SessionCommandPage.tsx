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
const CANONICAL_STAGE_ORDER = [
  'media_probe', 'audio_transcription', 'pii_redaction',
  'visual_extraction', 'visual_graph_assembly',
  'artifact_persistence', 'canonical_quality_gate',
] as const;

const CANONICAL_STAGE_META: Record<string, string> = {
  media_probe: 'Media Probe',
  audio_transcription: 'Transcription',
  pii_redaction: 'PII Safety',
  visual_extraction: 'Visual Analysis',
  visual_graph_assembly: 'Graph Assembly',
  artifact_persistence: 'Artifact Save',
  canonical_quality_gate: 'Quality Gate',
};

function computeConfidence(stages: { stage_id: string; status: string }[]): {
  transcript: number; visual: number; safety: number; overall: number;
} {
  const m = new Map(stages.map(s => [s.stage_id, s.status]));
  const done = (id: string) => m.get(id) === 'completed';
  let transcript = 0;
  if (done('audio_transcription')) transcript += 50;
  if (done('pii_redaction')) transcript += 50;
  let visual = 0;
  if (done('visual_extraction')) visual += 50;
  if (done('visual_graph_assembly')) visual += 50;
  const safety = done('pii_redaction') ? 100 : 0;
  const total = CANONICAL_STAGE_ORDER.length;
  const completed = CANONICAL_STAGE_ORDER.filter(id => done(id) || m.get(id) === 'skipped').length;
  const overall = Math.round((completed / total) * 100);
  return { transcript, visual, safety, overall };
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

function formatElapsed(ms: number): string {
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
    else if (rawStatus === 'processing' || rawStatus === 'running') status = 'processing';
    else status = 'completed';
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
        // Update tracked job to final state
        if (activeWorkflowId) {
          setTrackedJobs((prev) =>
            prev.map((j) =>
              j.job_id === activeWorkflowId
                ? {
                    ...j,
                    status: event.status,
                    progress_percent: 100,
                    current_stage: (event.status === 'completed' || event.status === 'degraded')
                      ? 'completed' : event.status === 'needs_review' ? 'needs_review' : 'failed',
                    error: event.error || null,
                  }
                : j,
            ),
          );
        }
        // Done — disconnect SSE
        setActiveWorkflowId(null);
        refetchSessions();
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
            // Use orchestrator workflow status (job_id is a workflow_id)
            const data = await api.getWorkflowStatus(job.job_id);
            const wfStatus = data.status || job.status;
            // Map workflow stages to progress info
            const stagesCompleted = Object.values(data.stages || {}).filter(
              (s: any) => s.status === 'completed' || s.status === 'skipped',
            ).length;
            const stagesTotal = Object.keys(data.stages || {}).length;
            const pct = stagesTotal > 0
              ? Math.round((stagesCompleted / stagesTotal) * 100)
              : (data.progress_percent ?? job.progress_percent);

            // Phase 1.4: When workflow reaches a terminal state, verify artifact status as
            // the authoritative completion signal before marking the job done.
            let finalStatus = wfStatus;
            let qualityGatePassed: boolean | null = null;
            let qualityGateOutcome: string | null = null;
            if (wfStatus === 'completed' || wfStatus === 'needs_review' || wfStatus === 'degraded') {
              const artStage = (data.stages || {}).artifact_persistence;
              const artifactId = artStage?.output?.artifact_id;
              if (artifactId) {
                try {
                  const artStatus = await api.getArtifactStatus(artifactId);
                  // Artifact status overrides workflow status for completion
                  finalStatus = artStatus.status;
                  qualityGatePassed = artStatus.quality_gate_passed;
                  qualityGateOutcome = artStatus.quality_gate_outcome;
                  // Surface quality gate failure as needs_review so UI shows warning
                  if (finalStatus === 'completed' && qualityGatePassed === false) {
                    finalStatus = 'needs_review';
                  }
                } catch {
                  // If artifact status endpoint is unavailable, trust workflow
                }
              }
            }

            return {
              job_id: job.job_id,
              type: job.type,
              filename: job.filename,
              status: finalStatus,
              progress_percent: (finalStatus === 'completed' || finalStatus === 'needs_review' || finalStatus === 'degraded') ? 100 : pct,
              current_stage: data.current_stage || job.current_stage,
              created_at: data.created_at || job.created_at,
              error: data.error || null,
              quality_gate_passed: qualityGatePassed,
              quality_gate_outcome: qualityGateOutcome,
              degraded_stages: data.input_data?._degraded_stages || null,
              // Carry stage data for cinema view (polling fallback when SSE unavailable)
              _stagesFromPoll: data.stages || null,
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
      (j) => j.status !== 'completed' && j.status !== 'failed' && j.status !== 'needs_review' && j.status !== 'degraded',
    );
    if (hasActive && !pollingRef.current) {
        pollJobs();
      pollingRef.current = setInterval(pollJobs, 5000);
    } else if (!hasActive && pollingRef.current) {
      clearInterval(pollingRef.current);
      pollingRef.current = null;
    }
  }, [trackedJobs, pollJobs]);

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
      const stagesCompleted = Object.values(data.stages || {}).filter(
        (s: any) => s.status === 'completed' || s.status === 'skipped',
      ).length;
      const stagesTotal = Object.keys(data.stages || {}).length;
      const pct = stagesTotal > 0
        ? Math.round((stagesCompleted / stagesTotal) * 100)
        : (data.progress_percent ?? 0);

      // Phase 1.4: If workflow reached terminal state, check artifact status for true readiness
      let effectiveStatus = data.status || 'unknown';
      if (effectiveStatus === 'completed' || effectiveStatus === 'needs_review') {
        const artStage = (data.stages || {}).artifact_persistence;
        const artifactId = artStage?.output?.artifact_id;
        if (artifactId) {
          try {
            const artStatus = await api.getArtifactStatus(artifactId);
            effectiveStatus = artStatus.status;
          } catch {
            // Fall back to workflow status if artifact endpoint unavailable
          }
        }
      }

      setLookupResult({
        job_id: data.workflow_id || id,
        type: data.chain_name?.includes('video') ? 'video' : 'audio',
        filename: data.chain_name || 'Workflow',
        status: effectiveStatus,
        progress_percent: (effectiveStatus === 'completed' || effectiveStatus === 'needs_review') ? 100 : pct,
        current_stage: data.current_stage || data.status || 'unknown',
        created_at: data.created_at || '',
        error: data.error || null,
      });
    } catch {
      setLookupError('Workflow not found. Verify the ID and ensure services are online.');
    }
    setLookupLoading(false);
  }, [lookupJobId]);

  // ── Upload handler (audio + video) ────────────────────────
  const handleFileUpload = useCallback(
    async (files: FileList | null) => {
      if (!files?.length || !user) return;
      setUploading(true);
      setUploadError(null);
      try {
        // Separate audio and video files from the selection
        const audioFile = Array.from(files).find(isAudioFile);
        const videoFile = Array.from(files).find(isVideoFile);

        if (!audioFile && !videoFile) {
          setUploadError('Please select at least one audio or video file.');
          setUploading(false);
          return;
        }

        const jobType: UploadJobType = (audioFile && videoFile) ? 'video' : videoFile ? 'video' : 'audio';

        // 1. Create a session via Platform API for tracking
        const primaryFile = videoFile || audioFile!;
        const sessionName = primaryFile.name.replace(/\.[^.]+$/, '');
        console.log('[Upload] Step 1: Creating session…', sessionName);
        const session = await api.createSession(user.tenant_id, sessionName);
        const sessionId: string = session.session_id;
        console.log('[Upload] Session created:', sessionId);

        // 2. Start canonical processing (unified upload + chain start)
        console.log('[Upload] Step 2: Starting canonical processing…');
        const result = await api.startCanonicalProcessing({
          sessionId,
          audioFile: audioFile || undefined,
          videoFile: videoFile || undefined,
          processingProfile: processingProfile,
          // Canonical-only mode: no downstream consumer chains.
        });
        console.log('[Upload] Canonical processing started:', result);

        const workflowId = result.workflow_id;
        const isCached = result.status === 'completed';

        // Track the workflow for progress display
        const canonicalId = result.artifact_id || null;
        if (canonicalId) {
          console.log('[Upload] Canonical ID (immediate):', canonicalId);
        }
        setTrackedJobs((prev) => [
          {
            job_id: workflowId,
            type: jobType,
            filename: [audioFile?.name, videoFile?.name].filter(Boolean).join(' + '),
            status: isCached ? 'completed' : 'running',
            progress_percent: isCached ? 100 : 0,
            current_stage: isCached ? 'cached' : 'starting',
            created_at: new Date().toISOString(),
            error: null,
            artifact_id: canonicalId,
            session_id: sessionId,
          } as TrackedJob,
          ...prev,
        ]);

        // 3. Connect SSE for real-time progress (unless already cached)
        if (!isCached) {
          setActiveWorkflowId(workflowId);
          console.log('[Upload] SSE stream connected for', workflowId);
        } else {
          console.log('[Upload] Using cached artifact — no processing needed');
          refetchSessions();
        }
      } catch (err: any) {
        // Handle 409 — duplicate upload, connect to existing workflow
        const status = err?.response?.status;
        const detail = err?.response?.data?.detail;
        if (status === 409 && detail?.existing_workflow_id) {
          const existingWfId = detail.existing_workflow_id;
          console.log('[Upload] 409 — attaching to existing workflow:', existingWfId);
          setUploadError(null);
          // Track the existing workflow
          setTrackedJobs((prev) => {
            if (prev.some((j) => j.job_id === existingWfId)) return prev;
            return [
              {
                job_id: existingWfId,
                type: 'audio' as UploadJobType,
                filename: 'Resuming existing processing...',
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
        } else {
          console.error('Upload failed:', err?.message || err);
          const msg = typeof detail === 'string' ? detail
            : detail?.message || err?.message || 'Upload failed — please check the backend and try again.';
          setUploadError(msg);
        }
      }
      setUploading(false);
    },
    [user, refetchSessions],
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
            : 'border-white/10 hover:border-white/20',
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
            <p className="text-sm font-semibold text-white">
              {uploading ? 'Uploading & processing...' : 'Upload Recording or Start Live Session'}
            </p>
            <p className="text-xs text-gray-500 mt-1">
              Drag & drop audio/video files, or click to browse. Supports WAV, MP3, MP4, WebM.
            </p>
          </div>
          <div className="flex gap-2 mt-4 sm:mt-0">
            <button className="btn-primary text-xs py-2 px-3">
              <FileAudio className="h-3.5 w-3.5" /> Upload
            </button>
            <button className="btn-secondary text-xs py-2 px-3">
              <Mic className="h-3.5 w-3.5" /> Record
            </button>
            <button className="btn-secondary text-xs py-2 px-3">
              <Video className="h-3.5 w-3.5" /> Connect Zoom
            </button>
          </div>
        </div>
        {/* Processing Profile Selector */}
        <div className="mt-3 pt-3 border-t border-white/5 flex items-center gap-3">
          <span className="text-[11px] text-gray-500">Visual Processing:</span>
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
                  : 'border-white/5 text-gray-500 hover:text-gray-300 hover:border-white/10',
              )}
            >
              {p === 'fast' ? 'Fast' : p === 'multimodal' ? 'Multimodal (default)' : 'Deep'}
            </button>
          ))}
          {processingProfile === 'multimodal' && (
            <span className="text-[10px] text-nexus-400/70">10 frames · 6 scenes · OCR enabled — best for E2E Architect</span>
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
            className="text-xs text-gray-400 hover:text-white"
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
            <h2 className="text-sm font-semibold text-gray-300">Processing</h2>
            <button
              className="text-xs text-gray-500 hover:text-red-400 transition-colors"
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
                      <p className="text-sm font-semibold text-white truncate">{job.filename}</p>
                      <div className="flex items-center gap-3 text-[10px] text-gray-500 mt-0.5">
                        <span>{job.type === 'audio' ? 'Audio' : 'Video'}</span>
                        <span>Workflow: <code className="text-nexus-400 font-mono">{job.job_id.slice(0, 12)}&hellip;</code></span>
                        {job.artifact_id && <span>Canonical: <code className="text-blue-400 font-mono">{job.artifact_id.slice(0, 12)}&hellip;</code></span>}
                        {sseStatus === 'connected' && hasSSEData && <span className="text-green-400">&bull; SSE Live</span>}
                        {!hasSSEData && isActive && <span className="text-yellow-400">&bull; Polling</span>}
                      </div>
                    </div>
                    <div className="flex items-center gap-2 shrink-0">
                      <button className="text-gray-500 hover:text-white transition-colors p-1" title="Copy Workflow ID" onClick={() => copyJobId(job.job_id)}>
                        {copiedJobId === job.job_id ? <CheckCircle2 className="h-3.5 w-3.5 text-green-400" /> : <Copy className="h-3.5 w-3.5" />}
                      </button>
                      <button className="text-gray-500 hover:text-yellow-400 transition-colors p-1" title="Cancel" onClick={() => {
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

                  {/* Stage Rail */}
                  <div className="flex items-stretch gap-0.5 overflow-x-auto pb-1">
                    {CANONICAL_STAGE_ORDER.map((stageId, idx) => {
                      const stage = stageMap.get(stageId);
                      const label = CANONICAL_STAGE_META[stageId] || stageId;
                      const isCurrent = stageId === (workflowProgress?.current_stage ?? job.current_stage);
                      const st = stage?.status || 'pending';
                      return (
                        <div key={stageId} className="flex items-center">
                          {idx > 0 && <div className={clsx('w-3 h-px shrink-0', st === 'completed' || st === 'skipped' ? 'bg-green-500/50' : 'bg-gray-700')} />}
                          <div className={clsx(
                            'flex flex-col items-center gap-0.5 px-2.5 py-2 rounded-lg text-center min-w-[76px] transition-all border',
                            isCurrent ? 'border-nexus-500/60 bg-nexus-500/10' :
                            st === 'completed' ? 'border-green-500/30 bg-green-500/5' :
                            st === 'failed' ? 'border-red-500/30 bg-red-500/5' :
                            st === 'skipped' ? 'border-yellow-500/30 bg-yellow-500/5' :
                            'border-white/5 bg-gray-800/30',
                          )}>
                            <div className="h-4 flex items-center">
                              {st === 'completed' ? <CheckCircle2 className="h-3.5 w-3.5 text-green-400" /> :
                               st === 'running' ? <Loader2 className="h-3.5 w-3.5 text-nexus-400 animate-spin" /> :
                               st === 'failed' ? <XCircle className="h-3.5 w-3.5 text-red-400" /> :
                               st === 'skipped' ? <AlertTriangle className="h-3.5 w-3.5 text-yellow-400" /> :
                               <div className="h-2 w-2 rounded-full bg-gray-600" />}
                            </div>
                            <span className={clsx('text-[10px] font-medium leading-tight', isCurrent ? 'text-nexus-300' : 'text-gray-400')}>{label}</span>
                            <span className="text-[9px] text-gray-600">{stage ? formatElapsed(stage.duration_ms) : '\u2014'}</span>
                          </div>
                        </div>
                      );
                    })}
                  </div>

                  {/* Confidence Meters */}
                  <div className="grid grid-cols-4 gap-3">
                    {(['transcript', 'visual', 'safety', 'overall'] as const).map(key => {
                      const val = confidence[key];
                      const label = key.charAt(0).toUpperCase() + key.slice(1);
                      const barCol = val === 100 ? 'bg-green-500' : val > 0 ? 'bg-nexus-500' : 'bg-gray-700';
                      return (
                        <div key={key}>
                          <div className="flex justify-between text-[10px] mb-1">
                            <span className="text-gray-400">{label}</span>
                            <span className="text-gray-500">{val}%</span>
                          </div>
                          <div className="w-full bg-gray-800 rounded-full h-1.5">
                            <div className={clsx(barCol, 'h-1.5 rounded-full transition-all duration-500')} style={{ width: `${val}%` }} />
                          </div>
                        </div>
                      );
                    })}
                  </div>

                  {/* Operator Truth Panel */}
                  <div className="bg-gray-800/50 rounded-lg px-3 py-2 flex flex-wrap items-center gap-x-4 gap-y-1 text-[10px]">
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

                  {/* Progress bar */}
                  <div className="w-full bg-gray-800 rounded-full h-1.5">
                    <div className={clsx(barColor, 'h-1.5 rounded-full transition-all duration-500')} style={{ width: `${Math.max(job.progress_percent, 2)}%` }} />
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
                    <p className="text-sm font-medium text-white truncate">{job.filename}</p>
                    <p className="text-[10px] text-gray-500 mt-0.5">
                      {job.type === 'audio' ? 'Audio' : 'Video'} &middot; Stage: <span className={statusColor}>{CANONICAL_STAGE_META[job.current_stage] || job.current_stage}</span>
                      {job.artifact_id && <> &middot; Canonical: <code className="text-blue-400 font-mono">{job.artifact_id.slice(0, 12)}&hellip;</code></>}
                    </p>
                  </div>
                  <div className="flex items-center gap-2 shrink-0">
                    <button className="text-gray-500 hover:text-white transition-colors p-1" title="Copy Workflow ID" onClick={() => copyJobId(job.job_id)}>
                      {copiedJobId === job.job_id ? <CheckCircle2 className="h-3.5 w-3.5 text-green-400" /> : <Copy className="h-3.5 w-3.5" />}
                    </button>
                    <button className="text-gray-500 hover:text-red-400 transition-colors p-1" title="Dismiss" onClick={() => setTrackedJobs(prev => prev.filter(j => j.job_id !== job.job_id))}>
                      <XCircle className="h-3.5 w-3.5" />
                    </button>
                  </div>
                </div>
                <div className="w-full bg-gray-800 rounded-full h-1.5">
                  <div className={clsx(barColor, 'h-1.5 rounded-full transition-all duration-500')} style={{ width: `${Math.max(job.progress_percent, isActive ? 2 : 0)}%` }} />
                </div>
                <div className="flex justify-between mt-1">
                  <span className="text-[10px] text-gray-500">
                    {job.status === 'failed' && job.error
                      ? `Error: ${job.error.slice(0, 80)}`
                      : job.quality_gate_passed === false
                        ? `Quality gate: ${job.quality_gate_outcome || 'failed'} \u2014 processing degraded`
                        : job.degraded_stages && job.degraded_stages.length > 0
                          ? `Needs review \u2014 skipped: ${job.degraded_stages.map(d => CANONICAL_STAGE_META[d] || d).join(', ')}`
                          : `${Math.round(job.progress_percent)}% complete`}
                  </span>
                </div>
              </div>
            );
          })}
        </div>
      )}

      {/* ── Job Lookup ────────────────────────────────────── */}
      <div className="card px-5 py-4">
        <h2 className="text-sm font-semibold text-gray-300 mb-3">Track Job by ID</h2>
        <div className="flex gap-2">
          <div className="relative flex-1">
            <Search className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-gray-500" />
            <input
              type="text"
              className="w-full bg-gray-800 border border-white/10 text-sm text-white rounded-lg pl-9 pr-3 py-2 placeholder-gray-500 focus:outline-none focus:border-nexus-500"
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
          <div className="mt-3 p-3 bg-gray-800/50 rounded-lg border border-white/5">
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
              <span className="text-sm font-medium text-white">{lookupResult.filename}</span>
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
              <div><span className="text-gray-500">Job ID:</span> <span className="text-gray-300 font-mono">{lookupResult.job_id}</span></div>
              <div><span className="text-gray-500">Type:</span> <span className="text-gray-300">{lookupResult.type}</span></div>
              <div><span className="text-gray-500">Stage:</span> <span className="text-gray-300">{lookupResult.current_stage}</span></div>
              <div><span className="text-gray-500">Progress:</span> <span className="text-gray-300">{Math.round(lookupResult.progress_percent)}%</span></div>
              {lookupResult.created_at && (
                <div><span className="text-gray-500">Started:</span> <span className="text-gray-300">{new Date(lookupResult.created_at).toLocaleString()}</span></div>
              )}
              {lookupResult.error && (
                <div className="col-span-2"><span className="text-gray-500">Error:</span> <span className="text-red-400">{lookupResult.error}</span></div>
              )}
            </div>
            {lookupResult.status !== 'completed' && lookupResult.status !== 'failed' && lookupResult.status !== 'needs_review' && (
              <div className="w-full bg-gray-700 rounded-full h-1.5 mt-2">
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
                <h3 className="text-lg font-semibold text-white">{session.title}</h3>
              </div>
              <div className="flex items-center gap-2 text-xs text-gray-400">
                <Clock className="h-3.5 w-3.5" />
                {formatDuration(session.duration_seconds ?? 0)}
              </div>
            </div>

            <div className="mt-4 flex flex-wrap items-center gap-6">
              <div className="flex items-center gap-2">
                <Users className="h-4 w-4 text-gray-400" />
                <span className="text-sm text-gray-300">{session.participants.length} participants</span>
                <div className="flex -space-x-2 ml-1">
                  {session.participants.slice(0, 5).map((p: SessionParticipant) => (
                    <div
                      key={p.speaker_id}
                      className="flex h-7 w-7 items-center justify-center rounded-full bg-gradient-to-br from-nexus-500 to-purple-600 text-[10px] font-bold text-white ring-2 ring-gray-900"
                      title={p.name}
                    >
                      {p.name.charAt(0)}
                    </div>
                  ))}
                  {session.participants.length > 5 && (
                    <div className="flex h-7 w-7 items-center justify-center rounded-full bg-gray-700 text-[10px] font-bold text-gray-300 ring-2 ring-gray-900">
                      +{session.participants.length - 5}
                    </div>
                  )}
                </div>
              </div>

              <div className="flex gap-5 text-sm">
                <div className="flex items-center gap-1.5 text-yellow-400">
                  <Brain className="h-4 w-4" />
                  <span className="font-semibold">{session.rules_extracted}</span>
                  <span className="text-gray-500">insights</span>
                </div>
                <div className="flex items-center gap-1.5 text-blue-400">
                  <BarChart3 className="h-4 w-4" />
                  <span className="font-semibold">{session.confidence_score}%</span>
                  <span className="text-gray-500">confidence</span>
                </div>
              </div>
            </div>
          </div>

          {/* Live participant bars */}
          <div className="px-6 py-3 bg-gray-900/60 flex items-center justify-between text-xs">
            <div className="flex gap-3">
              {session.participants.slice(0, 3).map((p: SessionParticipant) => (
                <span key={p.speaker_id} className="text-gray-400">
                  <span className="text-gray-200 font-medium">{p.name.split(' ')[0]}</span> — {p.rules_contributed || 0} insights
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
          icon={<FileAudio className="h-7 w-7 text-gray-500" />}
          title="No sessions yet"
          description="Upload a recording or start a live session to begin extracting knowledge."
        />
      ) : (
      <div>
        <h2 className="text-sm font-semibold text-gray-300 mb-3">Recent Sessions</h2>
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
                      <p className="text-sm font-medium text-white truncate group-hover:text-nexus-400 transition-colors">
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
                    <div className="mt-1 flex items-center gap-4 text-xs text-gray-500">
                      <span>{formatDate(session.created_at)}</span>
                      {hasArtifact && artifact.source_filename ? (
                        <span className="flex items-center gap-1 text-gray-400">
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
                        <p className="text-[10px] text-gray-500">Quality</p>
                      </div>
                    ) : (
                      <div>
                        <p className="text-lg font-bold text-white">{session.rules_extracted}</p>
                        <p className="text-[10px] text-gray-500">Insights</p>
                      </div>
                    )}
                    <div>
                      <p className="text-lg font-bold text-white">{session.tests_generated}</p>
                      <p className="text-[10px] text-gray-500">Documents</p>
                    </div>
                    {hasArtifact && artifact.processing_time_seconds != null ? (
                      <div>
                        <p className="text-lg font-bold text-gray-300">{formatProcessingTime(artifact.processing_time_seconds)}</p>
                        <p className="text-[10px] text-gray-500">Processing</p>
                      </div>
                    ) : (
                      <div>
                        <p className="text-lg font-bold text-nexus-400">{session.confidence_score}%</p>
                        <p className="text-[10px] text-gray-500">Confidence</p>
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
                        className="p-1.5 rounded hover:bg-yellow-500/20 text-gray-500 hover:text-yellow-400 transition-colors"
                        title="Cancel processing"
                      >
                        <Ban className="h-4 w-4" />
                      </button>
                    )}
                    <button
                      onClick={(e) => handleDeleteSession(e, session.session_id)}
                      className="p-1.5 rounded hover:bg-red-500/20 text-gray-500 hover:text-red-400 transition-colors"
                      title="Delete session"
                    >
                      <Trash2 className="h-4 w-4" />
                    </button>
                  </div>

                  <ChevronRight className="h-4 w-4 text-gray-600 group-hover:text-nexus-400 transition-colors shrink-0" />
                </div>
              );
            })}
        </div>
      </div>
      )}
    </div>
  );
}
