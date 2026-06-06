/**
 * TestCasesPanel — Test Factory output in Test Studio.
 *
 * Evidence-first + scalable display: each category (Demonstrated, Suggested
 * combinations, Negative, Boundary, Error-state) shows at most CAP cases; when
 * more exist, a "N more — Download / Push" message replaces the long list so
 * the UI stays fast at 100s of cases. Categories generate on demand via
 * buttons. No assumptions — every value is demonstrated or a captured option.
 */
import { useCallback, useEffect, useState } from 'react';
import {
  AlertTriangle, Bot, Camera, CheckCircle2, Download, FileSpreadsheet, FlaskConical,
  Loader2, Send, ShieldAlert, Sparkles, Upload,
} from 'lucide-react';
import { api } from '../services/api';
import { useAuth } from '../contexts/AuthContext';

// Role-based default for the technical "automation details" columns
// (Observed in Recording, Confidence). Automation-focused roles see them by
// default; functional test engineers, business analysts and domain experts get
// the clean test case and can opt in via the checkbox. The data is ALWAYS
// stored — this governs display only.
function defaultShowDetails(role?: string): boolean {
  const r = (role || '').toLowerCase();
  return r === 'admin' || r.includes('automation') || r.includes('sdet');
}

interface Observed {
  verb?: string; label?: string; kind?: string; value?: string; url?: string;
}
interface Step {
  step_number?: number; action?: string; data_ref?: string;
  expected?: string; expected_result?: string;
  observed?: Observed; provenance?: string;
  confidence?: string; confidence_reason?: string;
  screenshot?: string;
}

// Compact, honest evidence string from the signal captured in the recording.
function evidenceText(o?: Observed): string {
  if (!o || Object.keys(o).length === 0) return '';
  if (o.url) return `navigate → ${o.url}`;
  const tgt = o.label ? `"${o.label}"` : '';
  const val = o.value ? ` = "${o.value}"` : '';
  return `${o.verb || ''} ${tgt}${val}`.trim();
}
// Provenance badge: how grounded this step is.
const PROV: Record<string, { label: string; bg: string; fg: string }> = {
  demonstrated: { label: 'Observed', bg: 'rgba(34,197,94,0.14)', fg: '#15803d' },
  available: { label: 'Option', bg: 'rgba(245,158,11,0.16)', fg: '#b45309' },
  inferred: { label: 'Inferred', bg: 'rgba(100,116,139,0.14)', fg: '#475569' },
};
interface ProductionTestCase {
  name?: string; description?: string; steps?: Step[];
  priority?: string; type?: string; tags?: string[];
}
interface CaseRow {
  test_case_id: string; name: string; description: string; priority: string;
  type: string; confidence: string; status: string; step_count: number;
  tags: string[]; test_case: ProductionTestCase;
}

const CAP = 5;

const SECTIONS: { type: string; label: string; accent: string; badge: string }[] = [
  { type: 'functional', label: 'Demonstrated', accent: '#059669', badge: 'rgba(16,185,129,0.15)' },
  { type: 'combination', label: 'Suggested combinations', accent: '#d97706', badge: 'rgba(245,158,11,0.15)' },
  { type: 'negative', label: 'Negative', accent: '#e11d48', badge: 'rgba(225,29,72,0.13)' },
  { type: 'boundary', label: 'Boundary', accent: '#7c3aed', badge: 'rgba(124,58,237,0.13)' },
  { type: 'error_state', label: 'Error-state', accent: '#dc2626', badge: 'rgba(220,38,38,0.13)' },
];

export default function TestCasesPanel({ artifactId }: { artifactId: string }) {
  const { user } = useAuth();
  const [summary, setSummary] = useState<any>(null);
  const [bySection, setBySection] = useState<Record<string, CaseRow[]>>({});
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  // Role-aware display of the technical evidence columns. An explicit user
  // choice (persisted per user) overrides the role default; the underlying
  // data is always stored regardless.
  const prefKey = `nexus_tc_details_${user?.user_id || 'anon'}`;
  const [showDetails, setShowDetails] = useState<boolean>(() => {
    const saved = typeof localStorage !== 'undefined' ? localStorage.getItem(prefKey) : null;
    if (saved === 'on') return true;
    if (saved === 'off') return false;
    return defaultShowDetails(user?.role);
  });
  const toggleDetails = () => {
    setShowDetails((v) => {
      const nv = !v;
      try { localStorage.setItem(prefKey, nv ? 'on' : 'off'); } catch { /* ignore */ }
      return nv;
    });
  };

  const refresh = useCallback(async () => {
    if (!artifactId) return;
    setLoading(true); setError(null);
    try {
      const sum = await api.getTestFactorySummary(artifactId).catch(() => null);
      setSummary(sum);
      const counts: Record<string, number> = (sum && sum.by_type) || {};
      const sections: Record<string, CaseRow[]> = {};
      await Promise.all(SECTIONS.map(async (s) => {
        if (!counts[s.type]) { sections[s.type] = []; return; }
        const list = await api.listTestFactoryCases(artifactId, 1, CAP, 'active', s.type);
        sections[s.type] = list.items || [];
      }));
      setBySection(sections);
    } catch (e: any) {
      setError(e?.response?.data?.detail || String(e));
    } finally { setLoading(false); }
  }, [artifactId]);

  useEffect(() => { void refresh(); }, [refresh]);

  const run = async (label: string, fn: () => Promise<any>, ok?: string) => {
    setBusy(label); setError(null); setNotice(null);
    try { await fn(); if (ok) setNotice(ok); await refresh(); }
    catch (e: any) { setError(e?.response?.data?.detail || String(e)); }
    finally { setBusy(''); }
  };

  const download = async (format: string) => {
    setBusy(`export:${format}`); setError(null);
    try {
      const blob = await api.exportTestFactory(artifactId, format, showDetails);
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `nexus-testcases-${artifactId.slice(0, 8)}.${format === 'excel' ? 'xlsx' : format}`;
      a.click(); URL.revokeObjectURL(url);
    } catch (e: any) { setError(e?.response?.data?.detail || String(e)); }
    finally { setBusy(''); }
  };

  const total = summary?.total ?? 0;

  const btn = (label: string, onClick: () => void, cls: string, icon: any, key: string) => (
    <button onClick={onClick} disabled={!!busy}
      className={`flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-xs font-medium disabled:opacity-50 ${cls}`}>
      {busy === key ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : icon}
      {label}
    </button>
  );

  return (
    <section className="space-y-4">
      <div className="rounded-2xl px-4 py-3 flex items-center gap-3 flex-wrap"
        style={{ background: 'linear-gradient(135deg, rgba(16,185,129,0.06), rgba(56,189,248,0.05))', border: '1px solid rgba(16,185,129,0.22)' }}>
        <div className="p-1.5 rounded-lg" style={{ background: 'rgba(16,185,129,0.15)' }}>
          <FlaskConical className="h-4 w-4" style={{ color: '#059669' }} />
        </div>
        <div className="min-w-0">
          <div className="text-[13px] font-black text-slate-900 leading-tight">
            Test Cases <span className="text-slate-400 font-semibold">· from Pages &amp; Forms</span>
          </div>
          <div className="text-[10px] text-slate-500 font-semibold">
            {total} total · grounded in your recording — no assumptions
          </div>
        </div>
        <div className="ml-auto flex items-center gap-2 flex-wrap justify-end">
          {btn('Generate', () => run('generate', () => api.generateTestFactory(artifactId)), 'bg-emerald-600 text-white hover:bg-emerald-500', <Sparkles className="h-3.5 w-3.5" />, 'generate')}
          {showDetails && btn('Enrich test cases', () => run('enrich', () => api.enrichTestFactory(artifactId), 'Enriched: captured options, element anchors & outcomes, then regenerated.'), 'bg-sky-100 text-sky-700 hover:bg-sky-200', <Sparkles className="h-3.5 w-3.5" />, 'enrich')}
          <span className="w-px h-5 bg-slate-200" />
          {btn('Negative', () => run('negative', () => api.generateTestFactoryCategory(artifactId, 'negative')), 'bg-rose-100 text-rose-700 hover:bg-rose-200', <ShieldAlert className="h-3.5 w-3.5" />, 'negative')}
          {btn('Boundary', () => run('boundary', () => api.generateTestFactoryCategory(artifactId, 'boundary')), 'bg-violet-100 text-violet-700 hover:bg-violet-200', <ShieldAlert className="h-3.5 w-3.5" />, 'boundary')}
          {btn('Error-state', () => run('error_state', () => api.generateTestFactoryCategory(artifactId, 'error_state')), 'bg-red-100 text-red-700 hover:bg-red-200', <ShieldAlert className="h-3.5 w-3.5" />, 'error_state')}
          <span className="w-px h-5 bg-slate-200" />
          {btn('Excel', () => download('excel'), 'bg-slate-100 text-slate-700 hover:bg-slate-200', <FileSpreadsheet className="h-3.5 w-3.5" />, 'export:excel')}
          {btn('CSV', () => download('csv'), 'bg-slate-100 text-slate-700 hover:bg-slate-200', <Download className="h-3.5 w-3.5" />, 'export:csv')}
          {btn('Push', () => run('push', () => api.pushTestFactory(artifactId, 'qtest'), 'Pushed to qTest.'), 'bg-violet-100 text-violet-700 hover:bg-violet-200', <Upload className="h-3.5 w-3.5" />, 'push')}
        </div>
      </div>

      <CoArchitectChat artifactId={artifactId} />

      <div className="flex items-center gap-2 px-1">
        <label className="flex items-center gap-1.5 text-[11px] font-semibold text-slate-500 cursor-pointer select-none">
          <input type="checkbox" checked={showDetails} onChange={toggleDetails} className="h-3.5 w-3.5 accent-emerald-600" />
          Show automation details
        </label>
        <span className="text-[10px] text-slate-400">Observed-in-recording evidence — for automation engineers. Always stored; shown only when ticked.</span>
      </div>

      {notice && <div className="rounded-lg border border-emerald-200 bg-emerald-50 px-3 py-2 text-[11px] text-emerald-800">{notice}</div>}
      {error && <div className="rounded-lg border border-amber-300 bg-amber-50 px-3 py-2 text-[11px] text-amber-800 flex items-center gap-2"><AlertTriangle className="h-3.5 w-3.5" />{error}</div>}

      {loading ? (
        <div className="flex items-center gap-2 text-slate-400 text-sm px-2 py-8 justify-center">
          <Loader2 className="h-4 w-4 animate-spin" /> Loading test cases…
        </div>
      ) : total === 0 ? (
        <div className="rounded-xl border border-dashed border-slate-300 bg-slate-50 px-4 py-8 text-center">
          <p className="text-sm font-semibold text-slate-600">No test cases yet</p>
          <p className="text-[11px] text-slate-400 mt-1">Click <b>Generate</b> to build the demonstrated functional E2E, then <b>Negative</b> / <b>Boundary</b> for more coverage.</p>
        </div>
      ) : (
        SECTIONS.map((s) => {
          const items = bySection[s.type] || [];
          const count = summary?.by_type?.[s.type] ?? items.length;
          if (count === 0) return null;
          const more = count - items.length;
          return (
            <div key={s.type}>
              <div className="flex items-center gap-2 px-1 pb-1">
                <span className="text-[11px] font-black uppercase tracking-wide" style={{ color: s.accent }}>{s.label}</span>
                <span className="rounded-full px-1.5 py-0.5 text-[9px] font-black" style={{ background: s.badge, color: s.accent }}>{count}</span>
              </div>
              {items.map((row) => <TestCaseCard key={row.test_case_id} row={row} accent={s.accent} showDetails={showDetails} />)}
              {more > 0 && (
                <div className="rounded-xl border border-dashed px-3 py-2.5 flex items-center gap-2 flex-wrap"
                  style={{ borderColor: s.badge }}>
                  <span className="text-[11px] font-semibold text-slate-600">
                    + {more} more {s.label.toLowerCase()} test case{more === 1 ? '' : 's'} available.
                  </span>
                  <button onClick={() => download('excel')} disabled={!!busy}
                    className="ml-auto flex items-center gap-1 rounded-md px-2 py-1 text-[10px] font-semibold bg-slate-100 text-slate-700 hover:bg-slate-200 disabled:opacity-50">
                    <Download className="h-3 w-3" /> Download
                  </button>
                  <button onClick={() => run('push', () => api.pushTestFactory(artifactId, 'qtest'), 'Pushed to qTest.')} disabled={!!busy}
                    className="flex items-center gap-1 rounded-md px-2 py-1 text-[10px] font-semibold bg-violet-100 text-violet-700 hover:bg-violet-200 disabled:opacity-50">
                    <Upload className="h-3 w-3" /> Push
                  </button>
                </div>
              )}
            </div>
          );
        })
      )}
    </section>
  );
}

function TestCaseCard({ row, accent, showDetails }: { row: CaseRow; accent: string; showDetails: boolean }) {
  const [open, setOpen] = useState(false);
  const steps = row.test_case?.steps || [];
  const demo = row.confidence === 'demonstrated';
  const reviewCount = steps.filter((s) => s.confidence === 'review').length;
  const scored = steps.some((s) => s.confidence);
  return (
    <div className="rounded-xl mb-2 overflow-hidden" style={{ background: 'rgba(255,255,255,0.7)', border: `1px solid ${accent}33` }}>
      <button onClick={() => setOpen((o) => !o)} className="w-full flex items-center gap-2 px-3 py-2.5 text-left">
        {demo ? <CheckCircle2 className="h-4 w-4 shrink-0" style={{ color: accent }} />
              : <Sparkles className="h-4 w-4 shrink-0" style={{ color: accent }} />}
        <span className="text-[12px] font-bold text-slate-900 break-words">{row.name}</span>
        <span className="ml-auto shrink-0 rounded-full px-2 py-0.5 text-[9px] font-black uppercase" style={{ background: `${accent}22`, color: accent }}>{row.confidence}</span>
        {showDetails && scored && (
          reviewCount > 0
            ? <span className="shrink-0 inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[9px] font-black" style={{ background: 'rgba(245,158,11,0.16)', color: '#b45309' }}><AlertTriangle className="h-3 w-3" /> {reviewCount} to review</span>
            : <span className="shrink-0 inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[9px] font-black" style={{ background: 'rgba(34,197,94,0.14)', color: '#15803d' }}><CheckCircle2 className="h-3 w-3" /> all solid</span>
        )}
        <span className="shrink-0 text-[9px] text-slate-400 font-semibold">{row.step_count} steps · {row.priority}</span>
      </button>
      {open && (
        <div className="px-3 pb-3 overflow-x-auto">
          {row.description && <p className="text-[11px] text-slate-500 mb-2 leading-snug">{row.description}</p>}
          <table className="w-full text-[11px] border-collapse">
            <thead><tr className="text-slate-400 text-left">
              <th className="font-semibold py-1 pr-2 w-8">S.No</th>
              <th className="font-semibold py-1 pr-2">Test Step</th>
              <th className="font-semibold py-1 pr-2">Test Data</th>
              <th className="font-semibold py-1 pr-2">Expected Result</th>
              {showDetails && <th className="font-semibold py-1 pr-2">Observed in Recording</th>}
              {showDetails && <th className="font-semibold py-1">Confidence</th>}
            </tr></thead>
            <tbody>
              {steps.map((s, i) => {
                const ev = evidenceText(s.observed);
                const prov = PROV[s.provenance || ''] || null;
                return (
                <tr key={i} className="border-t border-slate-100 align-top">
                  <td className="py-1.5 pr-2 text-slate-400 font-mono">{s.step_number ?? i + 1}</td>
                  <td className="py-1.5 pr-2 text-slate-800">{s.action}</td>
                  <td className="py-1.5 pr-2">{s.data_ref ? <span className="rounded px-1.5 py-0.5 font-semibold" style={{ background: 'rgba(56,189,248,0.12)', color: '#0369a1' }}>{s.data_ref}</span> : <span className="text-slate-300">—</span>}</td>
                  <td className="py-1.5 pr-2 text-slate-600">{s.expected_result || s.expected || ''}</td>
                  {showDetails && (
                  <td className="py-1.5 pr-2">
                    <div className="flex flex-col gap-0.5">
                      {ev ? (
                        <>
                          {prov && <span className="self-start rounded px-1.5 py-0.5 text-[9px] font-bold uppercase tracking-wide" style={{ background: prov.bg, color: prov.fg }}>{prov.label}</span>}
                          <span className="font-mono text-[10px] text-slate-500 leading-tight">{ev}</span>
                        </>
                      ) : (
                        prov ? <span className="self-start rounded px-1.5 py-0.5 text-[9px] font-bold uppercase" style={{ background: prov.bg, color: prov.fg }}>{prov.label}</span> : !s.screenshot && <span className="text-slate-300">—</span>
                      )}
                      {s.screenshot && (
                        <a href={api.getFrameImageUrl(s.screenshot)} target="_blank" rel="noopener noreferrer"
                          className="self-start inline-flex items-center gap-1 text-[10px] font-semibold text-sky-600 hover:text-sky-700">
                          <Camera className="h-3 w-3" /> screenshot
                        </a>
                      )}
                    </div>
                  </td>
                  )}
                  {showDetails && (
                  <td className="py-1.5" title={s.confidence_reason || ''}>
                    {s.confidence === 'high' ? (
                      <span className="inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-[9px] font-bold" style={{ background: 'rgba(34,197,94,0.14)', color: '#15803d' }}><CheckCircle2 className="h-3 w-3" /> Solid</span>
                    ) : s.confidence === 'review' ? (
                      <span className="inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-[9px] font-bold cursor-help" style={{ background: 'rgba(245,158,11,0.16)', color: '#b45309' }}><AlertTriangle className="h-3 w-3" /> Review</span>
                    ) : <span className="text-slate-300">—</span>}
                  </td>
                  )}
                </tr>
              );})}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

function CoArchitectChat({ artifactId }: { artifactId: string }) {
  const [open, setOpen] = useState(false);
  const [messages, setMessages] = useState<{ role: string; content: string }[]>([]);
  const [input, setInput] = useState('');
  const [sending, setSending] = useState(false);

  const send = async () => {
    const q = input.trim();
    if (!q || sending) return;
    const history = messages.slice(-6);
    setMessages((m) => [...m, { role: 'user', content: q }]);
    setInput(''); setSending(true);
    try {
      const res = await api.assistantTestFactory(artifactId, q, history);
      setMessages((m) => [...m, { role: 'assistant', content: res.answer || '(no answer)' }]);
    } catch (e: any) {
      setMessages((m) => [...m, { role: 'assistant', content: 'Error: ' + (e?.response?.data?.detail || String(e)) }]);
    } finally { setSending(false); }
  };

  return (
    <div className="rounded-2xl overflow-hidden" style={{ border: '1px solid rgba(124,58,237,0.25)', background: 'rgba(124,58,237,0.03)' }}>
      <button onClick={() => setOpen((o) => !o)} className="w-full flex items-center gap-2 px-4 py-2.5 text-left">
        <Bot className="h-4 w-4" style={{ color: '#7c3aed' }} />
        <span className="text-[12px] font-black text-slate-900">Co-Architect</span>
        <span className="text-[10px] text-slate-400 font-semibold">· grounded in your recording · GPT-4o</span>
        <span className="ml-auto text-[10px] text-violet-600 font-semibold">{open ? 'Hide' : 'Ask'}</span>
      </button>
      {open && (
        <div className="px-3 pb-3 space-y-2">
          <div className="max-h-64 overflow-y-auto space-y-2">
            {messages.length === 0 && (
              <p className="text-[11px] text-slate-400 px-1">
                Ask about your recording or the generated tests — e.g. “What did the user fill in?”, “Explain the negative tests”, “Which tests cover the traveler page?”
              </p>
            )}
            {messages.map((m, i) => (
              <div key={i}
                className={`rounded-lg px-3 py-2 text-[11px] ${m.role === 'user' ? 'bg-violet-100 text-violet-900 ml-8' : 'bg-white border border-slate-200 text-slate-700 mr-8'}`}
                style={{ whiteSpace: 'pre-wrap' }}>{m.content}</div>
            ))}
            {sending && <div className="flex items-center gap-1.5 text-[11px] text-slate-400 px-1"><Loader2 className="h-3 w-3 animate-spin" /> thinking…</div>}
          </div>
          <div className="flex items-center gap-2">
            <input value={input} onChange={(e) => setInput(e.target.value)}
              onKeyDown={(e) => { if (e.key === 'Enter') void send(); }}
              placeholder="Ask Co-Architect…" disabled={sending}
              className="flex-1 rounded-lg border border-slate-200 px-3 py-1.5 text-[12px] focus:outline-none focus:border-violet-400" />
            <button onClick={() => void send()} disabled={sending || !input.trim()}
              className="flex items-center gap-1 rounded-lg px-3 py-1.5 text-xs font-medium bg-violet-600 text-white hover:bg-violet-500 disabled:opacity-50">
              <Send className="h-3.5 w-3.5" /> Send
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
