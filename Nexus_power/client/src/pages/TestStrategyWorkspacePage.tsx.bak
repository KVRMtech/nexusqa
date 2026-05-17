import { useState, useEffect, useCallback, useMemo } from 'react';
import { useParams, useSearchParams, useNavigate } from 'react-router-dom';
import { useAuth } from '../contexts/AuthContext';
import api from '../services/api';
import type {
  TestStrategyResponse,
  TestPlanSummary,
  TestScenario,
  TestCase,
  TestStep,
  TraceabilityEntry,
  CoverageBreakdown,
  EvidenceCitation,
  TestStrategyProvenance,
} from '../types/canonical';
import clsx from 'clsx';
import {
  ArrowLeft,
  FlaskConical,
  Target,
  Shield,
  AlertTriangle,
  CheckCircle2,
  XCircle,
  Loader2,
  ChevronRight,
  ChevronDown,
  Mic,
  Eye,
  Share2,
  Sparkles,
  FileText,
  Zap,
  Bug,
  BarChart3,
  Layers,
  Clock,
  Link2,
  ArrowRight,
  Download,
  Search,
  X,
  Route,
} from 'lucide-react';

// ── Priority helpers ────────────────────────────────────────

const PRIORITY_CONFIG: Record<string, { label: string; color: string; bg: string }> = {
  P0_critical: { label: 'P0 Critical', color: 'text-red-400', bg: 'bg-red-500/20 border-red-500/30' },
  P1_high:     { label: 'P1 High',     color: 'text-orange-400', bg: 'bg-orange-500/20 border-orange-500/30' },
  P2_medium:   { label: 'P2 Medium',   color: 'text-yellow-400', bg: 'bg-yellow-500/20 border-yellow-500/30' },
  P3_low:      { label: 'P3 Low',      color: 'text-blue-400', bg: 'bg-blue-500/20 border-blue-500/30' },
};

const CATEGORY_CONFIG: Record<string, { label: string; icon: React.ReactNode; color: string }> = {
  happy_path:  { label: 'Happy Path',  icon: <CheckCircle2 className="h-3.5 w-3.5" />, color: 'text-green-400 bg-green-500/20 border-green-500/30' },
  negative:    { label: 'Negative',    icon: <XCircle className="h-3.5 w-3.5" />,      color: 'text-red-400 bg-red-500/20 border-red-500/30' },
  boundary:    { label: 'Boundary',    icon: <Target className="h-3.5 w-3.5" />,        color: 'text-purple-400 bg-purple-500/20 border-purple-500/30' },
  edge_case:   { label: 'Edge Case',   icon: <Bug className="h-3.5 w-3.5" />,           color: 'text-amber-400 bg-amber-500/20 border-amber-500/30' },
  security:    { label: 'Security',    icon: <Shield className="h-3.5 w-3.5" />,        color: 'text-cyan-400 bg-cyan-500/20 border-cyan-500/30' },
  performance: { label: 'Performance', icon: <Zap className="h-3.5 w-3.5" />,           color: 'text-indigo-400 bg-indigo-500/20 border-indigo-500/30' },
  e2e:         { label: 'E2E Flow',    icon: <Route className="h-3.5 w-3.5" />,         color: 'text-teal-400 bg-teal-500/20 border-teal-500/30' },
};

// ── CSV Export ──────────────────────────────────────────────

function exportTestCasesCSV(cases: TestCase[], filename: string) {
  const header = ['Case ID', 'Title', 'Category', 'Priority', 'Preconditions', '#', 'Action', 'Input', 'Expected Behavior', 'Expected Result', 'Tags', 'Evidence'];
  const rows: string[][] = [];
  for (const tc of cases) {
    if (tc.steps.length === 0) {
      rows.push([
        tc.case_id, tc.title, tc.category, tc.priority,
        tc.preconditions.join('; '), '', '', '', '',
        tc.expected_result, tc.tags.join(', '),
        tc.evidence_trace.map(e => e.text).join('; '),
      ]);
    } else {
      for (const s of tc.steps) {
        rows.push([
          tc.case_id, tc.title, tc.category, tc.priority,
          tc.preconditions.join('; '),
          String(s.step_number), s.action, s.input_data || '', s.expected_behavior || '',
          tc.expected_result, tc.tags.join(', '),
          tc.evidence_trace.map(e => e.text).join('; '),
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

function confidenceColor(c: number): string {
  if (c >= 0.8) return 'text-green-400';
  if (c >= 0.6) return 'text-yellow-400';
  if (c >= 0.4) return 'text-orange-400';
  return 'text-red-400';
}

function modalityIcon(mod: string): React.ReactNode {
  switch (mod) {
    case 'transcript': return <Mic className="h-3 w-3" />;
    case 'visual':     return <Eye className="h-3 w-3" />;
    case 'graph':      return <Share2 className="h-3 w-3" />;
    case 'inferred':   return <Sparkles className="h-3 w-3" />;
    default:           return <FileText className="h-3 w-3" />;
  }
}

function coverageStatusColor(s: string): string {
  switch (s) {
    case 'covered': return 'text-green-400 bg-green-500/20 border-green-500/30';
    case 'partial': return 'text-yellow-400 bg-yellow-500/20 border-yellow-500/30';
    case 'gap':     return 'text-red-400 bg-red-500/20 border-red-500/30';
    default:        return 'text-gray-400 bg-gray-500/20 border-gray-500/30';
  }
}

// ── Sub-components ──────────────────────────────────────────

function EvidenceTraceCard({ citation }: { citation: EvidenceCitation }) {
  return (
    <div className="flex items-start gap-2 rounded border border-gray-700/50 bg-gray-800/40 px-2.5 py-1.5 text-[11px]">
      <span className="mt-0.5 text-gray-500">{modalityIcon(citation.source_modality)}</span>
      <div className="flex-1 min-w-0">
        <p className="text-gray-300 leading-relaxed line-clamp-2">"{citation.text}"</p>
        <div className="mt-1 flex items-center gap-2 text-[10px] text-gray-500">
          {citation.timestamp_range && (
            <span className="flex items-center gap-0.5">
              <Clock className="h-2.5 w-2.5" /> {citation.timestamp_range}
            </span>
          )}
          <span className={confidenceColor(citation.confidence)}>
            {(citation.confidence * 100).toFixed(0)}%
          </span>
        </div>
      </div>
    </div>
  );
}

function TestStepRow({ step }: { step: TestStep }) {
  return (
    <tr className="border-b border-gray-800/50 last:border-0">
      <td className="py-1.5 pr-2 text-[10px] font-mono text-gray-500 align-top w-6">{step.step_number}</td>
      <td className="py-1.5 pr-2 text-[11px] text-gray-300 align-top">{step.action}</td>
      <td className="py-1.5 pr-2 text-[11px] text-gray-400 align-top">{step.input_data || '—'}</td>
      <td className="py-1.5 text-[11px] text-gray-400 align-top">{step.expected_behavior}</td>
    </tr>
  );
}

function TestCaseCard({ tc, isExpanded, onToggle }: { tc: TestCase; isExpanded: boolean; onToggle: () => void }) {
  const pri = PRIORITY_CONFIG[tc.priority] || PRIORITY_CONFIG.P2_medium;
  const cat = CATEGORY_CONFIG[tc.category] || CATEGORY_CONFIG.happy_path;

  return (
    <div className="rounded-lg border border-gray-700/50 bg-gray-800/30 overflow-hidden">
      {/* Header */}
      <button
        onClick={onToggle}
        className="w-full flex items-center gap-3 px-4 py-3 text-left hover:bg-gray-800/50 transition-colors"
      >
        {isExpanded ? (
          <ChevronDown className="h-4 w-4 text-gray-500 shrink-0" />
        ) : (
          <ChevronRight className="h-4 w-4 text-gray-500 shrink-0" />
        )}
        <span className="text-[10px] font-mono text-gray-500">{tc.case_id}</span>
        <span className="text-sm text-gray-200 flex-1 line-clamp-1">{tc.title}</span>
        <span className={clsx('inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[10px] font-medium', cat.color)}>
          {cat.icon} {cat.label}
        </span>
        <span className={clsx('inline-flex rounded-full border px-2 py-0.5 text-[10px] font-medium', pri.bg, pri.color)}>
          {pri.label}
        </span>
      </button>

      {/* Expanded content */}
      {isExpanded && (
        <div className="border-t border-gray-700/50 px-4 py-3 space-y-3">
          {/* Preconditions */}
          {tc.preconditions.length > 0 && (
            <div>
              <h5 className="text-[10px] font-semibold uppercase tracking-wider text-gray-500 mb-1">Preconditions</h5>
              <ul className="space-y-0.5">
                {tc.preconditions.map((pre, i) => (
                  <li key={i} className="text-[11px] text-gray-400 flex items-start gap-1.5">
                    <span className="text-nexus-500 mt-0.5">•</span> {pre}
                  </li>
                ))}
              </ul>
            </div>
          )}

          {/* Test Steps */}
          {tc.steps.length > 0 && (
            <div>
              <h5 className="text-[10px] font-semibold uppercase tracking-wider text-gray-500 mb-1">Test Steps</h5>
              <table className="w-full">
                <thead>
                  <tr className="border-b border-gray-700">
                    <th className="text-left text-[10px] text-gray-500 font-medium pb-1 w-6">#</th>
                    <th className="text-left text-[10px] text-gray-500 font-medium pb-1">Action</th>
                    <th className="text-left text-[10px] text-gray-500 font-medium pb-1">Input</th>
                    <th className="text-left text-[10px] text-gray-500 font-medium pb-1">Expected</th>
                  </tr>
                </thead>
                <tbody>
                  {tc.steps.map((s) => <TestStepRow key={s.step_number} step={s} />)}
                </tbody>
              </table>
            </div>
          )}

          {/* Expected Result */}
          {tc.expected_result && (
            <div>
              <h5 className="text-[10px] font-semibold uppercase tracking-wider text-gray-500 mb-1">Expected Result</h5>
              <p className="text-[11px] text-green-400/80 bg-green-500/10 border border-green-500/20 rounded px-2.5 py-1.5">
                {tc.expected_result}
              </p>
            </div>
          )}

          {/* Tags */}
          {tc.tags.length > 0 && (
            <div className="flex items-center gap-1.5 flex-wrap">
              {tc.tags.map((tag) => (
                <span key={tag} className="rounded-full bg-gray-700/50 px-2 py-0.5 text-[10px] text-gray-400 border border-gray-600/30">
                  {tag}
                </span>
              ))}
            </div>
          )}

          {/* Evidence Trace */}
          {tc.evidence_trace.length > 0 && (
            <div>
              <h5 className="text-[10px] font-semibold uppercase tracking-wider text-nexus-400 mb-1.5 flex items-center gap-1">
                <Link2 className="h-3 w-3" /> KT Evidence Trace
              </h5>
              <div className="space-y-1">
                {tc.evidence_trace.map((ev, i) => <EvidenceTraceCard key={i} citation={ev} />)}
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function ScenarioSection({ scenario }: { scenario: TestScenario }) {
  const [expandedCases, setExpandedCases] = useState<Set<string>>(new Set());
  const [isOpen, setIsOpen] = useState(true);

  const toggle = useCallback((caseId: string) => {
    setExpandedCases(prev => {
      const next = new Set(prev);
      next.has(caseId) ? next.delete(caseId) : next.add(caseId);
      return next;
    });
  }, []);

  return (
    <div className="rounded-xl border border-gray-700/50 bg-gray-900/50 overflow-hidden">
      {/* Scenario Header */}
      <button
        onClick={() => setIsOpen(!isOpen)}
        className="w-full flex items-center gap-3 px-5 py-4 text-left hover:bg-gray-800/30 transition-colors"
      >
        {isOpen ? (
          <ChevronDown className="h-5 w-5 text-nexus-400 shrink-0" />
        ) : (
          <ChevronRight className="h-5 w-5 text-nexus-400 shrink-0" />
        )}
        <div className="flex items-center gap-2 shrink-0">
          <span className="flex items-center justify-center h-7 w-7 rounded-lg bg-nexus-500/20 text-nexus-400 text-xs font-bold">
            {scenario.workflow_step_number}
          </span>
        </div>
        <div className="flex-1 min-w-0">
          <h3 className="text-sm font-semibold text-gray-200 line-clamp-1">
            {scenario.workflow_step_name || `Step ${scenario.workflow_step_number}`}
          </h3>
          {scenario.description && (
            <p className="text-[11px] text-gray-500 mt-0.5 line-clamp-1">{scenario.description}</p>
          )}
        </div>
        <span className="text-[10px] font-mono text-gray-500">{scenario.scenario_id}</span>
        <span className="rounded-full bg-nexus-500/20 border border-nexus-500/30 px-2 py-0.5 text-[10px] font-medium text-nexus-400">
          {scenario.test_cases.length} cases
        </span>
      </button>

      {/* Test Cases */}
      {isOpen && scenario.test_cases.length > 0 && (
        <div className="border-t border-gray-700/30 px-4 py-3 space-y-2">
          {scenario.test_cases.map((tc) => (
            <TestCaseCard
              key={tc.case_id}
              tc={tc}
              isExpanded={expandedCases.has(tc.case_id)}
              onToggle={() => toggle(tc.case_id)}
            />
          ))}
        </div>
      )}
    </div>
  );
}

// ── Category-grouped view component ──────────────────────────

function CategorySection({ category, cases }: { category: string; cases: TestCase[] }) {
  const [expandedCases, setExpandedCases] = useState<Set<string>>(new Set());
  const [isOpen, setIsOpen] = useState(false);

  const toggle = useCallback((caseId: string) => {
    setExpandedCases(prev => {
      const next = new Set(prev);
      next.has(caseId) ? next.delete(caseId) : next.add(caseId);
      return next;
    });
  }, []);

  const cat = CATEGORY_CONFIG[category] || CATEGORY_CONFIG.happy_path;

  return (
    <div className="rounded-xl border border-gray-700/50 bg-gray-900/50 overflow-hidden">
      <button
        onClick={() => setIsOpen(!isOpen)}
        className="w-full flex items-center gap-3 px-5 py-4 text-left hover:bg-gray-800/30 transition-colors"
      >
        {isOpen ? (
          <ChevronDown className="h-5 w-5 text-gray-400 shrink-0" />
        ) : (
          <ChevronRight className="h-5 w-5 text-gray-400 shrink-0" />
        )}
        <span className={clsx('inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-xs font-medium', cat.color)}>
          {cat.icon} {cat.label}
        </span>
        <div className="flex-1 min-w-0">
          <p className="text-[11px] text-gray-500">{cases.length} test case{cases.length !== 1 ? 's' : ''}</p>
        </div>
        <span className="rounded-full bg-gray-700/50 border border-gray-600/30 px-2 py-0.5 text-[10px] font-mono text-gray-400">
          {cases.length}
        </span>
      </button>

      {isOpen && cases.length > 0 && (
        <div className="border-t border-gray-700/30 px-4 py-3 space-y-2">
          {cases.map((tc) => (
            <TestCaseCard
              key={tc.case_id}
              tc={tc}
              isExpanded={expandedCases.has(tc.case_id)}
              onToggle={() => toggle(tc.case_id)}
            />
          ))}
        </div>
      )}
    </div>
  );
}

// ── Main Page Component ─────────────────────────────────────

type PageState = 'loading' | 'generating' | 'ready' | 'error';

export default function TestStrategyWorkspacePage() {
  const { sessionId } = useParams<{ sessionId: string }>();
  const [searchParams] = useSearchParams();
  const navigate = useNavigate();
  const { token } = useAuth();

  const artifactId = searchParams.get('artifact_id') || '';

  const [state, setState] = useState<PageState>('loading');
  const [strategy, setStrategy] = useState<TestStrategyResponse | null>(null);
  const [error, setError] = useState('');
  const [viewMode, setViewMode] = useState<'workflow' | 'category'>('category');
  const [searchQuery, setSearchQuery] = useState('');
  const [activeFilters, setActiveFilters] = useState<Set<string>>(new Set());
  const [showMax, setShowMax] = useState(20);

  // ── Load or generate test strategy ────────────────────
  const loadStrategy = useCallback(async (forceRegenerate = false) => {
    if (!artifactId) {
      setError('No artifact_id in URL');
      setState('error');
      return;
    }

    setState(forceRegenerate ? 'generating' : 'loading');
    setError('');

    try {
      const data = await api.generateTestStrategy(artifactId, sessionId, forceRegenerate);
      setStrategy(data);
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
      loadStrategy();
    }
  }, [token, artifactId, loadStrategy]);

  // ── Derived stats ─────────────────────────────────────
  const stats = useMemo(() => {
    if (!strategy) return null;
    const c = strategy.coverage;
    return {
      totalScenarios: c.total_scenarios,
      totalCases: c.total_cases,
      coveragePct: c.coverage_percentage,
      p0Count: c.by_priority?.P0_critical || 0,
      p1Count: c.by_priority?.P1_high || 0,
      p2Count: c.by_priority?.P2_medium || 0,
      p3Count: c.by_priority?.P3_low || 0,
      happyPath: c.by_category?.happy_path || 0,
      negative: c.by_category?.negative || 0,
      boundary: c.by_category?.boundary || 0,
      edgeCase: c.by_category?.edge_case || 0,
      gaps: c.gap_areas || [],
    };
  }, [strategy]);

  // ── All test cases flat list ──────────────────────────
  const allTestCases = useMemo(() => {
    if (!strategy) return [];
    return strategy.test_scenarios.flatMap(s => s.test_cases);
  }, [strategy]);

  // ── Category grouping for "By Category" view ──────────
  const categoryGroups = useMemo(() => {
    if (!strategy) return [];
    const groups: Record<string, TestCase[]> = {};
    const order = ['happy_path', 'negative', 'boundary', 'edge_case', 'security', 'performance', 'e2e'];
    for (const sc of strategy.test_scenarios) {
      for (const tc of sc.test_cases) {
        const cat = tc.category || 'happy_path';
        if (!groups[cat]) groups[cat] = [];
        groups[cat].push(tc);
      }
    }
    return order
      .filter(cat => groups[cat] && groups[cat].length > 0)
      .map(cat => ({ category: cat, cases: groups[cat] }));
  }, [strategy]);

  // ── Loading state ─────────────────────────────────────
  if (state === 'loading') {
    return (
      <div className="flex h-full items-center justify-center">
        <div className="flex flex-col items-center gap-4">
          <Loader2 className="h-10 w-10 text-nexus-500 animate-spin" />
          <p className="text-sm text-gray-400">Loading test strategy...</p>
        </div>
      </div>
    );
  }

  if (state === 'generating') {
    return (
      <div className="flex h-full items-center justify-center">
        <div className="flex flex-col items-center gap-4 max-w-md text-center">
          <div className="relative">
            <FlaskConical className="h-12 w-12 text-nexus-500 animate-pulse" />
            <Loader2 className="absolute -bottom-1 -right-1 h-5 w-5 text-nexus-400 animate-spin" />
          </div>
          <h2 className="text-lg font-semibold text-gray-200">Test Architect is Analyzing...</h2>
          <p className="text-sm text-gray-400">
            Generating test scenarios from domain knowledge. Each test case will be traced
            back to evidence from the original KT recording.
          </p>
          <p className="text-xs text-gray-500 animate-pulse">This may take 1-3 minutes on CPU...</p>
        </div>
      </div>
    );
  }

  if (state === 'error') {
    return (
      <div className="flex h-full items-center justify-center">
        <div className="flex flex-col items-center gap-4 max-w-md text-center">
          <XCircle className="h-10 w-10 text-red-500" />
          <h2 className="text-lg font-semibold text-gray-200">Test Strategy Generation Failed</h2>
          <p className="text-sm text-red-400">{error}</p>
          <div className="flex gap-3">
            <button
              onClick={() => navigate(-1)}
              className="rounded-lg bg-gray-800 px-4 py-2 text-sm text-gray-300 hover:bg-gray-700 transition-colors"
            >
              Go Back
            </button>
            <button
              onClick={() => loadStrategy(true)}
              className="rounded-lg bg-nexus-600 px-4 py-2 text-sm text-white hover:bg-nexus-500 transition-colors"
            >
              Retry
            </button>
          </div>
        </div>
      </div>
    );
  }

  if (!strategy || !stats) return null;

  const plan = strategy.test_plan;
  const prov = strategy.provenance;

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
          <FlaskConical className="h-5 w-5 text-nexus-400" />
          <div className="flex-1 min-w-0">
            <h1 className="text-sm font-semibold text-gray-200 line-clamp-1">
              {plan.name || 'Test Strategy'}
            </h1>
            <p className="text-[11px] text-gray-500">
              {plan.source_persona && `From: ${plan.source_persona} • `}
              {stats.totalCases} test cases • {stats.coveragePct}% coverage
              {strategy.cached && ' • ⚡ cached'}
            </p>
          </div>
          <button
            onClick={() => loadStrategy(true)}
            className="rounded-lg bg-nexus-600/80 px-3 py-1.5 text-xs font-medium text-white hover:bg-nexus-500 transition-colors flex items-center gap-1.5"
          >
            <Sparkles className="h-3.5 w-3.5" /> Regenerate
          </button>
          <button
            onClick={() => exportTestCasesCSV(allTestCases, `test-cases-${artifactId.slice(0, 8)}.csv`)}
            className="rounded-lg bg-gray-800 px-3 py-1.5 text-xs font-medium text-gray-300 hover:bg-gray-700 transition-colors flex items-center gap-1.5"
            title="Download all test cases as CSV"
          >
            <Download className="h-3.5 w-3.5" /> Download CSV
          </button>
          <button
            onClick={() => navigate(`/sessions/${sessionId}/persona-workspace?artifact_id=${artifactId}`)}
            className="rounded-lg bg-gray-800 px-3 py-1.5 text-xs font-medium text-gray-300 hover:bg-gray-700 transition-colors flex items-center gap-1.5"
          >
            <ArrowRight className="h-3.5 w-3.5" /> View Persona
          </button>
        </div>
      </div>

      {/* ── 3-Panel Layout ───────────────────────────────── */}
      <div className="flex flex-1 overflow-hidden">

        {/* ── LEFT: Plan Summary + Coverage ──────────────── */}
        <div className="w-80 shrink-0 border-r border-gray-800 overflow-y-auto p-4 space-y-4">

          {/* Plan Summary */}
          <div className="rounded-xl border border-gray-700/50 bg-gray-900/50 p-4 space-y-3">
            <h2 className="text-xs font-semibold uppercase tracking-wider text-nexus-400 flex items-center gap-1.5">
              <Target className="h-3.5 w-3.5" /> Test Plan
            </h2>
            {plan.objective && (
              <div>
                <span className="text-[10px] text-gray-500 uppercase">Objective</span>
                <p className="text-[11px] text-gray-300 mt-0.5">{plan.objective}</p>
              </div>
            )}
            {plan.scope && (
              <div>
                <span className="text-[10px] text-gray-500 uppercase">Scope</span>
                <p className="text-[11px] text-gray-300 mt-0.5">{plan.scope}</p>
              </div>
            )}
            <div>
              <span className="text-[10px] text-gray-500 uppercase">Approach</span>
              <p className="text-[11px] text-gray-300 mt-0.5 capitalize">{plan.approach}</p>
            </div>
          </div>

          {/* Coverage Stats */}
          <div className="rounded-xl border border-gray-700/50 bg-gray-900/50 p-4 space-y-3">
            <h2 className="text-xs font-semibold uppercase tracking-wider text-green-400 flex items-center gap-1.5">
              <BarChart3 className="h-3.5 w-3.5" /> Coverage
            </h2>

            {/* Coverage bar */}
            <div>
              <div className="flex items-center justify-between text-[11px] mb-1">
                <span className="text-gray-400">Step Coverage</span>
                <span className={clsx('font-mono font-semibold', stats.coveragePct >= 80 ? 'text-green-400' : stats.coveragePct >= 50 ? 'text-yellow-400' : 'text-red-400')}>
                  {stats.coveragePct}%
                </span>
              </div>
              <div className="h-2 rounded-full bg-gray-800 overflow-hidden">
                <div
                  className={clsx('h-full rounded-full transition-all', stats.coveragePct >= 80 ? 'bg-green-500' : stats.coveragePct >= 50 ? 'bg-yellow-500' : 'bg-red-500')}
                  style={{ width: `${Math.min(stats.coveragePct, 100)}%` }}
                />
              </div>
            </div>

            {/* Stats grid */}
            <div className="grid grid-cols-2 gap-2">
              <div className="rounded-lg bg-gray-800/50 border border-gray-700/30 p-2 text-center">
                <p className="text-lg font-bold text-gray-200">{stats.totalScenarios}</p>
                <p className="text-[10px] text-gray-500">Scenarios</p>
              </div>
              <div className="rounded-lg bg-gray-800/50 border border-gray-700/30 p-2 text-center">
                <p className="text-lg font-bold text-gray-200">{stats.totalCases}</p>
                <p className="text-[10px] text-gray-500">Test Cases</p>
              </div>
            </div>

            {/* Priority breakdown */}
            <div>
              <span className="text-[10px] text-gray-500 uppercase">By Priority</span>
              <div className="mt-1 space-y-1">
                {[['P0_critical', stats.p0Count], ['P1_high', stats.p1Count], ['P2_medium', stats.p2Count], ['P3_low', stats.p3Count]].map(([key, count]) => {
                  const conf = PRIORITY_CONFIG[key as string];
                  return (
                    <div key={key as string} className="flex items-center gap-2 text-[11px]">
                      <span className={clsx('w-2 h-2 rounded-full', conf.bg.split(' ')[0])} />
                      <span className="text-gray-400 flex-1">{conf.label}</span>
                      <span className={clsx('font-mono font-semibold', conf.color)}>{count as number}</span>
                    </div>
                  );
                })}
              </div>
            </div>

            {/* Category breakdown */}
            <div>
              <span className="text-[10px] text-gray-500 uppercase">By Category</span>
              <div className="mt-1 space-y-1">
                {[['happy_path', stats.happyPath], ['negative', stats.negative], ['boundary', stats.boundary], ['edge_case', stats.edgeCase]].map(([key, count]) => {
                  const conf = CATEGORY_CONFIG[key as string];
                  return (
                    <div key={key as string} className="flex items-center gap-2 text-[11px]">
                      <span className="text-gray-500">{conf.icon}</span>
                      <span className="text-gray-400 flex-1">{conf.label}</span>
                      <span className="font-mono font-semibold text-gray-300">{count as number}</span>
                    </div>
                  );
                })}
              </div>
            </div>

            {/* Gaps */}
            {stats.gaps.length > 0 && (
              <div>
                <span className="text-[10px] text-red-400 uppercase flex items-center gap-1">
                  <AlertTriangle className="h-3 w-3" /> Coverage Gaps
                </span>
                <ul className="mt-1 space-y-0.5">
                  {stats.gaps.map((g, i) => (
                    <li key={i} className="text-[11px] text-red-400/70">• {g}</li>
                  ))}
                </ul>
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
              <div className="flex justify-between"><span className="text-gray-500">Generation</span><span className="text-gray-300">{(prov.generation_time_ms / 1000).toFixed(1)}s</span></div>
              {strategy.cached ? (
                <>
                  <div className="flex justify-between"><span className="text-gray-500">Original Processing</span><span className="text-gray-300">{(strategy.processing_time_ms / 1000).toFixed(1)}s</span></div>
                  <div className="flex justify-between"><span className="text-gray-500">Cache Load</span><span className="text-emerald-400 font-mono">{strategy.cache_hit_ms != null ? `${strategy.cache_hit_ms.toFixed(0)}ms` : '—'}</span></div>
                </>
              ) : (
                <div className="flex justify-between"><span className="text-gray-500">Total Time</span><span className="text-gray-300">{(strategy.processing_time_ms / 1000).toFixed(1)}s</span></div>
              )}
              {prov.generated_at && (
                <div className="flex justify-between"><span className="text-gray-500">Generated</span><span className="text-gray-300">{new Date(prov.generated_at).toLocaleString()}</span></div>
              )}
            </div>
          </div>
        </div>

        {/* ── CENTER: Test Scenarios ─────────────────────── */}
        <div className="flex-1 overflow-y-auto p-6 space-y-4">
          <div className="flex items-center gap-2 mb-2">
            <FlaskConical className="h-4 w-4 text-nexus-400" />
            <h2 className="text-sm font-semibold text-gray-200">
              {viewMode === 'workflow' ? 'Test Scenarios by Workflow Step' : 'Test Scenarios by Category'}
            </h2>
            <div className="ml-auto flex items-center gap-1 rounded-lg bg-gray-800/50 border border-gray-700/50 p-0.5">
              <button
                onClick={() => setViewMode('category')}
                className={clsx(
                  'rounded-md px-2.5 py-1 text-[10px] font-medium transition-colors',
                  viewMode === 'category'
                    ? 'bg-nexus-600/80 text-white'
                    : 'text-gray-400 hover:text-gray-200'
                )}
              >
                By Category
              </button>
              <button
                onClick={() => setViewMode('workflow')}
                className={clsx(
                  'rounded-md px-2.5 py-1 text-[10px] font-medium transition-colors',
                  viewMode === 'workflow'
                    ? 'bg-nexus-600/80 text-white'
                    : 'text-gray-400 hover:text-gray-200'
                )}
              >
                By Workflow
              </button>
            </div>
            <span className="text-[10px] text-gray-500 font-mono">
              {strategy.test_scenarios.length} scenarios
            </span>
          </div>

          {/* Search bar + category filter badges */}
          <div className="space-y-2">
            <div className="relative">
              <Search className="absolute left-3 top-1/2 -translate-y-1/2 h-3.5 w-3.5 text-gray-500" />
              <input
                type="text"
                placeholder="Search test cases by title, action, or tag..."
                value={searchQuery}
                onChange={(e) => { setSearchQuery(e.target.value); setShowMax(20); }}
                className="w-full rounded-lg border border-gray-700/50 bg-gray-800/50 pl-9 pr-8 py-2 text-xs text-gray-200 placeholder-gray-500 focus:border-nexus-500/50 focus:outline-none focus:ring-1 focus:ring-nexus-500/30"
              />
              {searchQuery && (
                <button onClick={() => setSearchQuery('')} className="absolute right-2.5 top-1/2 -translate-y-1/2 text-gray-500 hover:text-gray-300">
                  <X className="h-3.5 w-3.5" />
                </button>
              )}
            </div>
            <div className="flex items-center gap-1.5 flex-wrap">
              {Object.entries(CATEGORY_CONFIG).map(([key, cfg]) => {
                const isActive = activeFilters.has(key);
                return (
                  <button
                    key={key}
                    onClick={() => {
                      setActiveFilters(prev => {
                        const next = new Set(prev);
                        next.has(key) ? next.delete(key) : next.add(key);
                        return next;
                      });
                      setShowMax(20);
                    }}
                    className={clsx(
                      'inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[10px] font-medium transition-colors',
                      isActive ? cfg.color : 'text-gray-500 bg-gray-800/30 border-gray-700/30 hover:border-gray-600/50'
                    )}
                  >
                    {cfg.icon} {cfg.label}
                    {isActive && <X className="h-2.5 w-2.5 ml-0.5" />}
                  </button>
                );
              })}
              {(activeFilters.size > 0 || searchQuery) && (
                <button
                  onClick={() => { setActiveFilters(new Set()); setSearchQuery(''); }}
                  className="text-[10px] text-gray-500 hover:text-gray-300 ml-1"
                >
                  Clear all
                </button>
              )}
            </div>
          </div>

          {/* Filtered scenario list */}
          {(() => {
            const q = searchQuery.toLowerCase();
            const matchCase = (tc: TestCase) => {
              if (activeFilters.size > 0 && !activeFilters.has(tc.category)) return false;
              if (!q) return true;
              return tc.title.toLowerCase().includes(q) ||
                tc.case_id.toLowerCase().includes(q) ||
                tc.tags.some(t => t.toLowerCase().includes(q)) ||
                tc.steps.some(s => s.action.toLowerCase().includes(q));
            };

            if (viewMode === 'workflow') {
              const filtered = strategy.test_scenarios
                .map(sc => ({ ...sc, test_cases: sc.test_cases.filter(matchCase) }))
                .filter(sc => sc.test_cases.length > 0);
              const totalCases = filtered.reduce((n, s) => n + s.test_cases.length, 0);
              return (
                <>
                  {searchQuery || activeFilters.size > 0 ? (
                    <p className="text-[10px] text-gray-500">{totalCases} test case{totalCases !== 1 ? 's' : ''} match</p>
                  ) : null}
                  {filtered.map((sc) => (
                    <ScenarioSection key={sc.scenario_id} scenario={sc} />
                  ))}
                  {filtered.length === 0 && (
                    <p className="text-sm text-gray-500 text-center py-8">No test cases match your search.</p>
                  )}
                </>
              );
            } else {
              const filtered = categoryGroups
                .map(g => ({ ...g, cases: g.cases.filter(matchCase) }))
                .filter(g => g.cases.length > 0);
              const totalCases = filtered.reduce((n, g) => n + g.cases.length, 0);
              const visible = filtered.slice(0, showMax);
              return (
                <>
                  {searchQuery || activeFilters.size > 0 ? (
                    <p className="text-[10px] text-gray-500">{totalCases} test case{totalCases !== 1 ? 's' : ''} match</p>
                  ) : null}
                  {visible.map(({ category, cases }) => (
                    <CategorySection key={category} category={category} cases={cases} />
                  ))}
                  {filtered.length > showMax && (
                    <button
                      onClick={() => setShowMax(prev => prev + 20)}
                      className="w-full rounded-lg border border-gray-700/50 bg-gray-800/30 py-2 text-xs text-gray-400 hover:text-gray-200 hover:bg-gray-800/50 transition-colors"
                    >
                      Show more ({filtered.length - showMax} remaining)
                    </button>
                  )}
                  {filtered.length === 0 && (
                    <p className="text-sm text-gray-500 text-center py-8">No test cases match your search.</p>
                  )}
                </>
              );
            }
          })()}
        </div>

        {/* ── RIGHT: Traceability Matrix ────────────────── */}
        <div className="w-72 shrink-0 border-l border-gray-800 overflow-y-auto p-4 space-y-4">
          <div className="rounded-xl border border-gray-700/50 bg-gray-900/50 p-4 space-y-3">
            <h2 className="text-xs font-semibold uppercase tracking-wider text-amber-400 flex items-center gap-1.5">
              <Link2 className="h-3.5 w-3.5" /> Traceability Matrix
            </h2>
            <p className="text-[10px] text-gray-500">
              Requirement → Test Case mapping with coverage status
            </p>

            <div className="space-y-2">
              {strategy.traceability.map((entry, i) => (
                <TraceRow key={i} entry={entry} />
              ))}
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}

function TraceRow({ entry }: { entry: TraceabilityEntry }) {
  const [open, setOpen] = useState(false);
  const statusCfg = coverageStatusColor(entry.coverage_status);

  return (
    <div className="rounded-lg border border-gray-700/40 bg-gray-800/30 overflow-hidden">
      <button
        onClick={() => setOpen(!open)}
        className="w-full flex items-center gap-2 px-3 py-2 text-left hover:bg-gray-800/50 transition-colors"
      >
        <span className="flex items-center justify-center h-5 w-5 rounded bg-gray-700/50 text-[10px] font-mono text-gray-400 shrink-0">
          {entry.workflow_step_number}
        </span>
        <span className="text-[11px] text-gray-300 flex-1 line-clamp-1">{entry.requirement}</span>
        <span className={clsx('rounded-full border px-1.5 py-0.5 text-[9px] font-medium uppercase', statusCfg)}>
          {entry.coverage_status}
        </span>
      </button>
      {open && entry.test_case_ids.length > 0 && (
        <div className="border-t border-gray-700/30 px-3 py-2 space-y-1">
          {entry.test_case_ids.map((id) => (
            <div key={id} className="flex items-center gap-1.5 text-[10px]">
              <CheckCircle2 className="h-3 w-3 text-green-500" />
              <span className="text-gray-400 font-mono">{id}</span>
            </div>
          ))}
          <div className="flex items-center gap-1 text-[10px] text-gray-500 mt-1">
            <Link2 className="h-2.5 w-2.5" /> {entry.evidence_count} evidence links
          </div>
        </div>
      )}
    </div>
  );
}
