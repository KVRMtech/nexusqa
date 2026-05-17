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
import type { CanonicalArtifact, VisualScene, AppInstance, EvidenceControl, VisualEvidenceGraph } from '../types/canonical';
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
  X,
  ZoomIn,
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

  // ── Visual Evidence — Phase 8: single evidence-graph call feeds both replay + diagram ──
  const [rightTab, setRightTab] = useState<'transcript' | 'visual'>('transcript');
  const [evidenceGraph, setEvidenceGraph] = useState<VisualEvidenceGraph | null>(null);
  const [visualScenesLoading, setVisualScenesLoading] = useState(false);
  const [selectedScene, setSelectedScene] = useState<VisualScene | null>(null);

  useEffect(() => {
    if (!artifact?.artifact_id) return;
    setVisualScenesLoading(true);
    api.getVisualEvidenceGraph(artifact.artifact_id)
      .then(setEvidenceGraph)
      .catch(() => setEvidenceGraph(null))
      .finally(() => setVisualScenesLoading(false));
  }, [artifact?.artifact_id]);

  // Derived from shared evidence graph
  const visualScenes: VisualScene[] = evidenceGraph?.scenes ?? [];
  const appInstances: AppInstance[] = evidenceGraph?.app_instances ?? [];
  const controlsByScene = useMemo((): Map<string, EvidenceControl[]> => {
    if (!evidenceGraph) return new Map();
    const m = new Map<string, EvidenceControl[]>();
    for (const [sid, ctrls] of Object.entries(evidenceGraph.controls_by_scene)) {
      m.set(sid, ctrls);
    }
    return m;
  }, [evidenceGraph]);

  // Derived: scenes grouped by app_instance_id
  const scenesByInstance = useMemo(() => {
    const map = new Map<string, VisualScene[]>();
    for (const sc of visualScenes) {
      const key = sc.app_instance_id ?? '__unassigned__';
      if (!map.has(key)) map.set(key, []);
      map.get(key)!.push(sc);
    }
    return map;
  }, [visualScenes]);

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
      <Link to="/sessions" className="inline-flex items-center gap-2 text-sm text-slate-8000 hover:text-nexus-400 transition-colors">
        <ArrowLeft className="h-4 w-4" />
        Back to Sessions
      </Link>

      {/* Session Header */}
      <div className="card p-5">
        <div className="flex flex-col lg:flex-row lg:items-center lg:justify-between gap-4">
          <div>
            {sessionLoading ? (
              <div className="flex items-center gap-2">
                <Loader2 className="h-4 w-4 text-slate-400 animate-spin" />
                <span className="text-sm text-slate-400">Loading session&hellip;</span>
              </div>
            ) : (
              <>
                <h1 className="page-title">{sessionTitle}</h1>
                <div className="mt-2 flex flex-wrap items-center gap-4 text-sm text-slate-8000">
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
          <div className="mt-3 flex flex-wrap items-center gap-3 text-xs text-slate-400 border-t border-gray-100 pt-3">
            {artifact.source_filename && (
              <span className="flex items-center gap-1"><FileText className="h-3 w-3" /> {artifact.source_filename}</span>
            )}
            {artifact.has_real_transcript && <StatusBadge label="REAL TRANSCRIPT" variant="green" icon={<Mic className="h-2.5 w-2.5" />} />}
            {artifact.has_visual_semantics && <StatusBadge label="REAL VISUAL" variant="blue" icon={<Eye className="h-2.5 w-2.5" />} />}
            {artifact.brain_quality_score != null && (
              <span className="text-slate-8000">Quality: <span className="text-slate-600 font-mono">{(artifact.brain_quality_score * 100).toFixed(0)}%</span></span>
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
                <div className="flex h-7 w-7 items-center justify-center rounded-full bg-gradient-to-br from-nexus-500 to-purple-600 text-[10px] font-bold text-[#0a2540]">
                  {p.name.charAt(0)}
                </div>
                <div>
                  <p className="text-xs font-medium text-slate-700">{p.name}</p>
                  <p className="text-[10px] text-slate-400">{p.role ?? 'Participant'} — {Math.floor((p.speaking_time_seconds || 0) / 60)}m</p>
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
            {playing ? <Pause className="h-5 w-5 text-[#0a2540]" /> : <Play className="h-5 w-5 text-[#0a2540] ml-0.5" />}
          </button>
          <div className="flex-1 min-w-0">
            <div className="flex items-center gap-2 text-xs text-slate-8000 mb-1.5">
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
            <p className="text-[10px] text-slate-8000 mt-1">Knowledge Density — taller bars = more intelligence extracted</p>
          </div>
        </div>
      </div>

      {/* Main content: Timeline + Transcript side-by-side */}
      <div className="grid grid-cols-1 lg:grid-cols-5 gap-4">
        {/* Intelligence Timeline (3/5) */}
        <div className="lg:col-span-3 space-y-3">
          <div className="flex items-center justify-between">
            <h2 className="text-sm font-semibold text-slate-600 flex items-center gap-2">
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
                      : 'text-slate-400 hover:text-slate-600 hover:bg-white/[0.04]',
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
                    <Play className="h-3.5 w-3.5 text-slate-8000" />
                  </button>

                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-2 flex-wrap">
                      <span className="font-mono text-[11px] text-slate-400">{formatTime(evt.timestamp_seconds)}</span>
                      <EventBadge type={evt.event_type} />
                      {evt.confidence && (
                        <span className="text-[11px] text-slate-400">
                          <BarChart3 className="inline h-3 w-3 mr-0.5" />{evt.confidence}%
                        </span>
                      )}
                    </div>

                    <h4 className="text-sm font-medium text-[#0a2540] mt-1">{evt.title}</h4>
                    <p className="text-xs text-slate-8000 mt-1 leading-relaxed">{evt.description}</p>

                    <div className="mt-2 flex items-center gap-3">
                      {evt.speaker && (
                        <span className="flex items-center gap-1 text-[11px] text-slate-400">
                          <div className="h-4 w-4 rounded-full bg-gradient-to-br from-nexus-500 to-purple-600 flex items-center justify-center text-[8px] text-[#0a2540] font-bold">
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

        {/* Transcript + Visual Evidence Panel (2/5) */}
        <div className="lg:col-span-2 space-y-3">
          {/* Tab header */}
          <div className="flex items-center gap-1">
            <button
              onClick={() => setRightTab('transcript')}
              className={clsx(
                'flex items-center gap-1.5 px-3 py-1.5 rounded-md text-[11px] font-medium transition-colors',
                rightTab === 'transcript'
                  ? 'bg-nexus-500/15 text-nexus-400'
                  : 'text-slate-400 hover:text-slate-600 hover:bg-white/[0.04]',
              )}
            >
              <MessageSquare className="h-3.5 w-3.5" />
              Transcript
              {safeTranscriptSegments.length > 0 && (
                <StatusBadge label="PII-Safe" variant="green" />
              )}
            </button>
            <button
              onClick={() => setRightTab('visual')}
              className={clsx(
                'flex items-center gap-1.5 px-3 py-1.5 rounded-md text-[11px] font-medium transition-colors',
                rightTab === 'visual'
                  ? 'bg-nexus-500/15 text-nexus-400'
                  : 'text-slate-400 hover:text-slate-600 hover:bg-white/[0.04]',
              )}
            >
              <Layers className="h-3.5 w-3.5" />
              Visual Evidence
              {visualScenes.length > 0 && (
                <span className="ml-1 rounded-full bg-blue-500/20 px-1.5 py-px text-[9px] text-blue-400">
                  {visualScenes.length}
                </span>
              )}
            </button>
          </div>

          {/* ── Transcript Tab ── */}
          {rightTab === 'transcript' && (
          <div className="card p-4 space-y-4 max-h-[600px] overflow-y-auto">
            {artifactLoading && transcript.length === 0 ? (
              <div className="flex items-center justify-center py-8 gap-2 text-xs text-slate-400">
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
                      <div className="h-5 w-5 rounded-full bg-gradient-to-br from-nexus-500 to-purple-600 flex items-center justify-center text-[9px] text-[#0a2540] font-bold shrink-0">
                        {seg.speaker.charAt(0)}
                      </div>
                      <span className="text-xs font-medium text-slate-600">{seg.speaker}</span>
                      <span className="font-mono text-[10px] text-slate-8000">{formatTime(seg.start)}</span>
                      {seg.confidence != null && seg.confidence < 0.7 && (
                        <span className="text-[9px] text-yellow-500">Low confidence</span>
                      )}
                    </div>
                    <p className={clsx('text-sm leading-relaxed ml-7', isActive ? 'text-slate-700' : 'text-slate-8000')}>
                      {seg.text}
                    </p>
                  </div>
                );
              })
            )}
          </div>
          )} {/* end rightTab === 'transcript' */}

          {/* ── Visual Evidence Tab ── */}
          {rightTab === 'visual' && (
            <div className="space-y-3">
              {/* Completeness bar */}
              {visualScenes.length > 0 && (() => {
                const avgConf = visualScenes.reduce((sum, s) => sum + (s.completeness_confidence ?? 0), 0) / visualScenes.length;
                return (
                  <div className="card px-4 py-2.5 flex items-center gap-3">
                    <Layers className="h-4 w-4 text-blue-400 shrink-0" />
                    <p className="text-[11px] text-slate-600 flex-1">
                      <span className="font-semibold text-[#0a2540]">{visualScenes.length} scenes</span>
                      {' '}captured &middot; completeness confidence:&nbsp;
                      <span
                        className={clsx(
                          'font-semibold',
                          avgConf >= 0.8 ? 'text-green-400' : avgConf >= 0.5 ? 'text-yellow-400' : 'text-slate-8000',
                        )}
                      >
                        {Math.round(avgConf * 100)}%
                      </span>
                    </p>
                  </div>
                );
              })()}

              {/* Loading / empty state */}
              {visualScenesLoading ? (
                <div className="card p-8 flex items-center justify-center gap-2 text-xs text-slate-400">
                  <Loader2 className="h-4 w-4 animate-spin" /> Loading visual evidence&hellip;
                </div>
              ) : visualScenes.length === 0 ? (
                <div className="card p-8 text-center space-y-2">
                  <MonitorPlay className="h-10 w-10 text-slate-8000 mx-auto" />
                  <p className="text-sm font-medium text-slate-8000">Visual evidence processing in progress</p>
                  <p className="text-xs text-slate-8000">
                    Scenes will appear here once the canonical pipeline has completed frame extraction.
                  </p>
                </div>
              ) : (
                <>
                  {/* ── Swim lanes: one lane per app instance ── */}
                  {appInstances.length > 0 ? (
                    <div className="space-y-4">
                      {appInstances.map((inst) => {
                        const laneScenes = scenesByInstance.get(inst.instance_id) ?? [];
                        const lowConf = inst.segmentation_confidence < 0.8;
                        const confPct = Math.round(inst.segmentation_confidence * 100);
                        const basisLabel: Record<string, string> = {
                          app_type_change: 'App Type',
                          domain_change: 'Domain',
                          window_title_change: 'Window Title',
                          no_boundary: 'Session Start',
                        };
                        return (
                          <div key={inst.instance_id} className="space-y-2">
                            {/* Lane header */}
                            <div className="flex items-center gap-2 px-1 flex-wrap">
                              <span className="text-xs font-semibold text-slate-700 truncate max-w-[160px]" title={inst.app_name ?? undefined}>
                                {inst.app_name || inst.app_type || 'Unknown App'}
                              </span>
                              {inst.app_type && inst.app_type !== 'unknown' && (
                                <StatusBadge label={inst.app_type} variant="blue" />
                              )}
                              <span className="text-[9px] text-slate-400 rounded px-1.5 py-px bg-white/[0.04] border border-white/[0.07]">
                                {basisLabel[inst.segmentation_basis] ?? inst.segmentation_basis}
                              </span>
                              {lowConf ? (
                                <span className="flex items-center gap-0.5 text-[9px] text-yellow-400">
                                  <AlertTriangle className="h-2.5 w-2.5" /> Detected {confPct}%
                                </span>
                              ) : (
                                <span className="text-[9px] text-green-400/70">{confPct}%</span>
                              )}
                              <span className="text-[9px] text-slate-8000 ml-auto">{laneScenes.length} scene{laneScenes.length !== 1 ? 's' : ''}</span>
                            </div>
                            {/* Lane filmstrip */}
                            <div className="overflow-x-auto pb-1">
                              <div className="flex gap-3" style={{ minWidth: 'max-content' }}>
                                {laneScenes.map((scene) => {
                                  const imgUrl = scene.representative_frame_asset_path
                                    ? api.getFrameImageUrl(scene.representative_frame_asset_path)
                                    : '';
                                  const isSelected = selectedScene?.scene_id === scene.scene_id;
                                  const sConfPct = Math.round((scene.completeness_confidence ?? 0) * 100);
                                  const confColor =
                                    (scene.completeness_confidence ?? 0) >= 0.8
                                      ? { bg: 'rgba(34,197,94,0.15)', fg: '#4ade80' }
                                      : (scene.completeness_confidence ?? 0) >= 0.5
                                      ? { bg: 'rgba(234,179,8,0.15)', fg: '#fbbf24' }
                                      : { bg: 'rgba(156,163,175,0.12)', fg: '#9ca3af' };
                                  return (
                                    <button
                                      key={scene.scene_id}
                                      onClick={() => setSelectedScene(isSelected ? null : scene)}
                                      className={clsx(
                                        'group relative flex-shrink-0 w-44 rounded-xl overflow-hidden border transition-all text-left',
                                        isSelected
                                          ? 'border-nexus-500/60 ring-2 ring-nexus-500/25'
                                          : 'border-gray-200 hover:border-white/20',
                                      )}
                                    >
                                      {imgUrl ? (
                                        <div className="h-28 bg-white overflow-hidden relative">
                                          <img
                                            src={imgUrl}
                                            alt={scene.screen_name ?? `Scene ${scene.scene_index + 1}`}
                                            className="h-full w-full object-cover group-hover:scale-105 transition-transform duration-200"
                                            onError={(e) => {
                                              (e.target as HTMLImageElement).parentElement!.classList.add('bg-gray-100');
                                              (e.target as HTMLImageElement).style.display = 'none';
                                            }}
                                          />
                                          <div className="absolute inset-0 bg-gradient-to-t from-black/60 to-transparent opacity-0 group-hover:opacity-100 transition-opacity flex items-end justify-end p-1.5">
                                            <ZoomIn className="h-3.5 w-3.5 text-[#0a2540]" />
                                          </div>
                                        </div>
                                      ) : (
                                        <div className="h-28 bg-white shadow-sm flex items-center justify-center">
                                          <MonitorPlay className="h-7 w-7 text-slate-8000" />
                                        </div>
                                      )}
                                      <div className="bg-white p-2 space-y-0.5">
                                        <div className="flex items-start justify-between gap-1">
                                          <p className="text-[11px] font-medium text-slate-700 leading-tight truncate">
                                            {scene.screen_name ?? `Scene ${scene.scene_index + 1}`}
                                          </p>
                                          <span
                                            className="text-[9px] shrink-0 rounded-full px-1.5 py-px font-medium"
                                            style={{ background: confColor.bg, color: confColor.fg }}
                                          >
                                            {sConfPct}%
                                          </span>
                                        </div>
                                        <p className="text-[9px] text-slate-400 font-mono">
                                          {scene.start_ms != null ? formatTime(scene.start_ms / 1000) : '—'}
                                          {scene.start_ms != null && scene.end_ms != null
                                            ? ` → ${formatTime(scene.end_ms / 1000)}`
                                            : ''}
                                        </p>
                                        {scene.detected_url && (
                                          <p className="text-[9px] text-blue-400/70 truncate" title={scene.detected_url}>
                                            {scene.detected_url.replace(/^https?:\/\//, '').slice(0, 26)}
                                          </p>
                                        )}
                                      </div>
                                    </button>
                                  );
                                })}
                                {laneScenes.length === 0 && (
                                  <p className="text-[11px] text-slate-8000 italic py-8 px-2">No scenes in this instance yet.</p>
                                )}
                              </div>
                            </div>
                          </div>
                        );
                      })}
                      {/* Unassigned scenes (no app_instance_id) */}
                      {(scenesByInstance.get('__unassigned__') ?? []).length > 0 && (
                        <div className="space-y-2">
                          <div className="flex items-center gap-2 px-1">
                            <span className="text-xs font-semibold text-slate-400">Unassigned Scenes</span>
                          </div>
                          <div className="overflow-x-auto pb-1">
                            <div className="flex gap-3" style={{ minWidth: 'max-content' }}>
                              {(scenesByInstance.get('__unassigned__') ?? []).map((scene) => {
                                const imgUrl = scene.representative_frame_asset_path
                                  ? api.getFrameImageUrl(scene.representative_frame_asset_path)
                                  : '';
                                const isSelected = selectedScene?.scene_id === scene.scene_id;
                                return (
                                  <button
                                    key={scene.scene_id}
                                    onClick={() => setSelectedScene(isSelected ? null : scene)}
                                    className={clsx(
                                      'group relative flex-shrink-0 w-44 rounded-xl overflow-hidden border transition-all text-left',
                                      isSelected ? 'border-nexus-500/60 ring-2 ring-nexus-500/25' : 'border-gray-200 hover:border-white/20',
                                    )}
                                  >
                                    {imgUrl ? (
                                      <div className="h-28 bg-white overflow-hidden relative">
                                        <img
                                          src={imgUrl}
                                          alt={scene.screen_name ?? `Scene ${scene.scene_index + 1}`}
                                          className="h-full w-full object-cover group-hover:scale-105 transition-transform duration-200"
                                          onError={(e) => {
                                            (e.target as HTMLImageElement).parentElement!.classList.add('bg-gray-100');
                                            (e.target as HTMLImageElement).style.display = 'none';
                                          }}
                                        />
                                      </div>
                                    ) : (
                                      <div className="h-28 bg-white shadow-sm flex items-center justify-center">
                                        <MonitorPlay className="h-7 w-7 text-slate-8000" />
                                      </div>
                                    )}
                                    <div className="bg-white p-2">
                                      <p className="text-[11px] font-medium text-slate-700 truncate">
                                        {scene.screen_name ?? `Scene ${scene.scene_index + 1}`}
                                      </p>
                                    </div>
                                  </button>
                                );
                              })}
                            </div>
                          </div>
                        </div>
                      )}
                    </div>
                  ) : (
                    /* Fallback flat filmstrip (no app instances yet) */
                    <div className="overflow-x-auto pb-1">
                      <div className="flex gap-3" style={{ minWidth: 'max-content' }}>
                        {visualScenes.map((scene) => {
                          const imgUrl = scene.representative_frame_asset_path
                            ? api.getFrameImageUrl(scene.representative_frame_asset_path)
                            : '';
                          const isSelected = selectedScene?.scene_id === scene.scene_id;
                          const confPct = Math.round((scene.completeness_confidence ?? 0) * 100);
                          const confColor =
                            (scene.completeness_confidence ?? 0) >= 0.8
                              ? { bg: 'rgba(34,197,94,0.15)', fg: '#4ade80' }
                              : (scene.completeness_confidence ?? 0) >= 0.5
                              ? { bg: 'rgba(234,179,8,0.15)', fg: '#fbbf24' }
                              : { bg: 'rgba(156,163,175,0.12)', fg: '#9ca3af' };

                          return (
                            <button
                              key={scene.scene_id}
                              onClick={() => setSelectedScene(isSelected ? null : scene)}
                              className={clsx(
                                'group relative flex-shrink-0 w-44 rounded-xl overflow-hidden border transition-all text-left',
                                isSelected
                                  ? 'border-nexus-500/60 ring-2 ring-nexus-500/25'
                                  : 'border-gray-200 hover:border-white/20',
                              )}
                            >
                              {imgUrl ? (
                                <div className="h-28 bg-white overflow-hidden relative">
                                  <img
                                    src={imgUrl}
                                    alt={scene.screen_name ?? `Scene ${scene.scene_index + 1}`}
                                    className="h-full w-full object-cover group-hover:scale-105 transition-transform duration-200"
                                    onError={(e) => {
                                      (e.target as HTMLImageElement).parentElement!.classList.add('bg-gray-100');
                                      (e.target as HTMLImageElement).style.display = 'none';
                                    }}
                                  />
                                  <div className="absolute inset-0 bg-gradient-to-t from-black/60 to-transparent opacity-0 group-hover:opacity-100 transition-opacity flex items-end justify-end p-1.5">
                                    <ZoomIn className="h-3.5 w-3.5 text-[#0a2540]" />
                                  </div>
                                </div>
                              ) : (
                                <div className="h-28 bg-white shadow-sm flex items-center justify-center">
                                  <MonitorPlay className="h-7 w-7 text-slate-8000" />
                                </div>
                              )}
                              <div className="bg-white p-2 space-y-0.5">
                                <div className="flex items-start justify-between gap-1">
                                  <p className="text-[11px] font-medium text-slate-700 leading-tight truncate">
                                    {scene.screen_name ?? `Scene ${scene.scene_index + 1}`}
                                  </p>
                                  <span
                                    className="text-[9px] shrink-0 rounded-full px-1.5 py-px font-medium"
                                    style={{ background: confColor.bg, color: confColor.fg }}
                                  >
                                    {confPct}%
                                  </span>
                                </div>
                                <p className="text-[9px] text-slate-400 font-mono">
                                  {scene.start_ms != null ? formatTime(scene.start_ms / 1000) : '—'}
                                  {scene.start_ms != null && scene.end_ms != null
                                    ? ` → ${formatTime(scene.end_ms / 1000)}`
                                    : ''}
                                </p>
                                {scene.detected_url && (
                                  <p className="text-[9px] text-blue-400/70 truncate" title={scene.detected_url}>
                                    {scene.detected_url.replace(/^https?:\/\//, '').slice(0, 26)}
                                  </p>
                                )}
                              </div>
                            </button>
                          );
                        })}
                      </div>
                    </div>
                  )}

                  {/* Expanded scene detail panel */}
                  {selectedScene && (() => {
                    const imgUrl = selectedScene.representative_frame_asset_path
                      ? api.getFrameImageUrl(selectedScene.representative_frame_asset_path)
                      : '';
                    const confPct = Math.round((selectedScene.completeness_confidence ?? 0) * 100);
                    return (
                      <div className="card overflow-hidden">
                        <div className="flex items-center justify-between px-4 py-2 bg-white/[0.03] border-b border-gray-200">
                          <div className="flex items-center gap-2">
                            <Eye className="h-4 w-4 text-nexus-400" />
                            <span className="text-xs font-medium text-slate-700">
                              {selectedScene.screen_name ?? `Scene ${selectedScene.scene_index + 1}`}
                            </span>
                            <span className="text-[10px] text-slate-400">
                              &middot; Scene {selectedScene.scene_index + 1} of {visualScenes.length}
                            </span>
                          </div>
                          <button
                            onClick={() => setSelectedScene(null)}
                            className="rounded p-0.5 text-slate-400 hover:text-slate-600 hover:bg-white/[0.06] transition-colors"
                          >
                            <X className="h-4 w-4" />
                          </button>
                        </div>

                        <div className="flex divide-x divide-white/[0.06]">
                          {/* Full-size image */}
                          <div className="flex-1 bg-[#f5f7fa] flex items-center justify-center min-h-[220px] max-h-[400px] overflow-hidden">
                            {imgUrl ? (
                              <img
                                src={imgUrl}
                                alt={selectedScene.screen_name ?? `Scene ${selectedScene.scene_index + 1}`}
                                className="max-h-[400px] max-w-full object-contain"
                                onError={(e) => { (e.target as HTMLImageElement).style.display = 'none'; }}
                              />
                            ) : (
                              <MonitorPlay className="h-12 w-12 text-gray-700" />
                            )}
                          </div>

                          {/* OCR + metadata sidebar */}
                          <div className="w-56 shrink-0 flex flex-col">
                            {/* Stats */}
                            <div className="px-3 py-2 space-y-1.5 border-b border-gray-200">
                              <div className="flex items-center justify-between text-[10px]">
                                <span className="text-slate-400">Confidence</span>
                                <span className={clsx(
                                  'font-semibold',
                                  confPct >= 80 ? 'text-green-400' : confPct >= 50 ? 'text-yellow-400' : 'text-slate-8000',
                                )}>
                                  {confPct}%
                                </span>
                              </div>
                              <div className="flex items-center justify-between text-[10px]">
                                <span className="text-slate-400">Start</span>
                                <span className="text-slate-600 font-mono">
                                  {selectedScene.start_ms != null ? formatTime(selectedScene.start_ms / 1000) : '—'}
                                </span>
                              </div>
                              <div className="flex items-center justify-between text-[10px]">
                                <span className="text-slate-400">End</span>
                                <span className="text-slate-600 font-mono">
                                  {selectedScene.end_ms != null ? formatTime(selectedScene.end_ms / 1000) : '—'}
                                </span>
                              </div>
                              {selectedScene.detected_url && (
                                <div className="text-[10px]">
                                  <span className="text-slate-400 block">URL</span>
                                  <span className="text-blue-400/80 break-all leading-snug">
                                    {selectedScene.detected_url.slice(0, 60)}{selectedScene.detected_url.length > 60 ? '…' : ''}
                                  </span>
                                </div>
                              )}
                            </div>

                            {/* OCR text */}
                            <div className="flex-1 px-3 py-2 overflow-y-auto max-h-60">
                              <p className="text-[9px] text-slate-400 font-medium uppercase tracking-wider mb-1.5">OCR Text</p>
                              {selectedScene.ocr_text ? (
                                <p className="text-[10px] text-slate-600 font-mono leading-relaxed whitespace-pre-wrap break-words">
                                  {selectedScene.ocr_text}
                                </p>
                              ) : (
                                <p className="text-[10px] text-slate-8000 italic">No OCR text extracted</p>
                              )}
                            </div>
                          </div>
                        </div>
                      </div>
                    );
                  })()}

                  {/* Link to full flow diagram — only when graph substrate is persisted */}
                  {artifact && artifact.visual_e2e_ready !== false && (
                    <Link
                      to={`/sessions/${sessionId}/visual-flow?artifact_id=${artifact.artifact_id}`}
                      className="btn-ghost w-full text-center text-[11px] py-2 flex items-center justify-center gap-1.5"
                    >
                      <ArrowRight className="h-3.5 w-3.5" />
                      Open Visual Flow Diagram &amp; Generate Tests
                    </Link>
                  )}
                  {artifact && artifact.visual_e2e_ready === false && (
                    <div className="w-full text-center text-[11px] py-2 flex items-center justify-center gap-1.5 text-amber-500 bg-amber-50 rounded border border-amber-200">
                      <span>Visual graph not yet available — pipeline is still processing</span>
                    </div>
                  )}
                </>
              )}
            </div>
          )}
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
