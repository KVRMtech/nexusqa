import { useCallback, useEffect, useState } from 'react';
import { Camera, Check, Copy, Download, Eye, Flag, Layers, Loader2, RefreshCw, ShieldAlert } from 'lucide-react';
import { api } from '../services/api';

// Verdict styling + which stack a failure belongs to. Mirrors the backend
// classify_failure labels; real_regression / needs_review = "need you".
export const VERDICT: Record<string, { label: string; bg: string; fg: string; needYou: boolean }> = {
  real_regression: { label: 'Real regression', bg: 'rgba(239,68,68,0.14)', fg: '#b91c1c', needYou: true },
  needs_review: { label: 'Needs review', bg: 'rgba(245,158,11,0.16)', fg: '#b45309', needYou: true },
  selector_drift: { label: 'Selector drift', bg: 'rgba(56,189,248,0.14)', fg: '#0369a1', needYou: false },
  visual_change: { label: 'Visual change', bg: 'rgba(139,92,246,0.14)', fg: '#6d28d9', needYou: false },
  flake: { label: 'Flake', bg: 'rgba(100,116,139,0.14)', fg: '#475569', needYou: false },
  passed: { label: 'Passed', bg: 'rgba(34,197,94,0.14)', fg: '#15803d', needYou: false },
};

export interface Scenario {
  scenario_id: string; name: string; type: string; status: string;
  verdict: string; confidence: number; justification: string;
  step_number: number | null; baseline_screenshot: string; actual_screenshot: string;
  expected: string; error_message: string; is_flaky: boolean; flake_rate_pct: number;
}

// Client-side defect classification (E1 — no DB column): maps the deterministic
// triage verdict to a filing-ready defect type for the exported markdown.
const DEFECT_TYPE: Record<string, { label: string; bg: string; fg: string }> = {
  real_regression: { label: 'Product Bug', bg: 'rgba(239,68,68,0.12)', fg: '#b91c1c' },
  needs_review: { label: 'To Investigate', bg: 'rgba(245,158,11,0.14)', fg: '#b45309' },
  selector_drift: { label: 'Automation Bug', bg: 'rgba(56,189,248,0.12)', fg: '#0369a1' },
  visual_change: { label: 'UI Change', bg: 'rgba(139,92,246,0.12)', fg: '#6d28d9' },
  flake: { label: 'Flaky / No Defect', bg: 'rgba(100,116,139,0.12)', fg: '#475569' },
};

// A ready-to-file, evidence-rich defect built entirely from grounded triage
// fields — paste-ready into Jira / Linear / Xray.
export function defectMarkdown(s: Scenario): string {
  const dt = DEFECT_TYPE[s.verdict]?.label || 'To Investigate';
  const conf = Math.round((s.confidence || 0) * 100);
  const baseImg = s.baseline_screenshot ? api.getFrameImageUrl(s.baseline_screenshot) : '';
  return [
    `# [${dt}] ${s.name}`,
    ``,
    `**Verdict:** ${s.verdict}  ·  **Confidence:** ${conf}%  ·  **Type:** ${dt}`,
    `**Failed at:** step ${s.step_number ?? '—'}${s.is_flaky ? `  ·  flaky ${s.flake_rate_pct}%` : ''}`,
    `**Environment:** nexus-runner`,
    ``,
    `## What happened`,
    s.justification || '(no justification)',
    ``,
    `## Expected vs Observed`,
    `- **Expected:** ${s.expected || '(recorded baseline)'}`,
    `- **Observed:** failed at step ${s.step_number ?? '—'}`,
    ``,
    `## Evidence`,
    `- Known-good baseline: ${baseImg || '(none)'}`,
    `- This run: ${s.actual_screenshot || '(awaiting capture)'}`,
    ``,
    `## Error`,
    '```',
    s.error_message || '(no error message)',
    '```',
    ``,
    `_Grounded by Nexus — paste into Jira / Linear / Xray._`,
  ].join('\n');
}

export function downloadDefect(s: Scenario) {
  const blob = new Blob([defectMarkdown(s)], { type: 'text/markdown' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url; a.download = `defect-${(s.scenario_id || 'x').slice(0, 12)}.md`;
  a.click(); URL.revokeObjectURL(url);
}

export default function TriagePanel({ artifactId, scopeIds }: { artifactId: string; scopeIds?: string[] }) {
  const [data, setData] = useState<any>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    setLoading(true); setError(null);
    try { setData(await api.getTriage(artifactId)); }
    catch (e: any) { setError(e?.response?.data?.detail || String(e)); }
    finally { setLoading(false); }
  }, [artifactId]);
  useEffect(() => { void refresh(); }, [refresh]);

  const board = data?.board || { total: 0, failures: 0, need_you: 0, dont_need_you: 0, by_label: {} };
  const allScenarios: Scenario[] = (data?.scenarios || []).filter((s: Scenario) => s.verdict !== 'passed');
  // Scope the board to the currently-selected scripts so e.g. selecting only
  // "Demonstrated" shows just its result, not every test that ever ran (the
  // backend returns the latest run PER scenario). Empty/undefined scope = show all.
  const scenarios = (scopeIds && scopeIds.length)
    ? allScenarios.filter((s) => scopeIds.includes(s.scenario_id))
    : allScenarios;
  const needYou = scenarios.filter((s) => VERDICT[s.verdict]?.needYou ?? true).length;
  const byLabel: Record<string, number> = {};
  for (const s of scenarios) byLabel[s.verdict] = (byLabel[s.verdict] || 0) + 1;

  return (
    <section className="rounded-2xl p-4"
      style={{ background: 'linear-gradient(135deg, rgba(99,102,241,0.05), rgba(244,63,94,0.04))', border: '1px solid rgba(99,102,241,0.22)' }}>
      <div className="flex items-center gap-2 mb-3 flex-wrap">
        <ShieldAlert className="h-4 w-4" style={{ color: '#4f46e5' }} />
        <span className="text-[13px] font-black text-slate-900">Grounded Triage</span>
        <span className="text-[10px] text-slate-500 font-semibold">baseline-vs-actual + a verdict per failure</span>
        <button onClick={refresh} disabled={loading}
          className="ml-auto flex items-center gap-1 rounded-md px-2 py-1 text-[10px] font-semibold bg-slate-100 text-slate-700 hover:bg-slate-200 disabled:opacity-50">
          {loading ? <Loader2 className="h-3 w-3 animate-spin" /> : <RefreshCw className="h-3 w-3" />} Refresh
        </button>
      </div>

      {error && <div className="rounded-lg border border-amber-300 bg-amber-50 px-3 py-2 text-[11px] text-amber-800">{error}</div>}

      {scenarios.length === 0 ? (
        <div className="rounded-xl border border-dashed border-slate-300 bg-white/60 px-4 py-8 text-center">
          <p className="text-sm font-semibold text-slate-600">{board.total > 0 ? 'No results for the selected scripts yet' : 'No runs yet'}</p>
          <p className="text-[11px] text-slate-400 mt-1 max-w-md mx-auto leading-relaxed">
            {board.total > 0
              ? 'Run the selected script(s), or select more categories above, to see their grounded triage here.'
              : 'Run a script (or wire the bundled Nexus reporter) and each failure appears here — classified and shown beside its known-good baseline.'}
          </p>
        </div>
      ) : (
        <>
          <div className="flex items-center gap-2 flex-wrap mb-3 text-[11px] font-bold">
            <span className="rounded-full px-2.5 py-1" style={{ background: 'rgba(239,68,68,0.14)', color: '#b91c1c' }}>{needYou} need you</span>
            <span className="rounded-full px-2.5 py-1" style={{ background: 'rgba(100,116,139,0.12)', color: '#475569' }}>{scenarios.length - needYou} don't need you</span>
            <span className="text-slate-400 font-semibold">· {scenarios.length} failing</span>
            {Object.entries(byLabel).filter(([k]) => k !== 'passed').map(([k, n]) => {
              const v = VERDICT[k] || { label: k, fg: '#475569', bg: 'rgba(100,116,139,0.12)' };
              return <span key={k} className="rounded px-1.5 py-0.5 text-[10px]" style={{ background: v.bg, color: v.fg }}>{n as number} {v.label}</span>;
            })}
          </div>
          <div className="space-y-2">
            {scenarios.map((s) => <TriageCard key={s.scenario_id} s={s} artifactId={artifactId} />)}
          </div>
        </>
      )}
    </section>
  );
}

function TriageCard({ s, artifactId }: { s: Scenario; artifactId: string }) {
  const v = VERDICT[s.verdict] || { label: s.verdict, bg: 'rgba(100,116,139,0.12)', fg: '#475569', needYou: true };
  return (
    <div className="rounded-xl p-3" style={{ background: 'rgba(255,255,255,0.85)', border: `1px solid ${v.fg}33` }}>
      <div className="flex items-center gap-2 mb-2 flex-wrap">
        <span className="rounded px-2 py-0.5 text-[9px] font-black uppercase" style={{ background: v.bg, color: v.fg }}>{v.label}</span>
        {DEFECT_TYPE[s.verdict] ? (
          <span className="rounded px-1.5 py-0.5 text-[9px] font-bold" style={{ background: DEFECT_TYPE[s.verdict].bg, color: DEFECT_TYPE[s.verdict].fg }}>{DEFECT_TYPE[s.verdict].label}</span>
        ) : null}
        <span className="text-[12px] font-bold text-slate-900 break-words">{s.name}</span>
        <span className="ml-auto shrink-0 text-[9px] text-slate-400 font-semibold">
          {Math.round((s.confidence || 0) * 100)}% · step {s.step_number ?? '—'}{s.is_flaky ? ` · flaky ${s.flake_rate_pct}%` : ''}
        </span>
      </div>
      <p className="text-[11px] text-slate-700 mb-2 leading-snug">{s.justification}</p>
      {s.expected && <p className="text-[10px] text-slate-500 mb-2">Expected: {s.expected}</p>}
      <div className="grid grid-cols-2 gap-2">
        <Frame label="Known-good (recorded)" src={s.baseline_screenshot ? api.getFrameImageUrl(s.baseline_screenshot) : ''} />
        <Frame label="This run" src={s.actual_screenshot ? api.getRunScreenshotUrl(s.actual_screenshot) : ''} awaiting={!s.actual_screenshot} />
      </div>
      {s.error_message && (
        <p className="mt-2 text-[10px] font-mono text-rose-600 break-words" style={{ maxHeight: '3em', overflow: 'hidden' }}>{s.error_message}</p>
      )}
      <div className="mt-2 flex items-center gap-2">
        <button onClick={() => { void navigator.clipboard?.writeText(defectMarkdown(s)).catch(() => {}); }}
          title="Copy a ready-to-file, evidence-rich defect (markdown)"
          className="flex items-center gap-1 rounded-md px-2 py-1 text-[10px] font-semibold bg-slate-100 text-slate-700 hover:bg-slate-200">
          <Copy className="h-3 w-3" /> Copy defect
        </button>
        <button onClick={() => downloadDefect(s)}
          className="flex items-center gap-1 rounded-md px-2 py-1 text-[10px] font-semibold bg-slate-100 text-slate-700 hover:bg-slate-200">
          <Download className="h-3 w-3" /> Download .md
        </button>
      </div>
      <div className="mt-2 pt-2 border-t border-slate-100 space-y-2">
        <AdvisoryChecks artifactId={artifactId} s={s} />
        <VerdictFeedback artifactId={artifactId} s={s} />
      </div>
    </div>
  );
}

// ADVISORY oracle signals (Phase 2) — a deterministic pixel diff ($0, Pillow) and
// an optional VLM semantic check. Both are ON-DEMAND (never auto-batched — avoids a
// vision-cost burst) and NEVER change the verdict; they only add context beside the
// baseline/this-run frames. Endpoints degrade to available:false / 'uncertain'.
function AdvisoryChecks({ artifactId, s }: { artifactId: string; s: Scenario }) {
  const step = s.step_number ?? 0;
  const [pix, setPix] = useState<any>(null);
  const [pixBusy, setPixBusy] = useState(false);
  const [sem, setSem] = useState<any>(null);
  const [semBusy, setSemBusy] = useState(false);

  const runPix = async () => {
    setPixBusy(true);
    try { setPix(await api.getVisualDiff(artifactId, s.scenario_id, step)); }
    catch { setPix({ available: false, reason: 'check failed' }); }
    finally { setPixBusy(false); }
  };
  const runSem = async () => {
    setSemBusy(true);
    try { setSem(await api.semanticCheck(artifactId, s.scenario_id, step)); }
    catch { setSem({ semantic_match: 'uncertain', reason: 'check failed' }); }
    finally { setSemBusy(false); }
  };

  const pixLabel = () => {
    if (!pix) return null;
    if (!pix.available) return <span className="text-[9px] text-slate-400">{pix.reason || 'no images'}</span>;
    if (pix.identical) return <span className="text-[9px] font-bold text-emerald-600">pixel-identical</span>;
    const pct = Math.round((pix.changed_ratio || 0) * 1000) / 10;
    return <span className="text-[9px] font-bold text-violet-700">{pct}% changed · advisory</span>;
  };
  const semLabel = () => {
    if (!sem) return null;
    const m = sem.semantic_match || 'uncertain';
    const col = m === 'match' ? '#15803d' : m === 'deviation' ? '#b45309' : '#64748b';
    return <span className="text-[9px] font-bold" style={{ color: col }} title={sem.reason || ''}>semantic: {m} · advisory</span>;
  };

  return (
    <div className="flex items-center gap-2 flex-wrap">
      <span className="text-[9px] uppercase font-bold text-slate-400">Advisory</span>
      <button disabled={pixBusy} onClick={runPix}
        title="Deterministic pixel diff of recorded baseline vs this run — a badge, never a verdict"
        className="flex items-center gap-1 rounded-md px-2 py-1 text-[10px] font-semibold bg-violet-50 text-violet-700 hover:bg-violet-100 disabled:opacity-50">
        {pixBusy ? <Loader2 className="h-3 w-3 animate-spin" /> : <Layers className="h-3 w-3" />} Pixel diff
      </button>
      {pixLabel()}
      <button disabled={semBusy} onClick={runSem}
        title="Optional VLM check: does the screen still satisfy the recorded outcome? Signal only, never a verdict"
        className="flex items-center gap-1 rounded-md px-2 py-1 text-[10px] font-semibold bg-sky-50 text-sky-700 hover:bg-sky-100 disabled:opacity-50">
        {semBusy ? <Loader2 className="h-3 w-3 animate-spin" /> : <Eye className="h-3 w-3" />} Semantic check
      </button>
      {semLabel()}
    </div>
  );
}

// The DIRECT oracle label: a human confirms or OVERRIDES the engine's grounded
// verdict. De-identified server-side (verdict enums only — never raw text); the
// flywheel persists it only when capture is enabled, but the action always
// succeeds. This is the highest-value correction the system can learn from.
function VerdictFeedback({ artifactId, s }: { artifactId: string; s: Scenario }) {
  const [state, setState] = useState<'idle' | 'sending' | 'done' | 'error'>('idle');
  const [msg, setMsg] = useState('');
  const isRegression = s.verdict === 'real_regression';

  const send = async (agrees: boolean, corrected: string) => {
    setState('sending');
    try {
      await api.triageFeedback(artifactId, s.scenario_id, { verdict: s.verdict, agrees, corrected_verdict: corrected });
      setMsg(agrees ? 'Confirmed — thanks' : 'Override recorded — thanks');
      setState('done');
    } catch { setState('error'); }
  };

  if (state === 'done') {
    return <span className="flex items-center gap-1 text-[10px] font-semibold text-emerald-600"><Check className="h-3 w-3" /> {msg}</span>;
  }
  return (
    <div className="flex items-center gap-2 flex-wrap">
      <span className="text-[9px] uppercase font-bold text-slate-400">Is this verdict right?</span>
      <button disabled={state === 'sending'} onClick={() => send(true, '')}
        title="Confirm the engine's verdict — teaches the grounded oracle"
        className="flex items-center gap-1 rounded-md px-2 py-1 text-[10px] font-semibold bg-emerald-50 text-emerald-700 hover:bg-emerald-100 disabled:opacity-50">
        <Check className="h-3 w-3" /> Correct
      </button>
      {isRegression ? (
        <button disabled={state === 'sending'} onClick={() => send(false, 'flake')}
          title="The engine called this a real bug, but it isn't — record the correction"
          className="flex items-center gap-1 rounded-md px-2 py-1 text-[10px] font-semibold bg-slate-100 text-slate-600 hover:bg-slate-200 disabled:opacity-50">
          <Flag className="h-3 w-3" /> Not a real bug
        </button>
      ) : (
        <button disabled={state === 'sending'} onClick={() => send(false, 'real_regression')}
          title="The engine dismissed this, but it IS a real bug — record the correction"
          className="flex items-center gap-1 rounded-md px-2 py-1 text-[10px] font-semibold bg-rose-50 text-rose-700 hover:bg-rose-100 disabled:opacity-50">
          <Flag className="h-3 w-3" /> Actually a real bug
        </button>
      )}
      {state === 'error' && <span className="text-[9px] text-amber-600">couldn't record</span>}
    </div>
  );
}

export function Frame({ label, src, awaiting }: { label: string; src: string; awaiting?: boolean }) {
  return (
    <div className="rounded-lg overflow-hidden border border-slate-200 bg-slate-50">
      <div className="px-2 py-1 text-[9px] font-bold uppercase text-slate-500 bg-white/70">{label}</div>
      {src ? (
        <a href={src} target="_blank" rel="noopener noreferrer">
          <img src={src} alt={label} className="w-full h-32 object-cover object-top" />
        </a>
      ) : (
        <div className="h-32 flex items-center justify-center text-[10px] text-slate-400 gap-1">
          <Camera className="h-3 w-3" /> {awaiting ? 'awaiting failure capture' : 'no baseline'}
        </div>
      )}
    </div>
  );
}
