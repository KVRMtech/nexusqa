// ═══════════════════════════════════════════════════════════════
//  MODULE 9 — TEST DATA FORGE
//  "Synthetic test data generator with combinatorial coverage"
// ═══════════════════════════════════════════════════════════════

import { useState } from 'react';
import api from '../services/api';
import { useApiData } from '../hooks/useApiData';
import { PageHeader, Tabs } from '../components';
import { EmptyState } from '../components/EmptyState';
import {
  Hammer,
  Sparkles,
  Download,
  Upload,
  Play,
  CheckCircle2,
  Clock,
  ChevronDown,
  ChevronUp,
  Layers,
  BarChart3,
  Copy,
  Plus,
  Trash2,
  RefreshCw,
  FileJson,
  Table2,
} from 'lucide-react';
import clsx from 'clsx';

// ── Types ─────────────────────────────────────────────────

interface ForgeConfig {
  id: string;
  name: string;
  product: string;
  jurisdiction: string;
  count: number;
  strategy: 'boundary' | 'combinatorial' | 'random' | 'edge-case';
  fields: ForgeField[];
}

interface ForgeField {
  name: string;
  type: string;
  constraints: string;
}

interface ForgeResult {
  id: string;
  configName: string;
  status: 'completed' | 'generating' | 'failed';
  recordsGenerated: number;
  coverage: number;
  generatedAt: string;
  duration: string;
  format: string;
  sizeKB: number;
}

// ── Empty fallback (production) ─────────────────────────────

const EMPTY_CONFIGS: ForgeConfig[] = [];

const EMPTY_RESULTS: ForgeResult[] = [];

const STRATEGY_DESCRIPTIONS: Record<string, string> = {
  boundary: 'Focus on boundary values and edge thresholds',
  combinatorial: 'Cover all pairwise combinations of field values',
  random: 'Uniform random sampling within constraints',
  'edge-case': 'Unusual combinations that expose hidden defects',
};

// ── Component ─────────────────────────────────────────────

export default function DataForgePage() {
  const { data: configs, isLive } = useApiData(
    () => api.listDataForgeConfigs('t-1'),
    EMPTY_CONFIGS,
  );
  const { data: results } = useApiData(
    () => api.listDataForgeResults('t-1'),
    EMPTY_RESULTS,
  );
  const [selectedConfig, setSelectedConfig] = useState<string | null>(null);
  const [expandedResult, setExpandedResult] = useState<string | null>(null);
  const [activeTab, setActiveTab] = useState<'configs' | 'results'>('configs');

  const totalRecords = results.filter((r) => r.status === 'completed').reduce((s, r) => s + r.recordsGenerated, 0);
  const avgCoverage =
    results.filter((r) => r.status === 'completed').reduce((s, r) => s + r.coverage, 0) /
    Math.max(results.filter((r) => r.status === 'completed').length, 1);
  const activeGen = results.filter((r) => r.status === 'generating').length;

  return (
    <div className="space-y-6 animate-fade-in">
      {/* Header */}
      <PageHeader
        title="Test Data Forge"
        subtitle="Generate synthetic test data with intelligent combinatorial coverage."
        isLive={isLive}
      />

      {/* Summary */}
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
        <div className="stat-card p-4">
          <p className="text-[11px] font-medium text-gray-500 uppercase tracking-wider">Configurations</p>
          <p className="text-2xl font-bold text-white mt-1">{configs.length}</p>
        </div>
        <div className="stat-card p-4">
          <p className="text-[11px] font-medium text-gray-500 uppercase tracking-wider">Records Generated</p>
          <p className="text-2xl font-bold text-amber-400 mt-1">{totalRecords.toLocaleString()}</p>
        </div>
        <div className="stat-card p-4">
          <p className="text-[11px] font-medium text-gray-500 uppercase tracking-wider">Avg Coverage</p>
          <p className="text-2xl font-bold text-green-400 mt-1">{avgCoverage.toFixed(1)}%</p>
        </div>
        <div className="stat-card p-4">
          <p className="text-[11px] font-medium text-gray-500 uppercase tracking-wider">Generating Now</p>
          <p className="text-2xl font-bold text-blue-400 mt-1 flex items-center gap-2">
            {activeGen}
            {activeGen > 0 && <RefreshCw className="h-4 w-4 animate-spin" />}
          </p>
        </div>
      </div>

      {/* Tabs */}
      <div className="flex items-center gap-1 border-b border-white/[0.06]">
        <Tabs
          tabs={[
            { id: 'configs', label: 'Configurations' },
            { id: 'results', label: 'Generated Data' },
          ]}
          activeTab={activeTab}
          onChange={(id) => setActiveTab(id as typeof activeTab)}
        />
        <div className="ml-auto pb-1">
          <button className="btn-primary text-xs py-1.5 px-3">
            <Plus className="h-3.5 w-3.5" /> New Configuration
          </button>
        </div>
      </div>

      {/* Configs tab */}
      {activeTab === 'configs' && (
        <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
          {/* Config list */}
          <div className="lg:col-span-1 space-y-2">
            {configs.length === 0 && (
              <EmptyState title="No Configurations" description="Create a test data configuration to get started." />
            )}
            {configs.map((cfg) => (
              <button
                key={cfg.id}
                onClick={() => setSelectedConfig(cfg.id)}
                className={clsx(
                  'card w-full p-4 text-left transition-all',
                  selectedConfig === cfg.id ? 'ring-amber-500/30 bg-amber-500/5' : 'hover:bg-white/[0.02]',
                )}
              >
                <div className="flex items-center justify-between mb-1">
                  <h3 className="text-sm font-medium text-gray-200">{cfg.name}</h3>
                  <span className={clsx(
                    'text-[10px] font-semibold uppercase tracking-wider px-1.5 py-0.5 rounded',
                    cfg.strategy === 'boundary' && 'bg-amber-500/15 text-amber-400',
                    cfg.strategy === 'combinatorial' && 'bg-purple-500/15 text-purple-400',
                    cfg.strategy === 'random' && 'bg-blue-500/15 text-blue-400',
                    cfg.strategy === 'edge-case' && 'bg-red-500/15 text-red-400',
                  )}>
                    {cfg.strategy}
                  </span>
                </div>
                <p className="text-[11px] text-gray-500">{cfg.product} • {cfg.jurisdiction} • {cfg.count.toLocaleString()} records</p>
              </button>
            ))}
          </div>

          {/* Config detail */}
          <div className="lg:col-span-2">
            {selectedConfig && (() => {
              const cfg = configs.find((c) => c.id === selectedConfig);
              if (!cfg) return null;
              return (
                <div className="card p-5 space-y-5">
                  <div className="flex items-center justify-between">
                    <div>
                      <h2 className="text-lg font-semibold text-white">{cfg.name}</h2>
                      <p className="text-xs text-gray-500 mt-0.5">{STRATEGY_DESCRIPTIONS[cfg.strategy]}</p>
                    </div>
                    <div className="flex gap-2">
                      <button className="btn-primary text-xs py-1.5 px-3">
                        <Play className="h-3.5 w-3.5" /> Generate
                      </button>
                      <button className="btn-ghost text-xs py-1.5 px-3">
                        <Copy className="h-3.5 w-3.5" /> Clone
                      </button>
                    </div>
                  </div>

                  {/* Config details */}
                  <div className="grid grid-cols-3 gap-3">
                    <div className="rounded-lg bg-white/[0.03] p-3">
                      <p className="text-[10px] text-gray-500 uppercase tracking-wider">Product</p>
                      <p className="text-sm text-gray-200 mt-0.5">{cfg.product}</p>
                    </div>
                    <div className="rounded-lg bg-white/[0.03] p-3">
                      <p className="text-[10px] text-gray-500 uppercase tracking-wider">Jurisdiction</p>
                      <p className="text-sm text-gray-200 mt-0.5">{cfg.jurisdiction}</p>
                    </div>
                    <div className="rounded-lg bg-white/[0.03] p-3">
                      <p className="text-[10px] text-gray-500 uppercase tracking-wider">Count</p>
                      <p className="text-sm text-gray-200 mt-0.5">{cfg.count.toLocaleString()} records</p>
                    </div>
                  </div>

                  {/* Fields */}
                  <div>
                    <p className="text-[11px] font-semibold text-gray-500 uppercase tracking-wider mb-2">
                      DATA FIELDS ({cfg.fields.length})
                    </p>
                    <div className="space-y-1.5">
                      {cfg.fields.map((f, i) => (
                        <div
                          key={i}
                          className="flex items-center gap-3 rounded-lg bg-white/[0.03] p-2.5 hover:bg-white/[0.05] transition-colors"
                        >
                          <div className="flex h-6 w-6 items-center justify-center rounded bg-white/5 text-[10px] font-bold text-gray-500">
                            {i + 1}
                          </div>
                          <div className="flex-1 min-w-0">
                            <div className="flex items-center gap-2">
                              <span className="text-xs font-mono text-amber-400">{f.name}</span>
                              <span className="text-[10px] text-gray-600">({f.type})</span>
                            </div>
                            <p className="text-[10px] text-gray-500 truncate">{f.constraints}</p>
                          </div>
                          <button className="text-gray-600 hover:text-red-400 transition-colors">
                            <Trash2 className="h-3.5 w-3.5" />
                          </button>
                        </div>
                      ))}
                    </div>
                  </div>

                  {/* Coverage preview */}
                  <div className="rounded-lg bg-gradient-to-br from-amber-500/5 to-orange-500/5 p-4 ring-1 ring-amber-500/10">
                    <div className="flex items-center gap-2 mb-2">
                      <Sparkles className="h-4 w-4 text-amber-400" />
                      <p className="text-xs font-semibold text-amber-400">Coverage Estimate</p>
                    </div>
                    <div className="grid grid-cols-3 gap-3 text-center">
                      <div>
                        <p className="text-lg font-bold text-white">{cfg.fields.length}</p>
                        <p className="text-[10px] text-gray-500">Fields</p>
                      </div>
                      <div>
                        <p className="text-lg font-bold text-white">
                          {cfg.strategy === 'combinatorial' ? '100%' : `~${Math.min(95, 75 + cfg.fields.length * 3)}%`}
                        </p>
                        <p className="text-[10px] text-gray-500">Est. Coverage</p>
                      </div>
                      <div>
                        <p className="text-lg font-bold text-white">{cfg.count.toLocaleString()}</p>
                        <p className="text-[10px] text-gray-500">Records</p>
                      </div>
                    </div>
                  </div>
                </div>
              );
            })()}
          </div>
        </div>
      )}

      {/* Results tab */}
      {activeTab === 'results' && (
        <div className="space-y-3">
          {results.length === 0 && (
            <EmptyState title="No Generated Data" description="Run a configuration to generate test data." />
          )}
          {results.map((res) => (
            <div key={res.id} className="card overflow-hidden">
              <div className="flex items-center gap-4 p-4">
                {res.status === 'completed' ? (
                  <CheckCircle2 className="h-5 w-5 text-green-400 shrink-0" />
                ) : res.status === 'generating' ? (
                  <RefreshCw className="h-5 w-5 text-blue-400 shrink-0 animate-spin" />
                ) : (
                  <Clock className="h-5 w-5 text-gray-500 shrink-0" />
                )}

                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-2">
                    {res.status === 'completed' && <span className="badge-green">READY</span>}
                    {res.status === 'generating' && <span className="badge-blue">GENERATING</span>}
                    <h3 className="text-sm font-medium text-gray-200">{res.configName}</h3>
                  </div>
                  <div className="flex items-center gap-4 mt-1 text-[11px] text-gray-500">
                    <span>{res.recordsGenerated.toLocaleString()} records</span>
                    <span>Coverage: <span className={clsx(res.coverage >= 90 ? 'text-green-400' : 'text-yellow-400')}>{res.coverage}%</span></span>
                    <span>{res.duration}</span>
                    <span>{res.format}</span>
                    {res.sizeKB > 0 && <span>{res.sizeKB}KB</span>}
                  </div>

                  {/* Progress bar for generating */}
                  {res.status === 'generating' && (
                    <div className="mt-2 h-1.5 rounded-full bg-white/5 overflow-hidden w-48">
                      <div
                        className="h-full bg-gradient-to-r from-amber-500 to-orange-400 rounded-full animate-gradient-x"
                        style={{ width: `${res.coverage}%` }}
                      />
                    </div>
                  )}
                </div>

                {res.status === 'completed' && (
                  <div className="flex gap-2 shrink-0">
                    <button className="btn-ghost text-xs py-1.5 px-2">
                      <FileJson className="h-3.5 w-3.5" /> JSON
                    </button>
                    <button className="btn-ghost text-xs py-1.5 px-2">
                      <Table2 className="h-3.5 w-3.5" /> CSV
                    </button>
                    <button className="btn-ghost text-xs py-1.5 px-2">
                      <Upload className="h-3.5 w-3.5" /> Push to Env
                    </button>
                  </div>
                )}
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
