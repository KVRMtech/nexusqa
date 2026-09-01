// ═══════════════════════════════════════════════════════════════
//  Co-Architect Dock (P4) — visual-graph-constrained chat panel
//
//  Mounts as a slide-in panel inside the Test Studio.  Holds the
//  conversation history in component-local state (no DB persistence
//  yet — by design, P4 MVP).  Submits the entire history every turn
//  because the Heart endpoint is stateless.
// ═══════════════════════════════════════════════════════════════
import { useCallback, useEffect, useRef, useState } from 'react';
import clsx from 'clsx';
import {
  X,
  Send,
  Loader2,
  Sparkles,
  AlertTriangle,
  CheckCircle2,
  Target,
  Link2,
  ArrowRight,
  Bot,
  User as UserIcon,
} from 'lucide-react';
import api from '../services/api';
import type {
  CoArchitectMessage,
  CoArchitectProposedScenario,
  CoArchitectChatResponse,
} from '../types/canonical';

interface CoArchitectDockProps {
  artifactId: string;
  sessionId?: string;
  open: boolean;
  onClose: () => void;
  /** Called after proposals are successfully committed so the parent can
   *  refresh the scenario list. */
  onProposalsCommitted: () => void;
}

interface UiMessage extends CoArchitectMessage {
  /** Per-message identifier so React can key reliably. */
  id: string;
  /** Per-assistant-turn: proposed scenarios extracted by the backend. */
  proposed?: CoArchitectProposedScenario[];
  /** Per-assistant-turn: backend parse_warning surfaced inline. */
  warning?: string | null;
  /** Per-assistant-turn: latency/model metadata for trust display. */
  meta?: { model: string; latencyMs: number };
}

const SAMPLE_PROMPTS = [
  'Summarise the visual graph in 2 sentences.',
  'Which scenes are likely to break if the UI changes?',
  'Propose 2 boundary tests grounded in the visual graph.',
  'Are there any error scenes I should write tests for?',
];

export function CoArchitectDock({
  artifactId,
  sessionId,
  open,
  onClose,
  onProposalsCommitted,
}: CoArchitectDockProps) {
  const [messages, setMessages] = useState<UiMessage[]>([]);
  const [draft, setDraft] = useState('');
  const [proposeMode, setProposeMode] = useState(false);
  const [pending, setPending] = useState(false);
  const [committing, setCommitting] = useState<string | null>(null);
  const [committedTitles, setCommittedTitles] = useState<Set<string>>(new Set());
  const [error, setError] = useState<string | null>(null);
  const scrollerRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);

  // Auto-scroll on new messages
  useEffect(() => {
    if (open && scrollerRef.current) {
      scrollerRef.current.scrollTop = scrollerRef.current.scrollHeight;
    }
  }, [messages, open]);

  // Auto-focus input when dock opens
  useEffect(() => {
    if (open) {
      const id = window.setTimeout(() => inputRef.current?.focus(), 50);
      return () => window.clearTimeout(id);
    }
  }, [open]);

  const send = useCallback(async () => {
    const body = draft.trim();
    if (!body || pending) return;
    setError(null);
    const userMsg: UiMessage = {
      id: `u_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`,
      role: 'user',
      content: body,
    };
    const nextMessages = [...messages, userMsg];
    setMessages(nextMessages);
    setDraft('');
    setPending(true);

    try {
      const wireMessages = nextMessages.map(({ role, content }) => ({ role, content }));
      const resp: CoArchitectChatResponse = await api.coArchitectChat(
        artifactId,
        wireMessages,
        { proposeScenarios: proposeMode, sessionId },
      );
      const assistantMsg: UiMessage = {
        id: `a_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`,
        role: 'assistant',
        content: resp.response,
        proposed: resp.proposed_scenarios ?? [],
        warning: resp.parse_warning,
        meta: { model: resp.model_used, latencyMs: resp.latency_ms },
      };
      setMessages(prev => [...prev, assistantMsg]);
    } catch (err: unknown) {
      const detail = (err as { response?: { data?: { detail?: unknown } } })
        ?.response?.data?.detail;
      let detailMsg: string;
      if (typeof detail === 'string') detailMsg = detail;
      else if (detail && typeof detail === 'object') {
        detailMsg = (detail as { message?: string }).message
          ?? JSON.stringify(detail);
      } else if (err instanceof Error) detailMsg = err.message;
      else detailMsg = 'Co-Architect chat failed';
      setError(detailMsg);
    } finally {
      setPending(false);
    }
  }, [artifactId, sessionId, draft, messages, proposeMode, pending]);

  const commitProposal = useCallback(async (scenario: CoArchitectProposedScenario) => {
    if (committing) return;
    setCommitting(scenario.title);
    setError(null);
    try {
      const resp = await api.commitCoArchitectProposals(artifactId, [scenario]);
      if (resp.committed.length === 0) {
        setError(
          `Could not commit "${scenario.title}" — ${resp.dropped[0]?.reason ?? 'unknown reason'}`,
        );
        return;
      }
      setCommittedTitles(prev => new Set(prev).add(scenario.title));
      onProposalsCommitted();
    } catch (err: unknown) {
      const detail = (err as { response?: { data?: { detail?: unknown } } })
        ?.response?.data?.detail;
      let msg: string;
      if (typeof detail === 'string') msg = detail;
      else if (detail && typeof detail === 'object') {
        msg = (detail as { message?: string }).message
          ?? JSON.stringify(detail);
      } else if (err instanceof Error) msg = err.message;
      else msg = 'Commit failed';
      setError(msg);
    } finally {
      setCommitting(null);
    }
  }, [artifactId, committing, onProposalsCommitted]);

  if (!open) return null;

  return (
    <div className="absolute inset-y-0 right-0 z-40 flex w-full sm:w-[440px] flex-col bg-white border-l border-gray-200 shadow-xl">
      {/* Header */}
      <div className="shrink-0 flex items-center gap-2 border-b border-gray-200 bg-gradient-to-r from-violet-50 to-blue-50 px-4 py-3">
        <div className="flex h-7 w-7 items-center justify-center rounded-full bg-violet-500 text-white">
          <Bot className="h-4 w-4" />
        </div>
        <div className="flex-1 min-w-0">
          <h2 className="text-sm font-semibold text-slate-800">Co-Architect</h2>
          <p className="text-[10px] text-slate-500">
            Visual-graph-grounded · cannot hallucinate scenes/controls
          </p>
        </div>
        <button
          onClick={onClose}
          className="text-slate-500 hover:text-slate-700"
          aria-label="Close Co-Architect"
        >
          <X className="h-4 w-4" />
        </button>
      </div>

      {/* Messages */}
      <div
        ref={scrollerRef}
        className="flex-1 min-h-0 overflow-y-auto p-4 space-y-3 bg-[#fafafa]"
      >
        {messages.length === 0 && (
          <div className="rounded-lg border border-dashed border-gray-300 bg-white p-3 space-y-2">
            <p className="text-[11px] text-slate-600 leading-relaxed">
              Ask anything about the visual evidence graph. The agent is constrained
              to scenes, controls, and flow edges it can see — it can't reference
              transcripts or invent UI it didn't observe.
            </p>
            <div className="space-y-1">
              <p className="text-[9px] uppercase tracking-wider text-slate-400">
                Try
              </p>
              {SAMPLE_PROMPTS.map(p => (
                <button
                  key={p}
                  onClick={() => setDraft(p)}
                  className="block w-full text-left text-[10px] text-slate-600 hover:text-slate-900 hover:bg-slate-50 rounded px-2 py-1 border border-gray-100"
                >
                  {p}
                </button>
              ))}
            </div>
          </div>
        )}

        {messages.map(msg => (
          <ChatBubble
            key={msg.id}
            msg={msg}
            onCommit={commitProposal}
            committingTitle={committing}
            committedTitles={committedTitles}
          />
        ))}

        {pending && (
          <div className="flex items-center gap-2 text-[11px] text-slate-500">
            <Loader2 className="h-3.5 w-3.5 animate-spin" />
            Thinking…
          </div>
        )}

        {error && (
          <div className="rounded-md border border-red-300 bg-red-50 px-3 py-2 flex items-start gap-2">
            <AlertTriangle className="h-3.5 w-3.5 text-red-500 mt-0.5 shrink-0" />
            <div className="flex-1">
              <p className="text-[11px] font-medium text-red-700">Error</p>
              <p className="text-[10px] text-red-600 whitespace-pre-wrap">{error}</p>
            </div>
            <button
              onClick={() => setError(null)}
              className="text-red-500 hover:text-red-700 text-[10px]"
            >
              ✕
            </button>
          </div>
        )}
      </div>

      {/* Composer */}
      <div className="shrink-0 border-t border-gray-200 bg-white px-3 py-2 space-y-2">
        <label className="flex items-center gap-2 text-[10px] text-slate-600 cursor-pointer">
          <input
            type="checkbox"
            checked={proposeMode}
            onChange={e => setProposeMode(e.target.checked)}
            className="rounded border-gray-300"
          />
          <Sparkles className="h-3 w-3 text-violet-500" />
          Propose grounded scenarios (returns structured JSON)
        </label>
        <div className="flex items-end gap-2">
          <textarea
            ref={inputRef}
            value={draft}
            onChange={e => setDraft(e.target.value)}
            onKeyDown={e => {
              if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault();
                void send();
              }
            }}
            placeholder={
              proposeMode
                ? 'Describe what scenarios you want the agent to propose…'
                : 'Ask the Co-Architect anything about the visual graph…'
            }
            rows={2}
            maxLength={20000}
            disabled={pending}
            className="flex-1 rounded-md border border-gray-200 bg-white px-2 py-1.5 text-[11px] text-slate-700 placeholder-slate-400 focus:border-violet-500/50 focus:outline-none resize-none disabled:opacity-50"
          />
          <button
            onClick={() => void send()}
            disabled={!draft.trim() || pending}
            className="shrink-0 rounded-md bg-violet-600 text-white p-1.5 hover:bg-violet-500 disabled:opacity-50 disabled:cursor-not-allowed"
            aria-label="Send"
          >
            {pending ? <Loader2 className="h-4 w-4 animate-spin" /> : <Send className="h-4 w-4" />}
          </button>
        </div>
      </div>
    </div>
  );
}

// ═══════════════════════════════════════════════════════════════
//  Sub-components
// ═══════════════════════════════════════════════════════════════

function ChatBubble({
  msg,
  onCommit,
  committingTitle,
  committedTitles,
}: {
  msg: UiMessage;
  onCommit: (scenario: CoArchitectProposedScenario) => Promise<void>;
  committingTitle: string | null;
  committedTitles: Set<string>;
}) {
  const isUser = msg.role === 'user';
  return (
    <div className={clsx('flex gap-2', isUser ? 'justify-end' : 'justify-start')}>
      {!isUser && (
        <div className="shrink-0 flex h-6 w-6 items-center justify-center rounded-full bg-violet-100 text-violet-700">
          <Bot className="h-3.5 w-3.5" />
        </div>
      )}
      <div className={clsx('max-w-[90%] space-y-1.5', isUser && 'order-1')}>
        <div
          className={clsx(
            'rounded-lg px-3 py-2 text-[11px] whitespace-pre-wrap break-words',
            isUser
              ? 'bg-violet-600 text-white'
              : 'bg-white border border-gray-200 text-slate-700',
          )}
        >
          {msg.content}
        </div>

        {/* Per-assistant-turn metadata + warning */}
        {!isUser && msg.warning && (
          <p className="text-[9px] text-amber-600 flex items-start gap-1 px-1">
            <AlertTriangle className="h-2.5 w-2.5 mt-0.5 shrink-0" />
            {msg.warning}
          </p>
        )}
        {!isUser && msg.meta && (
          <p className="text-[9px] text-slate-400 px-1 font-mono">
            {msg.meta.model || 'llm'} · {msg.meta.latencyMs.toFixed(0)}ms
          </p>
        )}

        {/* Proposed scenarios */}
        {!isUser && msg.proposed && msg.proposed.length > 0 && (
          <div className="space-y-1.5">
            {msg.proposed.map(scenario => (
              <ProposedScenarioCard
                key={scenario.title}
                scenario={scenario}
                onCommit={onCommit}
                committing={committingTitle === scenario.title}
                committed={committedTitles.has(scenario.title)}
              />
            ))}
          </div>
        )}
      </div>
      {isUser && (
        <div className="shrink-0 flex h-6 w-6 items-center justify-center rounded-full bg-slate-200 text-slate-600 order-2">
          <UserIcon className="h-3.5 w-3.5" />
        </div>
      )}
    </div>
  );
}

function ProposedScenarioCard({
  scenario,
  onCommit,
  committing,
  committed,
}: {
  scenario: CoArchitectProposedScenario;
  onCommit: (scenario: CoArchitectProposedScenario) => Promise<void>;
  committing: boolean;
  committed: boolean;
}) {
  const fullyGrounded = scenario.steps.every(
    s => s.evidence_scene_id && s.evidence_control_id,
  );
  return (
    <div className="rounded-md border border-violet-300 bg-violet-50/40 px-3 py-2 space-y-1.5">
      <div className="flex items-start gap-2">
        <Sparkles className="h-3 w-3 text-violet-500 mt-0.5 shrink-0" />
        <p className="text-[11px] font-semibold text-violet-800 flex-1">
          {scenario.title}
        </p>
      </div>
      {scenario.rationale && (
        <p className="text-[10px] text-violet-700/80 italic">{scenario.rationale}</p>
      )}
      <div className="space-y-0.5">
        {scenario.steps.map(step => (
          <div key={step.step_number} className="text-[10px] text-slate-700 flex items-baseline gap-1.5">
            <span className="font-mono text-slate-400 shrink-0">{step.step_number}.</span>
            <span className="flex-1">{step.action}</span>
            <span className="inline-flex items-center gap-0.5 text-[9px] text-slate-500 shrink-0">
              <Link2 className="h-2 w-2" />
              {step.evidence_scene_id.slice(0, 8)}
            </span>
            <span className="inline-flex items-center gap-0.5 text-[9px] text-nexus-600 shrink-0">
              <Target className="h-2 w-2" />
              {step.evidence_control_id.slice(0, 8)}
            </span>
            {step.evidence_edge_id && (
              <span className="inline-flex items-center gap-0.5 text-[9px] text-purple-600 shrink-0">
                <ArrowRight className="h-2 w-2" />
                {step.evidence_edge_id.slice(0, 8)}
              </span>
            )}
          </div>
        ))}
      </div>
      <div className="flex items-center justify-between pt-1">
        <span
          className={clsx(
            'text-[9px] inline-flex items-center gap-1 rounded-full border px-1.5 py-0',
            fullyGrounded
              ? 'border-emerald-300 bg-emerald-50 text-emerald-700'
              : 'border-amber-300 bg-amber-50 text-amber-700',
          )}
        >
          {fullyGrounded ? <CheckCircle2 className="h-2.5 w-2.5" /> : <AlertTriangle className="h-2.5 w-2.5" />}
          {scenario.steps.length} step{scenario.steps.length !== 1 ? 's' : ''} grounded
        </span>
        {committed ? (
          <span className="text-[10px] text-emerald-700 inline-flex items-center gap-1">
            <CheckCircle2 className="h-3 w-3" /> Added to plan (draft)
          </span>
        ) : (
          <button
            onClick={() => void onCommit(scenario)}
            disabled={committing || !fullyGrounded}
            className="rounded-md bg-violet-600 text-white text-[10px] px-2.5 py-1 hover:bg-violet-500 disabled:opacity-50 disabled:cursor-not-allowed"
          >
            {committing ? 'Adding…' : 'Add to Test Plan'}
          </button>
        )}
      </div>
    </div>
  );
}
