/**
 * TestCasesPanel — surfaces the Test Factory output (Pages & Forms → grounded
 * test cases) inside Test Studio.
 *
 * Evidence-first by design: every step shows its real captured Test Data value
 * and an explicit confidence badge ("demonstrated" = the user did it;
 * "available" = a captured option, suggested coverage).  No assumptions.
 *
 * Self-contained: owns its own data fetching so it can be dropped into the
 * Test Studio page with a one-line render.  Additive — touches no frozen code.
 */
import { useCallback, useEffect, useState } from 'react';
import {
  AlertTriangle,
  CheckCircle2,
  Download,
  FileSpreadsheet,
  FlaskConical,
  Loader2,
  Sparkles,
  Upload,
} from 'lucide-react';
import { api } from '../services/api';

interface Step {
  step_number?: number;
  action?: string;
  data_ref?: string;
  expected?: string;
  expected_result?: string;
}
interface ProductionTestCase {
  test_id?: string;
  name?: string;
  description?: string;
  steps?: Step[];
  priority?: string;
  type?: string;
  tags?: string[];
}
interface CaseRow {
  test_case_id: string;
  name: string;
  description: string;
  priority: string;
  type: string;
  confidence: string;
  status: string;
  step_count: number;
  tags: string[];
  test_case: ProductionTestCase;
}

export default function TestCasesPanel({ artifactId }: { artifactId: string }) {
  const [items, setItems] = useState<CaseRow[]>([]);
  const [summary, setSummary] = useState<any>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState<string>('');
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    if (!artifactId) return;
    setLoading(true);
    setError(null);
    try {
      const [list, sum] = await Promise.all([
        api.listTestFactoryCases(artifactId, 1, 100),
        api.getTestFactorySummary(artifactId).catch(() => null),
      ]);
      setItems(list.items || []);
      setSummary(sum);
    } catch (e: any) {
      setError(e?.response?.data?.detail || String(e));
    } finally {
      setLoading(false);
    }
  }, [artifactId]);

  useEffect(() => { void refresh(); }, [refresh]);

  const run = async (label: string, fn: () => Promise<any>, ok?: string) => {
    setBusy(label);
    setError(null);
    setNotice(null);
    try {
      await fn();
      if (ok) setNotice(ok);
      await refresh();
    } catch (e: any) {
      setError(e?.response?.data?.detail || String(e));
    } finally {
      setBusy('');
    }
  };

  const download = async (format: string) => {
    setBusy(`export:${format}`);
    setError(null);
    try {
      const blob = await api.exportTestFactory(artifactId, format);
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `nexus-testcases-${artifactId.slice(0, 8)}.${format === 'excel' ? 'xlsx' : format}`;
      a.click();
      URL.revokeObjectURL(url);
    } catch (e: any) {
      setError(e?.response?.data?.detail || String(e));
    } finally {
      setBusy('');
    }
  };

  const demonstrated = items.filter((i) => i.confidence === 'demonstrated');
  const combinations = items.filter((i) => i.confidence !== 'demonstrated');

  return (
    <section className="space-y-4">
      {/* ── Header + actions ───────────────────────────────── */}
      <div
        className="rounded-2xl px-4 py-3 flex items-center gap-3 flex-wrap"
        style={{
          background: 'linear-gradient(135deg, rgba(16,185,129,0.06), rgba(56,189,248,0.05))',
          border: '1px solid rgba(16,185,129,0.22)',
        }}
      >
        <div className="p-1.5 rounded-lg" style={{ background: 'rgba(16,185,129,0.15)' }}>
          <FlaskConical className="h-4 w-4" style={{ color: '#059669' }} />
        </div>
        <div className="min-w-0">
          <div className="text-[13px] font-black text-slate-900 leading-tight">
            Test Cases <span className="text-slate-400 font-semibold">· from Pages &amp; Forms</span>
          </div>
          <div className="text-[10px] text-slate-500 font-semibold">
            {summary
              ? `${summary.by_priority?.P0_critical ?? demonstrated.length} demonstrated · ${combinations.length} suggested combinations · grounded in your recording`
              : 'Grounded in your recording — no assumptions'}
          </div>
        </div>

        <div className="ml-auto flex items-center gap-2">
          <button
            onClick={() => run('generate', () => api.generateTestFactory(artifactId))}
            disabled={!!busy}
            className="flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-xs font-medium bg-emerald-600 text-white hover:bg-emerald-500 disabled:opacity-50"
          >
            {busy === 'generate' ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Sparkles className="h-3.5 w-3.5" />}
            Generate
          </button>
          <button
            onClick={() => run('capture', () => api.captureTestFactoryOptions(artifactId), 'Captured options and regenerated combinations.')}
            disabled={!!busy}
            title="Read available field options from the recording, then generate grounded combinations"
            className="flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-xs font-medium bg-sky-100 text-sky-700 hover:bg-sky-200 disabled:opacity-50"
          >
            {busy === 'capture' ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Sparkles className="h-3.5 w-3.5" />}
            Capture options
          </button>
          <button
            onClick={() => download('excel')}
            disabled={!!busy || items.length === 0}
            className="flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-xs font-medium bg-slate-100 text-slate-700 hover:bg-slate-200 disabled:opacity-50"
          >
            {busy === 'export:excel' ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <FileSpreadsheet className="h-3.5 w-3.5" />}
            Excel
          </button>
          <button
            onClick={() => download('csv')}
            disabled={!!busy || items.length === 0}
            className="flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-xs font-medium bg-slate-100 text-slate-700 hover:bg-slate-200 disabled:opacity-50"
          >
            <Download className="h-3.5 w-3.5" /> CSV
          </button>
          <button
            onClick={() => run('push', () => api.pushTestFactory(artifactId, 'qtest'), 'Pushed to qTest.')}
            disabled={!!busy || items.length === 0}
            title="Push to a connected test-management tool"
            className="flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-xs font-medium bg-violet-100 text-violet-700 hover:bg-violet-200 disabled:opacity-50"
          >
            <Upload className="h-3.5 w-3.5" /> Push
          </button>
        </div>
      </div>

      {notice && (
        <div className="rounded-lg border border-emerald-200 bg-emerald-50 px-3 py-2 text-[11px] text-emerald-800">{notice}</div>
      )}
      {error && (
        <div className="rounded-lg border border-amber-300 bg-amber-50 px-3 py-2 text-[11px] text-amber-800 flex items-center gap-2">
          <AlertTriangle className="h-3.5 w-3.5" /> {error}
        </div>
      )}

      {loading ? (
        <div className="flex items-center gap-2 text-slate-400 text-sm px-2 py-8 justify-center">
          <Loader2 className="h-4 w-4 animate-spin" /> Loading test cases…
        </div>
      ) : items.length === 0 ? (
        <div className="rounded-xl border border-dashed border-slate-300 bg-slate-50 px-4 py-8 text-center">
          <p className="text-sm font-semibold text-slate-600">No test cases yet</p>
          <p className="text-[11px] text-slate-400 mt-1">
            Click <b>Generate</b> to build the demonstrated functional E2E from this recording's Pages &amp; Forms.
          </p>
        </div>
      ) : (
        <>
          {demonstrated.map((row) => <TestCaseCard key={row.test_case_id} row={row} hero />)}
          {combinations.length > 0 && (
            <div className="pt-1">
              <div className="text-[11px] font-bold text-slate-500 px-1 pb-1">
                Suggested coverage · grounded in captured options (not demonstrated)
              </div>
              {combinations.map((row) => <TestCaseCard key={row.test_case_id} row={row} />)}
            </div>
          )}
        </>
      )}
    </section>
  );
}

function TestCaseCard({ row, hero }: { row: CaseRow; hero?: boolean }) {
  const [open, setOpen] = useState<boolean>(!!hero);
  const steps = row.test_case?.steps || [];
  const isDemo = row.confidence === 'demonstrated';
  return (
    <div
      className="rounded-xl mb-2 overflow-hidden"
      style={{
        background: 'rgba(255,255,255,0.7)',
        border: `1px solid ${isDemo ? 'rgba(16,185,129,0.3)' : 'rgba(245,158,11,0.3)'}`,
      }}
    >
      <button onClick={() => setOpen((o) => !o)} className="w-full flex items-center gap-2 px-3 py-2.5 text-left">
        {isDemo
          ? <CheckCircle2 className="h-4 w-4 shrink-0" style={{ color: '#059669' }} />
          : <Sparkles className="h-4 w-4 shrink-0" style={{ color: '#d97706' }} />}
        <span className="text-[12px] font-bold text-slate-900 break-words">{row.name}</span>
        <span
          className="ml-auto shrink-0 rounded-full px-2 py-0.5 text-[9px] font-black uppercase tracking-wide"
          style={isDemo
            ? { background: 'rgba(16,185,129,0.15)', color: '#047857' }
            : { background: 'rgba(245,158,11,0.15)', color: '#b45309' }}
        >
          {row.confidence}
        </span>
        <span className="shrink-0 text-[9px] text-slate-400 font-semibold">{row.step_count} steps · {row.priority}</span>
      </button>

      {open && (
        <div className="px-3 pb-3 overflow-x-auto">
          <table className="w-full text-[11px] border-collapse">
            <thead>
              <tr className="text-slate-400 text-left">
                <th className="font-semibold py-1 pr-2 w-8">S.No</th>
                <th className="font-semibold py-1 pr-2">Test Step</th>
                <th className="font-semibold py-1 pr-2">Test Data</th>
                <th className="font-semibold py-1">Expected Result</th>
              </tr>
            </thead>
            <tbody>
              {steps.map((s, i) => (
                <tr key={i} className="border-t border-slate-100 align-top">
                  <td className="py-1.5 pr-2 text-slate-400 font-mono">{s.step_number ?? i + 1}</td>
                  <td className="py-1.5 pr-2 text-slate-800">{s.action}</td>
                  <td className="py-1.5 pr-2">
                    {s.data_ref
                      ? <span className="rounded px-1.5 py-0.5 font-semibold" style={{ background: 'rgba(56,189,248,0.12)', color: '#0369a1' }}>{s.data_ref}</span>
                      : <span className="text-slate-300">—</span>}
                  </td>
                  <td className="py-1.5 text-slate-600">{s.expected_result || s.expected || ''}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
