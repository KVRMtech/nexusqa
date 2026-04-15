// ═══════════════════════════════════════════════════════════════
//  QI ENGINEER PORTAL — Mission Dashboard Page
//  "Mission control for quality intelligence operations"
// ═══════════════════════════════════════════════════════════════

import { useState, useEffect, useCallback } from 'react';
import { useNavigate, useSearchParams } from 'react-router-dom';
import { useAuth } from '../contexts/AuthContext';
import { useMissionStore, usePersonaStore } from '../stores';
import { PageHeader } from '../components/PageHeader';
import { StatCard } from '../components/StatCard';
import { StatusBadge } from '../components/StatusBadge';
import { EmptyState } from '../components/EmptyState';
import type { MissionSummary, MissionStatus, MissionPriority, Persona, CreateMissionRequest } from '../types';
import { STAGE_LABELS } from '../types/qi';
import {
  Target,
  Rocket,
  CheckCircle2,
  XCircle,
  PauseCircle,
  Clock,
  Plus,
  ChevronRight,
  Loader2,
  BarChart3,
  FileStack,
  Users,
  Sparkles,
  Filter,
  Search,
} from 'lucide-react';
import clsx from 'clsx';

// ── Helpers ─────────────────────────────────────────────────

function formatDate(dateStr: string): string {
  return new Date(dateStr).toLocaleDateString('en-US', {
    month: 'short',
    day: 'numeric',
    year: 'numeric',
  });
}

function formatRelativeTime(dateStr: string): string {
  const diff = Date.now() - new Date(dateStr).getTime();
  const mins = Math.floor(diff / 60_000);
  if (mins < 1) return 'just now';
  if (mins < 60) return `${mins}m ago`;
  const hours = Math.floor(mins / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  return `${days}d ago`;
}

const STATUS_CONFIG: Record<MissionStatus, { label: string; variant: 'blue' | 'green' | 'yellow' | 'red' | 'gray'; icon: typeof Target }> = {
  draft: { label: 'Draft', variant: 'gray', icon: Clock },
  active: { label: 'Active', variant: 'blue', icon: Rocket },
  paused: { label: 'Paused', variant: 'yellow', icon: PauseCircle },
  completed: { label: 'Completed', variant: 'green', icon: CheckCircle2 },
  failed: { label: 'Failed', variant: 'red', icon: XCircle },
  cancelled: { label: 'Cancelled', variant: 'gray', icon: XCircle },
};

const PRIORITY_COLORS: Record<MissionPriority, string> = {
  critical: 'text-red-400',
  high: 'text-orange-400',
  medium: 'text-yellow-400',
  low: 'text-slate-400',
};

// ── Stage Progress Bar ──────────────────────────────────────

function StageProgress({ stages, currentStage }: { stages: { stage_number: number; stage_type: string; status: string }[]; currentStage: number }) {
  return (
    <div className="flex items-center gap-1">
      {[1, 2, 3, 4, 5].map((n) => {
        const stage = stages.find((s) => s.stage_number === n);
        const status = stage?.status ?? 'pending';
        return (
          <div
            key={n}
            title={`${STAGE_LABELS[n]}: ${status}`}
            className={clsx(
              'h-1.5 w-6 rounded-full transition-colors',
              status === 'completed' && 'bg-green-500',
              status === 'active' && 'bg-blue-500 animate-pulse',
              status === 'failed' && 'bg-red-500',
              status === 'skipped' && 'bg-slate-600',
              status === 'pending' && 'bg-slate-700',
            )}
          />
        );
      })}
    </div>
  );
}

// ── Mission Card ────────────────────────────────────────────

function MissionCard({ mission, personas, onClick }: { mission: MissionSummary; personas: Persona[]; onClick: () => void }) {
  const cfg = STATUS_CONFIG[mission.status] || STATUS_CONFIG.draft;
  const persona = personas.find((p) => p.persona_id === mission.persona_id);

  return (
    <button
      type="button"
      onClick={onClick}
      className="group w-full text-left rounded-lg border border-slate-700 bg-slate-800/50 p-4 hover:border-slate-500 hover:bg-slate-800 transition-all"
    >
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2 mb-1">
            <StatusBadge label={cfg.label} variant={cfg.variant} />
            {mission.priority !== 'medium' && (
              <span className={clsx('text-xs font-medium uppercase', PRIORITY_COLORS[mission.priority])}>
                {mission.priority}
              </span>
            )}
          </div>
          <h3 className="text-sm font-semibold text-white truncate group-hover:text-blue-300 transition-colors">
            {mission.title}
          </h3>
          {mission.description && (
            <p className="mt-0.5 text-xs text-slate-400 line-clamp-2">{mission.description}</p>
          )}
        </div>
        <ChevronRight className="h-4 w-4 text-slate-600 group-hover:text-slate-400 flex-shrink-0 mt-1" />
      </div>
      <div className="mt-3 flex items-center justify-between gap-4">
        <div className="flex items-center gap-3 text-xs text-slate-500">
          {persona && (
            <span className="flex items-center gap-1">
              <span>{persona.avatar_icon}</span>
              <span>{persona.name}</span>
            </span>
          )}
          <span>Stage {mission.current_stage}/5</span>
          <span>{formatRelativeTime(mission.updated_at)}</span>
        </div>
        <StageProgress stages={mission.stages} currentStage={mission.current_stage} />
      </div>
      {mission.tags.length > 0 && (
        <div className="mt-2 flex flex-wrap gap-1">
          {mission.tags.slice(0, 3).map((tag) => (
            <span key={tag} className="rounded bg-slate-700 px-1.5 py-0.5 text-[10px] text-slate-400">
              {tag}
            </span>
          ))}
          {mission.tags.length > 3 && (
            <span className="text-[10px] text-slate-500">+{mission.tags.length - 3}</span>
          )}
        </div>
      )}
    </button>
  );
}

// ── Create Mission Modal ────────────────────────────────────

function CreateMissionModal({
  isOpen,
  onClose,
  onCreate,
  personas,
  artifactId,
  sessionId,
}: {
  isOpen: boolean;
  onClose: () => void;
  onCreate: (body: CreateMissionRequest) => void;
  personas: Persona[];
  artifactId?: string;
  sessionId?: string;
}) {
  const [title, setTitle] = useState('');
  const [description, setDescription] = useState('');
  const [objective, setObjective] = useState('');
  const [personaId, setPersonaId] = useState('');
  const [priority, setPriority] = useState<MissionPriority>('medium');
  const [tagsInput, setTagsInput] = useState('');

  useEffect(() => {
    if (isOpen) {
      setTitle('');
      setDescription('');
      setObjective('');
      setPersonaId(personas.find((p) => p.is_active)?.persona_id ?? '');
      setPriority('medium');
      setTagsInput('');
    }
  }, [isOpen, personas]);

  if (!isOpen) return null;

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    if (!title.trim() || !personaId) return;
    const tags = tagsInput
      .split(',')
      .map((t) => t.trim())
      .filter(Boolean);
    const body: CreateMissionRequest = { title: title.trim(), description, objective, persona_id: personaId, priority, tags };
    if (artifactId) body.artifact_id = artifactId;
    if (sessionId) body.session_id = sessionId;
    onCreate(body);
    onClose();
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60" onClick={onClose}>
      <div
        className="w-full max-w-lg rounded-xl border border-slate-700 bg-slate-900 p-6 shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        <h2 className="text-lg font-bold text-white mb-4">New Mission</h2>
        {artifactId && (
          <div className="rounded-lg border border-blue-800/40 bg-blue-900/20 p-3 mb-4 flex items-center gap-2">
            <Sparkles className="h-4 w-4 text-blue-400 flex-shrink-0" />
            <span className="text-xs text-blue-300">
              Seeded from canonical artifact <span className="font-mono text-blue-200">{artifactId.slice(0, 12)}…</span>
            </span>
          </div>
        )}
        <form onSubmit={handleSubmit} className="space-y-4">
          <div>
            <label className="block text-xs font-medium text-slate-400 mb-1">Title *</label>
            <input
              value={title}
              onChange={(e) => setTitle(e.target.value)}
              className="w-full rounded border border-slate-600 bg-slate-800 px-3 py-2 text-sm text-white placeholder-slate-500 focus:border-blue-500 focus:outline-none"
              placeholder="e.g. API Compliance Validation for Payment Module"
              required
            />
          </div>
          <div>
            <label className="block text-xs font-medium text-slate-400 mb-1">Persona *</label>
            <select
              value={personaId}
              onChange={(e) => setPersonaId(e.target.value)}
              className="w-full rounded border border-slate-600 bg-slate-800 px-3 py-2 text-sm text-white focus:border-blue-500 focus:outline-none"
              required
            >
              <option value="">Select a persona…</option>
              {personas.filter((p) => p.is_active).map((p) => (
                <option key={p.persona_id} value={p.persona_id}>
                  {p.avatar_icon} {p.name}
                </option>
              ))}
            </select>
          </div>
          <div>
            <label className="block text-xs font-medium text-slate-400 mb-1">Description</label>
            <textarea
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              rows={2}
              className="w-full rounded border border-slate-600 bg-slate-800 px-3 py-2 text-sm text-white placeholder-slate-500 focus:border-blue-500 focus:outline-none resize-none"
              placeholder="Brief description of the mission…"
            />
          </div>
          <div>
            <label className="block text-xs font-medium text-slate-400 mb-1">Objective</label>
            <textarea
              value={objective}
              onChange={(e) => setObjective(e.target.value)}
              rows={2}
              className="w-full rounded border border-slate-600 bg-slate-800 px-3 py-2 text-sm text-white placeholder-slate-500 focus:border-blue-500 focus:outline-none resize-none"
              placeholder="What should this mission accomplish?"
            />
          </div>
          <div className="flex gap-4">
            <div className="flex-1">
              <label className="block text-xs font-medium text-slate-400 mb-1">Priority</label>
              <select
                value={priority}
                onChange={(e) => setPriority(e.target.value as MissionPriority)}
                className="w-full rounded border border-slate-600 bg-slate-800 px-3 py-2 text-sm text-white focus:border-blue-500 focus:outline-none"
              >
                <option value="low">Low</option>
                <option value="medium">Medium</option>
                <option value="high">High</option>
                <option value="critical">Critical</option>
              </select>
            </div>
            <div className="flex-1">
              <label className="block text-xs font-medium text-slate-400 mb-1">Tags</label>
              <input
                value={tagsInput}
                onChange={(e) => setTagsInput(e.target.value)}
                className="w-full rounded border border-slate-600 bg-slate-800 px-3 py-2 text-sm text-white placeholder-slate-500 focus:border-blue-500 focus:outline-none"
                placeholder="api, compliance, payment"
              />
            </div>
          </div>
          <div className="flex justify-end gap-3 pt-2">
            <button
              type="button"
              onClick={onClose}
              className="rounded px-4 py-2 text-sm text-slate-400 hover:text-white transition-colors"
            >
              Cancel
            </button>
            <button
              type="submit"
              disabled={!title.trim() || !personaId}
              className="rounded bg-blue-600 px-4 py-2 text-sm font-medium text-white hover:bg-blue-500 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
            >
              Create Mission
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}

// ── Main Page ───────────────────────────────────────────────

export default function MissionDashboardPage() {
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const artifactId = searchParams.get('artifact_id') || undefined;
  const sessionId = searchParams.get('session_id') || undefined;
  const { user } = useAuth();

  const {
    missions,
    totalMissions,
    isLoadingList,
    isLoadingDashboard,
    dashboard,
    statusFilter,
    error,
    fetchMissions,
    fetchDashboard,
    setFilters,
    createMission,
  } = useMissionStore();
  const { personas, isLoading: isLoadingPersonas, fetchPersonas } = usePersonaStore();

  const [showCreate, setShowCreate] = useState(!!artifactId);
  const [searchQuery, setSearchQuery] = useState('');

  useEffect(() => {
    fetchPersonas();
    fetchDashboard();
    fetchMissions();
  }, [fetchPersonas, fetchDashboard, fetchMissions]);

  const handleCreate = useCallback(
    async (body: CreateMissionRequest) => {
      try {
        const mission = await createMission(body);
        navigate(`/qi/missions/${mission.mission_id}`);
      } catch {
        // Error handled in store
      }
    },
    [createMission, navigate],
  );

  const handleStatusFilter = useCallback(
    (status: string | undefined) => {
      setFilters({ status });
      fetchMissions({ status });
    },
    [setFilters, fetchMissions],
  );

  const filteredMissions = searchQuery
    ? missions.filter(
        (m) =>
          m.title.toLowerCase().includes(searchQuery.toLowerCase()) ||
          m.description?.toLowerCase().includes(searchQuery.toLowerCase()) ||
          m.tags.some((t) => t.toLowerCase().includes(searchQuery.toLowerCase())),
      )
    : missions;

  // ── Dashboard Stats ─────────────────────────────────────
  const stats = dashboard;

  return (
    <div className="space-y-6">
      <PageHeader
        title="Mission Control"
        subtitle="Quality intelligence missions powered by AI personas"
        actions={
          <button
            onClick={() => setShowCreate(true)}
            className="flex items-center gap-2 rounded-lg bg-blue-600 px-4 py-2 text-sm font-medium text-white hover:bg-blue-500 transition-colors"
          >
            <Plus className="h-4 w-4" />
            New Mission
          </button>
        }
      />

      {/* Dashboard Stats */}
      {stats && (
        <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
          <StatCard
            label="Total Missions"
            value={stats.total_missions}
            icon={<Target className="h-5 w-5 text-blue-400" />}
          />
          <StatCard
            label="Active"
            value={stats.status_counts?.active ?? 0}
            icon={<Rocket className="h-5 w-5 text-green-400" />}
          />
          <StatCard
            label="Completed"
            value={stats.status_counts?.completed ?? 0}
            icon={<CheckCircle2 className="h-5 w-5 text-emerald-400" />}
          />
          <StatCard
            label="Artifacts"
            value={stats.total_artifacts}
            icon={<FileStack className="h-5 w-5 text-purple-400" />}
          />
        </div>
      )}

      {/* Filters Bar */}
      <div className="flex flex-wrap items-center gap-3">
        <div className="relative flex-1 min-w-[200px] max-w-sm">
          <Search className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-slate-500" />
          <input
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            placeholder="Search missions…"
            className="w-full rounded-lg border border-slate-700 bg-slate-800 pl-9 pr-3 py-2 text-sm text-white placeholder-slate-500 focus:border-blue-500 focus:outline-none"
          />
        </div>
        <div className="flex items-center gap-1 rounded-lg border border-slate-700 bg-slate-800 p-1">
          {[
            { key: undefined, label: 'All' },
            { key: 'active', label: 'Active' },
            { key: 'draft', label: 'Draft' },
            { key: 'completed', label: 'Done' },
            { key: 'paused', label: 'Paused' },
          ].map(({ key, label }) => (
            <button
              key={label}
              onClick={() => handleStatusFilter(key)}
              className={clsx(
                'rounded px-3 py-1 text-xs font-medium transition-colors',
                statusFilter === key
                  ? 'bg-blue-600 text-white'
                  : 'text-slate-400 hover:text-white hover:bg-slate-700',
              )}
            >
              {label}
            </button>
          ))}
        </div>
      </div>

      {/* Error */}
      {error && (
        <div className="rounded-lg border border-red-800 bg-red-900/30 p-3 text-sm text-red-300">
          {error}
        </div>
      )}

      {/* Loading */}
      {isLoadingList && (
        <div className="flex items-center justify-center py-12">
          <Loader2 className="h-6 w-6 animate-spin text-blue-400" />
          <span className="ml-2 text-sm text-slate-400">Loading missions…</span>
        </div>
      )}

      {/* Mission List */}
      {!isLoadingList && filteredMissions.length === 0 && (
        <EmptyState
          icon={<Target className="h-12 w-12 text-slate-600" />}
          title="No missions yet"
          description="Create your first QI mission to start generating quality intelligence."
          action={
            <button
              onClick={() => setShowCreate(true)}
              className="flex items-center gap-2 rounded-lg bg-blue-600 px-4 py-2 text-sm font-medium text-white hover:bg-blue-500"
            >
              <Plus className="h-4 w-4" />
              Create Mission
            </button>
          }
        />
      )}

      {!isLoadingList && filteredMissions.length > 0 && (
        <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
          {filteredMissions.map((mission) => (
            <MissionCard
              key={mission.mission_id}
              mission={mission}
              personas={personas}
              onClick={() => navigate(`/qi/missions/${mission.mission_id}`)}
            />
          ))}
        </div>
      )}

      {/* Pagination hint */}
      {!isLoadingList && totalMissions > filteredMissions.length && (
        <div className="text-center text-xs text-slate-500">
          Showing {filteredMissions.length} of {totalMissions} missions
        </div>
      )}

      {/* Create Modal */}
      <CreateMissionModal
        isOpen={showCreate}
        onClose={() => setShowCreate(false)}
        onCreate={handleCreate}
        personas={personas}
        artifactId={artifactId}
        sessionId={sessionId}
      />
    </div>
  );
}
