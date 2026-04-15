// ═══════════════════════════════════════════════════════════════
//  E2E Architect Workspace Page — 3-Panel Layout
//
//  Route: /sessions/:sessionId/e2e-architect?artifact_id=...
//  Calls POST /v1/e2e-architect/generate on mount
//
//  Panel Layout:
//    LEFT:   Variables + Decision Points + Coverage + Provenance
//    CENTER: E2E Scenarios (filterable, searchable, data matrix)
//    RIGHT:  Pairwise Coverage + Variable Impact Map
//
//  Key features:
//    - CSV export of all E2E scenarios
//    - Search + category filtering
//    - Data matrix cards per scenario
//    - Evidence trace with confidence scores
//    - Pairwise combination coverage stats
// ═══════════════════════════════════════════════════════════════

import { useState, useEffect, useCallback, useMemo } from 'react';
import { useParams, useSearchParams, useNavigate } from 'react-router-dom';
import { useAuth } from '../contexts/AuthContext';
import api from '../services/api';
import type {
  E2EArchitectResponse,
  E2EScenario,
  E2EVariable,
  DecisionPoint,
  E2ECoverageAnalysis,
  TestStep,
  EvidenceCitation,
  TestStrategyProvenance,
} from '../types/canonical';
import clsx from 'clsx';
import {
  ArrowLeft,
  Sparkles,
  Target,
  AlertTriangle,
  CheckCircle2,
  Loader2,
  ChevronRight,
  ChevronDown,
  Eye,
  Mic,
  Zap,
  BarChart3,
  Layers,
  Download,
  Search,
  X,
  ArrowRight,
  GitBranch,
  Variable,
  Shuffle,
  Shield,
  Clock,
  Activity,
} from 'lucide-react';

// ── Configs ─────────────────────────────────────────────────

type PageState = 'loading' | 'generating' | 'ready' | 'error' | 'prerequisites';

const PRIORITY_CONFIG: Record<string, { label: string; color: string; bg: string }> = {
  P0_critical: { label: 'P0 Critical', color: 'text-red-400', bg: 'bg-red-500/20 border-red-500/30' },
  P1_high:     { label: 'P1 High',     color: 'text-orange-400', bg: 'bg-orange-500/20 border-orange-500/30' },
  P2_medium:   { label: 'P2 Medium',   color: 'text-yellow-400', bg: 'bg-yellow-500/20 border-yellow-500/30' },
  P3_low:      { label: 'P3 Low',      color: 'text-blue-400', bg: 'bg-blue-500/20 border-blue-500/30' },
};

const CATEGORY_CONFIG: Record<string, { label: string; icon: React.ReactNode; color: string }> = {
  observed:              { label: 'Observed',        icon: <Eye className="h-3.5 w-3.5" />,        color: 'text-green-400 bg-green-500/20 border-green-500/30' },
  inferred_high_risk:    { label: 'Inferred Risk',   icon: <AlertTriangle className="h-3.5 w-3.5" />, color: 'text-red-400 bg-red-500/20 border-red-500/30' },
  boundary_assumption:   { label: 'Boundary',        icon: <Target className="h-3.5 w-3.5" />,     color: 'text-purple-400 bg-purple-500/20 border-purple-500/30' },
};

// ── CSV Export ─────────────────────────────────────────────

function exportE2EScenarioCSV(scenarios: E2EScenario[], filename: string) {
  const header = [
    'Scenario ID', 'Title', 'Category', 'Priority', 'Rationale',
    'Preconditions', 'Step #', 'Action', 'Input Data', 'Expected Behavior',
    'Expected Outcome', 'Data Matrix', 'Workflow Steps', 'Risk Areas', 'Evidence',
  ];
  const rows: string[][] = [];

  for (const sc of scenarios) {
    const dataMatrixStr = sc.data_matrix
      .map(dm => Object.entries(dm).map(([k, v]) => `${k}=${v}`).join(', '))
      .join(' | ');
    const wfSteps = sc.workflow_steps_covered.join(', ');
    const risks = sc.risk_areas_addressed.join('; ');
    const evidence = sc.evidence_sources.map(e => e.text).join('; ');

    if (sc.steps.length === 0) {
      rows.push([
        sc.scenario_id, sc.title, sc.category, sc.priority, sc.rationale,
        sc.preconditions.join('; '), '', '', '', '',
        sc.expected_outcome, dataMatrixStr, wfSteps, risks, evidence,
      ]);
    } else {
      for (const s of sc.steps) {
        rows.push([
          sc.scenario_id, sc.title, sc.category, sc.priority, sc.rationale,
          sc.preconditions.join('; '),
          String(s.step_number), s.action, s.input_data || '', s.expected_behavior || '',
          sc.expected_outcome, dataMatrixStr, wfSteps, risks, evidence,
        ]);
      }
    }
  }

  const escape = (v: string) => `"${v.replace(/"/g, '""')}"`;
  const csv = [header.map(escape).join(','), ...rows.map(r => r.map(escape).join(','))].join('\n');
  const blob = new Blob([csv], { type: 'text/csv;charset=utf-8;' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  a.click();
  URL.revokeObjectURL(url);
}

// ── Evidence Badge ────────────────────────────────────────

function ModalityBadge({ modality }: { modality: string }) {
  const cfg: Record<string, { icon: React.ReactNode; color: string }> = {
    visual:     { icon: <Eye className="h-2.5 w-2.5" />,  color: 'text-blue-400 bg-blue-500/20 border-blue-500/30' },
    transcript: { icon: <Mic className="h-2.5 w-2.5" />,  color: 'text-green-400 bg-green-500/20 border-green-500/30' },
    multimodal: { icon: <Activity className="h-2.5 w-2.5" />, color: 'text-purple-400 bg-purple-500/20 border-purple-500/30' },
  };
  const c = cfg[modality] ?? cfg.transcript;
  return (
    <span className={clsx('inline-flex items-center gap-0.5 rounded-full border px-1.5 py-0.5 text-[9px]', c.color)}>
      {c.icon} {modality}
    </span>
  );
}

// ═══════════════════════════════════════════════════════════════
//  Main Component
// ═══════════════════════════════════════════════════════════════

export default function E2EArchitectWorkspacePage() {
  const { sessionId } = useParams<{ sessionId: string }>();
  const [searchParams] = useSearchParams();
  const navigate = useNavigate();
  const { token } = useAuth();

  const artifactId = searchParams.get('artifact_id') || '';
  const isReady = searchParams.get('ready') !== '0';

  const [state, setState] = useState<PageState>('loading');
  const [result, setResult] = useState<E2EArchitectResponse | null>(null);
  const [error, setError] = useState('');
  const [searchQuery, setSearchQuery] = useState('');
  const [activeFilters, setActiveFilters] = useState<Set<string>>(new Set());
  const [expandedScenarios, setExpandedScenarios] = useState<Set<string>>(new Set());
  const [showMax, setShowMax] = useState(20);

  // ── Load or generate ──────────────────────────────────
  const loadArchitect = useCallback(async (forceRegenerate = false) => {
    if (!artifactId) {
      setError('No artifact_id in URL');
      setState('error');
      return;
    }
    setState(forceRegenerate ? 'generating' : 'loading');
    setError('');
    try {
      const data = await api.generateE2EArchitect(artifactId, sessionId, forceRegenerate);
      setResult(data);
      setState('ready');
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : 'Unknown error';
      const axiosMsg = (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail;
      setError(axiosMsg || msg);
      setState('error');
    }
  }, [artifactId, sessionId]);

  useEffect(() => {
    if (token && artifactId) {
      if (isReady) {
        loadArchitect();
      } else {
        setState('prerequisites');
      }
    }
  }, [token, artifactId, isReady, loadArchitect]);

  // ── Derived data ──────────────────────────────────────
  const arch = result?.e2e_architect;
  const prov = result?.provenance;
  const cov = arch?.coverage_analysis;

  const scenarios = useMemo(() => arch?.critical_combinations ?? [], [arch]);
  const variables = useMemo(() => arch?.variables ?? [], [arch]);
  const decisionPoints = useMemo(() => arch?.decision_points ?? [], [arch]);

  // ── Filtering ─────────────────────────────────────────
  const filteredScenarios = useMemo(() => {
    let list = scenarios;
    if (activeFilters.size > 0) {
      list = list.filter(sc => activeFilters.has(sc.category) || activeFilters.has(sc.priority));
    }
    if (searchQuery.trim()) {
      const q = searchQuery.toLowerCase();
      list = list.filter(sc =>
        sc.title.toLowerCase().includes(q) ||
        sc.rationale.toLowerCase().includes(q) ||
        sc.scenario_id.toLowerCase().includes(q) ||
        sc.risk_areas_addressed.some(r => r.toLowerCase().includes(q))
      );
    }
    return list;
  }, [scenarios, activeFilters, searchQuery]);

  const toggleFilter = (key: string) => {
    setActiveFilters(prev => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  };

  const toggleScenario = (id: string) => {
    setExpandedScenarios(prev => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  // ── Loading / Error / Prerequisites states ─────────────
  if (state === 'prerequisites') {
    return (
      <div className="flex h-full items-center justify-center bg-gray-950">
        <div className="flex flex-col items-center gap-4 text-center max-w-md">
          <Shield className="h-10 w-10 text-amber-400" />
          <h2 className="text-lg font-semibold text-gray-200">Prerequisites Not Met</h2>
          <p className="text-sm text-gray-400">
            E2E Architect requires a persona draft with workflow steps.
            Generate a Process Oracle persona first, then return here.
          </p>
          <div className="flex gap-3">
            <button
              onClick={() => navigate(-1)}
              className="rounded-lg bg-gray-800 px-4 py-2 text-sm text-gray-300 hover:bg-gray-700"
            >
              Go Back
            </button>
            <button
              onClick={() => navigate(`/sessions/${sessionId}/persona-workspace?artifact_id=${artifactId}`)}
              className="rounded-lg bg-nexus-600 px-4 py-2 text-sm text-white hover:bg-nexus-500"
            >
              Generate Persona
            </button>
            <button
              onClick={() => loadArchitect()}
              className="rounded-lg bg-gray-700 px-4 py-2 text-sm text-gray-300 hover:bg-gray-600"
            >
              Try Anyway
            </button>
          </div>
        </div>
      </div>
    );
  }

  if (state === 'loading' || state === 'generating') {
    return (
      <div className="flex h-full items-center justify-center bg-gray-950">
        <div className="flex flex-col items-center gap-4 text-center">
          <Loader2 className="h-10 w-10 animate-spin text-nexus-500" />
          <h2 className="text-lg font-semibold text-gray-200">
            {state === 'generating' ? 'Regenerating E2E Architect Analysis...' : 'Loading E2E Architect...'}
          </h2>
          <p className="text-sm text-gray-500 max-w-md">
            Two-pass LLM analysis: extracting testable variables, then generating critical E2E scenarios with pairwise data combinations.
          </p>
        </div>
      </div>
    );
  }

  if (state === 'error' || !result || !arch || !prov || !cov) {
    return (
      <div className="flex h-full items-center justify-center bg-gray-950">
        <div className="flex flex-col items-center gap-4 text-center max-w-md">
          <AlertTriangle className="h-10 w-10 text-red-400" />
          <h2 className="text-lg font-semibold text-gray-200">E2E Architect Error</h2>
          <p className="text-sm text-gray-400">{error || 'Failed to load E2E Architect analysis.'}</p>
          <div className="flex gap-3">
            <button
              onClick={() => navigate(-1)}
              className="rounded-lg bg-gray-800 px-4 py-2 text-sm text-gray-300 hover:bg-gray-700"
            >
              Go Back
            </button>
            <button
              onClick={() => loadArchitect(true)}
              className="rounded-lg bg-nexus-600 px-4 py-2 text-sm text-white hover:bg-nexus-500"
            >
              Retry
            </button>
          </div>
        </div>
      </div>
    );
  }

  // ═══════════════════════════════════════════════════════
  //  Ready state — 3-panel layout
  // ═══════════════════════════════════════════════════════

  return (
    <div className="flex h-full flex-col overflow-hidden bg-gray-950">
      {/* ── Top Bar ──────────────────────────────────────── */}
      <div className="shrink-0 border-b border-gray-800 bg-gray-900/80 px-6 py-3">
        <div className="flex items-center gap-4">
          <button
            onClick={() => navigate(-1)}
            className="flex items-center gap-1.5 text-sm text-gray-400 hover:text-gray-200 transition-colors"
          >
            <ArrowLeft className="h-4 w-4" /> Back
          </button>
          <div className="h-5 w-px bg-gray-700" />
          <Shuffle className="h-5 w-5 text-nexus-400" />
          <div className="flex-1 min-w-0">
            <h1 className="text-sm font-semibold text-gray-200">
              E2E Test Architect
            </h1>
            <p className="text-[11px] text-gray-500">
              {cov.total_scenarios} scenarios • {cov.variables_tested} variables • {cov.pairwise_combinations_generated} pairwise combos
              {result.cached && ' • ⚡ cached'}
            </p>
          </div>
          <button
            onClick={() => loadArchitect(true)}
            className="rounded-lg bg-nexus-600/80 px-3 py-1.5 text-xs font-medium text-white hover:bg-nexus-500 transition-colors flex items-center gap-1.5"
          >
            <Sparkles className="h-3.5 w-3.5" /> Regenerate
          </button>
          <button
            onClick={() => exportE2EScenarioCSV(scenarios, `e2e-scenarios-${artifactId.slice(0, 8)}.csv`)}
            className="rounded-lg bg-gray-800 px-3 py-1.5 text-xs font-medium text-gray-300 hover:bg-gray-700 transition-colors flex items-center gap-1.5"
            title="Download all E2E scenarios as CSV"
          >
            <Download className="h-3.5 w-3.5" /> Download CSV
          </button>
          <button
            onClick={() => navigate(`/sessions/${sessionId}/test-strategy?artifact_id=${artifactId}`)}
            className="rounded-lg bg-gray-800 px-3 py-1.5 text-xs font-medium text-gray-300 hover:bg-gray-700 transition-colors flex items-center gap-1.5"
          >
            <ArrowRight className="h-3.5 w-3.5" /> Test Strategy
          </button>
        </div>
      </div>

      {/* ── Visual Substrate Quality Banner ───────────────── */}
      {result.visual_substrate && result.visual_substrate.quality !== 'multimodal' && result.visual_substrate.quality !== 'deep' && (
        <div className="shrink-0 border-b border-amber-500/20 bg-amber-500/5 px-6 py-2 flex items-center gap-3">
          <AlertTriangle className="h-4 w-4 text-amber-400 shrink-0" />
          <p className="text-[11px] text-amber-300 flex-1">
            <span className="font-semibold">Visual substrate: {result.visual_substrate.quality}</span>
            {' '}&mdash; {result.visual_substrate.frame_count} frames{result.visual_substrate.has_ocr ? '' : ', OCR skipped'}.
            {result.visual_substrate.recommendation && (
              <span className="text-amber-400/70 ml-1">{result.visual_substrate.recommendation}</span>
            )}
          </p>
        </div>
      )}

      {/* ── 3-Panel Layout ────────────────────────────────── */}
      <div className="flex flex-1 overflow-hidden">

        {/* ═══ LEFT PANEL: Variables + Decision Points + Coverage ═══ */}
        <div className="w-80 shrink-0 border-r border-gray-800 overflow-y-auto p-4 space-y-4">

          {/* Variables */}
          <div className="rounded-xl border border-gray-700/50 bg-gray-900/50 p-4 space-y-3">
            <h2 className="text-xs font-semibold uppercase tracking-wider text-nexus-400 flex items-center gap-1.5">
              <Variable className="h-3.5 w-3.5" /> Variables ({variables.length})
            </h2>
            <div className="space-y-2">
              {variables.map((v, i) => (
                <div key={i} className="rounded-lg border border-gray-700/40 bg-gray-800/30 p-2.5 space-y-1">
                  <div className="flex items-center gap-2">
                    <span className="text-[11px] font-semibold text-gray-200">{v.name}</span>
                    <span className="text-[9px] px-1.5 py-0.5 rounded-full bg-gray-700/50 text-gray-400 border border-gray-600/30">
                      {v.type}
                    </span>
                  </div>
                  <div className="flex flex-wrap gap-1">
                    {v.observed_values.map((val, j) => (
                      <span key={`o-${j}`} className="text-[9px] px-1.5 py-0.5 rounded-full bg-green-500/15 text-green-400 border border-green-500/20">
                        {val}
                      </span>
                    ))}
                    {v.inferred_values.map((val, j) => (
                      <span key={`i-${j}`} className="text-[9px] px-1.5 py-0.5 rounded-full bg-amber-500/15 text-amber-400 border border-amber-500/20">
                        {val} ?
                      </span>
                    ))}
                  </div>
                  {v.impacts.length > 0 && (
                    <p className="text-[10px] text-gray-500">
                      Impacts: {v.impacts.join(', ')}
                    </p>
                  )}
                </div>
              ))}
              {variables.length === 0 && (
                <p className="text-[11px] text-gray-500 italic">No variables extracted</p>
              )}
            </div>
          </div>

          {/* Decision Points */}
          <div className="rounded-xl border border-gray-700/50 bg-gray-900/50 p-4 space-y-3">
            <h2 className="text-xs font-semibold uppercase tracking-wider text-amber-400 flex items-center gap-1.5">
              <GitBranch className="h-3.5 w-3.5" /> Decision Points ({decisionPoints.length})
            </h2>
            <div className="space-y-2">
              {decisionPoints.map((dp, i) => (
                <div key={i} className="rounded-lg border border-gray-700/40 bg-gray-800/30 p-2.5 space-y-1">
                  <div className="flex items-center gap-2">
                    <span className="flex items-center justify-center h-5 w-5 rounded bg-gray-700/50 text-[10px] font-mono text-gray-400 shrink-0">
                      {dp.step_number}
                    </span>
                    <span className="text-[11px] text-gray-200">{dp.condition}</span>
                  </div>
                  <div className="grid grid-cols-2 gap-1.5 mt-1">
                    <div className="text-[10px]">
                      <span className="text-green-500">✓</span>
                      <span className="text-gray-400 ml-1">{dp.observed_path || '—'}</span>
                    </div>
                    <div className="text-[10px]">
                      <span className="text-amber-500">?</span>
                      <span className="text-gray-400 ml-1">{dp.alternative_path || '—'}</span>
                    </div>
                  </div>
                </div>
              ))}
              {decisionPoints.length === 0 && (
                <p className="text-[11px] text-gray-500 italic">No decision points found</p>
              )}
            </div>
          </div>

          {/* Coverage */}
          <div className="rounded-xl border border-gray-700/50 bg-gray-900/50 p-4 space-y-3">
            <h2 className="text-xs font-semibold uppercase tracking-wider text-green-400 flex items-center gap-1.5">
              <BarChart3 className="h-3.5 w-3.5" /> Coverage
            </h2>

            {/* Workflow coverage bar */}
            <div>
              <div className="flex items-center justify-between text-[11px] mb-1">
                <span className="text-gray-400">Workflow Coverage</span>
                <span className={clsx('font-mono font-semibold',
                  cov.workflow_coverage_pct >= 80 ? 'text-green-400' :
                  cov.workflow_coverage_pct >= 50 ? 'text-yellow-400' :
                  'text-red-400'
                )}>
                  {cov.workflow_coverage_pct}%
                </span>
              </div>
              <div className="h-2 rounded-full bg-gray-800 overflow-hidden">
                <div
                  className={clsx('h-full rounded-full transition-all',
                    cov.workflow_coverage_pct >= 80 ? 'bg-green-500' :
                    cov.workflow_coverage_pct >= 50 ? 'bg-yellow-500' :
                    'bg-red-500'
                  )}
                  style={{ width: `${Math.min(cov.workflow_coverage_pct, 100)}%` }}
                />
              </div>
            </div>

            {/* Pairwise bar */}
            <div>
              <div className="flex items-center justify-between text-[11px] mb-1">
                <span className="text-gray-400">Pairwise Coverage</span>
                <span className="font-mono font-semibold text-nexus-400">
                  {cov.pairwise_coverage_pct}%
                </span>
              </div>
              <div className="h-2 rounded-full bg-gray-800 overflow-hidden">
                <div
                  className="h-full rounded-full bg-nexus-500 transition-all"
                  style={{ width: `${Math.min(cov.pairwise_coverage_pct, 100)}%` }}
                />
              </div>
            </div>

            {/* Stats grid */}
            <div className="grid grid-cols-2 gap-2">
              <div className="rounded-lg bg-gray-800/50 border border-gray-700/30 p-2 text-center">
                <p className="text-lg font-bold text-gray-200">{cov.total_scenarios}</p>
                <p className="text-[10px] text-gray-500">Scenarios</p>
              </div>
              <div className="rounded-lg bg-gray-800/50 border border-gray-700/30 p-2 text-center">
                <p className="text-lg font-bold text-gray-200">{cov.pairwise_combinations_generated}</p>
                <p className="text-[10px] text-gray-500">Pairwise Combos</p>
              </div>
              <div className="rounded-lg bg-gray-800/50 border border-gray-700/30 p-2 text-center">
                <p className="text-lg font-bold text-gray-200">{cov.variables_tested}</p>
                <p className="text-[10px] text-gray-500">Variables</p>
              </div>
              <div className="rounded-lg bg-gray-800/50 border border-gray-700/30 p-2 text-center">
                <p className="text-lg font-bold text-gray-200">{cov.decision_points_found}</p>
                <p className="text-[10px] text-gray-500">Decision Points</p>
              </div>
            </div>

            {/* Dedup info */}
            {cov.duplicates_removed > 0 && (
              <div className="text-[10px] text-gray-500 flex items-center gap-1">
                <Shield className="h-3 w-3 text-amber-400" />
                {cov.duplicates_removed} duplicate(s) removed (from {cov.scenarios_before_dedup})
              </div>
            )}

            {/* By category */}
            <div>
              <span className="text-[10px] text-gray-500 uppercase">By Category</span>
              <div className="mt-1 space-y-1">
                {Object.entries(cov.by_category).map(([key, count]) => {
                  const conf = CATEGORY_CONFIG[key];
                  if (!conf) return null;
                  return (
                    <div key={key} className="flex items-center gap-2 text-[11px]">
                      <span className="text-gray-500">{conf.icon}</span>
                      <span className="text-gray-400 flex-1">{conf.label}</span>
                      <span className="font-mono font-semibold text-gray-300">{count}</span>
                    </div>
                  );
                })}
              </div>
            </div>

            {/* By priority */}
            <div>
              <span className="text-[10px] text-gray-500 uppercase">By Priority</span>
              <div className="mt-1 space-y-1">
                {Object.entries(cov.by_priority).map(([key, count]) => {
                  const conf = PRIORITY_CONFIG[key];
                  if (!conf) return null;
                  return (
                    <div key={key} className="flex items-center gap-2 text-[11px]">
                      <span className={clsx('w-2 h-2 rounded-full', conf.bg.split(' ')[0])} />
                      <span className="text-gray-400 flex-1">{conf.label}</span>
                      <span className={clsx('font-mono font-semibold', conf.color)}>{count}</span>
                    </div>
                  );
                })}
              </div>
            </div>

            {/* Risk areas */}
            {cov.risk_areas_addressed.length > 0 && (
              <div>
                <span className="text-[10px] text-gray-500 uppercase">Risk Areas Addressed</span>
                <div className="mt-1 flex flex-wrap gap-1">
                  {cov.risk_areas_addressed.map((r, i) => (
                    <span key={i} className="text-[9px] px-1.5 py-0.5 rounded-full bg-red-500/10 text-red-400 border border-red-500/20">
                      {r}
                    </span>
                  ))}
                </div>
              </div>
            )}
          </div>

          {/* Provenance */}
          <div className="rounded-xl border border-gray-700/50 bg-gray-900/50 p-4 space-y-2">
            <h2 className="text-xs font-semibold uppercase tracking-wider text-gray-500 flex items-center gap-1.5">
              <Layers className="h-3.5 w-3.5" /> Provenance
            </h2>
            <div className="space-y-1 text-[11px]">
              <div className="flex justify-between"><span className="text-gray-500">Model</span><span className="text-gray-300 font-mono">{prov.model_used}</span></div>
              <div className="flex justify-between"><span className="text-gray-500">Steps Analysed</span><span className="text-gray-300">{prov.workflow_steps_analysed}</span></div>
              <div className="flex justify-between"><span className="text-gray-500">Risks Considered</span><span className="text-gray-300">{prov.risks_considered}</span></div>
              <div className="flex justify-between"><span className="text-gray-500">LLM Time</span><span className="text-gray-300">{(prov.generation_time_ms / 1000).toFixed(1)}s (2-pass)</span></div>
              {result.cached ? (
                <>
                  <div className="flex justify-between"><span className="text-gray-500">Original Processing</span><span className="text-gray-300">{(result.processing_time_ms / 1000).toFixed(1)}s</span></div>
                  <div className="flex justify-between"><span className="text-gray-500">Cache Load</span><span className="text-emerald-400 font-mono">{result.cache_hit_ms != null ? `${result.cache_hit_ms.toFixed(0)}ms` : '—'}</span></div>
                </>
              ) : (
                <div className="flex justify-between"><span className="text-gray-500">Total Time</span><span className="text-gray-300">{(result.processing_time_ms / 1000).toFixed(1)}s</span></div>
              )}
              {prov.generated_at && (
                <div className="flex justify-between"><span className="text-gray-500">Generated</span><span className="text-gray-300">{new Date(prov.generated_at).toLocaleString()}</span></div>
              )}
            </div>
          </div>
        </div>

        {/* ═══ CENTER PANEL: E2E Scenarios ════════════════════ */}
        <div className="flex-1 overflow-y-auto">

          {/* Search + Filter bar */}
          <div className="sticky top-0 z-10 border-b border-gray-800 bg-gray-950/95 backdrop-blur-sm px-6 py-3 space-y-2">
            <div className="flex items-center gap-3">
              <div className="relative flex-1">
                <Search className="absolute left-3 top-1/2 -translate-y-1/2 h-3.5 w-3.5 text-gray-500" />
                <input
                  type="text"
                  placeholder="Search scenarios..."
                  value={searchQuery}
                  onChange={e => setSearchQuery(e.target.value)}
                  className="w-full rounded-lg border border-gray-700/50 bg-gray-800/50 pl-9 pr-8 py-1.5 text-xs text-gray-200 placeholder-gray-500 focus:border-nexus-500/50 focus:outline-none"
                />
                {searchQuery && (
                  <button
                    onClick={() => setSearchQuery('')}
                    className="absolute right-2.5 top-1/2 -translate-y-1/2 text-gray-500 hover:text-gray-300"
                  >
                    <X className="h-3.5 w-3.5" />
                  </button>
                )}
              </div>
              <span className="text-[11px] text-gray-500">
                {filteredScenarios.length} of {scenarios.length}
              </span>
            </div>

            {/* Filter badges */}
            <div className="flex flex-wrap gap-1.5">
              {Object.entries(CATEGORY_CONFIG).map(([key, conf]) => (
                <button
                  key={key}
                  onClick={() => toggleFilter(key)}
                  className={clsx(
                    'flex items-center gap-1 rounded-full border px-2 py-0.5 text-[10px] transition-colors',
                    activeFilters.has(key)
                      ? conf.color
                      : 'border-gray-700/50 text-gray-500 hover:text-gray-300 hover:border-gray-600',
                  )}
                >
                  {conf.icon} {conf.label}
                </button>
              ))}
              <div className="h-4 w-px bg-gray-700 mx-1" />
              {Object.entries(PRIORITY_CONFIG).map(([key, conf]) => (
                <button
                  key={key}
                  onClick={() => toggleFilter(key)}
                  className={clsx(
                    'rounded-full border px-2 py-0.5 text-[10px] transition-colors',
                    activeFilters.has(key)
                      ? conf.bg + ' ' + conf.color
                      : 'border-gray-700/50 text-gray-500 hover:text-gray-300 hover:border-gray-600',
                  )}
                >
                  {conf.label}
                </button>
              ))}
              {activeFilters.size > 0 && (
                <button
                  onClick={() => setActiveFilters(new Set())}
                  className="text-[10px] text-gray-500 hover:text-gray-300 flex items-center gap-1"
                >
                  <X className="h-3 w-3" /> Clear
                </button>
              )}
            </div>
          </div>

          {/* Scenario Cards */}
          <div className="p-6 space-y-4">
            {filteredScenarios.slice(0, showMax).map(sc => (
              <ScenarioCard
                key={sc.scenario_id}
                scenario={sc}
                expanded={expandedScenarios.has(sc.scenario_id)}
                onToggle={() => toggleScenario(sc.scenario_id)}
              />
            ))}

            {filteredScenarios.length === 0 && (
              <div className="text-center py-12 text-gray-500">
                <Target className="h-8 w-8 mx-auto mb-3 opacity-50" />
                <p className="text-sm">No scenarios match your filters.</p>
              </div>
            )}

            {filteredScenarios.length > showMax && (
              <button
                onClick={() => setShowMax(prev => prev + 20)}
                className="w-full py-3 text-xs text-gray-400 hover:text-gray-200 border border-gray-700/30 rounded-lg hover:border-gray-600 transition-colors"
              >
                Show more ({filteredScenarios.length - showMax} remaining)
              </button>
            )}
          </div>
        </div>

        {/* ═══ RIGHT PANEL: Variable Impact + Pairwise Matrix ═══ */}
        <div className="w-72 shrink-0 border-l border-gray-800 overflow-y-auto p-4 space-y-4">

          {/* Variable Impact Map */}
          <div className="rounded-xl border border-gray-700/50 bg-gray-900/50 p-4 space-y-3">
            <h2 className="text-xs font-semibold uppercase tracking-wider text-nexus-400 flex items-center gap-1.5">
              <Zap className="h-3.5 w-3.5" /> Variable Impact
            </h2>
            <p className="text-[10px] text-gray-500">How variables map to scenarios</p>
            <div className="space-y-2">
              {variables.map((v, i) => {
                const scenarioCount = scenarios.filter(sc =>
                  sc.data_matrix.some(dm => v.name in dm)
                ).length;
                return (
                  <div key={i} className="flex items-center gap-2">
                    <span className="text-[11px] text-gray-300 flex-1 truncate">{v.name}</span>
                    <div className="flex items-center gap-1">
                      <div className="w-16 h-1.5 rounded-full bg-gray-800 overflow-hidden">
                        <div
                          className="h-full rounded-full bg-nexus-500"
                          style={{ width: `${scenarios.length > 0 ? (scenarioCount / scenarios.length * 100) : 0}%` }}
                        />
                      </div>
                      <span className="text-[10px] font-mono text-gray-400 w-6 text-right">{scenarioCount}</span>
                    </div>
                  </div>
                );
              })}
            </div>
          </div>

          {/* Workflow Step Coverage */}
          <div className="rounded-xl border border-gray-700/50 bg-gray-900/50 p-4 space-y-3">
            <h2 className="text-xs font-semibold uppercase tracking-wider text-green-400 flex items-center gap-1.5">
              <CheckCircle2 className="h-3.5 w-3.5" /> Steps Covered
            </h2>
            <div className="flex flex-wrap gap-1.5">
              {cov.workflow_steps_covered.map(step => (
                <span
                  key={step}
                  className="flex items-center justify-center h-7 w-7 rounded-lg bg-green-500/15 border border-green-500/20 text-[11px] font-mono text-green-400"
                >
                  {step}
                </span>
              ))}
              {cov.workflow_steps_covered.length === 0 && (
                <p className="text-[11px] text-gray-500 italic">No step coverage data</p>
              )}
            </div>
          </div>

          {/* Observed → Inferred Legend */}
          <div className="rounded-xl border border-gray-700/50 bg-gray-900/50 p-4 space-y-3">
            <h2 className="text-xs font-semibold uppercase tracking-wider text-gray-500 flex items-center gap-1.5">
              <Clock className="h-3.5 w-3.5" /> Legend
            </h2>
            <div className="space-y-2">
              <div className="flex items-center gap-2 text-[11px]">
                <span className="w-3 h-3 rounded-full bg-green-500/30 border border-green-500/40" />
                <span className="text-gray-400">Observed — seen in the demo recording</span>
              </div>
              <div className="flex items-center gap-2 text-[11px]">
                <span className="w-3 h-3 rounded-full bg-amber-500/30 border border-amber-500/40" />
                <span className="text-gray-400">Inferred — expected but not demonstrated</span>
              </div>
              <div className="flex items-center gap-2 text-[11px]">
                <span className="w-3 h-3 rounded-full bg-red-500/30 border border-red-500/40" />
                <span className="text-gray-400">High Risk — untested with high impact</span>
              </div>
              <div className="flex items-center gap-2 text-[11px]">
                <span className="w-3 h-3 rounded-full bg-purple-500/30 border border-purple-500/40" />
                <span className="text-gray-400">Boundary — edge values / limits</span>
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}

// ═══════════════════════════════════════════════════════════════
//  Scenario Card Sub-component
// ═══════════════════════════════════════════════════════════════

function ScenarioCard({
  scenario,
  expanded,
  onToggle,
}: {
  scenario: E2EScenario;
  expanded: boolean;
  onToggle: () => void;
}) {
  const catConf = CATEGORY_CONFIG[scenario.category] ?? CATEGORY_CONFIG.observed;
  const priConf = PRIORITY_CONFIG[scenario.priority] ?? PRIORITY_CONFIG.P1_high;

  return (
    <div className="rounded-xl border border-gray-700/50 bg-gray-900/40 overflow-hidden">
      {/* Header */}
      <button
        onClick={onToggle}
        className="w-full flex items-start gap-3 px-5 py-4 text-left hover:bg-gray-800/30 transition-colors"
      >
        <div className="shrink-0 mt-0.5">
          {expanded ? (
            <ChevronDown className="h-4 w-4 text-gray-500" />
          ) : (
            <ChevronRight className="h-4 w-4 text-gray-500" />
          )}
        </div>
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 flex-wrap">
            <span className="text-[10px] font-mono text-gray-500">{scenario.scenario_id}</span>
            <span className={clsx('inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[9px]', catConf.color)}>
              {catConf.icon} {catConf.label}
            </span>
            <span className={clsx('rounded-full border px-2 py-0.5 text-[9px] font-medium', priConf.bg, priConf.color)}>
              {priConf.label}
            </span>
          </div>
          <h3 className="text-sm font-medium text-gray-200 mt-1">{scenario.title}</h3>
          {scenario.rationale && (
            <p className="text-[11px] text-gray-500 mt-0.5 line-clamp-2">{scenario.rationale}</p>
          )}
        </div>
        {/* Step count + Data matrix count */}
        <div className="shrink-0 text-right space-y-0.5">
          <span className="text-[10px] text-gray-500">{scenario.steps.length} steps</span>
          {scenario.data_matrix.length > 0 && (
            <p className="text-[10px] text-nexus-400">{scenario.data_matrix.length} data set(s)</p>
          )}
        </div>
      </button>

      {/* Expanded content */}
      {expanded && (
        <div className="border-t border-gray-700/30 px-5 py-4 space-y-4">

          {/* Preconditions */}
          {scenario.preconditions.length > 0 && (
            <div>
              <span className="text-[10px] text-gray-500 uppercase">Preconditions</span>
              <ul className="mt-1 space-y-0.5">
                {scenario.preconditions.map((p, i) => (
                  <li key={i} className="text-[11px] text-gray-400">• {p}</li>
                ))}
              </ul>
            </div>
          )}

          {/* Test Steps */}
          {scenario.steps.length > 0 && (
            <div>
              <span className="text-[10px] text-gray-500 uppercase">Test Steps</span>
              <div className="mt-1 space-y-1.5">
                {scenario.steps.map(step => (
                  <div key={step.step_number} className="flex gap-2 text-[11px]">
                    <span className="shrink-0 flex items-center justify-center h-5 w-5 rounded bg-gray-700/50 text-[10px] font-mono text-gray-400">
                      {step.step_number}
                    </span>
                    <div className="flex-1 min-w-0">
                      <p className="text-gray-300">{step.action}</p>
                      {step.input_data && step.input_data !== 'N/A' && (
                        <p className="text-gray-500">Input: <span className="text-nexus-400">{step.input_data}</span></p>
                      )}
                      {step.expected_behavior && (
                        <p className="text-gray-500">→ {step.expected_behavior}</p>
                      )}
                    </div>
                  </div>
                ))}
              </div>
            </div>
          )}

          {/* Data Matrix */}
          {scenario.data_matrix.length > 0 && (
            <div>
              <span className="text-[10px] text-gray-500 uppercase flex items-center gap-1">
                <Shuffle className="h-3 w-3" /> Data Combinations
              </span>
              <div className="mt-1 space-y-1.5">
                {scenario.data_matrix.map((dm, idx) => (
                  <div key={idx} className="rounded-lg border border-nexus-500/20 bg-nexus-500/5 px-3 py-2">
                    <div className="flex flex-wrap gap-x-3 gap-y-1">
                      {Object.entries(dm).map(([k, v]) => (
                        <span key={k} className="text-[10px]">
                          <span className="text-gray-500">{k}:</span>
                          <span className="text-nexus-300 ml-1 font-medium">{v}</span>
                        </span>
                      ))}
                    </div>
                  </div>
                ))}
              </div>
            </div>
          )}

          {/* Expected Outcome */}
          {scenario.expected_outcome && (
            <div>
              <span className="text-[10px] text-gray-500 uppercase">Expected Outcome</span>
              <p className="text-[11px] text-gray-300 mt-0.5">{scenario.expected_outcome}</p>
            </div>
          )}

          {/* Evidence Sources */}
          {scenario.evidence_sources.length > 0 && (
            <div>
              <span className="text-[10px] text-gray-500 uppercase">Evidence</span>
              <div className="mt-1 space-y-1.5">
                {scenario.evidence_sources.map((ev, idx) => (
                  <div key={idx} className="rounded-lg border border-gray-700/30 bg-gray-800/20 px-3 py-2 flex items-start gap-2">
                    <ModalityBadge modality={ev.source_modality} />
                    <p className="text-[11px] text-gray-400 flex-1 italic">"{ev.text}"</p>
                    <span className={clsx(
                      'text-[9px] font-mono shrink-0',
                      ev.confidence >= 0.8 ? 'text-green-400' :
                      ev.confidence >= 0.5 ? 'text-yellow-400' :
                      'text-red-400'
                    )}>
                      {(ev.confidence * 100).toFixed(0)}%
                    </span>
                  </div>
                ))}
              </div>
            </div>
          )}

          {/* Meta badges */}
          <div className="flex flex-wrap gap-2 pt-2 border-t border-gray-700/20">
            {scenario.workflow_steps_covered.length > 0 && (
              <span className="text-[9px] text-gray-500">
                Steps: {scenario.workflow_steps_covered.join(', ')}
              </span>
            )}
            {scenario.risk_areas_addressed.length > 0 && (
              <span className="text-[9px] text-red-400/70">
                Risks: {scenario.risk_areas_addressed.join(', ')}
              </span>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
