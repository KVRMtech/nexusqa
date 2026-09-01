// ═══════════════════════════════════════════════════════════════
//  MODULE 6 — AI CONFIDENCE & GUARDRAIL DASHBOARD
//  "Every AI output passes through 4 guardrail stages"
// ═══════════════════════════════════════════════════════════════

import { useState } from 'react';
import api from '../services/api';
import { useApiData } from '../hooks/useApiData';
import { PageHeader } from '../components';
import { EmptyState } from '../components/EmptyState';
import {
  ShieldCheck,
  CheckCircle2,
  Clock,
  XCircle,
  ChevronDown,
  ChevronUp,
  Eye,
  Edit3,
  Undo2,
  BarChart3,
  FileWarning,
  Layers,
  AudioLines,
  ArrowRight,
  ThumbsUp,
  ThumbsDown,
} from 'lucide-react';
import clsx from 'clsx';

// ── Pipeline stage definitions (structural — zero metrics until data flows) ──

interface PipelineStage {
  id: string;
  name: string;
  icon: React.ReactNode;
  description: string;
  passRate: number;
  totalProcessed: number;
  avgLatencyMs: number;
  lastFailed: string | null;
}

const PIPELINE_STAGES: PipelineStage[] = [
  {
    id: 'schema',
    name: 'Schema Validation',
    icon: <Layers className="h-4 w-4" />,
    description: 'Validates structural correctness of every AI-extracted artifact.',
    passRate: 0,
    totalProcessed: 0,
    avgLatencyMs: 0,
    lastFailed: null,
  },
  {
    id: 'graph',
    name: 'Graph Consistency',
    icon: <BarChart3 className="h-4 w-4" />,
    description: 'Cross-references statements against existing knowledge graph for conflicts.',
    passRate: 0,
    totalProcessed: 0,
    avgLatencyMs: 0,
    lastFailed: null,
  },
  {
    id: 'lineage',
    name: 'Source Lineage',
    icon: <AudioLines className="h-4 w-4" />,
    description: 'Verifies every extracted artifact traces to a real audio/video source timestamp.',
    passRate: 0,
    totalProcessed: 0,
    avgLatencyMs: 0,
    lastFailed: null,
  },
  {
    id: 'review',
    name: 'Human Review Gate',
    icon: <Eye className="h-4 w-4" />,
    description: 'Items below confidence threshold (< 80%) are queued for human review.',
    passRate: 0,
    totalProcessed: 0,
    avgLatencyMs: 0,
    lastFailed: null,
  },
];

// ── Human review queue ────────────────────────────────────

interface ReviewItem {
  id: string;
  artifactType: 'rule' | 'test' | 'contradiction';
  title: string;
  confidence: number;
  flagReasons: string[];
  source: string;
  sourceDate: string;
  aiDraft: string;
}

const EMPTY_REVIEW_QUEUE: ReviewItem[] = [];

// ── Trust score trend ─────────────────────────────────────

interface TrustTrendPoint { date: string; score: number; }
const EMPTY_TRUST_TREND: TrustTrendPoint[] = [];

// ── Component ─────────────────────────────────────────────

export default function AIConfidencePage() {
  const { data: reviewQueue, isLive } = useApiData(
    () => api.getReviewQueue(),
    EMPTY_REVIEW_QUEUE,
  );
  const [expandedReview, setExpandedReview] = useState<string | null>(null);

  const overallTrust = EMPTY_TRUST_TREND.length > 0 ? EMPTY_TRUST_TREND[EMPTY_TRUST_TREND.length - 1].score : 0;
  const pendingReviews = reviewQueue.length;
  const totalProcessed = PIPELINE_STAGES[0].totalProcessed;
  const avgPipeline =
    PIPELINE_STAGES.reduce((s, p) => s + p.passRate, 0) / PIPELINE_STAGES.length;

  // Sparkline trend (mini bar chart)
  const maxScore = EMPTY_TRUST_TREND.length > 0 ? Math.max(...EMPTY_TRUST_TREND.map((t) => t.score)) : 0;
  const minScore = EMPTY_TRUST_TREND.length > 0 ? Math.min(...EMPTY_TRUST_TREND.map((t) => t.score)) : 0;
  const range = maxScore - minScore || 1;

  return (
    <div className="space-y-6 animate-fade-in">
      {/* Header */}
      <PageHeader
        title="AI Confidence & Guardrails"
        subtitle="Every AI output passes through 4 validation stages before it enters the knowledge base."
        isLive={isLive}
      />

      {/* Summary cards */}
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
        <div className="stat-card p-4">
          <p className="text-[11px] font-medium text-slate-400 uppercase tracking-wider">Trust Score</p>
          <p className="text-2xl font-bold text-emerald-400 mt-1">{overallTrust}%</p>
          <div className="flex items-center gap-0.5 mt-2">
            {EMPTY_TRUST_TREND.map((t, i) => (
              <div
                key={i}
                className="w-2 rounded-sm bg-emerald-500/60"
                style={{ height: `${8 + ((t.score - minScore) / range) * 20}px` }}
              />
            ))}
          </div>
        </div>
        <div className="stat-card p-4">
          <p className="text-[11px] font-medium text-slate-400 uppercase tracking-wider">Pipeline Health</p>
          <p className="text-2xl font-bold text-cyan-400 mt-1">{avgPipeline.toFixed(1)}%</p>
          <p className="text-[11px] text-slate-400 mt-1">avg pass rate all stages</p>
        </div>
        <div className="stat-card p-4">
          <p className="text-[11px] font-medium text-slate-400 uppercase tracking-wider">Artifacts Processed</p>
          <p className="text-2xl font-bold text-[#0a2540] mt-1">{totalProcessed.toLocaleString()}</p>
          <p className="text-[11px] text-slate-400 mt-1">rules + tests + contradictions</p>
        </div>
        <div className="stat-card p-4">
          <p className="text-[11px] font-medium text-slate-400 uppercase tracking-wider">Pending Reviews</p>
          <p className="text-2xl font-bold text-yellow-400 mt-1">{pendingReviews}</p>
          <p className="text-[11px] text-slate-400 mt-1">below 80% confidence</p>
        </div>
      </div>

      {/* Guardrail Pipeline */}
      <div>
        <h2 className="zone-header">Guardrail Pipeline</h2>
        <div className="flex items-stretch gap-1 mt-3">
          {PIPELINE_STAGES.map((stage, idx) => {
            const color =
              stage.passRate >= 97
                ? 'text-emerald-400 border-emerald-500/20'
                : stage.passRate >= 90
                  ? 'text-cyan-400 border-cyan-500/20'
                  : 'text-yellow-400 border-yellow-500/20';

            return (
              <div key={stage.id} className="flex items-stretch flex-1 gap-1">
                <div className={clsx('card flex-1 p-4 border', color.split(' ')[1])}>
                  <div className="flex items-center gap-2 mb-2">
                    <div className={clsx('flex h-7 w-7 items-center justify-center rounded-md bg-white/5', color.split(' ')[0])}>
                      {stage.icon}
                    </div>
                    <p className="text-xs font-medium text-slate-600">{stage.name}</p>
                  </div>
                  <p className={clsx('text-xl font-bold', color.split(' ')[0])}>
                    {stage.passRate}%
                  </p>
                  <p className="text-[10px] text-slate-400 mt-1">
                    {stage.totalProcessed} processed • {stage.avgLatencyMs}ms avg
                  </p>
                  {stage.lastFailed && (
                    <p className="text-[10px] text-slate-8000 mt-2 truncate" title={stage.lastFailed}>
                      <FileWarning className="inline h-3 w-3 text-yellow-600 mr-1" />
                      {stage.lastFailed}
                    </p>
                  )}
                </div>
                {idx < PIPELINE_STAGES.length - 1 && (
                  <div className="flex items-center text-gray-700">
                    <ArrowRight className="h-4 w-4" />
                  </div>
                )}
              </div>
            );
          })}
        </div>
      </div>

      {/* Human Review Queue */}
      <div>
        <div className="flex items-center justify-between">
          <h2 className="zone-header">Human Review Queue</h2>
          <span className="text-xs text-slate-400">{pendingReviews} items awaiting review</span>
        </div>
        <div className="mt-3 space-y-2">
          {reviewQueue.length === 0 && (
            <EmptyState title="No Items in Review Queue" description="Items below the confidence threshold will appear here for human review." />
          )}
          {reviewQueue.map((item) => {
            const isExpanded = expandedReview === item.id;
            const typeLabel =
              item.artifactType === 'rule' ? 'RULE' :
              item.artifactType === 'test' ? 'TEST' : 'CONFLICT';
            const typeBadge =
              item.artifactType === 'rule' ? 'badge-nexus' :
              item.artifactType === 'test' ? 'badge-green' : 'badge-red';

            return (
              <div key={item.id} className="card overflow-hidden">
                <button
                  onClick={() => setExpandedReview(isExpanded ? null : item.id)}
                  className="flex w-full items-center gap-4 p-4 text-left hover:bg-white/[0.02] transition-colors"
                >
                  {/* Confidence ring */}
                  <div className="relative h-10 w-10 shrink-0">
                    <svg viewBox="0 0 36 36" className="h-10 w-10 -rotate-90">
                      <circle cx="18" cy="18" r="15" fill="none" stroke="currentColor" strokeWidth="2" className="text-[#0a2540]/5" />
                      <circle
                        cx="18" cy="18" r="15" fill="none" strokeWidth="2.5"
                        strokeDasharray={`${(item.confidence / 100) * 94.2} 94.2`}
                        strokeLinecap="round"
                        className={clsx(
                          item.confidence >= 70 ? 'text-yellow-500' : 'text-red-500',
                        )}
                        stroke="currentColor"
                      />
                    </svg>
                    <span className="absolute inset-0 flex items-center justify-center text-[10px] font-bold text-slate-600">
                      {item.confidence}
                    </span>
                  </div>

                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-2 flex-wrap">
                      <span className={typeBadge}>{typeLabel}</span>
                      <h3 className="text-sm font-medium text-slate-700 truncate">{item.title}</h3>
                    </div>
                    <div className="flex items-center gap-3 mt-1 text-[11px] text-slate-400">
                      <span>{item.source}</span>
                      <span>{item.sourceDate}</span>
                    </div>
                  </div>

                  <div className="flex items-center gap-1.5 shrink-0">
                    {item.flagReasons.map((_, i) => (
                      <span key={i} className="h-2 w-2 rounded-full bg-yellow-500/60" title={item.flagReasons[i]} />
                    ))}
                  </div>

                  {isExpanded ? <ChevronUp className="h-4 w-4 text-slate-400 shrink-0" /> : <ChevronDown className="h-4 w-4 text-slate-400 shrink-0" />}
                </button>

                {isExpanded && (
                  <div className="border-t border-gray-200 p-4 space-y-4 animate-slide-in">
                    <div>
                      <p className="text-[11px] font-semibold text-slate-400 mb-1">FLAG REASONS</p>
                      <div className="flex flex-wrap gap-1.5">
                        {item.flagReasons.map((reason, i) => (
                          <span key={i} className="badge-yellow">{reason}</span>
                        ))}
                      </div>
                    </div>

                    <div>
                      <p className="text-[11px] font-semibold text-slate-400 mb-1">AI DRAFT</p>
                      <blockquote className="rounded-lg bg-white/[0.03] p-3 border-l-2 border-nexus-500 text-sm text-slate-600 italic leading-relaxed">
                        {item.aiDraft}
                      </blockquote>
                    </div>

                    <div className="flex gap-2">
                      <button className="btn-primary text-xs py-1.5 px-3">
                        <ThumbsUp className="h-3.5 w-3.5" /> Approve
                      </button>
                      <button className="btn-ghost text-xs py-1.5 px-3">
                        <Edit3 className="h-3.5 w-3.5" /> Edit & Approve
                      </button>
                      <button className="btn-ghost text-xs py-1.5 px-3 text-red-400 hover:text-red-300">
                        <ThumbsDown className="h-3.5 w-3.5" /> Reject
                      </button>
                      <button className="btn-ghost text-xs py-1.5 px-3 text-yellow-400">
                        <Undo2 className="h-3.5 w-3.5" /> Return to AI
                      </button>
                    </div>
                  </div>
                )}
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}
