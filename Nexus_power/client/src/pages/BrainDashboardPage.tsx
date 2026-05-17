// ═══════════════════════════════════════════════════════════════
//  NEXUS AI ENGINE FACTORY — Brain Dashboard (Tier & Intelligence)
// ═══════════════════════════════════════════════════════════════
import { useState, useEffect, useCallback } from 'react';
import { useAuth } from '../contexts/AuthContext';
import api from '../services/api';
import {
  Brain,
  Layers,
  Shield,
  CheckCircle2,
  XCircle,
  AlertTriangle,
  Activity,
  Cloud,
  Server,
  Wifi,
  Send,
  RefreshCw,
  ChevronDown,
  ChevronRight,
  Zap,
  BarChart3,
  MessageSquare,
} from 'lucide-react';
import clsx from 'clsx';

// ── Types ──────────────────────────────────────────────────────

interface TierStatus {
  overall_mode: string;
  total_engines: number;
  cloud_engines: string[];
  hybrid_engines: string[];
  onprem_engines: string[];
  engines: Record<string, EngineTierInfo>;
  recommended_tiers: Record<string, RecommendedTier>;
}

interface EngineTierInfo {
  active_tier: string;
  provider: string;
  all_tiers: Record<string, string>;
}

interface RecommendedTier {
  tier1: string;
  tier2: string;
  tier3: string;
}

interface QualityResult {
  session_id: string;
  overall_score: number;
  level: string;
  passed: boolean;
  rule_completeness: number;
  test_coverage: number;
  consistency: number;
  confidence_avg: number;
  pii_safety: number;
  gaps: string[];
  warnings: string[];
}

interface SessionAnalysis {
  session_id: string;
  completeness: number;
  engines_completed: string[];
  gaps: string[];
  recommended_next: string[];
}

interface BrainAnswer {
  answer: string;
  confidence: number;
}

// ── Helpers ────────────────────────────────────────────────────

const modeIcon = (mode: string) => {
  if (mode.includes('cloud')) return <Cloud className="h-5 w-5 text-blue-400" />;
  if (mode.includes('on-prem') || mode.includes('local')) return <Server className="h-5 w-5 text-green-400" />;
  return <Wifi className="h-5 w-5 text-yellow-400" />;
};

const modeBadge = (mode: string) => {
  const colors: Record<string, string> = {
    'full-cloud': 'bg-blue-500/20 text-blue-300 border-blue-500/30',
    'full-on-prem': 'bg-green-500/20 text-green-300 border-green-500/30',
    'hybrid': 'bg-yellow-500/20 text-yellow-300 border-yellow-500/30',
    'not-configured': 'bg-gray-500/20 text-slate-8000 border-gray-500/30',
  };
  return colors[mode] || colors['hybrid'];
};

const levelColor = (level: string) => {
  const map: Record<string, string> = {
    excellent: 'text-green-400',
    good: 'text-blue-400',
    acceptable: 'text-yellow-400',
    needs_review: 'text-orange-400',
    poor: 'text-red-400',
  };
  return map[level] || 'text-slate-8000';
};

const scoreBar = (score: number, label: string) => (
  <div className="flex items-center gap-3">
    <span className="text-xs text-slate-8000 w-32 shrink-0">{label}</span>
    <div className="flex-1 h-2 rounded-full bg-gray-100 overflow-hidden">
      <div
        className={clsx(
          'h-full rounded-full transition-all duration-500',
          score >= 0.75 ? 'bg-green-500' : score >= 0.6 ? 'bg-yellow-500' : 'bg-red-500',
        )}
        style={{ width: `${Math.round(score * 100)}%` }}
      />
    </div>
    <span className="text-xs font-mono text-slate-600 w-10 text-right">
      {Math.round(score * 100)}%
    </span>
  </div>
);

// ── Engine Tier Card ───────────────────────────────────────────

function EngineTierCard({
  name,
  info,
  recommended,
}: {
  name: string;
  info: EngineTierInfo;
  recommended?: RecommendedTier;
}) {
  const [expanded, setExpanded] = useState(false);
  const tierLabels: Record<string, string> = { tier1: 'Tier 1 (Cloud Best)', tier2: 'Tier 2 (Fallback)', tier3: 'Tier 3 (Local)' };

  return (
    <div className="rounded-lg bg-white shadow-sm border border-gray-200 overflow-hidden">
      <button
        onClick={() => setExpanded(!expanded)}
        className="w-full flex items-center justify-between px-4 py-3 hover:bg-white/[0.02] transition-colors"
      >
        <div className="flex items-center gap-3">
          <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-nexus-500/15">
            <Zap className="h-4 w-4 text-nexus-400" />
          </div>
          <div className="text-left">
            <p className="text-sm font-medium text-slate-700 capitalize">{name}</p>
            <p className="text-xs text-slate-400">
              Active: <span className="text-nexus-400">{info.provider || 'not configured'}</span>
              {info.active_tier && (
                <span className="ml-2 text-slate-8000">({info.active_tier})</span>
              )}
            </p>
          </div>
        </div>
        {expanded ? (
          <ChevronDown className="h-4 w-4 text-slate-400" />
        ) : (
          <ChevronRight className="h-4 w-4 text-slate-400" />
        )}
      </button>

      {expanded && (
        <div className="px-4 pb-3 space-y-2 border-t border-gray-200">
          <p className="text-[10px] font-bold text-slate-400 uppercase tracking-wider pt-2">
            Tier Configuration
          </p>
          {/* Configured tiers */}
          {info.all_tiers && Object.entries(info.all_tiers).map(([tier, provider]) => (
            <div key={tier} className="flex items-center justify-between text-xs">
              <span className="text-slate-8000">{tierLabels[tier] || tier}</span>
              <span className={clsx(
                'font-mono',
                tier === info.active_tier ? 'text-nexus-400 font-semibold' : 'text-slate-400',
              )}>
                {provider}
              </span>
            </div>
          ))}
          {/* Recommended */}
          {recommended && (
            <>
              <p className="text-[10px] font-bold text-slate-400 uppercase tracking-wider pt-2 mt-2 border-t border-gray-200">
                Recommended
              </p>
              {Object.entries(recommended).map(([tier, provider]) => (
                <div key={tier} className="flex items-center justify-between text-xs">
                  <span className="text-slate-400">{tierLabels[tier] || tier}</span>
                  <span className="text-slate-8000 font-mono">{provider as string}</span>
                </div>
              ))}
            </>
          )}
        </div>
      )}
    </div>
  );
}

// ── Main Page ──────────────────────────────────────────────────

export default function BrainDashboardPage() {
  const { user } = useAuth();
  const tenantId = user?.tenant_id || 'default';

  // State
  const [tiers, setTiers] = useState<TierStatus | null>(null);
  const [sessions, setSessions] = useState<{ session_id: string; completeness: number; engines_completed: string[] }[]>([]);
  const [selectedSession, setSelectedSession] = useState<string>('');
  const [sessionAnalysis, setSessionAnalysis] = useState<SessionAnalysis | null>(null);
  const [qualityResult, setQualityResult] = useState<QualityResult | null>(null);
  const [question, setQuestion] = useState('');
  const [brainAnswer, setBrainAnswer] = useState<BrainAnswer | null>(null);
  const [loading, setLoading] = useState({ tiers: false, sessions: false, quality: false, ask: false, analysis: false });
  const [error, setError] = useState<string | null>(null);

  // ── Fetch tier status ──────────────────────────────────
  const fetchTiers = useCallback(async () => {
    setLoading((l) => ({ ...l, tiers: true }));
    try {
      const data = await api.brainGetTiers();
      setTiers(data);
      setError(null);
    } catch {
      setError('Failed to fetch tier status — is the Brain Engine running?');
    } finally {
      setLoading((l) => ({ ...l, tiers: false }));
    }
  }, []);

  // ── Fetch sessions ─────────────────────────────────────
  const fetchSessions = useCallback(async () => {
    setLoading((l) => ({ ...l, sessions: true }));
    try {
      const data = await api.brainListSessions();
      setSessions(data.sessions || []);
    } catch {
      // Brain may not have sessions yet
    } finally {
      setLoading((l) => ({ ...l, sessions: false }));
    }
  }, []);

  // ── Session analysis ───────────────────────────────────
  const analyzeSession = useCallback(async (sid: string) => {
    if (!sid) return;
    setLoading((l) => ({ ...l, analysis: true }));
    try {
      const data = await api.brainSessionAnalyze(sid);
      setSessionAnalysis(data);
    } catch {
      setSessionAnalysis(null);
    } finally {
      setLoading((l) => ({ ...l, analysis: false }));
    }
  }, []);

  // ── Quality gate ───────────────────────────────────────
  const runQualityGate = useCallback(async () => {
    if (!selectedSession) return;
    setLoading((l) => ({ ...l, quality: true }));
    try {
      const data = await api.brainQualityGate({
        tenant_id: tenantId,
        session_id: selectedSession,
      });
      setQualityResult(data);
    } catch {
      setQualityResult(null);
    } finally {
      setLoading((l) => ({ ...l, quality: false }));
    }
  }, [selectedSession, tenantId]);

  // ── Ask Brain ──────────────────────────────────────────
  const askBrain = useCallback(async () => {
    if (!question.trim()) return;
    setLoading((l) => ({ ...l, ask: true }));
    try {
      const data = await api.brainAsk({
        tenant_id: tenantId,
        question: question.trim(),
        session_id: selectedSession || undefined,
      });
      setBrainAnswer(data);
    } catch {
      setBrainAnswer({ answer: 'Brain is unavailable. Make sure the Brain Engine is running.', confidence: 0 });
    } finally {
      setLoading((l) => ({ ...l, ask: false }));
    }
  }, [question, tenantId, selectedSession]);

  useEffect(() => {
    fetchTiers();
    fetchSessions();
  }, [fetchTiers, fetchSessions]);

  useEffect(() => {
    if (selectedSession) analyzeSession(selectedSession);
  }, [selectedSession, analyzeSession]);

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-3">
          <div className="flex h-10 w-10 items-center justify-center rounded-xl bg-gradient-to-br from-nexus-500 to-purple-600 shadow-lg shadow-nexus-500/25">
            <Brain className="h-6 w-6 text-[#0a2540]" />
          </div>
          <div>
            <h1 className="text-xl font-bold text-[#0a2540]">Brain Dashboard</h1>
            <p className="text-xs text-slate-8000">Intelligent Coordinator — Tiers, Quality Gates & Session Intelligence</p>
          </div>
        </div>
        <button
          onClick={() => { fetchTiers(); fetchSessions(); }}
          disabled={loading.tiers}
          className="flex items-center gap-2 rounded-lg bg-nexus-500/15 px-4 py-2 text-sm font-medium text-nexus-400 hover:bg-nexus-500/25 transition-colors disabled:opacity-50"
        >
          <RefreshCw className={clsx('h-4 w-4', loading.tiers && 'animate-spin')} />
          Refresh
        </button>
      </div>

      {error && (
        <div className="rounded-lg bg-red-500/10 border border-red-500/20 px-4 py-3 text-sm text-red-300">
          <AlertTriangle className="inline h-4 w-4 mr-2" />{error}
        </div>
      )}

      {/* ═══ Section 1: Multi-Tier Provider Status ═══ */}
      <section>
        <div className="flex items-center gap-2 mb-4">
          <Layers className="h-5 w-5 text-nexus-400" />
          <h2 className="text-lg font-semibold text-[#0a2540]">Multi-Tier Provider System</h2>
        </div>

        {tiers ? (
          <>
            {/* Summary cards */}
            <div className="grid grid-cols-1 md:grid-cols-4 gap-4 mb-4">
              <div className="rounded-lg bg-white shadow-sm border border-gray-200 p-4">
                <div className="flex items-center gap-2 mb-1">
                  {modeIcon(tiers.overall_mode)}
                  <span className="text-sm font-medium text-slate-700">Deployment Mode</span>
                </div>
                <span className={clsx('inline-block mt-1 px-2 py-0.5 rounded text-xs font-semibold border', modeBadge(tiers.overall_mode))}>
                  {tiers.overall_mode}
                </span>
              </div>
              <div className="rounded-lg bg-white shadow-sm border border-gray-200 p-4">
                <div className="flex items-center gap-2 mb-1">
                  <Activity className="h-5 w-5 text-slate-8000" />
                  <span className="text-sm font-medium text-slate-700">Total Engines</span>
                </div>
                <p className="text-2xl font-bold text-[#0a2540] mt-1">{tiers.total_engines}</p>
              </div>
              <div className="rounded-lg bg-white shadow-sm border border-gray-200 p-4">
                <div className="flex items-center gap-2 mb-1">
                  <Cloud className="h-5 w-5 text-blue-400" />
                  <span className="text-sm font-medium text-slate-700">Cloud Tier</span>
                </div>
                <p className="text-2xl font-bold text-blue-400 mt-1">{tiers.cloud_engines.length}</p>
                <p className="text-[10px] text-slate-400 mt-0.5">{tiers.cloud_engines.join(', ') || 'none'}</p>
              </div>
              <div className="rounded-lg bg-white shadow-sm border border-gray-200 p-4">
                <div className="flex items-center gap-2 mb-1">
                  <Server className="h-5 w-5 text-green-400" />
                  <span className="text-sm font-medium text-slate-700">On-Prem Tier</span>
                </div>
                <p className="text-2xl font-bold text-green-400 mt-1">{tiers.onprem_engines.length}</p>
                <p className="text-[10px] text-slate-400 mt-0.5">{tiers.onprem_engines.join(', ') || 'none'}</p>
              </div>
            </div>

            {/* Flow diagram: How Tiers Work */}
            <div className="rounded-lg bg-gray-50 border border-gray-200 p-4 mb-4">
              <p className="text-xs font-bold text-slate-400 uppercase tracking-wider mb-3">Pipeline Tier Failover Flow</p>
              <div className="flex items-center justify-center gap-2 text-xs flex-wrap">
                <div className="flex items-center gap-1.5 rounded-lg bg-blue-500/10 border border-blue-500/20 px-3 py-2">
                  <Cloud className="h-3.5 w-3.5 text-blue-400" />
                  <span className="text-blue-300 font-medium">Tier 1: Cloud Best</span>
                  <span className="text-blue-500/60 text-[10px]">(Claude, GPT-5, Gemini)</span>
                </div>
                <span className="text-slate-8000">→ failover →</span>
                <div className="flex items-center gap-1.5 rounded-lg bg-yellow-500/10 border border-yellow-500/20 px-3 py-2">
                  <Wifi className="h-3.5 w-3.5 text-yellow-400" />
                  <span className="text-yellow-300 font-medium">Tier 2: Fallback</span>
                  <span className="text-yellow-500/60 text-[10px]">(OpenAI, Azure)</span>
                </div>
                <span className="text-slate-8000">→ failover →</span>
                <div className="flex items-center gap-1.5 rounded-lg bg-green-500/10 border border-green-500/20 px-3 py-2">
                  <Server className="h-3.5 w-3.5 text-green-400" />
                  <span className="text-green-300 font-medium">Tier 3: Local On-Prem</span>
                  <span className="text-green-500/60 text-[10px]">(Ollama, Whisper)</span>
                </div>
              </div>
              <p className="text-center text-[10px] text-slate-400 mt-2">
                LLM engines (Brain, Heart, Eyes, Hands, Mouth, Spine) use TieredLLMRouter with auto-failover.
                Non-LLM engines (Ears, Shield, Backbone, Legs, Nerves) use specialised tooling.
                Configure via environment variables (see .env.tiers.example).
              </p>
            </div>

            {/* Per-engine tier cards */}
            <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-3">
              {Object.entries(tiers.engines).map(([name, info]) => (
                <EngineTierCard
                  key={name}
                  name={name}
                  info={info as EngineTierInfo}
                  recommended={tiers.recommended_tiers[name] as unknown as RecommendedTier}
                />
              ))}
            </div>
          </>
        ) : (
          <div className="rounded-lg bg-gray-50 border border-gray-200 p-8 text-center">
            <Layers className="h-8 w-8 text-slate-8000 mx-auto mb-2" />
            <p className="text-sm text-slate-8000">
              {loading.tiers ? 'Loading tier status...' : 'Brain Engine not reachable. Start it to view tiers.'}
            </p>
          </div>
        )}
      </section>

      {/* ═══ Section 2: Session Intelligence & Quality Gate ═══ */}
      <section>
        <div className="flex items-center gap-2 mb-4">
          <Shield className="h-5 w-5 text-nexus-400" />
          <h2 className="text-lg font-semibold text-[#0a2540]">Quality Gate & Session Intelligence</h2>
        </div>

        <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
          {/* Session selector + analysis */}
          <div className="rounded-lg bg-white shadow-sm border border-gray-200 p-4">
            <p className="text-xs font-bold text-slate-400 uppercase tracking-wider mb-3">Session Analysis</p>

            {sessions.length > 0 ? (
              <select
                value={selectedSession}
                onChange={(e) => setSelectedSession(e.target.value)}
                className="w-full rounded-lg bg-gray-100 border border-gray-200 px-3 py-2 text-sm text-slate-700 mb-3 focus:outline-none focus:border-nexus-500/50"
              >
                <option value="">Select a session...</option>
                {sessions.map((s) => (
                  <option key={s.session_id} value={s.session_id}>
                    {s.session_id} ({Math.round(s.completeness * 100)}% complete)
                  </option>
                ))}
              </select>
            ) : (
              <p className="text-xs text-slate-400 mb-3">
                No sessions tracked yet. Run a pipeline to see sessions here.
              </p>
            )}

            {sessionAnalysis && (
              <div className="space-y-3">
                <div className="flex items-center justify-between">
                  <span className="text-xs text-slate-8000">Completeness</span>
                  <span className="text-sm font-bold text-[#0a2540]">
                    {Math.round(sessionAnalysis.completeness * 100)}%
                  </span>
                </div>
                {scoreBar(sessionAnalysis.completeness, 'Overall')}

                <div>
                  <p className="text-[10px] font-bold text-slate-400 uppercase tracking-wider mb-1">Engines Completed</p>
                  <div className="flex flex-wrap gap-1">
                    {sessionAnalysis.engines_completed.map((e) => (
                      <span key={e} className="rounded bg-green-500/15 px-2 py-0.5 text-[10px] font-medium text-green-300">
                        {e}
                      </span>
                    ))}
                  </div>
                </div>

                {sessionAnalysis.gaps.length > 0 && (
                  <div>
                    <p className="text-[10px] font-bold text-orange-400 uppercase tracking-wider mb-1">Gaps</p>
                    {sessionAnalysis.gaps.map((g, i) => (
                      <p key={i} className="text-xs text-orange-300/80 flex items-start gap-1">
                        <AlertTriangle className="h-3 w-3 mt-0.5 shrink-0" />{g}
                      </p>
                    ))}
                  </div>
                )}

                {sessionAnalysis.recommended_next.length > 0 && (
                  <div>
                    <p className="text-[10px] font-bold text-nexus-400 uppercase tracking-wider mb-1">Recommended Next</p>
                    <div className="flex flex-wrap gap-1">
                      {sessionAnalysis.recommended_next.map((e) => (
                        <span key={e} className="rounded bg-nexus-500/15 px-2 py-0.5 text-[10px] font-medium text-nexus-300">
                          → {e}
                        </span>
                      ))}
                    </div>
                  </div>
                )}
              </div>
            )}
          </div>

          {/* Quality Gate */}
          <div className="rounded-lg bg-white shadow-sm border border-gray-200 p-4">
            <div className="flex items-center justify-between mb-3">
              <p className="text-xs font-bold text-slate-400 uppercase tracking-wider">Quality Gate</p>
              <button
                onClick={runQualityGate}
                disabled={!selectedSession || loading.quality}
                className="flex items-center gap-1.5 rounded-lg bg-nexus-500/15 px-3 py-1.5 text-xs font-medium text-nexus-400 hover:bg-nexus-500/25 transition-colors disabled:opacity-40"
              >
                <BarChart3 className="h-3.5 w-3.5" />
                Run Quality Gate
              </button>
            </div>

            {qualityResult ? (
              <div className="space-y-3">
                {/* Overall score */}
                <div className="flex items-center gap-3">
                  <div className={clsx(
                    'flex h-14 w-14 items-center justify-center rounded-xl border-2',
                    qualityResult.passed
                      ? 'bg-green-500/10 border-green-500/30'
                      : 'bg-red-500/10 border-red-500/30',
                  )}>
                    {qualityResult.passed ? (
                      <CheckCircle2 className="h-7 w-7 text-green-400" />
                    ) : (
                      <XCircle className="h-7 w-7 text-red-400" />
                    )}
                  </div>
                  <div>
                    <p className={clsx('text-xl font-bold', levelColor(qualityResult.level))}>
                      {Math.round(qualityResult.overall_score * 100)}%
                    </p>
                    <p className="text-xs text-slate-8000 capitalize">
                      {qualityResult.level} — {qualityResult.passed ? 'PASSED' : 'FAILED'}
                    </p>
                  </div>
                </div>

                {/* Dimension scores */}
                <div className="space-y-2">
                  {scoreBar(qualityResult.rule_completeness, 'Rule Completeness')}
                  {scoreBar(qualityResult.test_coverage, 'Test Coverage')}
                  {scoreBar(qualityResult.consistency, 'Consistency')}
                  {scoreBar(qualityResult.confidence_avg, 'Confidence')}
                  {scoreBar(qualityResult.pii_safety, 'PII Safety')}
                </div>

                {qualityResult.gaps.length > 0 && (
                  <div>
                    <p className="text-[10px] font-bold text-orange-400 uppercase tracking-wider mb-1">Quality Gaps</p>
                    {qualityResult.gaps.map((g, i) => (
                      <p key={i} className="text-xs text-orange-300/80">{g}</p>
                    ))}
                  </div>
                )}
              </div>
            ) : (
              <div className="text-center py-6">
                <Shield className="h-8 w-8 text-slate-8000 mx-auto mb-2" />
                <p className="text-xs text-slate-400">
                  Select a session and click "Run Quality Gate" to evaluate quality.
                </p>
              </div>
            )}
          </div>
        </div>
      </section>

      {/* ═══ Section 3: Ask Brain ═══ */}
      <section>
        <div className="flex items-center gap-2 mb-4">
          <MessageSquare className="h-5 w-5 text-nexus-400" />
          <h2 className="text-lg font-semibold text-[#0a2540]">Ask the Brain</h2>
        </div>

        <div className="rounded-lg bg-white shadow-sm border border-gray-200 p-4">
          <div className="flex items-center gap-2">
            <input
              type="text"
              value={question}
              onChange={(e) => setQuestion(e.target.value)}
              onKeyDown={(e) => e.key === 'Enter' && askBrain()}
              placeholder="Ask anything about the QA process, engines, tiers, or session status..."
              className="flex-1 rounded-lg bg-gray-100 border border-gray-200 px-4 py-2.5 text-sm text-slate-700 placeholder-slate-400 focus:outline-none focus:border-nexus-500/50"
            />
            <button
              onClick={askBrain}
              disabled={!question.trim() || loading.ask}
              className="flex items-center gap-2 rounded-lg bg-nexus-500 px-4 py-2.5 text-sm font-medium text-white hover:bg-nexus-600 transition-colors disabled:opacity-50"
            >
              <Send className="h-4 w-4" />
              Ask
            </button>
          </div>

          {brainAnswer && (
            <div className="mt-4 rounded-lg bg-gray-100/80 border border-gray-200 p-4">
              <div className="flex items-start gap-3">
                <Brain className="h-5 w-5 text-nexus-400 mt-0.5 shrink-0" />
                <div className="flex-1">
                  <p className="text-sm text-slate-700 whitespace-pre-wrap">{brainAnswer.answer}</p>
                  <p className="mt-2 text-[10px] text-slate-400">
                    Confidence: {Math.round(brainAnswer.confidence * 100)}%
                  </p>
                </div>
              </div>
            </div>
          )}
        </div>
      </section>

      {/* ═══ Section 4: How It Works ═══ */}
      <section>
        <div className="flex items-center gap-2 mb-4">
          <Zap className="h-5 w-5 text-nexus-400" />
          <h2 className="text-lg font-semibold text-[#0a2540]">How the Brain Coordinates Pipelines</h2>
        </div>

        <div className="rounded-lg bg-gray-50 border border-gray-200 p-6">
          <div className="grid grid-cols-1 md:grid-cols-3 gap-6">
            {/* Step 1 */}
            <div className="space-y-2">
              <div className="flex h-8 w-8 items-center justify-center rounded-full bg-blue-500/20 text-sm font-bold text-blue-400">1</div>
              <h3 className="text-sm font-semibold text-[#0a2540]">Pipeline Starts</h3>
              <p className="text-xs text-slate-8000 leading-relaxed">
                Client triggers a pipeline via the Orchestrator (e.g., <code className="text-nexus-400">qa-testing</code> chain).
                LLM-powered engines (Brain, Heart) use the <strong>TieredLLMRouter</strong> for
                Tier 1 → Tier 2 → Tier 3 failover. Non-LLM engines use dedicated tooling.
              </p>
            </div>
            {/* Step 2 */}
            <div className="space-y-2">
              <div className="flex h-8 w-8 items-center justify-center rounded-full bg-yellow-500/20 text-sm font-bold text-yellow-400">2</div>
              <h3 className="text-sm font-semibold text-[#0a2540]">Brain Tracks Each Stage</h3>
              <p className="text-xs text-slate-8000 leading-relaxed">
                After each stage completes, the Orchestrator notifies the Brain with the results.
                Brain updates session state, tracks which engines completed, and identifies gaps.
              </p>
            </div>
            {/* Step 3 */}
            <div className="space-y-2">
              <div className="flex h-8 w-8 items-center justify-center rounded-full bg-green-500/20 text-sm font-bold text-green-400">3</div>
              <h3 className="text-sm font-semibold text-[#0a2540]">Quality Gate at Completion</h3>
              <p className="text-xs text-slate-8000 leading-relaxed">
                When all stages complete, the Orchestrator requests a <strong>Quality Gate</strong> evaluation.
                Brain scores 5 dimensions (rules, tests, consistency, confidence, PII safety)
                and returns pass/fail. When policy enforcement is enabled
                (<code className="text-nexus-400">BRAIN_POLICY_ENFORCE=true</code>),
                workflows that fail the gate are held in <strong>needs_review</strong> status.
              </p>
            </div>
          </div>

          {/* API Endpoints */}
          <div className="mt-6 pt-4 border-t border-gray-200">
            <p className="text-[10px] font-bold text-slate-400 uppercase tracking-wider mb-2">Brain API Endpoints (via Gateway)</p>
            <div className="grid grid-cols-1 md:grid-cols-2 gap-1 text-xs font-mono">
              <span className="text-slate-8000"><span className="text-green-400">POST</span> /api/v1/brain/decide — Intelligent cross-engine decision</span>
              <span className="text-slate-8000"><span className="text-green-400">POST</span> /api/v1/brain/quality-gate — Quality evaluation</span>
              <span className="text-slate-8000"><span className="text-green-400">POST</span> /api/v1/brain/sessions/:id/update — Track engine output</span>
              <span className="text-slate-8000"><span className="text-blue-400">GET </span> /api/v1/brain/sessions/:id/analyze — Gap analysis</span>
              <span className="text-slate-8000"><span className="text-blue-400">GET </span> /api/v1/brain/tiers — All engine tier status</span>
              <span className="text-slate-8000"><span className="text-blue-400">GET </span> /api/v1/brain/tiers/:engine — Per-engine tiers</span>
              <span className="text-slate-8000"><span className="text-green-400">POST</span> /api/v1/brain/ask — Free-form Q&A</span>
              <span className="text-slate-8000"><span className="text-blue-400">GET </span> /api/v1/brain/llm-health — LLM provider health</span>
            </div>
          </div>
        </div>
      </section>
    </div>
  );
}
