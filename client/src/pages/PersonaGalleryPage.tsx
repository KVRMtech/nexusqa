// ═══════════════════════════════════════════════════════════════
//  QI ENGINEER PORTAL — Persona Gallery Page
//  "Browse and manage AI quality intelligence personas"
// ═══════════════════════════════════════════════════════════════

import { useState, useEffect, useCallback } from 'react';
import { useNavigate } from 'react-router-dom';
import { useAuth } from '../contexts/AuthContext';
import { usePersonaStore, selectSystemPersonas, selectCustomPersonas } from '../stores';
import { useMissionStore } from '../stores';
import { PageHeader } from '../components/PageHeader';
import { StatusBadge } from '../components/StatusBadge';
import { EmptyState } from '../components/EmptyState';
import type { Persona, CreatePersonaRequest, CreateMissionRequest } from '../types';
import { STAGE_LABELS } from '../types/qi';
import {
  Users,
  Plus,
  Brain,
  Shield,
  Code,
  Database,
  BookOpen,
  Loader2,
  Star,
  CheckCircle2,
  ChevronRight,
  Sparkles,
  Settings,
  Rocket,
} from 'lucide-react';
import clsx from 'clsx';

// ── Stage Config Display ────────────────────────────────────

function StageChips({ stageConfig }: { stageConfig: Record<string, { engines: string[]; auto_advance: boolean }> }) {
  const stages = Object.entries(stageConfig);
  if (stages.length === 0) return null;

  return (
    <div className="flex flex-wrap gap-1 mt-2">
      {stages.map(([stageType, cfg]) => {
        const stageNum = ['capture', 'understand', 'strategize', 'generate', 'validate'].indexOf(stageType) + 1;
        return (
          <div
            key={stageType}
            title={`${STAGE_LABELS[stageNum] || stageType}: ${cfg.engines.join(', ')}`}
            className="rounded bg-slate-700 px-1.5 py-0.5 text-[10px] text-slate-400"
          >
            {STAGE_LABELS[stageNum] || stageType}: {cfg.engines.length} engines
          </div>
        );
      })}
    </div>
  );
}

// ── Persona Card ────────────────────────────────────────────

function PersonaCard({
  persona,
  onSelect,
  onStartMission,
}: {
  persona: Persona;
  onSelect: () => void;
  onStartMission: () => void;
}) {
  return (
    <div className="group rounded-lg border border-slate-700 bg-slate-800/50 p-4 hover:border-slate-500 hover:bg-slate-800 transition-all">
      <div className="flex items-start gap-3">
        <div className="flex-shrink-0 flex items-center justify-center h-10 w-10 rounded-lg bg-slate-700 text-lg">
          {persona.avatar_icon || '🤖'}
        </div>
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2">
            <h3 className="text-sm font-semibold text-white truncate">{persona.name}</h3>
            {persona.is_system && (
              <span className="rounded bg-blue-900/30 px-1.5 py-0.5 text-[10px] font-medium text-blue-400">
                System
              </span>
            )}
            {!persona.is_active && (
              <StatusBadge label="Inactive" variant="gray" />
            )}
          </div>
          <p className="mt-0.5 text-xs text-slate-400 line-clamp-2">{persona.description}</p>
        </div>
      </div>

      {/* Capabilities */}
      {persona.capabilities.length > 0 && (
        <div className="mt-3 flex flex-wrap gap-1">
          {persona.capabilities.slice(0, 4).map((cap) => (
            <span
              key={cap}
              className="rounded bg-slate-700/70 px-1.5 py-0.5 text-[10px] text-slate-300"
            >
              {cap}
            </span>
          ))}
          {persona.capabilities.length > 4 && (
            <span className="text-[10px] text-slate-500">
              +{persona.capabilities.length - 4} more
            </span>
          )}
        </div>
      )}

      {/* Stage Config */}
      {persona.stage_config && Object.keys(persona.stage_config).length > 0 && (
        <StageChips stageConfig={persona.stage_config} />
      )}

      {/* Specialty Domains */}
      {persona.specialty_domains && persona.specialty_domains.length > 0 && (
        <div className="mt-2 flex flex-wrap gap-1">
          {persona.specialty_domains.slice(0, 3).map((domain) => (
            <span
              key={domain}
              className="rounded-full bg-purple-900/30 px-2 py-0.5 text-[10px] text-purple-300"
            >
              {domain}
            </span>
          ))}
        </div>
      )}

      {/* Actions */}
      <div className="mt-3 flex items-center justify-between pt-2 border-t border-slate-700/50">
        <button
          onClick={onSelect}
          className="text-[11px] text-slate-400 hover:text-white transition-colors flex items-center gap-1"
        >
          <Settings className="h-3 w-3" />
          Details
        </button>
        <button
          onClick={onStartMission}
          className="flex items-center gap-1 rounded bg-blue-600/80 px-2.5 py-1 text-[11px] font-medium text-white hover:bg-blue-500 transition-colors"
        >
          <Rocket className="h-3 w-3" />
          Start Mission
        </button>
      </div>
    </div>
  );
}

// ── Persona Detail Drawer ───────────────────────────────────

function PersonaDetailDrawer({
  persona,
  onClose,
}: {
  persona: Persona | null;
  onClose: () => void;
}) {
  if (!persona) return null;

  return (
    <div className="fixed inset-0 z-50 flex justify-end bg-black/50" onClick={onClose}>
      <div
        className="w-full max-w-md bg-slate-900 border-l border-slate-700 overflow-y-auto"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="p-6 space-y-5">
          {/* Header */}
          <div className="flex items-center gap-3">
            <div className="flex items-center justify-center h-12 w-12 rounded-lg bg-slate-700 text-xl">
              {persona.avatar_icon || '🤖'}
            </div>
            <div>
              <h2 className="text-base font-bold text-white">{persona.name}</h2>
              <p className="text-xs text-slate-500">{persona.slug}</p>
            </div>
          </div>

          {/* Description */}
          <div>
            <h3 className="text-xs font-medium text-slate-400 mb-1">Description</h3>
            <p className="text-sm text-slate-300">{persona.description}</p>
          </div>

          {/* System Prompt */}
          {persona.system_prompt && (
            <div>
              <h3 className="text-xs font-medium text-slate-400 mb-1">System Prompt</h3>
              <pre className="rounded bg-slate-800 p-3 text-xs text-slate-300 whitespace-pre-wrap max-h-40 overflow-y-auto">
                {persona.system_prompt}
              </pre>
            </div>
          )}

          {/* Capabilities */}
          {persona.capabilities.length > 0 && (
            <div>
              <h3 className="text-xs font-medium text-slate-400 mb-1">Capabilities</h3>
              <div className="flex flex-wrap gap-1.5">
                {persona.capabilities.map((cap) => (
                  <span key={cap} className="rounded bg-slate-700 px-2 py-0.5 text-xs text-slate-300">
                    {cap}
                  </span>
                ))}
              </div>
            </div>
          )}

          {/* Stage Config */}
          {persona.stage_config && Object.keys(persona.stage_config).length > 0 && (
            <div>
              <h3 className="text-xs font-medium text-slate-400 mb-2">Stage Configuration</h3>
              <div className="space-y-2">
                {Object.entries(persona.stage_config).map(([stage, cfg]) => {
                  const num = ['capture', 'understand', 'strategize', 'generate', 'validate'].indexOf(stage) + 1;
                  return (
                    <div key={stage} className="rounded border border-slate-700 bg-slate-800 p-2.5">
                      <div className="flex items-center justify-between mb-1">
                        <span className="text-xs font-medium text-white">
                          {num > 0 ? `Stage ${num}: ${STAGE_LABELS[num]}` : stage}
                        </span>
                        {cfg.auto_advance && (
                          <span className="text-[10px] text-green-400">Auto-advance</span>
                        )}
                      </div>
                      <div className="flex flex-wrap gap-1">
                        {cfg.engines.map((engine: string) => (
                          <span key={engine} className="rounded bg-slate-700 px-1.5 py-0.5 text-[10px] text-slate-400">
                            {engine}
                          </span>
                        ))}
                      </div>
                    </div>
                  );
                })}
              </div>
            </div>
          )}

          {/* Specialty Domains */}
          {persona.specialty_domains && persona.specialty_domains.length > 0 && (
            <div>
              <h3 className="text-xs font-medium text-slate-400 mb-1">Specialty Domains</h3>
              <div className="flex flex-wrap gap-1.5">
                {persona.specialty_domains.map((d) => (
                  <span key={d} className="rounded-full bg-purple-900/30 px-2.5 py-0.5 text-xs text-purple-300">
                    {d}
                  </span>
                ))}
              </div>
            </div>
          )}

          {/* Meta */}
          <div className="border-t border-slate-700 pt-3 space-y-1 text-xs text-slate-500">
            <p>ID: {persona.persona_id}</p>
            <p>Tenant: {persona.is_system ? 'System (all tenants)' : persona.tenant_id}</p>
            <p>Sort order: {persona.sort_order}</p>
            <p>Created: {new Date(persona.created_at).toLocaleString()}</p>
          </div>

          <button
            onClick={onClose}
            className="w-full rounded border border-slate-600 px-4 py-2 text-xs text-slate-400 hover:text-white hover:bg-slate-800 transition-colors"
          >
            Close
          </button>
        </div>
      </div>
    </div>
  );
}

// ── Main Page ───────────────────────────────────────────────

export default function PersonaGalleryPage() {
  const navigate = useNavigate();
  const { user } = useAuth();
  const { personas, isLoading, error, fetchPersonas, selectPersona, selectedPersona } = usePersonaStore();
  const { createMission } = useMissionStore();
  const [showDetail, setShowDetail] = useState(false);

  useEffect(() => {
    fetchPersonas();
  }, [fetchPersonas]);

  const systemPersonas = selectSystemPersonas(usePersonaStore.getState());
  const customPersonas = selectCustomPersonas(usePersonaStore.getState());

  const handleViewDetail = useCallback(
    (persona: Persona) => {
      selectPersona(persona);
      setShowDetail(true);
    },
    [selectPersona],
  );

  const handleStartMission = useCallback(
    async (persona: Persona) => {
      try {
        const mission = await createMission({
          title: `New ${persona.name} Mission`,
          persona_id: persona.persona_id,
          priority: 'medium',
        });
        navigate(`/qi/missions/${mission.mission_id}`);
      } catch {
        // Error handled in store
      }
    },
    [createMission, navigate],
  );

  return (
    <div className="space-y-6">
      <PageHeader
        title="Persona Gallery"
        subtitle="AI-powered quality intelligence personas with specialized capabilities"
      />

      {/* Error */}
      {error && (
        <div className="rounded-lg border border-red-800 bg-red-900/30 p-3 text-sm text-red-300">
          {error}
        </div>
      )}

      {/* Loading */}
      {isLoading && (
        <div className="flex items-center justify-center py-12">
          <Loader2 className="h-6 w-6 animate-spin text-blue-400" />
          <span className="ml-2 text-sm text-slate-400">Loading personas…</span>
        </div>
      )}

      {/* System Personas */}
      {!isLoading && systemPersonas.length > 0 && (
        <div>
          <div className="flex items-center gap-2 mb-3">
            <Star className="h-4 w-4 text-blue-400" />
            <h2 className="text-sm font-semibold text-white">System Personas</h2>
            <span className="text-xs text-slate-500">({systemPersonas.length})</span>
          </div>
          <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
            {systemPersonas.map((persona) => (
              <PersonaCard
                key={persona.persona_id}
                persona={persona}
                onSelect={() => handleViewDetail(persona)}
                onStartMission={() => handleStartMission(persona)}
              />
            ))}
          </div>
        </div>
      )}

      {/* Custom Personas */}
      {!isLoading && customPersonas.length > 0 && (
        <div>
          <div className="flex items-center gap-2 mb-3">
            <Users className="h-4 w-4 text-purple-400" />
            <h2 className="text-sm font-semibold text-white">Custom Personas</h2>
            <span className="text-xs text-slate-500">({customPersonas.length})</span>
          </div>
          <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
            {customPersonas.map((persona) => (
              <PersonaCard
                key={persona.persona_id}
                persona={persona}
                onSelect={() => handleViewDetail(persona)}
                onStartMission={() => handleStartMission(persona)}
              />
            ))}
          </div>
        </div>
      )}

      {/* Empty */}
      {!isLoading && personas.length === 0 && (
        <EmptyState
          icon={<Users className="h-12 w-12 text-slate-600" />}
          title="No personas available"
          description="Personas will be seeded when the database migration runs."
        />
      )}

      {/* Detail Drawer */}
      <PersonaDetailDrawer
        persona={showDetail ? selectedPersona : null}
        onClose={() => setShowDetail(false)}
      />
    </div>
  );
}
