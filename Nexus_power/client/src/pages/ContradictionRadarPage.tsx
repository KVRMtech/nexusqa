// ═══════════════════════════════════════════════════════════════
//  MODULE 5 — CONTRADICTION RADAR
//  "Automatic contradiction detection with side-by-side proof"
// ═══════════════════════════════════════════════════════════════

import { useState } from 'react';
import api from '../services/api';
import { useApiData } from '../hooks/useApiData';
import { PageHeader } from '../components';
import type { Contradiction, ContradictionSeverity } from '../types';
import { EmptyState } from '../components/EmptyState';
import {
  AlertTriangle,
  CheckCircle2,
  ChevronDown,
  ChevronUp,
  Play,
  Eye,
  Mail,
  MessageSquare,
  Crosshair,
  FlaskConical,
  Link2,
  XCircle,
  Shield,
  Clock,
} from 'lucide-react';
import clsx from 'clsx';

// ── Empty fallback (production) ─────────────────────────────

const EMPTY_CONTRADICTIONS: Contradiction[] = [];

const SEVERITY_ORDER: ContradictionSeverity[] = ['critical', 'high', 'medium', 'low'];
const SEVERITY_COLORS: Record<ContradictionSeverity, string> = {
  critical: 'badge-red',
  high: 'badge-yellow',
  medium: 'badge-blue',
  low: 'badge-gray',
};
const SEVERITY_RING: Record<ContradictionSeverity, string> = {
  critical: 'ring-red-500/30',
  high: 'ring-yellow-500/20',
  medium: 'ring-blue-500/20',
  low: 'ring-white/10',
};

export default function ContradictionRadarPage() {
  const { data: contradictions, isLive } = useApiData(
    () => api.listContradictions(),
    EMPTY_CONTRADICTIONS,
  );
  const [expandedId, setExpandedId] = useState<string | null>(null);
  const [filterSeverity, setFilterSeverity] = useState<ContradictionSeverity | 'all'>('all');
  const [filterStatus, setFilterStatus] = useState<string>('all');

  const counts = {
    critical: contradictions.filter((c) => c.severity === 'critical' && c.status !== 'resolved').length,
    high: contradictions.filter((c) => c.severity === 'high' && c.status !== 'resolved').length,
    medium: contradictions.filter((c) => c.severity === 'medium' && c.status !== 'resolved').length,
    low: contradictions.filter((c) => c.severity === 'low' && c.status !== 'resolved').length,
  };
  const totalOpen = counts.critical + counts.high + counts.medium + counts.low;

  const filtered = contradictions.filter((c) => {
    if (filterSeverity !== 'all' && c.severity !== filterSeverity) return false;
    if (filterStatus !== 'all' && c.status !== filterStatus) return false;
    return true;
  });

  return (
    <div className="space-y-6 animate-fade-in">
      {/* Header */}
      <PageHeader
        title="Contradiction Radar"
        subtitle={`${totalOpen} active conflicts detected across knowledge base.`}
        isLive={isLive}
      />

      {/* Severity summary bar */}
      <div className="flex items-center gap-3 flex-wrap">
        <button
          onClick={() => setFilterSeverity(filterSeverity === 'critical' ? 'all' : 'critical')}
          className={clsx('stat-card py-3 px-4 cursor-pointer hover:ring-red-500/30 transition-all', filterSeverity === 'critical' && 'ring-red-500/40')}
        >
          <div className="flex items-center gap-2">
            <span className="h-3 w-3 rounded-full bg-red-500 animate-pulse" />
            <span className="text-lg font-bold text-red-400">{counts.critical}</span>
            <span className="text-xs text-slate-400">Critical</span>
          </div>
        </button>
        <button
          onClick={() => setFilterSeverity(filterSeverity === 'high' ? 'all' : 'high')}
          className={clsx('stat-card py-3 px-4 cursor-pointer hover:ring-yellow-500/20 transition-all', filterSeverity === 'high' && 'ring-yellow-500/30')}
        >
          <div className="flex items-center gap-2">
            <span className="h-3 w-3 rounded-full bg-yellow-500" />
            <span className="text-lg font-bold text-yellow-400">{counts.high}</span>
            <span className="text-xs text-slate-400">High</span>
          </div>
        </button>
        <button
          onClick={() => setFilterSeverity(filterSeverity === 'medium' ? 'all' : 'medium')}
          className={clsx('stat-card py-3 px-4 cursor-pointer hover:ring-blue-500/20 transition-all', filterSeverity === 'medium' && 'ring-blue-500/30')}
        >
          <div className="flex items-center gap-2">
            <span className="h-3 w-3 rounded-full bg-blue-500" />
            <span className="text-lg font-bold text-blue-400">{counts.medium}</span>
            <span className="text-xs text-slate-400">Medium</span>
          </div>
        </button>
        <button
          onClick={() => setFilterSeverity(filterSeverity === 'low' ? 'all' : 'low')}
          className={clsx('stat-card py-3 px-4 cursor-pointer hover:ring-white/10 transition-all', filterSeverity === 'low' && 'ring-white/20')}
        >
          <div className="flex items-center gap-2">
            <span className="h-3 w-3 rounded-full bg-gray-500" />
            <span className="text-lg font-bold text-slate-8000">{counts.low}</span>
            <span className="text-xs text-slate-400">Low</span>
          </div>
        </button>

        <div className="ml-auto flex gap-1">
          {['all', 'open', 'escalated', 'resolved'].map((s) => (
            <button
              key={s}
              onClick={() => setFilterStatus(s)}
              className={clsx(
                'px-2.5 py-1 rounded-md text-[11px] font-medium transition-colors',
                filterStatus === s ? 'bg-white/[0.08] text-[#0a2540]' : 'text-slate-400 hover:text-slate-600',
              )}
            >
              {s.charAt(0).toUpperCase() + s.slice(1)}
            </button>
          ))}
        </div>
      </div>

      {/* Contradiction cards */}
      <div className="space-y-3">
        {contradictions.length === 0 && (
          <EmptyState title="No Contradictions Detected" description="Contradictions will appear here when conflicting statements are detected across sessions." />
        )}
        {filtered.map((ctr) => {
          const isExpanded = expandedId === ctr.contradiction_id;
          return (
            <div
              key={ctr.contradiction_id}
              className={clsx('card overflow-hidden transition-all', SEVERITY_RING[ctr.severity])}
            >
              {/* Header */}
              <button
                onClick={() => setExpandedId(isExpanded ? null : ctr.contradiction_id)}
                className="flex w-full items-center gap-4 p-5 text-left hover:bg-white/[0.02] transition-colors"
              >
                {ctr.status === 'resolved' ? (
                  <CheckCircle2 className="h-5 w-5 text-green-400 shrink-0" />
                ) : ctr.severity === 'critical' ? (
                  <AlertTriangle className="h-5 w-5 text-red-400 shrink-0 animate-pulse" />
                ) : (
                  <AlertTriangle className="h-5 w-5 text-yellow-400 shrink-0" />
                )}

                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-2 flex-wrap">
                    <span className={SEVERITY_COLORS[ctr.severity]}>
                      {ctr.severity.toUpperCase()}
                    </span>
                    {ctr.status === 'resolved' && <span className="badge-green">RESOLVED</span>}
                    {ctr.status === 'escalated' && <span className="badge-yellow">ESCALATED</span>}
                    <h3 className="text-sm font-medium text-[#0a2540]">{ctr.title}</h3>
                  </div>
                  <div className="mt-1 flex items-center gap-4 text-xs text-slate-400">
                    <span>{ctr.claim_a.speaker} vs {ctr.claim_b.speaker}</span>
                    <span className="flex items-center gap-1">
                      <FlaskConical className="h-3 w-3" /> {ctr.impact.affected_tests} tests affected
                    </span>
                    <span className="flex items-center gap-1">
                      <Link2 className="h-3 w-3" /> {ctr.impact.affected_tickets} tickets
                    </span>
                  </div>
                </div>

                {isExpanded ? <ChevronUp className="h-5 w-5 text-slate-400 shrink-0" /> : <ChevronDown className="h-5 w-5 text-slate-400 shrink-0" />}
              </button>

              {/* Expanded */}
              {isExpanded && (
                <div className="border-t border-gray-200 animate-slide-in">
                  {/* Side-by-side claims */}
                  <div className="grid grid-cols-1 md:grid-cols-2 gap-0 md:divide-x md:divide-white/[0.06]">
                    {/* Claim A */}
                    <div className="p-5">
                      <div className="flex items-center gap-2 mb-3">
                        <div className="h-6 w-6 rounded-full bg-gradient-to-br from-nexus-500 to-purple-600 flex items-center justify-center text-[10px] text-[#0a2540] font-bold">
                          {ctr.claim_a.speaker.charAt(0)}
                        </div>
                        <div>
                          <p className="text-xs font-medium text-slate-700">Claim A — {ctr.claim_a.speaker}</p>
                          <p className="text-[10px] text-slate-400">{ctr.claim_a.session_title} • {ctr.claim_a.date}</p>
                        </div>
                      </div>
                      <blockquote className="rounded-lg bg-white/[0.03] p-3 border-l-2 border-nexus-500 text-sm text-slate-600 italic">
                        "{ctr.claim_a.statement}"
                      </blockquote>
                      <div className="mt-2 flex items-center gap-3 text-[11px] text-slate-400">
                        <span>Confidence: <span className="text-slate-600 font-semibold">{ctr.claim_a.confidence}%</span></span>
                        <button className="flex items-center gap-1 text-nexus-400 hover:text-nexus-300">
                          <Play className="h-3 w-3" /> Play Audio
                        </button>
                      </div>
                    </div>

                    {/* Claim B */}
                    <div className="p-5 border-t md:border-t-0 border-gray-200">
                      <div className="flex items-center gap-2 mb-3">
                        <div className="h-6 w-6 rounded-full bg-gradient-to-br from-orange-500 to-red-600 flex items-center justify-center text-[10px] text-[#0a2540] font-bold">
                          {ctr.claim_b.speaker.charAt(0)}
                        </div>
                        <div>
                          <p className="text-xs font-medium text-slate-700">Claim B — {ctr.claim_b.speaker}</p>
                          <p className="text-[10px] text-slate-400">{ctr.claim_b.session_title} • {ctr.claim_b.date}</p>
                        </div>
                      </div>
                      <blockquote className="rounded-lg bg-white/[0.03] p-3 border-l-2 border-orange-500 text-sm text-slate-600 italic">
                        "{ctr.claim_b.statement}"
                      </blockquote>
                      <div className="mt-2 flex items-center gap-3 text-[11px] text-slate-400">
                        <span>Confidence: <span className="text-slate-600 font-semibold">{ctr.claim_b.confidence}%</span></span>
                        <button className="flex items-center gap-1 text-nexus-400 hover:text-nexus-300">
                          <Play className="h-3 w-3" /> Play Audio
                        </button>
                      </div>
                    </div>
                  </div>

                  {/* AI Analysis */}
                  <div className="p-5 bg-nexus-500/5 border-t border-gray-200">
                    <div className="flex items-start gap-3">
                      <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-nexus-500/15 shrink-0">
                        <MessageSquare className="h-4 w-4 text-nexus-400" />
                      </div>
                      <div>
                        <p className="text-xs font-semibold text-nexus-400 mb-1">AI Analysis</p>
                        <p className="text-sm text-slate-600 leading-relaxed">{ctr.ai_analysis}</p>
                        {ctr.ai_recommendation && (
                          <p className="text-sm text-nexus-300 mt-2 font-medium">
                            Recommendation: {ctr.ai_recommendation}
                          </p>
                        )}
                      </div>
                    </div>

                    {/* Impact */}
                    <div className="mt-4 flex items-center gap-4 text-xs text-slate-400">
                      <span className="font-medium text-slate-8000">Impact:</span>
                      <span>{ctr.impact.affected_rules} rules</span>
                      <span>{ctr.impact.affected_tests} tests</span>
                      <span>{ctr.impact.affected_tickets} tickets</span>
                      <span className="text-slate-8000">Domains: {ctr.impact.domains.join(', ')}</span>
                    </div>

                    {/* Resolution section */}
                    {ctr.status === 'resolved' ? (
                      <div className="mt-4 rounded-lg bg-green-500/10 p-3 ring-1 ring-green-500/20">
                        <div className="flex items-center gap-2 text-xs text-green-400 mb-1">
                          <CheckCircle2 className="h-3.5 w-3.5" />
                          <span className="font-semibold">Resolved by {ctr.resolved_by} on {new Date(ctr.resolved_at!).toLocaleDateString()}</span>
                        </div>
                        <p className="text-xs text-slate-8000">{ctr.resolution_note}</p>
                      </div>
                    ) : (
                      <div className="mt-4 flex gap-2">
                        <button className="btn-primary text-xs py-1.5 px-3">
                          <CheckCircle2 className="h-3.5 w-3.5" /> Accept Claim A
                        </button>
                        <button className="btn-secondary text-xs py-1.5 px-3">
                          <CheckCircle2 className="h-3.5 w-3.5" /> Accept Claim B
                        </button>
                        <button className="btn-ghost text-xs py-1.5 px-3 text-yellow-400">
                          <Mail className="h-3.5 w-3.5" /> Escalate to Both
                        </button>
                        <button className="btn-ghost text-xs py-1.5 px-3">
                          <Eye className="h-3.5 w-3.5" /> View Full Context
                        </button>
                      </div>
                    )}
                  </div>
                </div>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}
