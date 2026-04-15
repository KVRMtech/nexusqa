// ═══════════════════════════════════════════════════════════════
//  MODULE 7 — LIVING TRACEABILITY MATRIX
//  "Speaker → Rule → Test → Result → Jira"
// ═══════════════════════════════════════════════════════════════

import { useState } from 'react';
import api from '../services/api';
import { useApiData } from '../hooks/useApiData';
import { PageHeader } from '../components';
import { EmptyState } from '../components/EmptyState';
import {
  Link2,
  ArrowRight,
  AlertTriangle,
  CheckCircle2,
  XCircle,
  ChevronDown,
  ChevronUp,
  Search,
  Filter,
  ExternalLink,
  Radio,
  FileText,
  Bug,
  Zap,
  Clock,
} from 'lucide-react';
import clsx from 'clsx';

// ── Types ─────────────────────────────────────────────────

interface TraceRow {
  id: string;
  speaker: string;
  session: string;
  sessionDate: string;
  ruleId: string;
  ruleTitle: string;
  ruleConfidence: number;
  tests: TraceTest[];
  jiraTickets: string[];
  coverage: 'full' | 'partial' | 'none';
}

interface TraceTest {
  testId: string;
  title: string;
  type: 'web' | 'api' | 'mainframe' | 'data-driven';
  lastResult: 'pass' | 'fail' | 'not-run';
  passRate: number;
}

// ── Empty fallback (production) ─────────────────────────────

const EMPTY_TRACES: TraceRow[] = [];

// ── Component ─────────────────────────────────────────────

export default function TraceabilityMatrixPage() {
  const { data: traces, isLive } = useApiData(
    () => api.listTraces('t-1'),
    EMPTY_TRACES,
  );
  const [search, setSearch] = useState('');
  const [expandedRow, setExpandedRow] = useState<string | null>(null);
  const [coverageFilter, setCoverageFilter] = useState<string>('all');

  const fullCoverage = traces.filter((t) => t.coverage === 'full').length;
  const partialCoverage = traces.filter((t) => t.coverage === 'partial').length;
  const noCoverage = traces.filter((t) => t.coverage === 'none').length;
  const totalTests = traces.reduce((s, t) => s + t.tests.length, 0);
  const failingTests = traces.reduce(
    (s, t) => s + t.tests.filter((tc) => tc.lastResult === 'fail').length,
    0,
  );

  const filtered = traces.filter((t) => {
    if (coverageFilter !== 'all' && t.coverage !== coverageFilter) return false;
    if (search) {
      const q = search.toLowerCase();
      return (
        t.ruleId.toLowerCase().includes(q) ||
        t.ruleTitle.toLowerCase().includes(q) ||
        t.speaker.toLowerCase().includes(q) ||
        t.tests.some((tc) => tc.testId.toLowerCase().includes(q))
      );
    }
    return true;
  });

  const TestTypeBadge = ({ type }: { type: string }) => {
    const cls =
      type === 'web' ? 'badge-blue' :
      type === 'api' ? 'badge-nexus' :
      type === 'mainframe' ? 'badge-purple' : 'badge-yellow';
    return <span className={cls}>{type.toUpperCase()}</span>;
  };

  const ResultIcon = ({ result }: { result: string }) => {
    if (result === 'pass') return <CheckCircle2 className="h-3.5 w-3.5 text-green-400" />;
    if (result === 'fail') return <XCircle className="h-3.5 w-3.5 text-red-400" />;
    return <Clock className="h-3.5 w-3.5 text-gray-500" />;
  };

  return (
    <div className="space-y-6 animate-fade-in">
      {/* Header */}
      <PageHeader
        title="Living Traceability Matrix"
        subtitle="Every rule traces back to a speaker & forward to tests and Jira tickets."
        isLive={isLive}
      />

      {/* Summary cards */}
      <div className="grid grid-cols-2 lg:grid-cols-5 gap-3">
        <div className="stat-card p-4">
          <p className="text-[11px] font-medium text-gray-500 uppercase tracking-wider">Total Rules</p>
          <p className="text-2xl font-bold text-white mt-1">{traces.length}</p>
        </div>
        <button
          onClick={() => setCoverageFilter(coverageFilter === 'full' ? 'all' : 'full')}
          className={clsx('stat-card p-4 text-left transition-all', coverageFilter === 'full' && 'ring-green-500/30')}
        >
          <p className="text-[11px] font-medium text-gray-500 uppercase tracking-wider">Full Coverage</p>
          <p className="text-2xl font-bold text-green-400 mt-1">{fullCoverage}</p>
        </button>
        <button
          onClick={() => setCoverageFilter(coverageFilter === 'partial' ? 'all' : 'partial')}
          className={clsx('stat-card p-4 text-left transition-all', coverageFilter === 'partial' && 'ring-yellow-500/30')}
        >
          <p className="text-[11px] font-medium text-gray-500 uppercase tracking-wider">Partial</p>
          <p className="text-2xl font-bold text-yellow-400 mt-1">{partialCoverage}</p>
        </button>
        <button
          onClick={() => setCoverageFilter(coverageFilter === 'none' ? 'all' : 'none')}
          className={clsx('stat-card p-4 text-left transition-all', coverageFilter === 'none' && 'ring-red-500/30')}
        >
          <p className="text-[11px] font-medium text-gray-500 uppercase tracking-wider">No Coverage</p>
          <p className="text-2xl font-bold text-red-400 mt-1">{noCoverage}</p>
        </button>
        <div className="stat-card p-4">
          <p className="text-[11px] font-medium text-gray-500 uppercase tracking-wider">Failing Tests</p>
          <p className="text-2xl font-bold text-red-400 mt-1">{failingTests}</p>
          <p className="text-[11px] text-gray-500">of {totalTests} total</p>
        </div>
      </div>

      {/* Search */}
      <div className="relative max-w-md">
        <Search className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-gray-500" />
        <input
          type="text"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          placeholder="Search rules, tests, speakers…"
          className="input-field pl-10 w-full"
        />
      </div>

      {/* Gap alert */}
      {noCoverage > 0 && coverageFilter !== 'full' && (
        <div className="rounded-xl bg-red-500/10 ring-1 ring-red-500/20 p-4 flex items-start gap-3">
          <AlertTriangle className="h-5 w-5 text-red-400 shrink-0 mt-0.5" />
          <div>
            <p className="text-sm font-semibold text-red-400">Coverage Gap Detected</p>
            <p className="text-xs text-gray-400 mt-0.5">
              {noCoverage} rule(s) have zero test coverage. These represent untested knowledge — a blind spot in your quality assurance.
            </p>
          </div>
        </div>
      )}

      {/* Traceability rows */}
      <div className="space-y-2">
        {traces.length === 0 && (
          <EmptyState title="No Trace Data" description="Traceability links will appear as rules are extracted and linked to tests and Jira tickets." />
        )}
        {filtered.map((row) => {
          const isExpanded = expandedRow === row.id;
          const coverageColor =
            row.coverage === 'full' ? 'bg-green-500' :
            row.coverage === 'partial' ? 'bg-yellow-500' : 'bg-red-500';
          const passingTests = row.tests.filter((t) => t.lastResult === 'pass').length;
          const failTests = row.tests.filter((t) => t.lastResult === 'fail').length;

          return (
            <div key={row.id} className="card overflow-hidden">
              <button
                onClick={() => setExpandedRow(isExpanded ? null : row.id)}
                className="flex w-full items-center gap-4 p-4 text-left hover:bg-white/[0.02] transition-colors"
              >
                {/* Coverage indicator */}
                <div className={clsx('h-8 w-1 rounded-full shrink-0', coverageColor)} />

                {/* Chain: Speaker → Rule → Tests → Jira */}
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-1.5 text-xs text-gray-400 flex-wrap">
                    <Radio className="h-3 w-3 text-pink-400" />
                    <span className="font-medium text-gray-300 truncate max-w-[120px]">{row.speaker}</span>
                    <ArrowRight className="h-3 w-3 text-gray-600" />
                    <FileText className="h-3 w-3 text-nexus-400" />
                    <span className="font-mono text-nexus-400">{row.ruleId}</span>
                    <ArrowRight className="h-3 w-3 text-gray-600" />
                    <Zap className="h-3 w-3 text-cyan-400" />
                    <span>
                      {row.tests.length === 0 ? (
                        <span className="text-red-400">0 tests</span>
                      ) : (
                        <span>
                          <span className="text-green-400">{passingTests}✓</span>
                          {failTests > 0 && <span className="text-red-400 ml-1">{failTests}✗</span>}
                        </span>
                      )}
                    </span>
                    {row.jiraTickets.length > 0 && (
                      <>
                        <ArrowRight className="h-3 w-3 text-gray-600" />
                        <Bug className="h-3 w-3 text-blue-400" />
                        <span className="text-blue-400">{row.jiraTickets.length}</span>
                      </>
                    )}
                  </div>

                  <p className="text-sm text-gray-200 mt-1 truncate">{row.ruleTitle}</p>
                  <p className="text-[10px] text-gray-600 mt-0.5">{row.session} • {row.sessionDate}</p>
                </div>

                {/* Confidence */}
                <div className="text-right shrink-0">
                  <p className={clsx('text-sm font-bold', row.ruleConfidence >= 90 ? 'text-green-400' : 'text-yellow-400')}>
                    {row.ruleConfidence}%
                  </p>
                  <p className="text-[10px] text-gray-600">confidence</p>
                </div>

                {isExpanded ? <ChevronUp className="h-4 w-4 text-gray-500 shrink-0" /> : <ChevronDown className="h-4 w-4 text-gray-500 shrink-0" />}
              </button>

              {isExpanded && (
                <div className="border-t border-white/[0.06] p-4 animate-slide-in">
                  {row.tests.length === 0 ? (
                    <div className="text-center py-6">
                      <AlertTriangle className="h-8 w-8 text-red-400/50 mx-auto mb-2" />
                      <p className="text-sm text-gray-400">No tests linked to this rule</p>
                      <button className="btn-primary text-xs mt-3 py-1.5 px-3">
                        <Zap className="h-3.5 w-3.5" /> Auto-Generate Tests
                      </button>
                    </div>
                  ) : (
                    <div>
                      <table className="w-full text-left">
                        <thead>
                          <tr className="text-[10px] text-gray-500 uppercase tracking-wider border-b border-white/[0.04]">
                            <th className="pb-2 pr-2">Test ID</th>
                            <th className="pb-2 pr-2">Title</th>
                            <th className="pb-2 pr-2">Type</th>
                            <th className="pb-2 pr-2">Last Result</th>
                            <th className="pb-2 text-right">Pass Rate</th>
                          </tr>
                        </thead>
                        <tbody>
                          {row.tests.map((tc) => (
                            <tr key={tc.testId} className="border-b border-white/[0.03] last:border-0">
                              <td className="py-2 pr-2 text-xs font-mono text-cyan-400">{tc.testId}</td>
                              <td className="py-2 pr-2 text-xs text-gray-300">{tc.title}</td>
                              <td className="py-2 pr-2"><TestTypeBadge type={tc.type} /></td>
                              <td className="py-2 pr-2">
                                <div className="flex items-center gap-1.5 text-xs">
                                  <ResultIcon result={tc.lastResult} />
                                  <span className={clsx(
                                    tc.lastResult === 'pass' && 'text-green-400',
                                    tc.lastResult === 'fail' && 'text-red-400',
                                    tc.lastResult === 'not-run' && 'text-gray-500',
                                  )}>
                                    {tc.lastResult}
                                  </span>
                                </div>
                              </td>
                              <td className="py-2 text-right">
                                <div className="flex items-center justify-end gap-2">
                                  <div className="w-16 h-1.5 rounded-full bg-white/5 overflow-hidden">
                                    <div
                                      className={clsx('h-full rounded-full', tc.passRate >= 80 ? 'bg-green-500' : tc.passRate >= 50 ? 'bg-yellow-500' : 'bg-red-500')}
                                      style={{ width: `${tc.passRate}%` }}
                                    />
                                  </div>
                                  <span className="text-xs text-gray-400 w-8 text-right">{tc.passRate}%</span>
                                </div>
                              </td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  )}

                  {row.jiraTickets.length > 0 && (
                    <div className="mt-3 pt-3 border-t border-white/[0.04] flex items-center gap-2">
                      <Bug className="h-3.5 w-3.5 text-blue-400" />
                      <span className="text-xs text-gray-500">Linked Jira:</span>
                      {row.jiraTickets.map((j) => (
                        <button key={j} className="text-xs text-blue-400 hover:text-blue-300 flex items-center gap-1">
                          {j} <ExternalLink className="h-3 w-3" />
                        </button>
                      ))}
                    </div>
                  )}
                </div>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}
