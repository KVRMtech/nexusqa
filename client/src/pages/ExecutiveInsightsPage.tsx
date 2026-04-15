// ═══════════════════════════════════════════════════════════════
//  MODULE 11 — EXECUTIVE INSIGHTS DASHBOARD
//  "Landing page — high-level KPIs, ROI, risk grade"
// ═══════════════════════════════════════════════════════════════

import { useState } from 'react';
import api from '../services/api';
import { useApiData } from '../hooks/useApiData';
import { PageHeader } from '../components/PageHeader';
import { StatCard } from '../components/StatCard';
import { StatusBadge } from '../components/StatusBadge';
import { ProgressBar } from '../components/ProgressBar';
import { EmptyState } from '../components/EmptyState';
import {
  BarChart3,
  TrendingUp,
  TrendingDown,
  Shield,
  AlertTriangle,
  CheckCircle2,
  Zap,
  Users,
  FileText,
  Bug,
  Clock,
  Download,
  Calendar,
  ArrowUpRight,
  Brain,
  Target,
  DollarSign,
  Activity,
} from 'lucide-react';
import clsx from 'clsx';
import { Link } from 'react-router-dom';

// ── Empty fallback (production) ─────────────────────────────

const KPI_CARDS: { label: string; value: string; change: string; trend: 'up' | 'down'; color: string; icon: any }[] = [];

const ROI_METRICS: { label: string; value: string; equivalent: string; period: string; highlight?: boolean }[] = [];

const TOP_RISKS: { id: number; title: string; severity: string; domain: string; dueDate?: string; link: string }[] = [];

const EMPTY_WEEKLY_TREND: { week: string; rules: number; tests: number; defects: number }[] = [];

const EMPTY_ENGINE_STATUS: { name: string; status: string; load: number }[] = [];

const SEVERITY_BADGE: Record<string, string> = {
  critical: 'badge-red',
  high: 'badge-yellow',
  medium: 'badge-blue',
  low: 'badge-gray',
};

// ── Component ─────────────────────────────────────────────

export default function ExecutiveInsightsPage() {
  const { data: engineStatus, isLive } = useApiData(
    () => api.getEngineStatus(),
    EMPTY_ENGINE_STATUS,
  );
  const { data: weeklyTrend } = useApiData(
    () => api.getWeeklyTrend('t-1'),
    EMPTY_WEEKLY_TREND,
  );
  const maxRules = weeklyTrend.length > 0 ? Math.max(...weeklyTrend.map((w) => w.rules)) : 1;
  const maxTests = weeklyTrend.length > 0 ? Math.max(...weeklyTrend.map((w) => w.tests)) : 1;

  return (
    <div className="space-y-6 animate-fade-in">
      {/* Header */}
      <PageHeader
        zone="ZONE 4 · OPERATIONS"
        title="Executive Insights"
        subtitle="AI Engine Factory performance — real-time operational intelligence."
        isLive={isLive}
        actions={
          <>
            <button className="btn-ghost text-xs py-1.5 px-3">
              <Calendar className="h-3.5 w-3.5" /> Schedule Digest
            </button>
            <button className="btn-primary text-xs py-1.5 px-3">
              <Download className="h-3.5 w-3.5" /> Board Report
            </button>
          </>
        }
      />

      {/* KPI cards */}
      {KPI_CARDS.length === 0 && weeklyTrend.length === 0 && engineStatus.length === 0 && (
        <EmptyState title="No Data Available" description="Executive insights will populate as engines process knowledge sessions." />
      )}
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
        {KPI_CARDS.map((kpi) => (
          <div key={kpi.label} className="card-glow p-5 relative overflow-hidden">
            {/* Background gradient accent */}
            <div className={clsx('absolute top-0 left-0 right-0 h-1 bg-gradient-to-r', kpi.color)} />
            <div className="flex items-start justify-between">
              <div>
                <p className="text-[11px] font-medium text-gray-500 uppercase tracking-wider">{kpi.label}</p>
                <p className="text-3xl font-bold text-white mt-2">{kpi.value}</p>
                <div className="flex items-center gap-1 mt-1">
                  {kpi.trend === 'up' ? (
                    <TrendingUp className="h-3.5 w-3.5 text-green-400" />
                  ) : (
                    <TrendingDown className="h-3.5 w-3.5 text-red-400" />
                  )}
                  <span className={clsx('text-[11px]', kpi.trend === 'up' ? 'text-green-400' : 'text-red-400')}>
                    {kpi.change}
                  </span>
                </div>
              </div>
              <div className={clsx('flex h-10 w-10 items-center justify-center rounded-xl bg-gradient-to-br opacity-50', kpi.color)}>
                <kpi.icon className="h-5 w-5 text-white" />
              </div>
            </div>
          </div>
        ))}
      </div>

      {/* Main content grid */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
        {/* Weekly trend chart (2 cols) */}
        <div className="lg:col-span-2 card p-5">
          <h2 className="zone-header mb-4">Weekly Velocity</h2>
          <div className="flex items-end gap-1.5 h-40">
            {weeklyTrend.map((w, i) => (
              <div key={i} className="flex-1 flex flex-col items-center gap-1">
                <div className="w-full flex flex-col items-center gap-0.5 flex-1 justify-end">
                  {/* Rules bar */}
                  <div
                    className="w-3 rounded-t bg-nexus-500/70 transition-all"
                    style={{ height: `${(w.rules / maxRules) * 100}%` }}
                    title={`${w.rules} rules`}
                  />
                  {/* Tests bar */}
                  <div
                    className="w-3 rounded-t bg-cyan-500/70 transition-all"
                    style={{ height: `${(w.tests / maxTests) * 80}%` }}
                    title={`${w.tests} tests`}
                  />
                </div>
                <span className="text-[9px] text-gray-600">{w.week}</span>
              </div>
            ))}
          </div>
          <div className="flex items-center gap-4 mt-3 text-[10px] text-gray-500">
            <span className="flex items-center gap-1"><span className="h-2 w-2 rounded-sm bg-nexus-500/70" /> Rules Extracted</span>
            <span className="flex items-center gap-1"><span className="h-2 w-2 rounded-sm bg-cyan-500/70" /> Tests Generated</span>
          </div>
        </div>

        {/* Engine status (1 col) */}
        <div className="card p-5">
          <div className="flex items-center justify-between mb-4">
            <h2 className="zone-header">Engine Status</h2>
            <span className="flex items-center gap-1.5 text-[10px] text-green-400">
              <span className="h-2 w-2 rounded-full bg-green-400 animate-pulse" />
              {engineStatus.filter((e) => e.status === 'online').length}/{engineStatus.length} Online
            </span>
          </div>
          <div className="space-y-2">
            {engineStatus.map((eng) => (
              <div key={eng.name} className="flex items-center gap-2">
                <span className={clsx(
                  'h-1.5 w-1.5 rounded-full',
                  eng.status === 'online' ? 'bg-green-400' : eng.status === 'warning' ? 'bg-yellow-400 animate-pulse' : 'bg-red-400',
                )} />
                <span className="text-[11px] text-gray-400 flex-1 truncate">{eng.name}</span>
                <ProgressBar
                  value={eng.load}
                  variant={eng.load > 80 ? 'yellow' : eng.load > 50 ? 'nexus' : 'green'}
                  size="sm"
                  className="w-12"
                />
                <span className="text-[9px] text-gray-600 w-6 text-right">{eng.load}%</span>
              </div>
            ))}
          </div>
        </div>
      </div>

      {/* Bottom section: ROI + Top Risks */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        {/* ROI */}
        <div className="card p-5">
          <div className="flex items-center gap-2 mb-4">
            <DollarSign className="h-4 w-4 text-green-400" />
            <h2 className="zone-header">ROI Summary — Q1 2026</h2>
          </div>
          <div className="space-y-2">
            {ROI_METRICS.map((m, i) => (
              <div
                key={i}
                className={clsx(
                  'flex items-center justify-between py-2 px-3 rounded-lg',
                  m.highlight
                    ? 'bg-gradient-to-r from-green-500/10 to-emerald-500/10 ring-1 ring-green-500/20'
                    : 'bg-white/[0.02]',
                )}
              >
                <div className="flex-1">
                  <p className={clsx('text-xs', m.highlight ? 'font-bold text-green-300' : 'text-gray-300')}>{m.label}</p>
                  {m.value && <p className="text-[10px] text-gray-500">{m.value} • {m.period}</p>}
                </div>
                <p className={clsx('text-sm font-bold', m.highlight ? 'text-green-400 text-lg' : 'text-white')}>
                  {m.equivalent}
                </p>
              </div>
            ))}
          </div>
        </div>

        {/* Top Risks */}
        <div className="card p-5">
          <div className="flex items-center gap-2 mb-4">
            <AlertTriangle className="h-4 w-4 text-yellow-400" />
            <h2 className="zone-header">Top Risks</h2>
          </div>
          <div className="space-y-2">
            {TOP_RISKS.map((risk) => (
              <Link
                key={risk.id}
                to={risk.link}
                className="flex items-start gap-3 py-2.5 px-3 rounded-lg bg-white/[0.02] hover:bg-white/[0.04] transition-colors group"
              >
                <span className="text-xs text-gray-600 font-mono mt-0.5">#{risk.id}</span>
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-2 flex-wrap">
                    <span className={SEVERITY_BADGE[risk.severity]}>{risk.severity.toUpperCase()}</span>
                    <span className="text-[10px] text-gray-600">{risk.domain}</span>
                  </div>
                  <p className="text-xs text-gray-300 mt-0.5 truncate">{risk.title}</p>
                  {risk.dueDate && (
                    <p className="text-[10px] text-gray-600 mt-0.5 flex items-center gap-1">
                      <Clock className="h-3 w-3" /> Due: {risk.dueDate}
                    </p>
                  )}
                </div>
                <ArrowUpRight className="h-3.5 w-3.5 text-gray-600 group-hover:text-nexus-400 transition-colors shrink-0 mt-1" />
              </Link>
            ))}
          </div>
        </div>
      </div>
    </div>
  );
}
