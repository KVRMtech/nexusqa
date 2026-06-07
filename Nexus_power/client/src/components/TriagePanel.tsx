import { useCallback, useEffect, useState } from 'react';
import { Camera, Loader2, RefreshCw, ShieldAlert } from 'lucide-react';
import { api } from '../services/api';

// Verdict styling + which stack a failure belongs to. Mirrors the backend
// classify_failure labels; real_regression / needs_review = "need you".
const VERDICT: Record<string, { label: string; bg: string; fg: string; needYou: boolean }> = {
  real_regression: { label: 'Real regression', bg: 'rgba(239,68,68,0.14)', fg: '#b91c1c', needYou: true },
  needs_review: { label: 'Needs review', bg: 'rgba(245,158,11,0.16)', fg: '#b45309', needYou: true },
  selector_drift: { label: 'Selector drift', bg: 'rgba(56,189,248,0.14)', fg: '#0369a1', needYou: false },
  visual_change: { label: 'Visual change', bg: 'rgba(139,92,246,0.14)', fg: '#6d28d9', needYou: false },
  flake: { label: 'Flake', bg: 'rgba(100,116,139,0.14)', fg: '#475569', needYou: false },
  passed: { label: 'Passed', bg: 'rgba(34,197,94,0.14)', fg: '#15803d', needYou: false },
};

interface Scenario {
  scenario_id: string; name: string; type: string; status: string;
  verdict: string; confidence: number; justification: string;
  step_number: number | null; baseline_screenshot: string; actual_screenshot: string;
  expected: string; error_message: string; is_flaky: boolean; flake_rate_pct: number;
}

export default function TriagePanel({ artifactId }: { artifactId: string }) {
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
  const scenarios: Scenario[] = (data?.scenarios || []).filter((s: Scenario) => s.verdict !== 'passed');

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

      {board.total === 0 ? (
        <div className="rounded-xl border border-dashed border-slate-300 bg-white/60 px-4 py-8 text-center">
          <p className="text-sm font-semibold text-slate-600">No runs yet</p>
          <p className="text-[11px] text-slate-400 mt-1 max-w-md mx-auto leading-relaxed">
            Generate Playwright, run it with the bundled Nexus reporter
            (set <span className="font-mono">NEXUS_ENDPOINT</span>, <span className="font-mono">NEXUS_TOKEN</span>,
            <span className="font-mono"> NEXUS_ARTIFACT_ID</span>), and the triage board appears here — each failure
            classified and shown beside its known-good baseline.
          </p>
        </div>
      ) : (
        <>
          <div className="flex items-center gap-2 flex-wrap mb-3 text-[11px] font-bold">
            <span className="rounded-full px-2.5 py-1" style={{ background: 'rgba(239,68,68,0.14)', color: '#b91c1c' }}>{board.need_you} need you</span>
            <span className="rounded-full px-2.5 py-1" style={{ background: 'rgba(100,116,139,0.12)', color: '#475569' }}>{board.dont_need_you} don't need you</span>
            <span className="text-slate-400 font-semibold">· {board.failures} of {board.total} failed</span>
            {Object.entries(board.by_label).filter(([k]) => k !== 'passed').map(([k, n]) => {
              const v = VERDICT[k] || { label: k, fg: '#475569', bg: 'rgba(100,116,139,0.12)' };
              return <span key={k} className="rounded px-1.5 py-0.5 text-[10px]" style={{ background: v.bg, color: v.fg }}>{n as number} {v.label}</span>;
            })}
          </div>
          <div className="space-y-2">
            {scenarios.map((s) => <TriageCard key={s.scenario_id} s={s} />)}
          </div>
        </>
      )}
    </section>
  );
}

function TriageCard({ s }: { s: Scenario }) {
  const v = VERDICT[s.verdict] || { label: s.verdict, bg: 'rgba(100,116,139,0.12)', fg: '#475569', needYou: true };
  return (
    <div className="rounded-xl p-3" style={{ background: 'rgba(255,255,255,0.85)', border: `1px solid ${v.fg}33` }}>
      <div className="flex items-center gap-2 mb-2 flex-wrap">
        <span className="rounded px-2 py-0.5 text-[9px] font-black uppercase" style={{ background: v.bg, color: v.fg }}>{v.label}</span>
        <span className="text-[12px] font-bold text-slate-900 break-words">{s.name}</span>
        <span className="ml-auto shrink-0 text-[9px] text-slate-400 font-semibold">
          {Math.round((s.confidence || 0) * 100)}% · step {s.step_number ?? '—'}{s.is_flaky ? ` · flaky ${s.flake_rate_pct}%` : ''}
        </span>
      </div>
      <p className="text-[11px] text-slate-700 mb-2 leading-snug">{s.justification}</p>
      {s.expected && <p className="text-[10px] text-slate-500 mb-2">Expected: {s.expected}</p>}
      <div className="grid grid-cols-2 gap-2">
        <Frame label="Known-good (recorded)" src={s.baseline_screenshot ? api.getFrameImageUrl(s.baseline_screenshot) : ''} />
        <Frame label="This run" src={s.actual_screenshot || ''} awaiting={!s.actual_screenshot} />
      </div>
      {s.error_message && (
        <p className="mt-2 text-[10px] font-mono text-rose-600 break-words" style={{ maxHeight: '3em', overflow: 'hidden' }}>{s.error_message}</p>
      )}
    </div>
  );
}

function Frame({ label, src, awaiting }: { label: string; src: string; awaiting?: boolean }) {
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
