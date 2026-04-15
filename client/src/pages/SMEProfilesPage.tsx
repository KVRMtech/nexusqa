// ═══════════════════════════════════════════════════════════════
//  MODULE 3 — SME KNOWLEDGE PROFILES
//  "Bus factor detection + expertise maps"
// ═══════════════════════════════════════════════════════════════

import { useState } from 'react';
import api from '../services/api';
import { useApiData } from '../hooks/useApiData';
import { PageHeader } from '../components';
import type { SMEProfile } from '../types';
import { EmptyState } from '../components/EmptyState';
import {
  UserCircle,
  AlertTriangle,
  Brain,
  Mic,
  Clock,
  ChevronDown,
  ChevronUp,
  Shield,
  Users,
  TrendingUp,
  Mail,
  FileText,
} from 'lucide-react';
import clsx from 'clsx';

// ── Empty fallback (production) ─────────────────────────────
const EMPTY_PROFILES: SMEProfile[] = [];

function ExpertiseBar({ domain, count, maxCount, confidence }: { domain: string; count: number; maxCount: number; confidence: number }) {
  const pct = (count / maxCount) * 100;
  return (
    <div className="group">
      <div className="flex items-center justify-between mb-1">
        <span className="text-xs text-gray-300">{domain}</span>
        <span className="text-[11px] text-gray-500">{count} rules • {confidence}% avg</span>
      </div>
      <div className="h-2 rounded-full bg-white/[0.06] overflow-hidden">
        <div
          className="h-full rounded-full bg-gradient-to-r from-nexus-600 to-nexus-400 transition-all duration-500 group-hover:shadow-[0_0_8px_rgba(99,102,241,0.4)]"
          style={{ width: `${pct}%` }}
        />
      </div>
    </div>
  );
}

export default function SMEProfilesPage() {
  const { data: profiles, isLive } = useApiData(
    () => api.listSMEProfiles('t-1'),
    EMPTY_PROFILES,
  );
  const [expandedId, setExpandedId] = useState<string | null>(null);
  const [sortBy, setSortBy] = useState<'rules' | 'risk' | 'recent'>('rules');

  const totalBusFactors = profiles.reduce(
    (sum, p) => sum + p.bus_factor_risks.filter((r) => r.sole_source).length,
    0,
  );
  const soleSourceRules = profiles.reduce(
    (sum, p) => sum + p.bus_factor_risks.filter((r) => r.sole_source).reduce((s, r) => s + r.rules_count, 0),
    0,
  );

  const sorted = [...profiles].sort((a, b) => {
    if (sortBy === 'rules') return b.total_rules_contributed - a.total_rules_contributed;
    if (sortBy === 'risk') return b.bus_factor_risks.filter((r) => r.sole_source).length - a.bus_factor_risks.filter((r) => r.sole_source).length;
    return new Date(b.last_active).getTime() - new Date(a.last_active).getTime();
  });

  return (
    <div className="space-y-6 animate-fade-in">
      {/* Header */}
      <PageHeader
        title="SME Knowledge Profiles"
        subtitle="Track expertise, detect knowledge gaps, and prevent knowledge loss."
        isLive={isLive}
      />

      {/* Risk Banner */}
      {totalBusFactors > 0 && (
        <div className="card p-4 bg-gradient-to-r from-red-500/10 to-orange-500/10 ring-red-500/20">
          <div className="flex items-start gap-3">
            <AlertTriangle className="h-5 w-5 text-red-400 shrink-0 mt-0.5" />
            <div>
              <p className="text-sm font-semibold text-red-400">
                Bus Factor Alert: {totalBusFactors} knowledge domains at risk
              </p>
              <p className="text-xs text-gray-400 mt-1">
                <span className="text-red-300 font-semibold">{soleSourceRules} critical rules</span> have only one source contributor.
                If these individuals leave, the knowledge has NO backup. Consider scheduling cross-training sessions.
              </p>
              <button className="btn-ghost text-xs text-red-400 mt-2 px-2 py-1">
                <Mail className="h-3 w-3" /> Schedule Cross-Training
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Summary Row */}
      <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
        <div className="stat-card">
          <div className="flex items-center gap-3">
            <div className="flex h-10 w-10 items-center justify-center rounded-lg bg-nexus-500/15">
              <Users className="h-5 w-5 text-nexus-400" />
            </div>
            <div>
              <p className="text-2xl font-bold text-white">{profiles.length}</p>
              <p className="text-xs text-gray-500">Active SMEs</p>
            </div>
          </div>
        </div>
        <div className="stat-card">
          <div className="flex items-center gap-3">
            <div className="flex h-10 w-10 items-center justify-center rounded-lg bg-purple-500/15">
              <Brain className="h-5 w-5 text-purple-400" />
            </div>
            <div>
              <p className="text-2xl font-bold text-white">
                {profiles.reduce((s, p) => s + p.total_rules_contributed, 0)}
              </p>
              <p className="text-xs text-gray-500">Total Rules</p>
            </div>
          </div>
        </div>
        <div className="stat-card">
          <div className="flex items-center gap-3">
            <div className="flex h-10 w-10 items-center justify-center rounded-lg bg-red-500/15">
              <AlertTriangle className="h-5 w-5 text-red-400" />
            </div>
            <div>
              <p className="text-2xl font-bold text-red-400">{totalBusFactors}</p>
              <p className="text-xs text-gray-500">Bus Factor Risks</p>
            </div>
          </div>
        </div>
        <div className="stat-card">
          <div className="flex items-center gap-3">
            <div className="flex h-10 w-10 items-center justify-center rounded-lg bg-green-500/15">
              <Mic className="h-5 w-5 text-green-400" />
            </div>
            <div>
              <p className="text-2xl font-bold text-white">
                {profiles.filter((p) => p.voice_enrolled).length}/{profiles.length}
              </p>
              <p className="text-xs text-gray-500">Voice Enrolled</p>
            </div>
          </div>
        </div>
      </div>

      {/* Sort controls */}
      <div className="flex gap-2">
        {[
          { key: 'rules' as const, label: 'Most Knowledge' },
          { key: 'risk' as const, label: 'Highest Risk' },
          { key: 'recent' as const, label: 'Recently Active' },
        ].map((s) => (
          <button
            key={s.key}
            onClick={() => setSortBy(s.key)}
            className={clsx(
              'px-3 py-1.5 rounded-lg text-xs font-medium transition-colors',
              sortBy === s.key ? 'bg-nexus-500/15 text-nexus-400' : 'text-gray-500 hover:text-gray-300 hover:bg-white/[0.04]',
            )}
          >
            {s.label}
          </button>
        ))}
      </div>

      {/* Profile Cards */}
      <div className="space-y-3">
        {profiles.length === 0 && (
          <EmptyState title="No SME Profiles" description="SME profiles appear as knowledge transfer sessions are recorded and processed." />
        )}
        {sorted.map((profile) => {
          const isExpanded = expandedId === profile.speaker_id;
          const maxRules = Math.max(...profile.expertise_areas.map((a) => a.rules_count));
          const soleSourceCount = profile.bus_factor_risks.filter((r) => r.sole_source).length;

          return (
            <div key={profile.speaker_id} className="card overflow-hidden">
              {/* Header row */}
              <button
                onClick={() => setExpandedId(isExpanded ? null : profile.speaker_id)}
                className="flex w-full items-center gap-4 p-5 text-left hover:bg-white/[0.02] transition-colors"
              >
                <div className="flex h-12 w-12 items-center justify-center rounded-full bg-gradient-to-br from-nexus-500 to-purple-600 text-lg font-bold text-white shrink-0">
                  {profile.name.charAt(0)}
                </div>

                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-2">
                    <p className="text-sm font-semibold text-white">{profile.name}</p>
                    {profile.voice_enrolled && <span className="badge-green text-[10px]"><Mic className="h-2.5 w-2.5" /> Voice</span>}
                    {soleSourceCount > 0 && (
                      <span className="badge-red text-[10px]"><AlertTriangle className="h-2.5 w-2.5" /> {soleSourceCount} risk{soleSourceCount > 1 ? 's' : ''}</span>
                    )}
                  </div>
                  <p className="text-xs text-gray-500">{profile.role} — {profile.department}</p>
                </div>

                <div className="hidden sm:flex gap-8 text-center shrink-0">
                  <div>
                    <p className="text-lg font-bold text-white">{profile.sessions_count}</p>
                    <p className="text-[10px] text-gray-500">Sessions</p>
                  </div>
                  <div>
                    <p className="text-lg font-bold text-nexus-400">{profile.total_rules_contributed}</p>
                    <p className="text-[10px] text-gray-500">Rules</p>
                  </div>
                  <div>
                    <p className="text-lg font-bold text-white">{profile.expertise_areas.length}</p>
                    <p className="text-[10px] text-gray-500">Domains</p>
                  </div>
                </div>

                {isExpanded ? <ChevronUp className="h-5 w-5 text-gray-500 shrink-0" /> : <ChevronDown className="h-5 w-5 text-gray-500 shrink-0" />}
              </button>

              {/* Expanded Detail */}
              {isExpanded && (
                <div className="border-t border-white/[0.06] animate-slide-in">
                  <div className="grid grid-cols-1 lg:grid-cols-3 gap-0 lg:divide-x lg:divide-white/[0.06]">
                    {/* Expertise Map */}
                    <div className="p-5 lg:col-span-1">
                      <h4 className="text-xs font-bold uppercase tracking-wider text-gray-500 mb-3">Expertise Map</h4>
                      <div className="space-y-3">
                        {profile.expertise_areas.map((area) => (
                          <ExpertiseBar
                            key={area.domain}
                            domain={area.domain}
                            count={area.rules_count}
                            maxCount={maxRules}
                            confidence={area.confidence_avg}
                          />
                        ))}
                      </div>
                    </div>

                    {/* Bus Factor Risks */}
                    <div className="p-5 lg:col-span-1 border-t lg:border-t-0 border-white/[0.06]">
                      <h4 className="text-xs font-bold uppercase tracking-wider text-gray-500 mb-3">
                        <AlertTriangle className="inline h-3 w-3 text-red-400 mr-1" />
                        Bus Factor Analysis
                      </h4>
                      {profile.bus_factor_risks.length === 0 ? (
                        <div className="flex items-center gap-2 text-xs text-green-400">
                          <Shield className="h-4 w-4" />
                          No single points of failure detected
                        </div>
                      ) : (
                        <div className="space-y-3">
                          {profile.bus_factor_risks.map((risk) => (
                            <div
                              key={risk.domain}
                              className={clsx(
                                'rounded-lg p-3',
                                risk.sole_source ? 'bg-red-500/10 ring-1 ring-red-500/20' : 'bg-white/[0.03]',
                              )}
                            >
                              <div className="flex items-center justify-between">
                                <span className="text-xs font-medium text-gray-200">{risk.domain}</span>
                                {risk.sole_source ? (
                                  <span className="badge-red text-[10px]">SOLE SOURCE</span>
                                ) : (
                                  <span className="badge-green text-[10px]">BACKED UP</span>
                                )}
                              </div>
                              <p className="text-[11px] text-gray-500 mt-1">
                                {risk.rules_count} rules • {risk.sole_source
                                  ? `No backup. If ${profile.name.split(' ')[0]} leaves, these rules have ZERO redundancy.`
                                  : `Nearest backup: ${risk.nearest_backup}`
                                }
                              </p>
                            </div>
                          ))}
                        </div>
                      )}
                    </div>

                    {/* Knowledge Timeline */}
                    <div className="p-5 border-t lg:border-t-0 border-white/[0.06]">
                      <h4 className="text-xs font-bold uppercase tracking-wider text-gray-500 mb-3">Knowledge Timeline</h4>
                      <div className="space-y-3">
                        {profile.knowledge_timeline.map((entry, idx) => (
                          <div key={idx} className="flex items-center gap-3">
                            <div className="flex flex-col items-center">
                              <div className="h-2 w-2 rounded-full bg-nexus-500" />
                              {idx < profile.knowledge_timeline.length - 1 && (
                                <div className="w-px h-6 bg-white/10" />
                              )}
                            </div>
                            <div className="flex-1 min-w-0">
                              <p className="text-xs font-medium text-gray-300 truncate">{entry.session_title}</p>
                              <div className="flex items-center gap-2 text-[10px] text-gray-500">
                                <span>{new Date(entry.date).toLocaleDateString('en-US', { month: 'short', day: 'numeric' })}</span>
                                <span>•</span>
                                <span className="text-nexus-400 font-semibold">{entry.rules_extracted} rules</span>
                              </div>
                            </div>
                          </div>
                        ))}
                      </div>

                      <div className="flex gap-2 mt-4">
                        <button className="btn-ghost text-[11px] py-1 px-2">
                          <FileText className="h-3 w-3" /> Export Report
                        </button>
                        <button className="btn-ghost text-[11px] py-1 px-2">
                          <Mail className="h-3 w-3" /> Request Training
                        </button>
                      </div>
                    </div>
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
