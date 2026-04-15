// ═══════════════════════════════════════════════════════════════
//  Canonical Result Page — The Product Reveal
//
//  Route: /sessions/:sessionId/canonical
//  Loads data via the adapter (Phase 3A) and renders 5 sections:
//    1. Canonical Hero (quality, provenance, semantic badges)
//    2. Evidence Story (transcript + visual stats, honest framing)
//    3. Trust Panel (quality gate, score breakdown, model provenance)
//    4. Processing Timeline (7 stages, parallel paths, durations)
//    5. Launchpad (8 action cards with honest state badges)
// ═══════════════════════════════════════════════════════════════

import { useState, useEffect } from 'react';
import { useParams, useNavigate, Link } from 'react-router-dom';
import { useAuth } from '../contexts/AuthContext';
import api from '../services/api';
import { buildCanonicalAssetViewModel } from '../adapters/canonicalAssetAdapter';
import { StatusBadge } from '../components/StatusBadge';
import type {
  CanonicalAssetViewModel,
  CanonicalLaunchAction,
  QualityScoreBreakdown,
  WorkflowTimelineEntry,
  StallEvent,
} from '../types/canonical';
import clsx from 'clsx';
import {
  ArrowLeft,
  Award,
  ShieldCheck,
  Eye,
  Mic,
  AlertTriangle,
  Clock,
  FileText,
  Users,
  BarChart3,
  CheckCircle2,
  XCircle,
  Loader2,
  Share2,
  Target,
  GitBranch,
  CheckSquare,
  PlayCircle,
  UserPlus,
  ClipboardCheck,
  Package,
  Zap,
  RefreshCw,
  Timer,
  Activity,
  ChevronRight,
  ExternalLink,
  Lock,
} from 'lucide-react';

// ── Helpers ────────────────────────────────────────────────

function qualityGrade(score: number | null): { label: string; color: string; variant: 'green' | 'blue' | 'yellow' | 'orange' | 'red' | 'gray' } {
  if (score == null) return { label: 'N/A', color: 'text-gray-400', variant: 'gray' };
  const pct = score * 100;
  if (pct >= 90) return { label: 'EXCELLENT', color: 'text-green-400', variant: 'green' };
  if (pct >= 75) return { label: 'GOOD', color: 'text-blue-400', variant: 'blue' };
  if (pct >= 50) return { label: 'FAIR', color: 'text-yellow-400', variant: 'yellow' };
  return { label: 'NEEDS REVIEW', color: 'text-orange-400', variant: 'orange' };
}

function formatDuration(seconds: number): string {
  if (seconds < 60) return `${Math.round(seconds)}s`;
  const m = Math.floor(seconds / 60);
  const s = Math.round(seconds % 60);
  if (m < 60) return `${m}m ${s}s`;
  const h = Math.floor(m / 60);
  return `${h}h ${m % 60}m`;
}

function formatTimestamp(iso: string | null): string {
  if (!iso) return '—';
  return new Date(iso).toLocaleString();
}

function provenanceText(vm: CanonicalAssetViewModel): string {
  if (vm.provenance === 'fresh') return 'Freshly produced';
  if (vm.provenance === 'cache_reuse') {
    return `Reused from cache (produced: ${formatTimestamp(vm.produced_at)})`;
  }
  if (vm.provenance === 'alias') {
    return `Aliased to this session (original session: ${vm.original_session_id ?? 'unknown'}, produced: ${formatTimestamp(vm.produced_at)})`;
  }
  return 'Unknown provenance';
}

function provenanceVariant(vm: CanonicalAssetViewModel): 'green' | 'purple' | 'blue' {
  if (vm.provenance === 'fresh') return 'green';
  if (vm.provenance === 'cache_reuse') return 'purple';
  return 'blue';
}

function outcomeVariant(outcome: string): 'green' | 'red' | 'orange' | 'gray' {
  if (outcome === 'pass') return 'green';
  if (outcome === 'fail') return 'red';
  if (outcome === 'needs_review') return 'orange';
  return 'gray';
}

function outcomeLabel(outcome: string): string {
  if (outcome === 'pass') return 'PASSED';
  if (outcome === 'fail') return 'FAILED';
  if (outcome === 'needs_review') return 'NEEDS REVIEW';
  return outcome.toUpperCase();
}

function syncStateLabel(state: string): { label: string; variant: 'green' | 'yellow' | 'orange' } {
  if (state === 'synced') return { label: 'Synced', variant: 'green' };
  if (state === 'stale') return { label: 'Stale', variant: 'yellow' };
  return { label: 'Fallback', variant: 'orange' };
}

// Launch action icon mapping (lucide icons by name from adapter)
const LAUNCH_ICON_MAP: Record<string, React.ReactNode> = {
  'share-2': <Share2 className="h-5 w-5" />,
  'target': <Target className="h-5 w-5" />,
  'git-branch': <GitBranch className="h-5 w-5" />,
  'check-square': <CheckSquare className="h-5 w-5" />,
  'play-circle': <PlayCircle className="h-5 w-5" />,
  'user-plus': <UserPlus className="h-5 w-5" />,
  'clipboard-check': <ClipboardCheck className="h-5 w-5" />,
  'package': <Package className="h-5 w-5" />,
};

// Canonical 7-stage definitions for timeline rendering
const CANONICAL_STAGES = [
  { id: 'media_probe', label: 'Media Probe', parallel: null },
  { id: 'audio_transcription', label: 'Audio Transcription', parallel: 'audio' },
  { id: 'visual_extraction', label: 'Visual Extraction', parallel: 'visual' },
  { id: 'pii_redaction', label: 'PII Redaction', parallel: null },
  { id: 'visual_graph_assembly', label: 'Visual Graph', parallel: null },
  { id: 'artifact_persistence', label: 'Artifact Save', parallel: null },
  { id: 'canonical_quality_gate', label: 'Quality Gate', parallel: null },
] as const;

// ── Score Bar Component ────────────────────────────────────

function ScoreBar({ label, value }: { label: string; value: number | null }) {
  const pct = value != null ? Math.round(value * 100) : 0;
  const barColor =
    pct >= 80 ? 'bg-green-500' :
    pct >= 50 ? 'bg-yellow-500' :
    pct > 0 ? 'bg-orange-500' : 'bg-gray-700';
  return (
    <div>
      <div className="flex justify-between text-xs mb-1">
        <span className="text-gray-400">{label}</span>
        <span className="text-gray-300 font-mono">{value != null ? `${pct}%` : '—'}</span>
      </div>
      <div className="w-full bg-gray-800 rounded-full h-2">
        <div
          className={clsx(barColor, 'h-2 rounded-full transition-all duration-700')}
          style={{ width: `${pct}%` }}
        />
      </div>
    </div>
  );
}

// ── Launch Action Card ─────────────────────────────────────

function LaunchCard({ action, navigate }: { action: CanonicalLaunchAction; navigate: (path: string) => void }) {
  const icon = LAUNCH_ICON_MAP[action.icon] ?? <ExternalLink className="h-5 w-5" />;
  const isDisabled = action.state === 'coming_next';
  const stateConfig: Record<string, { label: string; variant: 'green' | 'blue' | 'yellow' | 'gray' }> = {
    ready: { label: 'Ready', variant: 'green' },
    preview_only: { label: 'Preview', variant: 'blue' },
    requires_mapping: { label: 'Needs Mapping', variant: 'yellow' },
    coming_next: { label: 'Coming Next', variant: 'gray' },
  };
  const badge = stateConfig[action.state] ?? stateConfig.coming_next;

  return (
    <button
      disabled={isDisabled}
      onClick={() => !isDisabled && navigate(action.route)}
      className={clsx(
        'text-left rounded-xl border p-4 transition-all group',
        isDisabled
          ? 'border-white/5 bg-gray-900/30 opacity-50 cursor-not-allowed'
          : 'border-white/10 bg-gray-800/40 hover:border-nexus-500/40 hover:bg-gray-800/70 cursor-pointer',
      )}
    >
      <div className="flex items-start gap-3">
        <div className={clsx('shrink-0 mt-0.5', isDisabled ? 'text-gray-600' : 'text-nexus-400 group-hover:text-nexus-300')}>
          {isDisabled ? <Lock className="h-5 w-5" /> : icon}
        </div>
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 mb-1">
            <span className={clsx('text-sm font-semibold', isDisabled ? 'text-gray-500' : 'text-white group-hover:text-nexus-300')}>
              {action.label}
            </span>
            <StatusBadge label={badge.label} variant={badge.variant} />
          </div>
          <p className="text-xs text-gray-500 leading-relaxed">{action.description}</p>
          {action.reason && action.state !== 'ready' && (
            <p className="text-[10px] text-gray-600 mt-1 italic">{action.reason}</p>
          )}
        </div>
        {!isDisabled && <ChevronRight className="h-4 w-4 text-gray-600 group-hover:text-nexus-400 shrink-0 mt-1" />}
      </div>
    </button>
  );
}

// ═══════════════════════════════════════════════════════════════
//  Main Page Component
// ═══════════════════════════════════════════════════════════════

export default function CanonicalResultPage() {
  const { sessionId } = useParams<{ sessionId: string }>();
  const navigate = useNavigate();
  const { user } = useAuth();
  const tenantId = user?.tenant_id ?? '';

  const [vm, setVm] = useState<CanonicalAssetViewModel | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!sessionId || !tenantId) return;
    let cancelled = false;
    const sid = sessionId;
    const tid = tenantId;

    async function load() {
      setLoading(true);
      setError(null);
      try {
        // First resolve the artifact_id from session artifacts
        const artifacts = await api.listSessionArtifacts(sid, tid);
        if (!artifacts || artifacts.length === 0) {
          throw new Error('No canonical artifact found for this session');
        }
        // Use the first (most recent) artifact
        const artifactId = artifacts[0].artifact_id;
        const viewModel = await buildCanonicalAssetViewModel(artifactId, sid, tid);
        if (!cancelled) setVm(viewModel);
      } catch (err: unknown) {
        if (!cancelled) {
          setError(err instanceof Error ? err.message : 'Failed to load canonical asset');
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    }

    load();
    return () => { cancelled = true; };
  }, [sessionId, tenantId]);

  // ── Loading state ──────────────────────────────────────────
  if (loading) {
    return (
      <div className="flex items-center justify-center min-h-[60vh]">
        <div className="flex flex-col items-center gap-4">
          <Loader2 className="h-8 w-8 text-nexus-400 animate-spin" />
          <p className="text-sm text-gray-400">Loading canonical asset&hellip;</p>
        </div>
      </div>
    );
  }

  // ── Error state ────────────────────────────────────────────
  if (error || !vm) {
    return (
      <div className="flex items-center justify-center min-h-[60vh]">
        <div className="flex flex-col items-center gap-4 text-center max-w-md">
          <XCircle className="h-8 w-8 text-red-400" />
          <p className="text-sm text-red-400">{error ?? 'Failed to load canonical asset'}</p>
          <button
            onClick={() => navigate(-1)}
            className="text-xs text-gray-400 hover:text-white transition-colors flex items-center gap-1"
          >
            <ArrowLeft className="h-3.5 w-3.5" /> Go back
          </button>
        </div>
      </div>
    );
  }

  const grade = qualityGrade(vm.quality_score);

  // ── Timeline stage lookup helper ───────────────────────────
  function getTimelineStage(stageId: string): { status: string; duration_ms: number; started_at: string | null; completed_at: string | null } | null {
    // Scan timeline entries for stage start/complete events
    let started_at: string | null = null;
    let completed_at: string | null = null;
    let status = 'pending';
    let duration_ms = 0;

    for (const entry of vm!.timeline) {
      const ev = entry.event?.toLowerCase() ?? '';
      const det = entry.detail?.toLowerCase() ?? '';
      const mentionsStage = ev.includes(stageId) || det.includes(stageId);
      if (!mentionsStage) continue;

      if (ev.includes('start') || det.includes('started')) {
        started_at = entry.timestamp;
        status = 'running';
      }
      if (ev.includes('complete') || det.includes('completed') || det.includes('finished')) {
        completed_at = entry.timestamp;
        status = 'completed';
      }
      if (ev.includes('skip') || det.includes('skipped')) {
        status = 'skipped';
      }
      if (ev.includes('fail') || det.includes('failed') || det.includes('error')) {
        status = 'failed';
      }
      if (ev.includes('degrad') || det.includes('degraded')) {
        status = 'degraded';
      }
    }

    if (started_at && completed_at) {
      duration_ms = new Date(completed_at).getTime() - new Date(started_at).getTime();
    }

    if (status === 'pending' && vm!.degraded_stages.includes(stageId)) status = 'degraded';
    if (status === 'pending' && vm!.skipped_stages.includes(stageId)) status = 'skipped';

    return { status, duration_ms, started_at, completed_at };
  }

  // Compute wall time from timeline
  const timelineStarted = vm.timeline.length > 0 ? vm.timeline[0].timestamp : vm.created_at;
  const timelineEnded = vm.timeline.length > 0 ? vm.timeline[vm.timeline.length - 1].timestamp : vm.completed_at;
  const wallTimeMs = timelineStarted && timelineEnded
    ? new Date(timelineEnded).getTime() - new Date(timelineStarted).getTime()
    : vm.processing_time_seconds * 1000;

  return (
    <div className="space-y-6 pb-12">
      {/* Back navigation */}
      <button
        onClick={() => navigate('/sessions')}
        className="flex items-center gap-1.5 text-xs text-gray-500 hover:text-white transition-colors"
      >
        <ArrowLeft className="h-3.5 w-3.5" />
        Back to Sessions
      </button>

      {/* ═══════════════════════════════════════════════════════ */}
      {/* Section 1: Canonical Hero                               */}
      {/* ═══════════════════════════════════════════════════════ */}
      <div className="card p-6 space-y-5">
        {/* Needs Review Banner */}
        {vm.quality_outcome === 'needs_review' && vm.review_reasons.length > 0 && (
          <div className="bg-orange-500/10 border border-orange-500/30 rounded-lg px-4 py-3">
            <div className="flex items-start gap-2">
              <AlertTriangle className="h-4 w-4 text-orange-400 shrink-0 mt-0.5" />
              <div>
                <p className="text-sm font-medium text-orange-300">This asset needs review</p>
                <ul className="mt-1 space-y-0.5">
                  {vm.review_reasons.map((reason, i) => (
                    <li key={i} className="text-xs text-orange-400/80">&bull; {reason}</li>
                  ))}
                </ul>
              </div>
            </div>
          </div>
        )}

        {/* Quality Score + Grade */}
        <div className="flex items-start justify-between gap-4">
          <div>
            <div className="flex items-center gap-3">
              <div className={clsx('text-4xl font-black tabular-nums', grade.color)}>
                {vm.quality_score != null ? `${(vm.quality_score * 100).toFixed(0)}%` : '—'}
              </div>
              <div>
                <StatusBadge label={grade.label} variant={grade.variant} />
                <p className="text-xs text-gray-500 mt-1">Canonical Quality Score</p>
              </div>
            </div>
          </div>
          <div className="text-right text-xs text-gray-500 space-y-0.5 shrink-0">
            <p>Session: <code className="text-nexus-400 font-mono">{vm.session_id.slice(0, 12)}&hellip;</code></p>
            {vm.workflow_id && <p>Workflow: <code className="text-purple-400 font-mono">{vm.workflow_id.slice(0, 12)}&hellip;</code></p>}
            <p>Artifact: <code className="text-blue-400 font-mono">{vm.artifact_id.slice(0, 12)}&hellip;</code></p>
          </div>
        </div>

        {/* Source Info */}
        <div className="flex flex-wrap items-center gap-x-5 gap-y-2 text-xs text-gray-400">
          {vm.source_filename && (
            <span className="flex items-center gap-1.5">
              <FileText className="h-3.5 w-3.5" /> {vm.source_filename}
            </span>
          )}
          {vm.duration_seconds > 0 && (
            <span className="flex items-center gap-1.5">
              <Clock className="h-3.5 w-3.5" /> {formatDuration(vm.duration_seconds)}
            </span>
          )}
          {vm.processing_time_seconds > 0 && (
            <span className="flex items-center gap-1.5">
              <Timer className="h-3.5 w-3.5" /> Processed in {formatDuration(vm.processing_time_seconds)}
            </span>
          )}
          {vm.source_type && (
            <span className="flex items-center gap-1.5">
              {vm.source_type === 'audio' ? <Mic className="h-3.5 w-3.5" /> : <Eye className="h-3.5 w-3.5" />}
              {vm.source_type.charAt(0).toUpperCase() + vm.source_type.slice(1)}
            </span>
          )}
        </div>

        {/* Semantic Badges + Provenance */}
        <div className="flex flex-wrap items-center gap-2">
          {vm.has_real_transcript && (
            <StatusBadge label="REAL TRANSCRIPT" variant="green" icon={<Mic className="h-3 w-3" />} />
          )}
          {vm.has_visual_semantics && (
            <StatusBadge label="REAL VISUAL" variant="blue" icon={<Eye className="h-3 w-3" />} />
          )}
          {vm.score_breakdown.pii != null && vm.score_breakdown.pii > 0 && (
            <StatusBadge label="PII SAFE" variant="purple" icon={<ShieldCheck className="h-3 w-3" />} />
          )}
          <StatusBadge
            label={vm.provenance === 'fresh' ? 'Freshly Produced' : vm.provenance === 'cache_reuse' ? 'Cache Reuse' : 'Aliased'}
            variant={provenanceVariant(vm)}
            icon={vm.cache_hit ? <Zap className="h-3 w-3" /> : undefined}
          />
        </div>

        {/* Provenance detail (if cached or aliased) */}
        {vm.provenance !== 'fresh' && (
          <p className="text-[11px] text-gray-500 border-t border-white/5 pt-3">
            {provenanceText(vm)}
          </p>
        )}
      </div>

      {/* ═══════════════════════════════════════════════════════ */}
      {/* Section 2: Evidence Story                               */}
      {/* ═══════════════════════════════════════════════════════ */}
      <div className="card p-6 space-y-5">
        <h2 className="text-sm font-semibold text-gray-300">Extracted Evidence</h2>

        {/* Transcript */}
        <div className="space-y-3">
          <h3 className="text-xs font-medium text-gray-400 uppercase tracking-wider">Transcript Evidence</h3>
          <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
            <div className="bg-gray-800/40 rounded-lg px-3 py-2">
              <p className="text-lg font-bold text-white">{vm.transcript_word_count.toLocaleString()}</p>
              <p className="text-[10px] text-gray-500">Words</p>
            </div>
            <div className="bg-gray-800/40 rounded-lg px-3 py-2">
              <p className="text-lg font-bold text-white">{vm.transcript_segment_count}</p>
              <p className="text-[10px] text-gray-500">Segments</p>
            </div>
            <div className="bg-gray-800/40 rounded-lg px-3 py-2">
              <p className="text-lg font-bold text-white">{vm.speaker_count}</p>
              <p className="text-[10px] text-gray-500">Speakers</p>
            </div>
            <div className="bg-gray-800/40 rounded-lg px-3 py-2">
              <p className="text-lg font-bold text-white">{formatDuration(vm.duration_seconds)}</p>
              <p className="text-[10px] text-gray-500">Duration</p>
            </div>
          </div>
          {vm.safe_transcript_preview && (
            <div className="bg-gray-800/30 border border-white/5 rounded-lg p-4">
              <p className="text-xs text-gray-300 leading-relaxed whitespace-pre-wrap font-mono">
                {vm.safe_transcript_preview}
                {vm.safe_transcript_preview.length >= 500 && '…'}
              </p>
              <Link
                to={`/sessions/${vm.session_id}/replay`}
                className="inline-flex items-center gap-1 text-[11px] text-nexus-400 hover:text-nexus-300 mt-3 transition-colors"
              >
                View Full Transcript <ChevronRight className="h-3 w-3" />
              </Link>
            </div>
          )}
        </div>

        {/* Visual */}
        <div className="space-y-3 border-t border-white/5 pt-5">
          <h3 className="text-xs font-medium text-gray-400 uppercase tracking-wider">Detected Visual Evidence</h3>
          <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
            <div className="bg-gray-800/40 rounded-lg px-3 py-2">
              <p className="text-lg font-bold text-white">{vm.frame_count}</p>
              <p className="text-[10px] text-gray-500">Frames Extracted</p>
            </div>
            <div className="bg-gray-800/40 rounded-lg px-3 py-2">
              <p className="text-lg font-bold text-white">{vm.scene_count}</p>
              <p className="text-[10px] text-gray-500">Scenes Detected</p>
            </div>
            <div className="bg-gray-800/40 rounded-lg px-3 py-2">
              <p className="text-lg font-bold text-white">{vm.visual_graph?.edges?.length ?? 0}</p>
              <p className="text-[10px] text-gray-500">Transitions</p>
            </div>
            <div className="bg-gray-800/40 rounded-lg px-3 py-2">
              <p className="text-lg font-bold text-white">{vm.application_types_seen.length}</p>
              <p className="text-[10px] text-gray-500">App Types</p>
            </div>
          </div>

          {/* Application types detected */}
          {vm.application_types_seen.length > 0 && (
            <div>
              <p className="text-[11px] text-gray-500 mb-1.5">Detected application types:</p>
              <div className="flex flex-wrap gap-1.5">
                {vm.application_types_seen.map((appType) => (
                  <StatusBadge key={appType} label={appType} variant="blue" />
                ))}
              </div>
            </div>
          )}

          {/* Visual summary (honest framing — aggregate, not inflated claims) */}
          {vm.visual_summary && (
            <div className="bg-gray-800/30 border border-white/5 rounded-lg p-4">
              <p className="text-[11px] text-gray-500 mb-1">Visual analysis summary (OCR, scene transitions, aggregate observations):</p>
              <p className="text-xs text-gray-300 leading-relaxed">{vm.visual_summary}</p>
            </div>
          )}
        </div>
      </div>

      {/* ═══════════════════════════════════════════════════════ */}
      {/* Section 3: Trust Panel                                  */}
      {/* ═══════════════════════════════════════════════════════ */}
      <div className="card p-6 space-y-5">
        <h2 className="text-sm font-semibold text-gray-300">Trust &amp; Quality Gate</h2>

        {/* Quality Gate Outcome */}
        <div className="flex items-center gap-3">
          <StatusBadge
            label={outcomeLabel(vm.quality_outcome)}
            variant={outcomeVariant(vm.quality_outcome)}
            icon={vm.quality_outcome === 'pass' ? <CheckCircle2 className="h-3 w-3" /> : vm.quality_outcome === 'fail' ? <XCircle className="h-3 w-3" /> : <AlertTriangle className="h-3 w-3" />}
          />
          {vm.semantic_completeness_score != null && (
            <span className="text-xs text-gray-500">
              Semantic completeness: <span className="text-gray-300 font-mono">{(vm.semantic_completeness_score * 100).toFixed(0)}%</span>
            </span>
          )}
        </div>

        {/* Score Breakdown */}
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
          <ScoreBar label="Transcript" value={vm.score_breakdown.transcript} />
          <ScoreBar label="Visual" value={vm.score_breakdown.visual} />
          <ScoreBar label="PII Redaction" value={vm.score_breakdown.pii} />
          <ScoreBar label="Completeness" value={vm.score_breakdown.completeness} />
        </div>

        {/* Model Provenance Table */}
        {Object.keys(vm.model_provenance).length > 0 && (
          <div className="border-t border-white/5 pt-4">
            <h3 className="text-xs font-medium text-gray-400 uppercase tracking-wider mb-2">Model Provenance</h3>
            <div className="overflow-x-auto">
              <table className="w-full text-xs">
                <thead>
                  <tr className="text-gray-500 border-b border-white/5">
                    <th className="text-left py-1.5 pr-6 font-medium">Dimension</th>
                    <th className="text-left py-1.5 font-medium">Model</th>
                  </tr>
                </thead>
                <tbody>
                  {Object.entries(vm.model_provenance).map(([dimension, model]) => (
                    <tr key={dimension} className="border-b border-white/5 last:border-0">
                      <td className="py-1.5 pr-6 text-gray-400">{dimension}</td>
                      <td className="py-1.5 text-gray-300 font-mono">{model}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        )}

        {/* Review Reasons */}
        {vm.review_reasons.length > 0 && (
          <div className="border-t border-white/5 pt-4">
            <h3 className="text-xs font-medium text-gray-400 uppercase tracking-wider mb-2">Review Reasons</h3>
            <ul className="space-y-1">
              {vm.review_reasons.map((reason, i) => (
                <li key={i} className="text-xs text-orange-400/90 flex items-start gap-1.5">
                  <AlertTriangle className="h-3 w-3 shrink-0 mt-0.5" />
                  {reason}
                </li>
              ))}
            </ul>
          </div>
        )}

        {/* Operator Truth */}
        <div className="border-t border-white/5 pt-4">
          <h3 className="text-xs font-medium text-gray-400 uppercase tracking-wider mb-2">Operator Truth</h3>
          <div className="flex flex-wrap items-center gap-x-5 gap-y-2 text-xs">
            <span className="flex items-center gap-1.5">
              <Activity className="h-3.5 w-3.5 text-gray-500" />
              Workflow Sync: <StatusBadge label={syncStateLabel(vm.workflow_sync_state).label} variant={syncStateLabel(vm.workflow_sync_state).variant} />
            </span>
            <span className="flex items-center gap-1.5 text-gray-400">
              <RefreshCw className="h-3.5 w-3.5" />
              Retries: <span className="text-gray-300 font-mono">{vm.retry_count}</span>
            </span>
            {vm.degraded_stages.length > 0 && (
              <span className="flex items-center gap-1.5 text-orange-400">
                <AlertTriangle className="h-3.5 w-3.5" />
                Degraded: {vm.degraded_stages.join(', ')}
              </span>
            )}
            {vm.skipped_stages.length > 0 && (
              <span className="flex items-center gap-1.5 text-yellow-400">
                <AlertTriangle className="h-3.5 w-3.5" />
                Skipped: {vm.skipped_stages.join(', ')}
              </span>
            )}
          </div>
          {vm.stall_events.length > 0 && (
            <div className="mt-2 space-y-0.5">
              {vm.stall_events.map((stall, i) => (
                <p key={i} className="text-[11px] text-red-400/80 flex items-center gap-1">
                  <Timer className="h-3 w-3" />
                  Stall at {stall.stage}: {stall.duration_seconds > 0 ? `${stall.duration_seconds.toFixed(1)}s` : 'detected'}
                </p>
              ))}
            </div>
          )}
        </div>
      </div>

      {/* ═══════════════════════════════════════════════════════ */}
      {/* Section 4: Processing Timeline                          */}
      {/* ═══════════════════════════════════════════════════════ */}
      <div className="card p-6 space-y-5">
        <div className="flex items-center justify-between">
          <h2 className="text-sm font-semibold text-gray-300">Processing Timeline</h2>
          <div className="flex items-center gap-4 text-xs text-gray-500">
            <span>Start: {formatTimestamp(timelineStarted)}</span>
            <span>End: {formatTimestamp(timelineEnded)}</span>
            <span className="text-gray-300 font-medium">Wall: {formatDuration(wallTimeMs / 1000)}</span>
          </div>
        </div>

        {/* Stage visualization */}
        <div className="space-y-2">
          {CANONICAL_STAGES.map((stageDef) => {
            const info = getTimelineStage(stageDef.id);
            const st = info?.status ?? 'pending';
            const dur = info?.duration_ms ?? 0;
            const maxDur = Math.max(wallTimeMs, 1);
            const barWidth = dur > 0 ? Math.max((dur / maxDur) * 100, 3) : 0;

            const statusIcon =
              st === 'completed' ? <CheckCircle2 className="h-3.5 w-3.5 text-green-400" /> :
              st === 'running' ? <Loader2 className="h-3.5 w-3.5 text-nexus-400 animate-spin" /> :
              st === 'failed' ? <XCircle className="h-3.5 w-3.5 text-red-400" /> :
              st === 'skipped' ? <AlertTriangle className="h-3.5 w-3.5 text-yellow-400" /> :
              st === 'degraded' ? <AlertTriangle className="h-3.5 w-3.5 text-orange-400" /> :
              <div className="h-2.5 w-2.5 rounded-full bg-gray-600 ml-0.5" />;

            const barColor =
              st === 'completed' ? 'bg-green-500/70' :
              st === 'failed' ? 'bg-red-500/70' :
              st === 'skipped' ? 'bg-yellow-500/40' :
              st === 'degraded' ? 'bg-orange-500/60' :
              'bg-gray-700/50';

            return (
              <div key={stageDef.id} className="flex items-center gap-3">
                <div className="w-5 flex justify-center shrink-0">{statusIcon}</div>
                <div className="w-36 shrink-0">
                  <span className="text-xs text-gray-300">{stageDef.label}</span>
                  {stageDef.parallel && (
                    <span className="text-[9px] text-gray-600 ml-1.5">({stageDef.parallel})</span>
                  )}
                </div>
                <div className="flex-1 bg-gray-800/60 rounded-full h-3 overflow-hidden">
                  {barWidth > 0 && (
                    <div
                      className={clsx(barColor, 'h-3 rounded-full transition-all duration-500')}
                      style={{ width: `${barWidth}%` }}
                    />
                  )}
                </div>
                <span className="w-16 text-right text-[11px] text-gray-500 font-mono shrink-0">
                  {dur > 0 ? formatDuration(dur / 1000) : '—'}
                </span>
              </div>
            );
          })}
        </div>

        {/* Parallel path indicator */}
        <div className="flex items-center gap-2 text-[10px] text-gray-600 border-t border-white/5 pt-3">
          <span className="inline-flex items-center gap-1"><span className="w-2 h-2 rounded-full bg-blue-500/40 inline-block" /> audio path</span>
          <span className="inline-flex items-center gap-1"><span className="w-2 h-2 rounded-full bg-purple-500/40 inline-block" /> visual path</span>
          <span className="ml-auto text-gray-500">Audio transcription and visual extraction execute in parallel</span>
        </div>
      </div>

      {/* ═══════════════════════════════════════════════════════ */}
      {/* Section 5: Launchpad                                    */}
      {/* ═══════════════════════════════════════════════════════ */}
      <div className="card p-6 space-y-5">
        <div>
          <h2 className="text-sm font-semibold text-gray-300">What you can build from this asset</h2>
          <p className="text-xs text-gray-500 mt-0.5">Capture once, operationalize everywhere</p>
        </div>

        <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
          {vm.launch_actions.map((action) => (
            <LaunchCard key={action.id} action={action} navigate={navigate} />
          ))}
        </div>
      </div>
    </div>
  );
}
