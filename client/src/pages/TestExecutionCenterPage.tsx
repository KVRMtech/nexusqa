// ═══════════════════════════════════════════════════════════════
//  MODULE 8 — TEST GENERATION & EXECUTION CENTER
//  "Generate + execute + auto-file defects in one flow"
// ═══════════════════════════════════════════════════════════════

import { useState } from 'react';
import api from '../services/api';
import { useApiData } from '../hooks/useApiData';
import { PageHeader, Tabs } from '../components';
import { EmptyState } from '../components/EmptyState';
import {
  Zap,
  Play,
  CheckCircle2,
  XCircle,
  Clock,
  Globe,
  Server,
  Cpu,
  Database,
  Bug,
  RotateCw,
  ChevronDown,
  ChevronUp,
  PlusCircle,
  Pause,
  ExternalLink,
} from 'lucide-react';
import clsx from 'clsx';

// ── Types ─────────────────────────────────────────────────

interface TestSuite {
  id: string;
  name: string;
  type: 'web' | 'api' | 'mainframe' | 'data-driven';
  totalTests: number;
  passed: number;
  failed: number;
  running: number;
  notRun: number;
  lastRun: string | null;
}

interface ExecutionRun {
  id: string;
  name: string;
  status: 'running' | 'completed' | 'failed' | 'queued';
  progress: number;
  totalTests: number;
  passed: number;
  failed: number;
  startedAt: string;
  duration: string;
  failures: ExecutionFailure[];
}

interface ExecutionFailure {
  testId: string;
  testName: string;
  error: string;
  screenshot?: string;
  jiraTicket?: string;
}

// ── Empty fallback (production) ─────────────────────────────

const EMPTY_SUITES: TestSuite[] = [];

const EMPTY_RUNS: ExecutionRun[] = [];

// ── Component ─────────────────────────────────────────────

export default function TestExecutionCenterPage() {
  const { data: suites, isLive } = useApiData(
    () => api.listTestSuites('t-1'),
    EMPTY_SUITES,
  );
  const { data: runs } = useApiData(
    () => api.listTestRuns('t-1'),
    EMPTY_RUNS,
  );
  const [expandedRun, setExpandedRun] = useState<string | null>(null);
  const [activeTab, setActiveTab] = useState<'suites' | 'runs'>('runs');

  const totalTests = suites.reduce((s, t) => s + t.totalTests, 0);
  const totalPassed = suites.reduce((s, t) => s + t.passed, 0);
  const totalFailed = suites.reduce((s, t) => s + t.failed, 0);
  const totalRunning = suites.reduce((s, t) => s + t.running, 0);
  const passRate = totalTests > 0 ? ((totalPassed / totalTests) * 100).toFixed(1) : '0';

  const SuiteIcon = ({ type }: { type: string }) => {
    if (type === 'web') return <Globe className="h-4 w-4 text-blue-400" />;
    if (type === 'api') return <Server className="h-4 w-4 text-nexus-400" />;
    if (type === 'mainframe') return <Cpu className="h-4 w-4 text-purple-400" />;
    return <Database className="h-4 w-4 text-yellow-400" />;
  };

  const StatusBadge = ({ status }: { status: string }) => {
    if (status === 'running') return <span className="badge-blue flex items-center gap-1"><RotateCw className="h-3 w-3 animate-spin" /> RUNNING</span>;
    if (status === 'completed') return <span className="badge-green">COMPLETED</span>;
    if (status === 'failed') return <span className="badge-red">FAILED</span>;
    return <span className="badge-gray">QUEUED</span>;
  };

  return (
    <div className="space-y-6 animate-fade-in">
      {/* Header */}
      <PageHeader
        title="Test Execution Center"
        subtitle="Generate, execute, and auto-file defects — all from one control panel."
        isLive={isLive}
      />

      {/* Summary strip */}
      <div className="grid grid-cols-2 lg:grid-cols-5 gap-3">
        <div className="stat-card p-4">
          <p className="text-[11px] font-medium text-gray-500 uppercase tracking-wider">Total Tests</p>
          <p className="text-2xl font-bold text-white mt-1">{totalTests}</p>
        </div>
        <div className="stat-card p-4">
          <p className="text-[11px] font-medium text-gray-500 uppercase tracking-wider">Pass Rate</p>
          <p className="text-2xl font-bold text-green-400 mt-1">{passRate}%</p>
        </div>
        <div className="stat-card p-4">
          <p className="text-[11px] font-medium text-gray-500 uppercase tracking-wider">Passed</p>
          <p className="text-2xl font-bold text-green-400 mt-1">{totalPassed}</p>
        </div>
        <div className="stat-card p-4">
          <p className="text-[11px] font-medium text-gray-500 uppercase tracking-wider">Failed</p>
          <p className="text-2xl font-bold text-red-400 mt-1">{totalFailed}</p>
        </div>
        <div className="stat-card p-4">
          <p className="text-[11px] font-medium text-gray-500 uppercase tracking-wider">Running</p>
          <p className="text-2xl font-bold text-blue-400 mt-1 flex items-center gap-2">
            {totalRunning}
            {totalRunning > 0 && <span className="h-2 w-2 rounded-full bg-blue-400 animate-pulse" />}
          </p>
        </div>
      </div>

      {/* Test type breakdown - horizontal cards */}
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
        {suites.map((suite) => {
          const pr = suite.totalTests > 0 ? (suite.passed / suite.totalTests) * 100 : 0;
          return (
            <div key={suite.id} className="card p-4">
              <div className="flex items-center gap-2 mb-3">
                <SuiteIcon type={suite.type} />
                <span className="text-xs font-medium text-gray-300">{suite.name}</span>
              </div>
              {/* Stacked bar */}
              <div className="flex h-2 rounded-full overflow-hidden bg-white/5 mb-2">
                <div className="bg-green-500" style={{ width: `${(suite.passed / suite.totalTests) * 100}%` }} />
                <div className="bg-red-500" style={{ width: `${(suite.failed / suite.totalTests) * 100}%` }} />
                <div className="bg-blue-400 animate-pulse" style={{ width: `${(suite.running / suite.totalTests) * 100}%` }} />
              </div>
              <div className="flex items-center justify-between text-[10px] text-gray-500">
                <span><span className="text-green-400 font-semibold">{suite.passed}</span> pass</span>
                <span><span className="text-red-400 font-semibold">{suite.failed}</span> fail</span>
                <span>{suite.totalTests} total</span>
              </div>
            </div>
          );
        })}
      </div>

      {/* Tabs */}
      <div className="flex items-center gap-1 border-b border-white/[0.06] pb-0">
        <Tabs
          tabs={[
            { id: 'runs', label: 'Execution Runs' },
            { id: 'suites', label: 'Test Suites' },
          ]}
          activeTab={activeTab}
          onChange={(id) => setActiveTab(id as typeof activeTab)}
        />
        <div className="ml-auto flex gap-2 pb-1">
          <button className="btn-primary text-xs py-1.5 px-3">
            <Play className="h-3.5 w-3.5" /> New Run
          </button>
          <button className="btn-ghost text-xs py-1.5 px-3">
            <PlusCircle className="h-3.5 w-3.5" /> Generate Tests
          </button>
        </div>
      </div>

      {/* Execution runs */}
      {activeTab === 'runs' && (
        <div className="space-y-3">
          {runs.length === 0 && (
            <EmptyState title="No Execution Runs" description="Start a test run to see execution results here." />
          )}
          {runs.map((run) => {
            const isExpanded = expandedRun === run.id;
            return (
              <div key={run.id} className="card overflow-hidden">
                <button
                  onClick={() => setExpandedRun(isExpanded ? null : run.id)}
                  className="flex w-full items-center gap-4 p-4 text-left hover:bg-white/[0.02] transition-colors"
                >
                  {/* Progress ring */}
                  <div className="relative h-10 w-10 shrink-0">
                    <svg viewBox="0 0 36 36" className="h-10 w-10 -rotate-90">
                      <circle cx="18" cy="18" r="15" fill="none" stroke="currentColor" strokeWidth="2" className="text-white/5" />
                      <circle
                        cx="18" cy="18" r="15" fill="none" strokeWidth="2.5"
                        strokeDasharray={`${(run.progress / 100) * 94.2} 94.2`}
                        strokeLinecap="round"
                        className={clsx(
                          run.status === 'running' ? 'text-blue-400' :
                          run.failed > 0 ? 'text-yellow-500' : 'text-green-500',
                        )}
                        stroke="currentColor"
                      />
                    </svg>
                    <span className="absolute inset-0 flex items-center justify-center text-[9px] font-bold text-gray-300">
                      {run.progress}%
                    </span>
                  </div>

                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-2 flex-wrap">
                      <StatusBadge status={run.status} />
                      <h3 className="text-sm font-medium text-gray-200">{run.name}</h3>
                    </div>
                    <div className="flex items-center gap-4 mt-1 text-[11px] text-gray-500">
                      <span>{run.totalTests} tests</span>
                      <span className="text-green-400">{run.passed} pass</span>
                      {run.failed > 0 && <span className="text-red-400">{run.failed} fail</span>}
                      <span>{run.duration}</span>
                    </div>
                  </div>

                  {/* Live progress bar */}
                  {run.status === 'running' && (
                    <div className="w-32 shrink-0 hidden lg:block">
                      <div className="h-1.5 rounded-full bg-white/5 overflow-hidden">
                        <div
                          className="h-full bg-gradient-to-r from-blue-500 to-cyan-400 rounded-full transition-all duration-1000 animate-gradient-x"
                          style={{ width: `${run.progress}%` }}
                        />
                      </div>
                    </div>
                  )}

                  {isExpanded ? <ChevronUp className="h-4 w-4 text-gray-500 shrink-0" /> : <ChevronDown className="h-4 w-4 text-gray-500 shrink-0" />}
                </button>

                {isExpanded && run.failures.length > 0 && (
                  <div className="border-t border-white/[0.06] animate-slide-in">
                    <div className="p-3 pb-1">
                      <p className="text-[11px] font-semibold text-red-400 uppercase tracking-wider flex items-center gap-1.5">
                        <XCircle className="h-3.5 w-3.5" /> Live Failure Stream ({run.failures.length})
                      </p>
                    </div>
                    {run.failures.map((f, i) => (
                      <div key={i} className="px-4 py-3 border-b border-white/[0.03] last:border-0 hover:bg-white/[0.02]">
                        <div className="flex items-start gap-3">
                          <XCircle className="h-4 w-4 text-red-400 shrink-0 mt-0.5" />
                          <div className="flex-1 min-w-0">
                            <p className="text-xs font-medium text-gray-300">
                              <span className="font-mono text-red-400">{f.testId}</span> — {f.testName}
                            </p>
                            <p className="text-xs text-gray-500 mt-1 font-mono leading-relaxed">{f.error}</p>
                          </div>
                          <div className="flex gap-1.5 shrink-0">
                            {f.jiraTicket ? (
                              <button className="text-[10px] text-blue-400 flex items-center gap-1 hover:text-blue-300">
                                <Bug className="h-3 w-3" /> {f.jiraTicket} <ExternalLink className="h-3 w-3" />
                              </button>
                            ) : (
                              <button className="btn-ghost text-[10px] py-1 px-2 text-yellow-400">
                                <Bug className="h-3 w-3" /> Auto-File Jira
                              </button>
                            )}
                          </div>
                        </div>
                      </div>
                    ))}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}

      {/* Test suites tab */}
      {activeTab === 'suites' && (
        <div className="card overflow-hidden">
          {suites.length === 0 ? (
            <EmptyState title="No Test Suites" description="Generate tests from knowledge sessions to create test suites." />
          ) : (
          <table className="w-full text-left">
            <thead>
              <tr className="text-[10px] text-gray-500 uppercase tracking-wider border-b border-white/[0.06]">
                <th className="p-3">Suite</th>
                <th className="p-3">Type</th>
                <th className="p-3">Tests</th>
                <th className="p-3">Pass Rate</th>
                <th className="p-3">Status</th>
                <th className="p-3 text-right">Actions</th>
              </tr>
            </thead>
            <tbody>
              {suites.map((suite) => {
                const pr = suite.totalTests > 0 ? ((suite.passed / suite.totalTests) * 100).toFixed(0) : '0';
                return (
                  <tr key={suite.id} className="border-b border-white/[0.03] last:border-0 hover:bg-white/[0.02]">
                    <td className="p-3">
                      <div className="flex items-center gap-2">
                        <SuiteIcon type={suite.type} />
                        <span className="text-sm text-gray-200">{suite.name}</span>
                      </div>
                    </td>
                    <td className="p-3">
                      <span className={clsx(
                        'text-[10px] font-semibold uppercase tracking-wider',
                        suite.type === 'web' && 'text-blue-400',
                        suite.type === 'api' && 'text-nexus-400',
                        suite.type === 'mainframe' && 'text-purple-400',
                        suite.type === 'data-driven' && 'text-yellow-400',
                      )}>
                        {suite.type}
                      </span>
                    </td>
                    <td className="p-3 text-sm text-gray-300">{suite.totalTests}</td>
                    <td className="p-3">
                      <div className="flex items-center gap-2">
                        <div className="w-16 h-1.5 rounded-full bg-white/5 overflow-hidden">
                          <div
                            className={clsx('h-full rounded-full', Number(pr) >= 90 ? 'bg-green-500' : Number(pr) >= 70 ? 'bg-yellow-500' : 'bg-red-500')}
                            style={{ width: `${pr}%` }}
                          />
                        </div>
                        <span className="text-xs text-gray-400">{pr}%</span>
                      </div>
                    </td>
                    <td className="p-3">
                      {suite.running > 0 ? (
                        <span className="badge-blue text-[10px] flex items-center gap-1 w-fit">
                          <RotateCw className="h-3 w-3 animate-spin" /> {suite.running} running
                        </span>
                      ) : (
                        <span className="text-[10px] text-gray-500">Idle</span>
                      )}
                    </td>
                    <td className="p-3 text-right">
                      <button className="btn-ghost text-[10px] py-1 px-2">
                        <Play className="h-3 w-3" /> Run
                      </button>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
          )}
        </div>
      )}
    </div>
  );
}
