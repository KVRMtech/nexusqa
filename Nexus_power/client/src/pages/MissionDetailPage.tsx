// ═══════════════════════════════════════════════════════════════
//  QI ENGINEER PORTAL — Mission Detail Page
//  "5-stage pipeline view with conversational AI interface"
// ═══════════════════════════════════════════════════════════════

import { useState, useEffect, useRef, useCallback } from 'react';
import { useParams, useNavigate } from 'react-router-dom';
import { useAuth } from '../contexts/AuthContext';
import { useMissionStore, usePersonaStore } from '../stores';
import { PageHeader } from '../components/PageHeader';
import { StatusBadge } from '../components/StatusBadge';
import type {
  Mission,
  MissionStage,
  MissionMessage,
  MissionArtifact,
  MissionStatus,
  StageType,
} from '../types';
import { STAGE_LABELS, STAGE_DESCRIPTIONS } from '../types/qi';
import {
  ArrowLeft,
  Play,
  CheckCircle2,
  SkipForward,
  Send,
  Loader2,
  FileStack,
  MessageSquare,
  Sparkles,
  AlertTriangle,
  Trash2,
  PauseCircle,
  XCircle,
  ChevronDown,
  Clock,
  Zap,
  Target,
  Brain,
  Lightbulb,
  Wand2,
  ShieldCheck,
} from 'lucide-react';
import clsx from 'clsx';

// ── Helpers ─────────────────────────────────────────────────

const STAGE_ICONS: Record<number, typeof Target> = {
  1: Target,
  2: Brain,
  3: Lightbulb,
  4: Wand2,
  5: ShieldCheck,
};

function formatDuration(seconds: number): string {
  if (seconds < 60) return `${Math.round(seconds)}s`;
  const m = Math.floor(seconds / 60);
  const s = Math.round(seconds % 60);
  return `${m}m ${s}s`;
}

function formatTimestamp(dateStr: string): string {
  return new Date(dateStr).toLocaleTimeString('en-US', {
    hour: '2-digit',
    minute: '2-digit',
  });
}

function formatDate(dateStr: string): string {
  return new Date(dateStr).toLocaleDateString('en-US', {
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  });
}

const STATUS_BADGE: Record<string, { label: string; variant: 'blue' | 'green' | 'yellow' | 'red' | 'gray' }> = {
  pending: { label: 'Pending', variant: 'gray' },
  active: { label: 'Active', variant: 'blue' },
  completed: { label: 'Completed', variant: 'green' },
  skipped: { label: 'Skipped', variant: 'gray' },
  failed: { label: 'Failed', variant: 'red' },
  draft: { label: 'Draft', variant: 'gray' },
  paused: { label: 'Paused', variant: 'yellow' },
  cancelled: { label: 'Cancelled', variant: 'gray' },
};

// ── Stage Pipeline ──────────────────────────────────────────

function StagePipeline({
  stages,
  currentStage,
  onSelect,
  selectedStage,
}: {
  stages: MissionStage[];
  currentStage: number;
  onSelect: (n: number) => void;
  selectedStage: number;
}) {
  return (
    <div className="flex items-center gap-2 overflow-x-auto pb-2">
      {[1, 2, 3, 4, 5].map((n) => {
        const stage = stages.find((s) => s.stage_number === n);
        const status = stage?.status ?? 'pending';
        const Icon = STAGE_ICONS[n] ?? Target;
        const isSelected = selectedStage === n;

        return (
          <button
            key={n}
            onClick={() => onSelect(n)}
            className={clsx(
              'flex items-center gap-2 rounded-lg border px-3 py-2 text-xs font-medium transition-all min-w-fit',
              isSelected && 'ring-2 ring-blue-500',
              status === 'completed' && 'border-green-700 bg-green-900/30 text-green-300',
              status === 'active' && 'border-blue-700 bg-blue-900/30 text-blue-300',
              status === 'failed' && 'border-red-700 bg-red-900/30 text-red-300',
              status === 'skipped' && 'border-gray-200 bg-white text-slate-8000',
              status === 'pending' && 'border-gray-200 bg-white text-slate-400',
            )}
          >
            <Icon className="h-3.5 w-3.5 flex-shrink-0" />
            <span>{STAGE_LABELS[n]}</span>
            {status === 'completed' && <CheckCircle2 className="h-3 w-3 text-green-400" />}
            {status === 'active' && <Loader2 className="h-3 w-3 animate-spin" />}
          </button>
        );
      })}
    </div>
  );
}

// ── Stage Detail Panel ──────────────────────────────────────

function StagePanel({
  stage,
  mission,
  artifacts,
  onStart,
  onComplete,
  onAdvance,
}: {
  stage: MissionStage | undefined;
  mission: Mission;
  artifacts: MissionArtifact[];
  onStart: () => void;
  onComplete: () => void;
  onAdvance: () => void;
}) {
  if (!stage) {
    return (
      <div className="rounded-lg border border-gray-200 bg-white p-6 text-center text-slate-8000">
        Stage not found
      </div>
    );
  }

  const stageArtifacts = artifacts.filter((a) => a.stage_id === stage.stage_id);
  const badge = STATUS_BADGE[stage.status] ?? STATUS_BADGE.pending;

  return (
    <div className="rounded-lg border border-gray-200 bg-white overflow-hidden">
      {/* Stage Header */}
      <div className="border-b border-gray-200 px-4 py-3 flex items-center justify-between">
        <div>
          <div className="flex items-center gap-2">
            <h3 className="text-sm font-bold text-[#0a2540]">
              Stage {stage.stage_number}: {stage.stage_label || STAGE_LABELS[stage.stage_number]}
            </h3>
            <StatusBadge label={badge.label} variant={badge.variant} />
          </div>
          <p className="mt-0.5 text-xs text-slate-8000">
            {STAGE_DESCRIPTIONS[stage.stage_number]}
          </p>
        </div>
        <div className="flex items-center gap-2">
          {stage.status === 'pending' && mission.status !== 'completed' && (
            <button
              onClick={onStart}
              className="flex items-center gap-1 rounded bg-blue-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-blue-500 transition-colors"
            >
              <Play className="h-3 w-3" />
              Start
            </button>
          )}
          {stage.status === 'active' && (
            <>
              <button
                onClick={onComplete}
                className="flex items-center gap-1 rounded bg-green-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-green-500 transition-colors"
              >
                <CheckCircle2 className="h-3 w-3" />
                Complete
              </button>
              <button
                onClick={onAdvance}
                className="flex items-center gap-1 rounded border border-gray-200 px-3 py-1.5 text-xs font-medium text-slate-600 hover:bg-gray-100 transition-colors"
              >
                <SkipForward className="h-3 w-3" />
                Skip & Advance
              </button>
            </>
          )}
        </div>
      </div>

      {/* Stage Timing */}
      {(stage.started_at || stage.duration_seconds > 0) && (
        <div className="border-b border-gray-200 px-4 py-2 flex items-center gap-4 text-xs text-slate-8000">
          {stage.started_at && (
            <span className="flex items-center gap-1">
              <Clock className="h-3 w-3" />
              Started: {formatDate(stage.started_at)}
            </span>
          )}
          {stage.duration_seconds > 0 && (
            <span className="flex items-center gap-1">
              <Zap className="h-3 w-3" />
              Duration: {formatDuration(stage.duration_seconds)}
            </span>
          )}
        </div>
      )}

      {/* Engine Calls */}
      {stage.engine_calls && stage.engine_calls.length > 0 && (
        <div className="border-b border-gray-200 px-4 py-3">
          <h4 className="text-xs font-medium text-slate-400 mb-2">Engine Calls</h4>
          <div className="flex flex-wrap gap-2">
            {stage.engine_calls.map((call, i) => (
              <div
                key={i}
                className={clsx(
                  'rounded px-2 py-1 text-[10px] font-medium',
                  call.status === 'ok' && 'bg-green-900/30 text-green-400',
                  call.status === 'error' && 'bg-red-900/30 text-red-400',
                  call.status === 'timeout' && 'bg-yellow-900/30 text-yellow-400',
                  call.status === 'skipped' && 'bg-gray-100 text-slate-8000',
                )}
              >
                {call.engine} → {call.endpoint} ({call.duration_ms}ms)
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Outputs / Content */}
      {stage.outputs && Object.keys(stage.outputs).length > 0 && (
        <div className="border-b border-gray-200 px-4 py-3">
          <h4 className="text-xs font-medium text-slate-400 mb-2">Stage Outputs</h4>
          <pre className="rounded bg-white p-3 text-xs text-slate-600 overflow-x-auto max-h-48">
            {JSON.stringify(stage.outputs, null, 2)}
          </pre>
        </div>
      )}

      {/* Artifacts for this stage */}
      {stageArtifacts.length > 0 && (
        <div className="px-4 py-3">
          <h4 className="text-xs font-medium text-slate-400 mb-2">
            Artifacts ({stageArtifacts.length})
          </h4>
          <div className="space-y-1.5">
            {stageArtifacts.map((artifact) => (
              <div
                key={artifact.artifact_id}
                className="flex items-center justify-between rounded border border-gray-200 bg-white/90 px-3 py-2"
              >
                <div className="flex items-center gap-2">
                  <FileStack className="h-3.5 w-3.5 text-purple-400" />
                  <span className="text-xs text-[#0a2540] font-medium">{artifact.name}</span>
                  <span className="text-[10px] text-slate-8000 uppercase">{artifact.artifact_type}</span>
                </div>
                {artifact.item_count > 0 && (
                  <span className="text-[10px] text-slate-8000">{artifact.item_count} items</span>
                )}
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Empty stage */}
      {stage.status === 'pending' && stageArtifacts.length === 0 && Object.keys(stage.outputs || {}).length === 0 && (
        <div className="px-4 py-8 text-center text-xs text-slate-600">
          Stage not yet started. Click "Start" to begin processing.
        </div>
      )}
    </div>
  );
}

// ── Chat Panel ──────────────────────────────────────────────

function ChatPanel({
  messages,
  isSending,
  onSend,
  personaName,
  personaIcon,
}: {
  messages: MissionMessage[];
  isSending: boolean;
  onSend: (content: string) => void;
  personaName: string;
  personaIcon: string;
}) {
  const [input, setInput] = useState('');
  const messagesEndRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages.length]);

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    if (!input.trim() || isSending) return;
    onSend(input.trim());
    setInput('');
  };

  return (
    <div className="flex flex-col rounded-lg border border-gray-200 bg-white overflow-hidden h-[500px]">
      {/* Chat Header */}
      <div className="border-b border-gray-200 px-4 py-2 flex items-center gap-2">
        <MessageSquare className="h-4 w-4 text-blue-400" />
        <span className="text-xs font-medium text-[#0a2540]">Mission Chat</span>
        <span className="text-xs text-slate-8000">— {personaIcon} {personaName}</span>
      </div>

      {/* Messages */}
      <div className="flex-1 overflow-y-auto px-4 py-3 space-y-3">
        {messages.map((msg) => (
          <div
            key={msg.message_id}
            className={clsx(
              'flex gap-2',
              msg.role === 'user' && 'justify-end',
            )}
          >
            {msg.role !== 'user' && (
              <div className="flex-shrink-0 mt-0.5">
                {msg.role === 'assistant' ? (
                  <span className="text-sm">{personaIcon}</span>
                ) : (
                  <Sparkles className="h-4 w-4 text-yellow-400" />
                )}
              </div>
            )}
            <div
              className={clsx(
                'max-w-[80%] rounded-lg px-3 py-2 text-xs',
                msg.role === 'user'
                  ? 'bg-blue-600 text-white'
                  : msg.role === 'system'
                    ? 'bg-yellow-900/30 text-yellow-200 border border-yellow-800/50'
                    : 'bg-gray-100 text-slate-700',
              )}
            >
              {msg.content_type === 'markdown' ? (
                <div className="prose prose-sm prose-invert max-w-none whitespace-pre-wrap">{msg.content}</div>
              ) : (
                <p className="whitespace-pre-wrap">{msg.content}</p>
              )}
              <div className="mt-1 text-[10px] opacity-50 text-right">
                {msg.stage_number > 0 && `Stage ${msg.stage_number} · `}
                {formatTimestamp(msg.created_at)}
              </div>
            </div>
          </div>
        ))}
        {isSending && (
          <div className="flex gap-2">
            <span className="text-sm">{personaIcon}</span>
            <div className="rounded-lg bg-gray-100 px-3 py-2">
              <Loader2 className="h-3.5 w-3.5 animate-spin text-blue-400" />
            </div>
          </div>
        )}
        <div ref={messagesEndRef} />
      </div>

      {/* Input */}
      <form onSubmit={handleSubmit} className="border-t border-gray-200 px-3 py-2 flex gap-2">
        <input
          value={input}
          onChange={(e) => setInput(e.target.value)}
          disabled={isSending}
          placeholder="Type a message…"
          className="flex-1 rounded border border-gray-200 bg-white px-3 py-2 text-xs text-[#0a2540] placeholder-slate-400 focus:border-blue-500 focus:outline-none disabled:opacity-50"
        />
        <button
          type="submit"
          disabled={!input.trim() || isSending}
          className="rounded bg-blue-600 p-2 text-white hover:bg-blue-500 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
        >
          <Send className="h-3.5 w-3.5" />
        </button>
      </form>
    </div>
  );
}

// ── Progress Bar ────────────────────────────────────────────

function ProgressBar({ value, className }: { value: number; className?: string }) {
  return (
    <div className={clsx('h-2 w-full rounded-full bg-gray-100', className)}>
      <div
        className="h-full rounded-full bg-gradient-to-r from-blue-600 to-blue-400 transition-all duration-500"
        style={{ width: `${Math.min(100, Math.max(0, value))}%` }}
      />
    </div>
  );
}

// ── Main Page ───────────────────────────────────────────────

export default function MissionDetailPage() {
  const { missionId } = useParams<{ missionId: string }>();
  const navigate = useNavigate();
  const { user } = useAuth();

  const {
    activeMission,
    activeStages,
    activeMessages,
    activeArtifacts,
    isLoadingDetail,
    isSendingMessage,
    error,
    fetchMission,
    fetchArtifacts,
    startStage,
    completeStage,
    advanceMission,
    sendMessage,
    deleteMission,
    clearActiveMission,
  } = useMissionStore();
  const { personas, fetchPersonas } = usePersonaStore();

  const [selectedStageNum, setSelectedStageNum] = useState(1);
  const [showDeleteConfirm, setShowDeleteConfirm] = useState(false);

  useEffect(() => {
    if (missionId) {
      fetchMission(missionId);
      fetchArtifacts(missionId);
      fetchPersonas();
    }
    return () => clearActiveMission();
  }, [missionId, fetchMission, fetchArtifacts, fetchPersonas, clearActiveMission]);

  useEffect(() => {
    if (activeMission) {
      setSelectedStageNum(activeMission.current_stage || 1);
    }
  }, [activeMission?.current_stage]);

  const persona = personas.find((p) => p.persona_id === activeMission?.persona_id);
  const selectedStage = activeStages.find((s) => s.stage_number === selectedStageNum);

  const handleSendMessage = useCallback(
    (content: string) => {
      if (missionId) sendMessage(missionId, content);
    },
    [missionId, sendMessage],
  );

  const handleDelete = useCallback(async () => {
    if (missionId) {
      await deleteMission(missionId);
      navigate('/qi/missions');
    }
  }, [missionId, deleteMission, navigate]);

  // ── Loading ─────────────────────────────────────────────
  if (isLoadingDetail || !activeMission) {
    return (
      <div className="flex items-center justify-center py-20">
        <Loader2 className="h-6 w-6 animate-spin text-blue-400" />
        <span className="ml-2 text-sm text-slate-400">Loading mission…</span>
      </div>
    );
  }

  const mission = activeMission;
  const missionBadge = STATUS_BADGE[mission.status] ?? STATUS_BADGE.draft;

  return (
    <div className="space-y-5">
      {/* Header */}
      <div className="flex items-start justify-between gap-4">
        <div className="flex items-center gap-3">
          <button
            onClick={() => navigate('/qi/missions')}
            className="rounded p-1 text-slate-400 hover:text-[#0a2540] hover:bg-gray-100 transition-colors"
          >
            <ArrowLeft className="h-5 w-5" />
          </button>
          <div>
            <div className="flex items-center gap-2">
              <h1 className="text-lg font-bold text-[#0a2540]">{mission.title}</h1>
              <StatusBadge label={missionBadge.label} variant={missionBadge.variant} />
            </div>
            {persona && (
              <p className="text-xs text-slate-8000 mt-0.5">
                {persona.avatar_icon} {persona.name} · Created {formatDate(mission.created_at)}
              </p>
            )}
          </div>
        </div>
        <div className="flex items-center gap-2">
          <button
            onClick={() => setShowDeleteConfirm(true)}
            className="rounded p-1.5 text-slate-8000 hover:text-red-400 hover:bg-gray-100 transition-colors"
            title="Delete mission"
          >
            <Trash2 className="h-4 w-4" />
          </button>
        </div>
      </div>

      {/* Progress */}
      <div className="space-y-1">
        <div className="flex items-center justify-between text-xs">
          <span className="text-slate-400">Mission Progress</span>
          <span className="text-[#0a2540] font-medium">{Math.round(mission.progress_pct)}%</span>
        </div>
        <ProgressBar value={mission.progress_pct} />
      </div>

      {/* Error */}
      {error && (
        <div className="rounded-lg border border-red-800 bg-red-900/30 p-3 text-sm text-red-300 flex items-center gap-2">
          <AlertTriangle className="h-4 w-4 flex-shrink-0" />
          {error}
        </div>
      )}

      {/* Stage Pipeline */}
      <StagePipeline
        stages={activeStages}
        currentStage={mission.current_stage}
        selectedStage={selectedStageNum}
        onSelect={setSelectedStageNum}
      />

      {/* Main Content: Stage Detail + Chat */}
      <div className="grid gap-5 lg:grid-cols-[1fr_380px]">
        <StagePanel
          stage={selectedStage}
          mission={mission}
          artifacts={activeArtifacts}
          onStart={() => missionId && startStage(missionId, selectedStageNum)}
          onComplete={() => missionId && completeStage(missionId, selectedStageNum)}
          onAdvance={() => missionId && advanceMission(missionId, true)}
        />
        <ChatPanel
          messages={activeMessages}
          isSending={isSendingMessage}
          onSend={handleSendMessage}
          personaName={persona?.name ?? 'Assistant'}
          personaIcon={persona?.avatar_icon ?? '🤖'}
        />
      </div>

      {/* Mission Details */}
      {(mission.description || mission.objective) && (
        <div className="rounded-lg border border-gray-200 bg-white p-4">
          {mission.description && (
            <div className="mb-3">
              <h3 className="text-xs font-medium text-slate-400 mb-1">Description</h3>
              <p className="text-sm text-slate-600">{mission.description}</p>
            </div>
          )}
          {mission.objective && (
            <div>
              <h3 className="text-xs font-medium text-slate-400 mb-1">Objective</h3>
              <p className="text-sm text-slate-600">{mission.objective}</p>
            </div>
          )}
        </div>
      )}

      {/* Delete Confirmation */}
      {showDeleteConfirm && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60" onClick={() => setShowDeleteConfirm(false)}>
          <div
            className="w-full max-w-sm rounded-xl border border-gray-200 bg-white p-6 shadow-2xl"
            onClick={(e) => e.stopPropagation()}
          >
            <h3 className="text-sm font-bold text-[#0a2540] mb-2">Delete Mission?</h3>
            <p className="text-xs text-slate-400 mb-4">
              This will permanently delete "{mission.title}" and all associated stages, artifacts, and messages. This action cannot be undone.
            </p>
            <div className="flex justify-end gap-3">
              <button
                onClick={() => setShowDeleteConfirm(false)}
                className="rounded px-3 py-1.5 text-xs text-slate-400 hover:text-[#0a2540]"
              >
                Cancel
              </button>
              <button
                onClick={handleDelete}
                className="rounded bg-red-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-red-500"
              >
                Delete
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
