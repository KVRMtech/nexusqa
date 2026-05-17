// ═══════════════════════════════════════════════════════════════
//  Persona Workspace Page — 3-Panel Evidence-Backed Layout
//
//  Route: /sessions/:sessionId/persona-workspace?artifact_id=...
//  Calls POST /v1/personas/generate-draft on mount, then renders:
//    Left:   Persona Profile + Grounding Contract summary
//    Center: Process Map (domain workflows as a step flow)
//    Right:  Evidence Rail (all citations with modality badges)
// ═══════════════════════════════════════════════════════════════

import { useState, useEffect, useCallback, useMemo, useRef } from 'react';
import { useParams, useSearchParams, useNavigate } from 'react-router-dom';
import { useAuth } from '../contexts/AuthContext';
import api from '../services/api';
import { StatusBadge } from '../components/StatusBadge';
import type {
  PersonaDraftResponse,
  PersonaDraftProfile,
  DomainMap,
  DomainWorkflow,
  DomainRisk,
  GroundingContract,
  EvidenceCitation,
  PersonaDraftProvenance,
} from '../types/canonical';
import clsx from 'clsx';
import {
  ArrowLeft,
  Brain,
  Users,
  Monitor,
  GitBranch,
  AlertTriangle,
  HelpCircle,
  Loader2,
  CheckCircle2,
  XCircle,
  Save,
  Mic,
  Eye,
  Share2,
  Zap,
  ChevronRight,
  ChevronDown,
  Shield,
  ShieldCheck,
  FlaskConical,
  Database,
  BookOpen,
  UserCircle,
  FileText,
  Activity,
  Target,
  Sparkles,
  Microscope,
  Compass,
  Lightbulb,
} from 'lucide-react';

// ── Avatar icon mapping ────────────────────────────────────

const AVATAR_ICON_MAP: Record<string, React.ReactNode> = {
  brain:          <Brain className="h-6 w-6 text-nexus-400" />,
  'shield-check': <ShieldCheck className="h-6 w-6 text-green-400" />,
  'flask-conical':<FlaskConical className="h-6 w-6 text-purple-400" />,
  database:       <Database className="h-6 w-6 text-cyan-400" />,
  'book-open':    <BookOpen className="h-6 w-6 text-amber-400" />,
  'user-circle':  <UserCircle className="h-6 w-6 text-blue-400" />,
  target:         <Target className="h-6 w-6 text-red-400" />,
  microscope:     <Microscope className="h-6 w-6 text-indigo-400" />,
  compass:        <Compass className="h-6 w-6 text-teal-400" />,
  lightbulb:      <Lightbulb className="h-6 w-6 text-yellow-400" />,
};

function renderAvatarIcon(icon: string | undefined): React.ReactNode {
  if (!icon) return <Brain className="h-6 w-6 text-nexus-400" />;
  return AVATAR_ICON_MAP[icon] || <Brain className="h-6 w-6 text-nexus-400" />;
}

// ── Capability label formatting ─────────────────────────────

const CAPABILITY_LABELS: Record<string, string> = {
  rule_extraction: 'Rule Extraction',
  test_generation: 'Test Generation',
  knowledge_graph: 'Knowledge Graph',
  compliance_check: 'Compliance Check',
  data_generation: 'Data Generation',
  contradiction_detection: 'Contradiction Detection',
  report_generation: 'Report Generation',
  test_execution: 'Test Execution',
};

function formatCapability(cap: string): string {
  return CAPABILITY_LABELS[cap] || cap.replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase());
}

// ── Helpers ────────────────────────────────────────────────

function confidenceColor(c: number): string {
  if (c >= 0.8) return 'text-green-400';
  if (c >= 0.6) return 'text-yellow-400';
  if (c >= 0.4) return 'text-orange-400';
  return 'text-red-400';
}

function confidenceBg(c: number): string {
  if (c >= 0.8) return 'bg-green-500/20 border-green-500/30';
  if (c >= 0.6) return 'bg-yellow-500/20 border-yellow-500/30';
  if (c >= 0.4) return 'bg-orange-500/20 border-orange-500/30';
  return 'bg-red-500/20 border-red-500/30';
}

function modalityIcon(mod: string): React.ReactNode {
  switch (mod) {
    case 'transcript': return <Mic className="h-3.5 w-3.5" />;
    case 'visual':     return <Eye className="h-3.5 w-3.5" />;
    case 'graph':      return <Share2 className="h-3.5 w-3.5" />;
    case 'inferred':   return <Sparkles className="h-3.5 w-3.5" />;
    default:           return <FileText className="h-3.5 w-3.5" />;
  }
}

function modalityLabel(mod: string): string {
  switch (mod) {
    case 'transcript': return 'Transcript';
    case 'visual':     return 'Visual';
    case 'graph':      return 'Graph';
    case 'inferred':   return 'Inferred';
    default:           return mod;
  }
}

function modalityBadgeColor(mod: string): string {
  switch (mod) {
    case 'transcript': return 'bg-blue-500/20 text-blue-400 border-blue-500/30';
    case 'visual':     return 'bg-purple-500/20 text-purple-400 border-purple-500/30';
    case 'graph':      return 'bg-cyan-500/20 text-cyan-400 border-cyan-500/30';
    case 'inferred':   return 'bg-gray-500/20 text-gray-400 border-gray-500/30';
    default:           return 'bg-gray-500/20 text-gray-400 border-gray-500/30';
  }
}

function severityColor(s: string): string {
  switch (s) {
    case 'critical': return 'text-red-400 bg-red-500/20 border-red-500/30';
    case 'high':     return 'text-orange-400 bg-orange-500/20 border-orange-500/30';
    case 'medium':   return 'text-yellow-400 bg-yellow-500/20 border-yellow-500/30';
    case 'low':      return 'text-blue-400 bg-blue-500/20 border-blue-500/30';
    default:         return 'text-gray-400 bg-gray-500/20 border-gray-500/30';
  }
}

// ── Collapsible Evidence Group ──────────────────────────────

function EvidenceGroup({
  label,
  icon,
  citations,
  startIndex,
  colorClass,
}: {
  label: string;
  icon: React.ReactNode;
  citations: EvidenceCitation[];
  startIndex: number;
  colorClass: string;
}) {
  const [open, setOpen] = useState(citations.length <= 4);
  if (citations.length === 0) return null;
  return (
    <div className="space-y-1.5">
      <button
        onClick={() => setOpen(!open)}
        className="w-full flex items-center gap-2 text-left group"
      >
        {open ? (
          <ChevronDown className="h-3 w-3 text-gray-500 shrink-0" />
        ) : (
          <ChevronRight className="h-3 w-3 text-gray-500 shrink-0" />
        )}
        <span className={clsx('text-[10px] font-semibold uppercase tracking-wider', colorClass)}>
          {icon} {label}
        </span>
        <span className="ml-auto text-[10px] font-mono text-gray-600">{citations.length}</span>
      </button>
      {open && (
        <div className="space-y-1.5 pl-1">
          {citations.map((c, i) => (
            <EvidenceCard key={i} citation={c} index={startIndex + i} />
          ))}
        </div>
      )}
    </div>
  );
}

// ── Evidence Citation Card ─────────────────────────────────

function EvidenceCard({ citation, index }: { citation: EvidenceCitation; index: number }) {
  return (
    <div className="rounded-lg border border-white/10 bg-gray-800/50 p-3 space-y-2">
      <div className="flex items-start gap-2">
        <span className="shrink-0 text-[10px] font-mono font-bold text-gray-500 bg-gray-700/50 rounded px-1.5 py-0.5">
          E{index + 1}
        </span>
        <p className="text-xs text-gray-300 leading-relaxed flex-1">{citation.text}</p>
      </div>
      <div className="flex items-center gap-2 flex-wrap">
        <span className={clsx('inline-flex items-center gap-1 text-[10px] font-medium px-2 py-0.5 rounded-full border', modalityBadgeColor(citation.source_modality))}>
          {modalityIcon(citation.source_modality)}
          {modalityLabel(citation.source_modality)}
        </span>
        {citation.timestamp_range && (
          <span className="text-[10px] text-gray-500 font-mono">{citation.timestamp_range}</span>
        )}
        <span className={clsx('text-[10px] font-mono font-bold', confidenceColor(citation.confidence))}>
          {Math.round(citation.confidence * 100)}%
        </span>
      </div>
    </div>
  );
}

// ── Workflow Step Card ─────────────────────────────────────

function WorkflowStepCard({ step, isLast }: { step: DomainWorkflow; isLast: boolean }) {
  const [expanded, setExpanded] = useState(false);

  return (
    <div className="relative">
      {/* Connector line */}
      {!isLast && (
        <div className="absolute left-5 top-12 bottom-0 w-px bg-gradient-to-b from-nexus-500/40 to-transparent" />
      )}

      <div
        onClick={() => setExpanded(!expanded)}
        className="relative flex items-start gap-3 p-3 rounded-lg border border-white/10 bg-gray-800/40 hover:bg-gray-800/70 hover:border-nexus-500/30 transition-all cursor-pointer group"
      >
        {/* Step number circle */}
        <div className="shrink-0 w-10 h-10 rounded-full bg-nexus-500/20 border border-nexus-500/40 flex items-center justify-center">
          <span className="text-xs font-bold text-nexus-400">{step.step_number}</span>
        </div>

        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2">
            <h4 className="text-sm font-semibold text-white group-hover:text-nexus-300">{step.name}</h4>
            {expanded ? <ChevronDown className="h-3.5 w-3.5 text-gray-500" /> : <ChevronRight className="h-3.5 w-3.5 text-gray-500" />}
          </div>
          <p className="text-xs text-gray-400 mt-0.5 line-clamp-2">{step.description}</p>

          {/* Tags row */}
          <div className="flex flex-wrap gap-1.5 mt-2">
            {step.actors.map((a) => (
              <span key={a} className="inline-flex items-center gap-1 text-[10px] px-1.5 py-0.5 rounded-full bg-blue-500/15 text-blue-400 border border-blue-500/25">
                <Users className="h-2.5 w-2.5" /> {a}
              </span>
            ))}
            {step.systems.map((s) => (
              <span key={s} className="inline-flex items-center gap-1 text-[10px] px-1.5 py-0.5 rounded-full bg-purple-500/15 text-purple-400 border border-purple-500/25">
                <Monitor className="h-2.5 w-2.5" /> {s}
              </span>
            ))}
          </div>
        </div>
      </div>

      {/* Expanded details */}
      {expanded && (
        <div className="ml-13 mt-2 mb-4 space-y-2 pl-4 border-l border-white/5">
          {step.decisions.length > 0 && (
            <div>
              <h5 className="text-[10px] font-semibold text-gray-500 uppercase tracking-wider mb-1">Decisions</h5>
              <ul className="space-y-1">
                {step.decisions.map((d, i) => (
                  <li key={i} className="text-xs text-gray-400 flex items-start gap-1.5">
                    <GitBranch className="h-3 w-3 text-yellow-500 shrink-0 mt-0.5" />
                    {d}
                  </li>
                ))}
              </ul>
            </div>
          )}
          {step.evidence.length > 0 && (
            <div>
              <h5 className="text-[10px] font-semibold text-gray-500 uppercase tracking-wider mb-1">Evidence ({step.evidence.length})</h5>
              <div className="space-y-1.5">
                {step.evidence.map((e, i) => (
                  <EvidenceCard key={i} citation={e} index={i} />
                ))}
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

// ── Risk Card ──────────────────────────────────────────────

function RiskCard({ risk }: { risk: DomainRisk }) {
  return (
    <div className={clsx('rounded-lg border p-3', severityColor(risk.severity))}>
      <div className="flex items-start gap-2">
        <AlertTriangle className="h-4 w-4 shrink-0 mt-0.5" />
        <div>
          <p className="text-xs font-medium">{risk.description}</p>
          <div className="flex items-center gap-2 mt-1">
            <span className="text-[10px] font-semibold uppercase">{risk.severity}</span>
            <span className="text-[10px] text-gray-500">· {risk.evidence.length} citation{risk.evidence.length !== 1 ? 's' : ''}</span>
          </div>
        </div>
      </div>
    </div>
  );
}

// ── Grounding Summary Bar ──────────────────────────────────

function GroundingSummary({ contract }: { contract: GroundingContract }) {
  const dist = contract.modality_distribution;
  const total = dist.transcript + dist.visual + dist.graph + dist.inferred;

  return (
    <div className="rounded-xl border border-white/10 bg-gray-800/50 p-4 space-y-3">
      <div className="flex items-center gap-2">
        <Shield className="h-4 w-4 text-nexus-400" />
        <h3 className="text-xs font-semibold text-white uppercase tracking-wider">Grounding Contract</h3>
      </div>

      {/* Stats row */}
      <div className="grid grid-cols-3 gap-3">
        <div className="text-center">
          <div className="text-lg font-bold text-white">{contract.total_evidence_count}</div>
          <div className="text-[10px] text-gray-500 uppercase">Citations</div>
        </div>
        <div className="text-center">
          <div className={clsx('text-lg font-bold', confidenceColor(contract.avg_confidence))}>
            {Math.round(contract.avg_confidence * 100)}%
          </div>
          <div className="text-[10px] text-gray-500 uppercase">Avg Confidence</div>
        </div>
        <div className="text-center">
          <div className="text-lg font-bold text-white">{contract.open_questions.length}</div>
          <div className="text-[10px] text-gray-500 uppercase">Open Questions</div>
        </div>
      </div>

      {/* Modality distribution bar */}
      {total > 0 && (
        <div>
          <div className="text-[10px] text-gray-500 mb-1 uppercase tracking-wider">Evidence Sources</div>
          <div className="flex h-2 rounded-full overflow-hidden bg-gray-700">
            {dist.transcript > 0 && <div className="bg-blue-500" style={{ width: `${(dist.transcript / total) * 100}%` }} />}
            {dist.visual > 0 && <div className="bg-purple-500" style={{ width: `${(dist.visual / total) * 100}%` }} />}
            {dist.graph > 0 && <div className="bg-cyan-500" style={{ width: `${(dist.graph / total) * 100}%` }} />}
            {dist.inferred > 0 && <div className="bg-gray-500" style={{ width: `${(dist.inferred / total) * 100}%` }} />}
          </div>
          <div className="flex gap-3 mt-1.5 flex-wrap">
            {dist.transcript > 0 && <span className="text-[10px] text-blue-400">● Transcript ({dist.transcript})</span>}
            {dist.visual > 0 && <span className="text-[10px] text-purple-400">● Visual ({dist.visual})</span>}
            {dist.graph > 0 && <span className="text-[10px] text-cyan-400">● Graph ({dist.graph})</span>}
            {dist.inferred > 0 && <span className="text-[10px] text-gray-400">● Inferred ({dist.inferred})</span>}
          </div>
        </div>
      )}

      {/* Open questions */}
      {contract.open_questions.length > 0 && (
        <div>
          <div className="text-[10px] text-gray-500 mb-1.5 uppercase tracking-wider">Open Questions</div>
          <ul className="space-y-1">
            {contract.open_questions.map((q, i) => (
              <li key={i} className="text-xs text-yellow-400/80 flex items-start gap-1.5">
                <HelpCircle className="h-3 w-3 shrink-0 mt-0.5" />
                {q}
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}

// ═══════════════════════════════════════════════════════════════
//  Main Page Component
// ═══════════════════════════════════════════════════════════════

type PageState = 'loading' | 'generating' | 'ready' | 'error' | 'saving' | 'saved';

export default function PersonaWorkspacePage() {
  const { sessionId } = useParams<{ sessionId: string }>();
  const [searchParams] = useSearchParams();
  const artifactId = searchParams.get('artifact_id') ?? '';
  const navigate = useNavigate();
  const { user } = useAuth();

  const [state, setState] = useState<PageState>('loading');
  const [draft, setDraft] = useState<PersonaDraftResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [elapsed, setElapsed] = useState(0);
  const [isCached, setIsCached] = useState(false);
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null);

  // ── Generate draft on mount ───────────────────────────────
  const generate = useCallback(async (forceRegenerate = false) => {
    if (!artifactId) {
      setError('No artifact_id provided');
      setState('error');
      return;
    }

    setState('generating');
    setElapsed(0);
    setIsCached(false);

    // Start elapsed timer
    const start = Date.now();
    timerRef.current = setInterval(() => {
      setElapsed(Math.round((Date.now() - start) / 1000));
    }, 1000);

    try {
      const resp = await api.generatePersonaDraft(artifactId, sessionId, forceRegenerate);
      setDraft(resp);
      setIsCached(resp.cached === true);
      setState('ready');
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : 'Failed to generate persona draft';
      setError(msg);
      setState('error');
    } finally {
      if (timerRef.current) clearInterval(timerRef.current);
    }
  }, [artifactId, sessionId]);

  useEffect(() => {
    generate(false);
    return () => {
      if (timerRef.current) clearInterval(timerRef.current);
    };
  }, [generate]);

  // ── Save persona ──────────────────────────────────────────
  const handleSave = async () => {
    if (!draft) return;
    setState('saving');
    try {
      const slug = draft.persona.name
        .toLowerCase()
        .replace(/[^a-z0-9]+/g, '-')
        .replace(/^-|-$/g, '');

      const metadata: Record<string, unknown> = {
        generated_from: 'process_oracle',
        artifact_id: artifactId,
        session_id: sessionId,
        domain_map: draft.domain_map,
        grounding_contract: draft.grounding_contract,
        provenance: draft.provenance,
      };

      const saveWithSlug = async (s: string) => {
        await api.createPersona({
          name: draft.persona.name,
          slug: s,
          description: draft.persona.description,
          avatar_icon: draft.persona.avatar_icon,
          system_prompt: draft.persona.system_prompt,
          capabilities: draft.persona.capabilities,
          stage_config: draft.persona.stage_config,
          specialty_domains: draft.persona.specialty_domains,
          metadata_json: metadata,
        });
      };

      try {
        await saveWithSlug(slug);
      } catch (firstErr: unknown) {
        // If slug collision (409), retry with timestamp suffix
        const is409 = firstErr instanceof Error && firstErr.message?.includes('409');
        const axiosStatus = (firstErr as { response?: { status?: number } })?.response?.status;
        if (is409 || axiosStatus === 409) {
          const suffix = Date.now().toString(36);
          await saveWithSlug(`${slug}-${suffix}`);
        } else {
          throw firstErr;
        }
      }

      setState('saved');
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : 'Failed to save persona';
      setError(msg);
      setState('error');
    }
  };

  // ── Collect all evidence citations (deduplicated, grouped) ─
  const evidenceGroups = useMemo<{
    actors: EvidenceCitation[];
    systems: EvidenceCitation[];
    workflows: EvidenceCitation[];
    risks: EvidenceCitation[];
  }>(() => {
    if (!draft) return { actors: [], systems: [], workflows: [], risks: [] };
    const dm = draft.domain_map;
    const dedup = (arr: EvidenceCitation[]) => {
      const seen = new Set<string>();
      return arr.filter((c) => {
        const key = c.text.trim().toLowerCase();
        if (seen.has(key)) return false;
        seen.add(key);
        return true;
      });
    };
    return {
      actors: dedup(dm.actors.flatMap((a) => a.evidence)),
      systems: dedup(dm.systems.flatMap((s) => s.evidence)),
      workflows: dedup(dm.workflows.flatMap((w) => w.evidence)),
      risks: dedup(dm.risks.flatMap((r) => r.evidence)),
    };
  }, [draft]);

  const allEvidence = useMemo<EvidenceCitation[]>(() => {
    const all = [
      ...evidenceGroups.actors,
      ...evidenceGroups.systems,
      ...evidenceGroups.workflows,
      ...evidenceGroups.risks,
    ];
    const seen = new Set<string>();
    return all.filter((c) => {
      const key = c.text.trim().toLowerCase();
      if (seen.has(key)) return false;
      seen.add(key);
      return true;
    });
  }, [evidenceGroups]);

  // ── Loading / Generating state ────────────────────────────
  if (state === 'loading' || state === 'generating') {
    return (
      <div className="flex h-full items-center justify-center bg-gray-950">
        <div className="flex flex-col items-center gap-4 max-w-md text-center">
          <div className="relative">
            <Brain className="h-16 w-16 text-nexus-500 animate-pulse" />
            <Loader2 className="absolute -top-2 -right-2 h-6 w-6 text-nexus-400 animate-spin" />
          </div>
          <h2 className="text-xl font-bold text-white">Generating Process Oracle</h2>
          <p className="text-sm text-gray-400">
            Analyzing canonical artifact to build a grounded digital SME...
          </p>
          <div className="flex items-center gap-2 text-gray-500">
            <Activity className="h-4 w-4 animate-pulse" />
            <span className="text-sm font-mono">{elapsed}s elapsed</span>
          </div>
          <div className="w-64 bg-gray-800 rounded-full h-1.5 overflow-hidden">
            <div className="h-full bg-nexus-500 rounded-full animate-pulse" style={{ width: '60%' }} />
          </div>
          <p className="text-xs text-gray-600">
            LLM is reading transcript, visual analysis, and graph data...
          </p>
        </div>
      </div>
    );
  }

  // ── Error state ───────────────────────────────────────────
  if (state === 'error') {
    return (
      <div className="flex h-full items-center justify-center bg-gray-950">
        <div className="flex flex-col items-center gap-4 max-w-md text-center">
          <XCircle className="h-16 w-16 text-red-500" />
          <h2 className="text-xl font-bold text-white">Generation Failed</h2>
          <p className="text-sm text-red-400">{error}</p>
          <div className="flex gap-3">
            <button
              onClick={() => navigate(-1)}
              className="px-4 py-2 text-sm text-gray-400 border border-white/10 rounded-lg hover:bg-gray-800 transition-colors"
            >
              Go Back
            </button>
            <button
              onClick={() => generate(false)}
              className="px-4 py-2 text-sm text-white bg-nexus-600 rounded-lg hover:bg-nexus-500 transition-colors"
            >
              Retry
            </button>
          </div>
        </div>
      </div>
    );
  }

  // ── Saved state ───────────────────────────────────────────
  if (state === 'saved') {
    return (
      <div className="flex h-full items-center justify-center bg-gray-950">
        <div className="flex flex-col items-center gap-4 max-w-md text-center">
          <CheckCircle2 className="h-16 w-16 text-green-500" />
          <h2 className="text-xl font-bold text-white">Persona Saved</h2>
          <p className="text-sm text-gray-400">
            <strong className="text-white">{draft?.persona.name}</strong> has been saved successfully.
          </p>
          <div className="flex gap-3">
            <button
              onClick={() => navigate(`/sessions/${sessionId}/canonical`)}
              className="px-4 py-2 text-sm text-white bg-nexus-600 rounded-lg hover:bg-nexus-500 transition-colors"
            >
              Back to Canonical
            </button>
          </div>
        </div>
      </div>
    );
  }

  // ── Ready: 3-panel layout ─────────────────────────────────
  if (!draft) return null;

  const { persona, domain_map: dm, grounding_contract: gc, provenance } = draft;

  return (
    <div className="flex flex-col h-full bg-gray-950 overflow-hidden">
      {/* ── Top Bar ────────────────────────────────────────── */}
      <div className="shrink-0 border-b border-white/10 bg-gray-900/70 backdrop-blur-xl px-6 py-3">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-3">
            <button
              onClick={() => navigate(`/sessions/${sessionId}/canonical`)}
              className="text-gray-400 hover:text-white transition-colors"
            >
              <ArrowLeft className="h-5 w-5" />
            </button>
            <div>
              <h1 className="text-lg font-bold text-white flex items-center gap-2">
                <Brain className="h-5 w-5 text-nexus-400" />
                {persona.name}
                {isCached && (
                  <span className="text-[10px] font-medium px-2 py-0.5 rounded-full bg-green-500/20 text-green-400 border border-green-500/30">
                    Cached · Instant
                  </span>
                )}
              </h1>
              <p className="text-xs text-gray-500">
                {isCached
                  ? `Loaded from cache in ${draft.cache_hit_ms ?? 0}ms`
                  : `Generated in ${Math.round(draft.processing_time_ms / 1000)}s`}
                · Model: {provenance.model_used}
                · Backend: {provenance.model_backend}
              </p>
            </div>
          </div>
          <div className="flex items-center gap-3">
            <StatusBadge
              label={`${gc.total_evidence_count} citations`}
              variant={gc.avg_confidence >= 0.7 ? 'green' : gc.avg_confidence >= 0.5 ? 'yellow' : 'orange'}
            />
            {isCached && (
              <button
                onClick={() => generate(true)}
                className="flex items-center gap-1.5 px-3 py-2 rounded-lg text-xs font-medium text-gray-400 border border-white/10 hover:bg-gray-800 hover:text-white transition-all"
              >
                <Sparkles className="h-3.5 w-3.5" />
                Regenerate
              </button>
            )}
            <button
              onClick={handleSave}
              disabled={state === 'saving'}
              className={clsx(
                'flex items-center gap-2 px-4 py-2 rounded-lg text-sm font-medium transition-all',
                'bg-nexus-600 hover:bg-nexus-500 text-white',
                state === 'saving' && 'opacity-50 cursor-wait',
              )}
            >
              {state === 'saving' ? <Loader2 className="h-4 w-4 animate-spin" /> : <Save className="h-4 w-4" />}
              Save Persona
            </button>
            <button
              onClick={() => navigate(`/sessions/${sessionId}/test-strategy?artifact_id=${artifactId}`)}
              className="flex items-center gap-2 px-4 py-2 rounded-lg text-sm font-medium transition-all bg-purple-600 hover:bg-purple-500 text-white"
            >
              <FlaskConical className="h-4 w-4" />
              Test Architect
            </button>
          </div>
        </div>
      </div>

      {/* ── 3-Panel Layout ─────────────────────────────────── */}
      <div className="flex flex-1 overflow-hidden">

        {/* ── LEFT PANEL: Persona Profile ──────────────────── */}
        <div className="w-80 shrink-0 border-r border-white/10 overflow-y-auto custom-scrollbar">
          <div className="p-4 space-y-4">
            {/* Profile card */}
            <div className="rounded-xl border border-white/10 bg-gray-800/50 p-4 space-y-3">
              <div className="flex items-center gap-3">
                <div className="w-12 h-12 rounded-xl bg-nexus-500/20 border border-nexus-500/40 flex items-center justify-center">
                  {renderAvatarIcon(persona.avatar_icon)}
                </div>
                <div>
                  <h2 className="text-sm font-bold text-white">{persona.name}</h2>
                  <p className="text-xs text-gray-400">{persona.specialty_domains.join(' · ')}</p>
                </div>
              </div>
              <p className="text-xs text-gray-300 leading-relaxed">{persona.description}</p>
            </div>

            {/* Capabilities */}
            <div className="rounded-xl border border-white/10 bg-gray-800/50 p-4">
              <h3 className="text-xs font-semibold text-gray-500 uppercase tracking-wider mb-2 flex items-center gap-1.5">
                <Zap className="h-3.5 w-3.5 text-nexus-400" /> Capabilities
              </h3>
              <div className="flex flex-wrap gap-1.5">
                {persona.capabilities.map((cap, i) => (
                  <span key={i} className="text-[10px] px-2 py-1 rounded-full bg-nexus-500/15 text-nexus-400 border border-nexus-500/25 flex items-center gap-1">
                    <Zap className="h-2.5 w-2.5" />
                    {formatCapability(cap)}
                  </span>
                ))}
              </div>
            </div>

            {/* Domain Summary */}
            <div className="rounded-xl border border-white/10 bg-gray-800/50 p-4 space-y-3">
              <h3 className="text-xs font-semibold text-gray-500 uppercase tracking-wider flex items-center gap-1.5">
                <Target className="h-3.5 w-3.5 text-nexus-400" /> Domain Map
              </h3>
              <div className="space-y-2">
                {dm.actors.length > 0 && (
                  <div className="p-2 rounded-lg bg-gray-700/30">
                    <div className="flex items-center gap-1.5 mb-1">
                      <Users className="h-3 w-3 text-blue-400" />
                      <span className="text-[10px] font-semibold text-blue-400 uppercase">{dm.actors.length} Actor{dm.actors.length !== 1 ? 's' : ''}</span>
                    </div>
                    <div className="space-y-0.5">
                      {dm.actors.map((a, i) => (
                        <div key={i} className="text-xs text-gray-300 truncate" title={a.role}>{a.name}{a.role && a.role !== a.name ? <span className="text-gray-500"> — {a.role}</span> : null}</div>
                      ))}
                    </div>
                  </div>
                )}
                {dm.systems.length > 0 && (
                  <div className="p-2 rounded-lg bg-gray-700/30">
                    <div className="flex items-center gap-1.5 mb-1">
                      <Monitor className="h-3 w-3 text-purple-400" />
                      <span className="text-[10px] font-semibold text-purple-400 uppercase">{dm.systems.length} System{dm.systems.length !== 1 ? 's' : ''}</span>
                    </div>
                    <div className="space-y-0.5">
                      {dm.systems.map((s, i) => (
                        <div key={i} className="text-xs text-gray-300 truncate" title={s.purpose}>{s.name}</div>
                      ))}
                    </div>
                  </div>
                )}
                {dm.workflows.length > 0 && (
                  <div className="p-2 rounded-lg bg-gray-700/30">
                    <div className="flex items-center gap-1.5 mb-1">
                      <GitBranch className="h-3 w-3 text-cyan-400" />
                      <span className="text-[10px] font-semibold text-cyan-400 uppercase">{dm.workflows.length} Workflow Step{dm.workflows.length !== 1 ? 's' : ''}</span>
                    </div>
                    <div className="space-y-0.5">
                      {dm.workflows.sort((a, b) => a.step_number - b.step_number).map((w, i) => (
                        <div key={i} className="text-xs text-gray-300 truncate">
                          <span className="text-gray-500 font-mono">{w.step_number}.</span> {w.name}
                        </div>
                      ))}
                    </div>
                  </div>
                )}
                {dm.decisions.length > 0 && (
                  <div className="p-2 rounded-lg bg-gray-700/30">
                    <div className="flex items-center gap-1.5 mb-1">
                      <AlertTriangle className="h-3 w-3 text-yellow-400" />
                      <span className="text-[10px] font-semibold text-yellow-400 uppercase">{dm.decisions.length} Decision{dm.decisions.length !== 1 ? 's' : ''}</span>
                    </div>
                    <div className="space-y-0.5">
                      {dm.decisions.map((d, i) => (
                        <div key={i} className="text-xs text-gray-300 truncate">{d}</div>
                      ))}
                    </div>
                  </div>
                )}
              </div>

              {dm.entities.length > 0 && (
                <div>
                  <div className="text-[10px] text-gray-500 mb-1">Key Entities</div>
                  <div className="flex flex-wrap gap-1">
                    {dm.entities.slice(0, 12).map((e, i) => (
                      <span key={i} className="text-[10px] px-1.5 py-0.5 rounded bg-gray-700/50 text-gray-400">{e}</span>
                    ))}
                    {dm.entities.length > 12 && (
                      <span className="text-[10px] text-gray-600">+{dm.entities.length - 12} more</span>
                    )}
                  </div>
                </div>
              )}
            </div>

            {/* Grounding Contract */}
            <GroundingSummary contract={gc} />

            {/* System Prompt Preview */}
            <div className="rounded-xl border border-white/10 bg-gray-800/50 p-4">
              <h3 className="text-xs font-semibold text-gray-500 uppercase tracking-wider mb-2 flex items-center gap-1.5">
                <FileText className="h-3.5 w-3.5 text-nexus-400" /> System Prompt
              </h3>
              <pre className="text-[10px] text-gray-400 whitespace-pre-wrap font-mono leading-relaxed max-h-40 overflow-y-auto custom-scrollbar">
                {persona.system_prompt}
              </pre>
            </div>
          </div>
        </div>

        {/* ── CENTER PANEL: Process Map ────────────────────── */}
        <div className="flex-1 overflow-y-auto custom-scrollbar">
          <div className="p-6 space-y-6">
            {/* Workflow steps */}
            <div>
              <h2 className="text-sm font-bold text-white uppercase tracking-wider mb-4 flex items-center gap-2">
                <GitBranch className="h-4 w-4 text-nexus-400" />
                Process Flow ({dm.workflows.length} steps)
              </h2>
              {dm.workflows.length > 0 ? (
                <div className="space-y-3">
                  {dm.workflows
                    .sort((a, b) => a.step_number - b.step_number)
                    .map((wf, i) => (
                      <WorkflowStepCard
                        key={wf.step_number}
                        step={wf}
                        isLast={i === dm.workflows.length - 1}
                      />
                    ))}
                </div>
              ) : (
                <div className="text-center py-12 text-gray-600">
                  <GitBranch className="h-8 w-8 mx-auto mb-2 opacity-50" />
                  <p className="text-sm">No workflow steps extracted</p>
                </div>
              )}
            </div>

            {/* Actors & Systems detail */}
            {(dm.actors.length > 0 || dm.systems.length > 0) && (
              <div className="grid grid-cols-2 gap-4">
                {/* Actors */}
                {dm.actors.length > 0 && (
                  <div>
                    <h3 className="text-xs font-semibold text-gray-500 uppercase tracking-wider mb-3 flex items-center gap-1.5">
                      <Users className="h-3.5 w-3.5 text-blue-400" /> Actors ({dm.actors.length})
                    </h3>
                    <div className="space-y-2">
                      {dm.actors.map((actor, i) => (
                        <div key={i} className="rounded-lg border border-white/10 bg-gray-800/40 p-3">
                          <div className="text-sm font-semibold text-white">{actor.name}</div>
                          <div className="text-xs text-gray-400 mt-0.5">{actor.role}</div>
                          <div className="text-[10px] text-gray-600 mt-1">{actor.evidence.length} citation{actor.evidence.length !== 1 ? 's' : ''}</div>
                        </div>
                      ))}
                    </div>
                  </div>
                )}

                {/* Systems */}
                {dm.systems.length > 0 && (
                  <div>
                    <h3 className="text-xs font-semibold text-gray-500 uppercase tracking-wider mb-3 flex items-center gap-1.5">
                      <Monitor className="h-3.5 w-3.5 text-purple-400" /> Systems ({dm.systems.length})
                    </h3>
                    <div className="space-y-2">
                      {dm.systems.map((sys, i) => (
                        <div key={i} className="rounded-lg border border-white/10 bg-gray-800/40 p-3">
                          <div className="text-sm font-semibold text-white">{sys.name}</div>
                          <div className="text-xs text-gray-400 mt-0.5">{sys.purpose}</div>
                          <div className="text-[10px] text-gray-600 mt-1">{sys.evidence.length} citation{sys.evidence.length !== 1 ? 's' : ''}</div>
                        </div>
                      ))}
                    </div>
                  </div>
                )}
              </div>
            )}

            {/* Risks */}
            {dm.risks.length > 0 && (
              <div>
                <h3 className="text-xs font-semibold text-gray-500 uppercase tracking-wider mb-3 flex items-center gap-1.5">
                  <AlertTriangle className="h-3.5 w-3.5 text-orange-400" /> Identified Risks ({dm.risks.length})
                </h3>
                <div className="space-y-2">
                  {dm.risks.map((risk, i) => (
                    <RiskCard key={i} risk={risk} />
                  ))}
                </div>
              </div>
            )}

            {/* Unknowns */}
            {dm.unknowns.length > 0 && (
              <div>
                <h3 className="text-xs font-semibold text-gray-500 uppercase tracking-wider mb-3 flex items-center gap-1.5">
                  <HelpCircle className="h-3.5 w-3.5 text-gray-400" /> Unknowns ({dm.unknowns.length})
                </h3>
                <div className="space-y-1.5">
                  {dm.unknowns.map((u, i) => (
                    <div key={i} className="text-xs text-gray-400 flex items-start gap-2 p-2 rounded-lg bg-gray-800/30">
                      <span className="text-gray-600 shrink-0">?</span>
                      {u}
                    </div>
                  ))}
                </div>
              </div>
            )}
          </div>
        </div>

        {/* ── RIGHT PANEL: Evidence Rail ────────────────────── */}
        <div className="w-80 shrink-0 border-l border-white/10 overflow-y-auto custom-scrollbar">
          <div className="p-4 space-y-4">
            <div className="flex items-center justify-between">
              <h2 className="text-xs font-bold text-gray-500 uppercase tracking-wider flex items-center gap-1.5">
                <FileText className="h-3.5 w-3.5 text-nexus-400" /> Evidence Rail
              </h2>
              <span className="text-[10px] text-gray-600 font-mono">{allEvidence.length} total</span>
            </div>

            {allEvidence.length > 0 ? (
              <div className="space-y-4">
                <EvidenceGroup
                  label="Actors"
                  icon={<Users className="inline h-3 w-3" />}
                  citations={evidenceGroups.actors}
                  startIndex={0}
                  colorClass="text-blue-400"
                />
                <EvidenceGroup
                  label="Systems"
                  icon={<Monitor className="inline h-3 w-3" />}
                  citations={evidenceGroups.systems}
                  startIndex={evidenceGroups.actors.length}
                  colorClass="text-purple-400"
                />
                <EvidenceGroup
                  label="Workflows"
                  icon={<GitBranch className="inline h-3 w-3" />}
                  citations={evidenceGroups.workflows}
                  startIndex={evidenceGroups.actors.length + evidenceGroups.systems.length}
                  colorClass="text-nexus-400"
                />
                <EvidenceGroup
                  label="Risks"
                  icon={<AlertTriangle className="inline h-3 w-3" />}
                  citations={evidenceGroups.risks}
                  startIndex={evidenceGroups.actors.length + evidenceGroups.systems.length + evidenceGroups.workflows.length}
                  colorClass="text-orange-400"
                />
              </div>
            ) : (
              <div className="text-center py-12 text-gray-600">
                <FileText className="h-8 w-8 mx-auto mb-2 opacity-50" />
                <p className="text-xs">No evidence citations</p>
              </div>
            )}

            {/* Provenance footer */}
            <div className="rounded-xl border border-white/10 bg-gray-800/50 p-3 space-y-1.5">
              <h4 className="text-[10px] font-semibold text-gray-500 uppercase tracking-wider">Provenance</h4>
              <div className="space-y-1 text-[10px] text-gray-400">
                <div className="flex justify-between">
                  <span>Artifact</span>
                  <span className="font-mono text-gray-500">{provenance.artifact_id.slice(0, 8)}...</span>
                </div>
                <div className="flex justify-between">
                  <span>Quality Score</span>
                  <span className={clsx('font-mono', confidenceColor(provenance.artifact_quality_score))}>
                    {Math.round(provenance.artifact_quality_score * 100)}%
                  </span>
                </div>
                <div className="flex justify-between">
                  <span>Model</span>
                  <span className="font-mono text-gray-500">{provenance.model_used}</span>
                </div>
                <div className="flex justify-between">
                  <span>Backend</span>
                  <span className="font-mono text-gray-500">{provenance.model_backend}</span>
                </div>
                <div className="flex justify-between">
                  <span>Generated</span>
                  <span className="font-mono text-gray-500">
                    {new Date(provenance.generated_at).toLocaleString()}
                  </span>
                </div>
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
