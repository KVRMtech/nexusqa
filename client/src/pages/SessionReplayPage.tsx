// ═══════════════════════════════════════════════════════════════
//  MODULE 2 — SESSION REPLAY & INTELLIGENCE TIMELINE
//  Deep-dive analyst view — real session data, PII-safe transcript,
//  visual graph evidence. Companion to CanonicalResultPage.
// ═══════════════════════════════════════════════════════════════

import { useState, useEffect, useRef, useCallback, useMemo } from 'react';
import { useParams, Link, useNavigate } from 'react-router-dom';
import { useAuth } from '../contexts/AuthContext';
import api from '../services/api';
import { useApiData } from '../hooks/useApiData';
import { StatusBadge } from '../components/StatusBadge';
import { EmptyState } from '../components/EmptyState';
import type { IntelligenceEvent, TranscriptSegment, SessionParticipant, KTSession } from '../types';
import type { CanonicalArtifact, ArtifactTranscript } from '../types/canonical';
import {
  ArrowLeft,
  Brain,
  AlertTriangle,
  Link2,
  FlaskConical,
  Lightbulb,
  CheckSquare,
  Play,
  Pause,
  Volume2,
  ChevronRight,
  MonitorPlay,
  BarChart3,
  Clock,
  Users,
  Eye,
  MessageSquare,
  Flag,
  FileText,
  Mic,
  Timer,
  Loader2,
  Award,
  Layers,
  ArrowRight,
} from 'lucide-react';
import clsx from 'clsx';

// ── Empty fallbacks ─────────────────────────────────────────

const EMPTY_EVENTS: IntelligenceEvent[] = [];
const EMPTY_TRANSCRIPT: TranscriptSegment[] = [];

function formatTime(seconds: number): string {
  const m = Math.floor(seconds / 60);
  const s = Math.floor(seconds % 60);
  return `${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
}

function formatDuration(seconds: number): string {
  if (seconds < 60) return `${Math.round(seconds)}s`;
  const m = Math.floor(seconds / 60);
  const s = Math.round(seconds % 60);
  if (m < 60) return `${m}m ${s}s`;
  const h = Math.floor(m / 60);
  return `${h}h ${m % 60}m`;
}

function formatDate(iso: string): string {
  return new Date(iso).toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' });
}

function EventIcon({ type }: { type: IntelligenceEvent['event_type'] }) {
  switch (type) {
    case 'rule_extracted':
      return <Brain className="h-4 w-4 text-nexus-400" />;
    case 'contradiction':
      return <AlertTriangle className="h-4 w-4 text-red-400" />;
    case 'system_link':
      return <Link2 className="h-4 w-4 text-blue-400" />;
    case 'test_generated':
      return <FlaskConical className="h-4 w-4 text-green-400" />;
    case 'key_decision':
      return <Lightbulb className="h-4 w-4 text-yellow-400" />;
    case 'action_item':
      return <CheckSquare className="h-4 w-4 text-purple-400" />;
  }
}

function EventBadge({ type }: { type: IntelligenceEvent['event_type'] }) {
  const map: Record<string, [string, string]> = {
    rule_extracted: ['badge-nexus', 'RULE'],
    contradiction: ['badge-red', 'CONTRADICTION'],
    system_link: ['badge-blue', 'SYSTEM LINK'],
    test_generated: ['badge-green', 'TEST GENERATED'],
    key_decision: ['badge-yellow', 'DECISION'],
    action_item: ['badge-purple', 'ACTION ITEM'],
  };
  const [cls, label] = map[type] || ['badge-gray', type];
  return <span className={cls}>{label}</span>;
}

// ── Visual graph blob extraction helper ─────────────────────

interface VisualAnalysisBlob {
  frames?: Array<{ description?: string; timestamp?: number; ocr_text?: string }>;
  scenes?: Array<{ description?: string; start_time?: number; end_time?: number; transition?: string }>;
  application_types_seen?: string[];
  model_version?: string;
}

function extractVisualAnalysis(artifact: CanonicalArtifact | null): VisualAnalysisBlob {
  if (!artifact?.full_artifact_json) return {};
  const blob = artifact.full_artifact_json as Record<string, unknown>;
  return (blob.visual_analysis ?? {}) as VisualAnalysisBlob;
}

export default function SessionReplayPage() {
  const { sessionId } = useParams<{ sessionId: string }>();
  const navigate = useNavigate();
  const { user } = useAuth();
  const tenantId = user?.tenant_id ?? '';

  // ── Session detail (real data) ────────────────────────────
  const [session, setSession] = useState<KTSession | null>(null);
  const [sessionLoading, setSessionLoading] = useState(true);

  useEffect(() => {
    if (!sessionId) return;
    let cancelled = false;
    api.getSession(sessionId)
      .then((data: unknown) => { if (!cancelled) setSession(data as KTSession); })
      .catch(() => {})
      .finally(() => { if (!cancelled) setSessionLoading(false); });
    return () => { cancelled = true; };
  }, [sessionId]);

  // ── Canonical artifact (for PII-safe transcript + visual graph) ──
  const [artifact, setArtifact] = useState<CanonicalArtifact | null>(null);
  const [safeTranscriptText, setSafeTranscriptText] = useState<string>('');
  const [safeTranscriptSegments, setSafeTranscriptSegments] = useState<TranscriptSegment[]>([]);
  const [artifactLoading, setArtifactLoading] = useState(true);

  useEffect(() => {
    if (!sessionId || !tenantId) return;
    let cancelled = false;

    async function loadArtifact() {
      setArtifactLoading(true);
      try {
        // Step 1: Get artifact_id from the list endpoint (returns slim items)
        const artifacts = await api.listSessionArtifacts(sessionId!, tenantId);
        if (cancelled || !artifacts || artifacts.length === 0) {
          setArtifactLoading(false);
          return;
        }
        const artifactId = artifacts[0].artifact_id;

        // Step 2: Fetch the full artifact record (includes full_artifact_json)
        const art = await api.getArtifact(artifactId);
        if (cancelled) return;
        setArtifact(art);

        // Step 3: Load PII-safe transcript + parse segments from full blob
        try {
          const transcriptResp = await api.getArtifactTranscript(artifactId);
          if (!cancelled && transcriptResp.safe_transcript_text) {
            setSafeTranscriptText(transcriptResp.safe_transcript_text);
            // Parse raw transcript segments from full_artifact_json
            const blob = (art.full_artifact_json ?? {}) as Record<string, unknown>;
            const rawTranscript = blob.transcript as { segments?: Array<{ text?: string; speaker?: string; start?: number; end?: number; confidence?: number }> } | undefined;
            if (rawTranscript?.segments) {
              const segments: TranscriptSegment[] = rawTranscript.segments.map((seg) => ({
                start: seg.start ?? 0,
                end: seg.end ?? 0,
                speaker: seg.speaker ?? 'Unknown',
                text: seg.text ?? '',
                confidence: seg.confidence,
              }));
              if (!cancelled) setSafeTranscriptSegments(segments);
            }
          }
        } catch { /* transcript endpoint may not be available */ }
      } catch { /* no artifact for this session */ }
      if (!cancelled) setArtifactLoading(false);
    }

    loadArtifact();
    return () => { cancelled = true; };
  }, [sessionId, tenantId]);

  // ── Intelligence events (from session events endpoint) ────
  const { data: events, isLive } = useApiData(
    () => api.getSessionEvents(sessionId || ''),
    EMPTY_EVENTS,
    !!sessionId,
  );

  // ── Fallback transcript (original endpoint, if artifact transcript unavailable) ──
  const { data: legacyTranscript } = useApiData(
    () => api.getSessionTranscript(sessionId || ''),
    EMPTY_TRANSCRIPT,
    !!sessionId,
  );

  // ── Resolved values from real data ────────────────────────
  const participants = session?.participants ?? [];
  const totalDuration = session?.duration_seconds ?? artifact?.duration_seconds ?? 0;
  const sessionTitle = session?.title ?? 'Session Replay';
  const sessionDate = session?.created_at ? formatDate(session.created_at) : '';
  const rulesExtracted = session?.rules_extracted ?? 0;
  const contradictionsFound = session?.contradictions_found ?? 0;
  const testsGenerated = session?.tests_generated ?? 0;
  const confidenceScore = session?.confidence_score ?? 0;

  // Use artifact-backed transcript segments when available, fall back to legacy
  const transcript = safeTranscriptSegments.length > 0 ? safeTranscriptSegments : legacyTranscript;

  // Visual analysis from artifact
  const visualAnalysis = useMemo(() => extractVisualAnalysis(artifact), [artifact]);

  const [playing, setPlaying] = useState(false);
  const [currentTime, setCurrentTime] = useState(0);
  const [selectedEvent, setSelectedEvent] = useState<IntelligenceEvent | null>(null);
  const [filterType, setFilterType] = useState<string>('all');

  const filteredEvents = useMemo(() => {
    if (filterType === 'all') return events;
    return events.filter((e) => e.event_type === filterType);
  }, [filterType, events]);

  const jumpToTime = useCallback((seconds: number) => {
    setCurrentTime(seconds);
    setPlaying(false);
  }, []);

  // Calculate knowledge density
  const densityBars = useMemo(() => {
    if (totalDuration === 0) return new Array(40).fill(0) as number[];
    const buckets = new Array(40).fill(0);
    events.forEach((e) => {
      const bucket = Math.min(Math.floor(e.timestamp_seconds / (totalDuration / 40)), 39);
      buckets[bucket]++;
    });
    const max = Math.max(...buckets, 1);
    return buckets.map((v: number) => v / max);
  }, [events, totalDuration]);

  // Current transcript segments (window around playhead)
  const currentTranscript = transcript.filter(
    (seg) => seg.start <= currentTime + 60 && seg.end >= currentTime - 10,
  );

  return (
    <div className="space-y-4 animate-fade-in">
      {/* Breadcrumb */}
      <Link to="/sessions" className="inline-flex items-center gap-2 text-sm text-gray-400 hover:text-nexus-400 transition-colors">
        <ArrowLeft className="h-4 w-4" />
        Back to Sessions
      </Link>

      {/* Session Header */}
      <div className="card p-5">
        <div className="flex flex-col lg:flex-row lg:items-center lg:justify-between gap-4">
          <div>
            {sessionLoading ? (
              <div className="flex items-center gap-2">
                <Loader2 className="h-4 w-4 text-gray-500 animate-spin" />
                <span className="text-sm text-gray-500">Loading session&hellip;</span>
              </div>
            ) : (
              <>
                <h1 className="page-title">{sessionTitle}</h1>
                <div className="mt-2 flex flex-wrap items-center gap-4 text-sm text-gray-400">
                  {sessionDate && (
                    <span className="flex items-center gap-1.5"><Clock className="h-3.5 w-3.5" /> {sessionDate}{totalDuration > 0 ? ` — ${formatDuration(totalDuration)}` : ''}</span>
                  )}
                  {participants.length > 0 && (
                    <span className="flex items-center gap-1.5"><Users className="h-3.5 w-3.5" /> {participants.length} participant{participants.length !== 1 ? 's' : ''}</span>
                  )}
                  {rulesExtracted > 0 && (
                    <span className="flex items-center gap-1.5"><Brain className="h-3.5 w-3.5 text-nexus-400" /> {rulesExtracted} rules extracted</span>
                  )}
                  {contradictionsFound > 0 && (
                    <span className="flex items-center gap-1.5"><AlertTriangle className="h-3.5 w-3.5 text-yellow-400" /> {contradictionsFound} contradiction{contradictionsFound !== 1 ? 's' : ''}</span>
                  )}
                </div>
              </>
            )}
          </div>
          <div className="flex gap-3 shrink-0">
            {confidenceScore > 0 && <StatusBadge label={`${confidenceScore}% Confidence`} variant="green" />}
            {testsGenerated > 0 && <StatusBadge label={`${testsGenerated} Tests Generated`} variant="nexus" />}
            {artifact && (
              <button
                onClick={() => navigate(`/sessions/${sessionId}/canonical`)}
                className="flex items-center gap-1 px-2.5 py-1 rounded-md text-xs font-medium text-nexus-400 hover:bg-nexus-500/10 border border-nexus-500/30 transition-colors"
              >
                <Award className="h-3 w-3" /> View Canonical Asset <ArrowRight className="h-3 w-3" />
              </button>
            )}
          </div>
        </div>

        {/* Artifact info bar (when available) */}
        {artifact && (
          <div className="mt-3 flex flex-wrap items-center gap-3 text-xs text-gray-500 border-t border-white/5 pt-3">
            {artifact.source_filename && (
              <span className="flex items-center gap-1"><FileText className="h-3 w-3" /> {artifact.source_filename}</span>
            )}
            {artifact.has_real_transcript && <StatusBadge label="REAL TRANSCRIPT" variant="green" icon={<Mic className="h-2.5 w-2.5" />} />}
            {artifact.has_visual_semantics && <StatusBadge label="REAL VISUAL" variant="blue" icon={<Eye className="h-2.5 w-2.5" />} />}
            {artifact.brain_quality_score != null && (
              <span className="text-gray-400">Quality: <span className="text-gray-300 font-mono">{(artifact.brain_quality_score * 100).toFixed(0)}%</span></span>
            )}
            {artifact.processing_time_seconds > 0 && (
              <span className="flex items-center gap-1"><Timer className="h-3 w-3" /> Processed in {formatDuration(artifact.processing_time_seconds)}</span>
            )}
          </div>
        )}

        {/* Speakers */}
        {participants.length > 0 && (
          <div className="mt-4 flex gap-4">
            {participants.map((p) => (
              <div key={p.speaker_id} className="flex items-center gap-2">
                <div className="flex h-7 w-7 items-center justify-center rounded-full bg-gradient-to-br from-nexus-500 to-purple-600 text-[10px] font-bold text-white">
                  {p.name.charAt(0)}
                </div>
                <div>
                  <p className="text-xs font-medium text-gray-200">{p.name}</p>
                  <p className="text-[10px] text-gray-500">{p.role ?? 'Participant'} — {Math.floor((p.speaking_time_seconds || 0) / 60)}m</p>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>

      {/* Audio Player + Knowledge Density */}
      <div className="card p-4">
        <div className="flex items-center gap-4">
          <button
            onClick={() => setPlaying(!playing)}
            className="flex h-10 w-10 items-center justify-center rounded-full bg-nexus-500 hover:bg-nexus-400 transition-colors shrink-0"
          >
            {playing ? <Pause className="h-5 w-5 text-white" /> : <Play className="h-5 w-5 text-white ml-0.5" />}
          </button>
          <div className="flex-1 min-w-0">
            <div className="flex items-center gap-2 text-xs text-gray-400 mb-1.5">
              <span className="font-mono">{formatTime(currentTime)}</span>
              <span>/</span>
              <span className="font-mono">{formatTime(totalDuration)}</span>
              <Volume2 className="h-3 w-3 ml-2" />
            </div>

            {/* Progress bar with knowledge density overlay */}
            <div className="relative h-8 cursor-pointer group" onClick={(e) => {
              if (totalDuration <= 0) return;
              const rect = e.currentTarget.getBoundingClientRect();
              const pct = (e.clientX - rect.left) / rect.width;
              jumpToTime(pct * totalDuration);
            }}>
              {/* Density bars */}
              <div className="absolute bottom-0 left-0 right-0 flex items-end gap-px h-6">
                {densityBars.map((v, i) => (
                  <div
                    key={i}
                    className={clsx(
                      'flex-1 rounded-t-sm transition-all duration-150',
                      totalDuration > 0 && i <= Math.floor(currentTime / (totalDuration / 40))
                        ? 'bg-nexus-500/40'
                        : 'bg-white/[0.06] group-hover:bg-white/[0.1]',
                    )}
                    style={{ height: `${Math.max(v * 100, 8)}%` }}
                  />
                ))}
              </div>
              {/* Playhead */}
              {totalDuration > 0 && (
                <div
                  className="absolute top-0 bottom-0 w-0.5 bg-nexus-400 z-10"
                  style={{ left: `${(currentTime / totalDuration) * 100}%` }}
                >
                  <div className="absolute -top-1 -left-1.5 h-3 w-3 rounded-full bg-nexus-400 shadow-lg shadow-nexus-500/40" />
                </div>
              )}
              {/* Event markers */}
              {totalDuration > 0 && events.map((evt, i) => (
                <div
                  key={i}
                  className="absolute top-0 w-1 h-2 rounded-b"
                  style={{ left: `${(evt.timestamp_seconds / totalDuration) * 100}%` }}
                >
                  <div className={clsx(
                    'h-full w-full rounded-b',
                    evt.event_type === 'contradiction' ? 'bg-red-400' :
                    evt.event_type === 'rule_extracted' ? 'bg-nexus-400' :
                    evt.event_type === 'test_generated' ? 'bg-green-400' :
                    'bg-yellow-400',
                  )} />
                </div>
              ))}
            </div>
            <p className="text-[10px] text-gray-600 mt-1">Knowledge Density — taller bars = more intelligence extracted</p>
          </div>
        </div>
      </div>

      {/* Main content: Timeline + Transcript side-by-side */}
      <div className="grid grid-cols-1 lg:grid-cols-5 gap-4">
        {/* Intelligence Timeline (3/5) */}
        <div className="lg:col-span-3 space-y-3">
          <div className="flex items-center justify-between">
            <h2 className="text-sm font-semibold text-gray-300 flex items-center gap-2">
              <Sparkles className="h-4 w-4 text-nexus-400" />
              Intelligence Timeline
            </h2>
            <div className="flex gap-1">
              {[
                { key: 'all', label: 'All' },
                { key: 'rule_extracted', label: 'Rules' },
                { key: 'contradiction', label: 'Conflicts' },
                { key: 'test_generated', label: 'Tests' },
                { key: 'key_decision', label: 'Decisions' },
              ].map((f) => (
                <button
                  key={f.key}
                  onClick={() => setFilterType(f.key)}
                  className={clsx(
                    'px-2.5 py-1 rounded-md text-[11px] font-medium transition-colors',
                    filterType === f.key
                      ? 'bg-nexus-500/15 text-nexus-400'
                      : 'text-gray-500 hover:text-gray-300 hover:bg-white/[0.04]',
                  )}
                >
                  {f.label}
                </button>
              ))}
            </div>
          </div>

          <div className="space-y-2">
            {filteredEvents.length === 0 && (
              <EmptyState title="No Intelligence Events" description="Events will populate as sessions are processed by the AI engines." />
            )}
            {filteredEvents.map((evt, idx) => (
              <div
                key={idx}
                className={clsx(
                  'card p-4 cursor-pointer transition-all hover:ring-white/10',
                  selectedEvent === evt && 'ring-nexus-500/40 bg-nexus-500/5',
                  evt.event_type === 'contradiction' && 'ring-red-500/20',
                )}
                onClick={() => { setSelectedEvent(evt); jumpToTime(evt.timestamp_seconds); }}
              >
                <div className="flex items-start gap-3">
                  <button
                    className="flex h-8 w-8 items-center justify-center rounded-lg bg-white/[0.05] hover:bg-white/[0.1] shrink-0 transition-colors"
                    onClick={(e) => { e.stopPropagation(); jumpToTime(evt.timestamp_seconds); }}
                    title={`Jump to ${formatTime(evt.timestamp_seconds)}`}
                  >
                    <Play className="h-3.5 w-3.5 text-gray-400" />
                  </button>

                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-2 flex-wrap">
                      <span className="font-mono text-[11px] text-gray-500">{formatTime(evt.timestamp_seconds)}</span>
                      <EventBadge type={evt.event_type} />
                      {evt.confidence && (
                        <span className="text-[11px] text-gray-500">
                          <BarChart3 className="inline h-3 w-3 mr-0.5" />{evt.confidence}%
                        </span>
                      )}
                    </div>

                    <h4 className="text-sm font-medium text-white mt-1">{evt.title}</h4>
                    <p className="text-xs text-gray-400 mt-1 leading-relaxed">{evt.description}</p>

                    <div className="mt-2 flex items-center gap-3">
                      {evt.speaker && (
                        <span className="flex items-center gap-1 text-[11px] text-gray-500">
                          <div className="h-4 w-4 rounded-full bg-gradient-to-br from-nexus-500 to-purple-600 flex items-center justify-center text-[8px] text-white font-bold">
                            {evt.speaker.charAt(0)}
                          </div>
                          {evt.speaker}
                        </span>
                      )}
                      {evt.linked_nodes?.map((node) => (
                        <span key={node} className="badge-gray text-[10px]">{node}</span>
                      ))}
                    </div>

                    {evt.event_type === 'contradiction' && (
                      <div className="mt-3 flex gap-2">
                        <button className="btn-ghost text-[11px] py-1 px-2">
                          <Eye className="h-3 w-3" /> View Both Clips
                        </button>
                        <button className="btn-ghost text-[11px] py-1 px-2 text-green-400">
                          <CheckSquare className="h-3 w-3" /> Resolve
                        </button>
                        <button className="btn-ghost text-[11px] py-1 px-2 text-yellow-400">
                          <Flag className="h-3 w-3" /> Escalate
                        </button>
                      </div>
                    )}
                  </div>
                </div>
              </div>
            ))}
          </div>
        </div>

        {/* Transcript Panel (2/5) — PII-safe via artifact transcript */}
        <div className="lg:col-span-2 space-y-3">
          <div className="flex items-center justify-between">
            <h2 className="text-sm font-semibold text-gray-300 flex items-center gap-2">
              <MessageSquare className="h-4 w-4 text-gray-400" />
              Transcript
              {safeTranscriptSegments.length > 0 && <StatusBadge label="PII-Safe" variant="green" />}
            </h2>
            {artifact && (
              <span className="text-[10px] text-gray-600">
                {transcript.length} segment{transcript.length !== 1 ? 's' : ''}
              </span>
            )}
          </div>

          <div className="card p-4 space-y-4 max-h-[600px] overflow-y-auto">
            {artifactLoading && transcript.length === 0 ? (
              <div className="flex items-center justify-center py-8 gap-2 text-xs text-gray-500">
                <Loader2 className="h-4 w-4 animate-spin" /> Loading transcript&hellip;
              </div>
            ) : transcript.length === 0 ? (
              <EmptyState title="No Transcript" description="Transcript data will appear after processing completes." />
            ) : (
              transcript.map((seg, idx) => {
                const isActive = currentTime >= seg.start && currentTime <= seg.end;
                return (
                  <div
                    key={idx}
                    className={clsx(
                      'rounded-lg p-3 cursor-pointer transition-all',
                      isActive ? 'bg-nexus-500/10 ring-1 ring-nexus-500/30' : 'hover:bg-white/[0.03]',
                    )}
                    onClick={() => jumpToTime(seg.start)}
                  >
                    <div className="flex items-center gap-2 mb-1">
                      <div className="h-5 w-5 rounded-full bg-gradient-to-br from-nexus-500 to-purple-600 flex items-center justify-center text-[9px] text-white font-bold shrink-0">
                        {seg.speaker.charAt(0)}
                      </div>
                      <span className="text-xs font-medium text-gray-300">{seg.speaker}</span>
                      <span className="font-mono text-[10px] text-gray-600">{formatTime(seg.start)}</span>
                      {seg.confidence != null && seg.confidence < 0.7 && (
                        <span className="text-[9px] text-yellow-500">Low confidence</span>
                      )}
                    </div>
                    <p className={clsx('text-sm leading-relaxed ml-7', isActive ? 'text-gray-200' : 'text-gray-400')}>
                      {seg.text}
                    </p>
                  </div>
                );
              })
            )}
          </div>

          {/* Visual Evidence Panel — replaces screen recording placeholder */}
          <div className="card overflow-hidden">
            <div className="flex items-center gap-2 px-4 py-2 bg-white/[0.03]">
              <MonitorPlay className="h-4 w-4 text-gray-400" />
              <span className="text-xs font-medium text-gray-400">Visual Evidence</span>
              {visualAnalysis.application_types_seen && visualAnalysis.application_types_seen.length > 0 ? (
                <span className="ml-auto flex gap-1">
                  {visualAnalysis.application_types_seen.map((appType) => (
                    <StatusBadge key={appType} label={appType} variant="blue" />
                  ))}
                </span>
              ) : (
                <span className="badge-gray text-[10px] ml-auto">
                  {artifact?.has_visual_semantics ? 'Analyzed' : 'No visual data'}
                </span>
              )}
            </div>

            {/* Visual analysis content */}
            <div className="p-4 space-y-3">
              {/* Scene transitions / keyframe evidence */}
              {visualAnalysis.scenes && visualAnalysis.scenes.length > 0 ? (
                <div className="space-y-2">
                  <p className="text-[11px] text-gray-500 font-medium uppercase tracking-wider">Scene Transitions</p>
                  <div className="space-y-1.5 max-h-48 overflow-y-auto">
                    {visualAnalysis.scenes.map((scene, i) => (
                      <div
                        key={i}
                        className="flex items-start gap-2 p-2 rounded-md bg-gray-800/30 hover:bg-gray-800/50 transition-colors cursor-pointer"
                        onClick={() => scene.start_time != null && jumpToTime(scene.start_time)}
                      >
                        <Layers className="h-3.5 w-3.5 text-blue-400 shrink-0 mt-0.5" />
                        <div className="flex-1 min-w-0">
                          {scene.description && (
                            <p className="text-xs text-gray-300">{scene.description}</p>
                          )}
                          <div className="flex items-center gap-2 text-[10px] text-gray-500 mt-0.5">
                            {scene.start_time != null && <span>{formatTime(scene.start_time)}</span>}
                            {scene.start_time != null && scene.end_time != null && (
                              <span>&rarr; {formatTime(scene.end_time)}</span>
                            )}
                            {scene.transition && (
                              <span className="text-purple-400">Transition: {scene.transition}</span>
                            )}
                          </div>
                        </div>
                      </div>
                    ))}
                  </div>
                </div>
              ) : visualAnalysis.frames && visualAnalysis.frames.length > 0 ? (
                <div className="space-y-2">
                  <p className="text-[11px] text-gray-500 font-medium uppercase tracking-wider">Extracted Keyframes</p>
                  <div className="space-y-1.5 max-h-48 overflow-y-auto">
                    {visualAnalysis.frames.map((frame, i) => (
                      <div
                        key={i}
                        className="flex items-start gap-2 p-2 rounded-md bg-gray-800/30 hover:bg-gray-800/50 transition-colors cursor-pointer"
                        onClick={() => frame.timestamp != null && jumpToTime(frame.timestamp)}
                      >
                        <Eye className="h-3.5 w-3.5 text-gray-500 shrink-0 mt-0.5" />
                        <div className="flex-1 min-w-0">
                          {frame.ocr_text && (
                            <p className="text-xs text-gray-300 font-mono">{frame.ocr_text.slice(0, 200)}{frame.ocr_text.length > 200 ? '…' : ''}</p>
                          )}
                          {frame.description && (
                            <p className="text-xs text-gray-400 mt-0.5">{frame.description}</p>
                          )}
                          {frame.timestamp != null && (
                            <span className="text-[10px] text-gray-600">{formatTime(frame.timestamp)}</span>
                          )}
                        </div>
                      </div>
                    ))}
                  </div>
                </div>
              ) : (
                <div className="py-6 text-center">
                  <MonitorPlay className="h-8 w-8 text-gray-600 mx-auto mb-2" />
                  <p className="text-xs text-gray-500">
                    {artifactLoading ? 'Loading visual analysis…' : 'No visual evidence extracted for this session'}
                  </p>
                </div>
              )}

              {/* Aggregate stats at bottom */}
              {artifact && artifact.has_visual_semantics && (
                <div className="flex flex-wrap gap-3 pt-2 border-t border-white/5 text-[10px] text-gray-500">
                  {artifact.frame_count > 0 && <span>{artifact.frame_count} frames</span>}
                  {artifact.scene_count > 0 && <span>{artifact.scene_count} scenes</span>}
                  {visualAnalysis.scenes && visualAnalysis.scenes.filter(s => s.transition).length > 0 && (
                    <span>{visualAnalysis.scenes.filter(s => s.transition).length} transitions</span>
                  )}
                  {artifact.visual_summary && (
                    <span className="text-gray-400" title={artifact.visual_summary}>
                      Summary: {artifact.visual_summary.slice(0, 80)}{artifact.visual_summary.length > 80 ? '…' : ''}
                    </span>
                  )}
                </div>
              )}
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}

// Sparkles icon (not in lucide-react)
const Sparkles = ({ className }: { className?: string }) => (
  <svg className={className} xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
    <path d="m12 3-1.912 5.813a2 2 0 0 1-1.275 1.275L3 12l5.813 1.912a2 2 0 0 1 1.275 1.275L12 21l1.912-5.813a2 2 0 0 1 1.275-1.275L21 12l-5.813-1.912a2 2 0 0 1-1.275-1.275L12 3Z"/>
    <path d="M5 3v4"/><path d="M19 17v4"/><path d="M3 5h4"/><path d="M17 19h4"/>
  </svg>
);
