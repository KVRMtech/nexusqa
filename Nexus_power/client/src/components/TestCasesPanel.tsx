/**
 * TestCasesPanel — Test Factory output in Test Studio.
 *
 * Evidence-first + scalable display: each category (Demonstrated, Suggested
 * combinations, Negative, Boundary, Error-state) shows at most CAP cases; when
 * more exist, a "N more — Download / Push" message replaces the long list so
 * the UI stays fast at 100s of cases. Categories generate on demand via
 * buttons. No assumptions — every value is demonstrated or a captured option.
 *
 * Layout: a two-pane "studio" — a calm grouped LIST on the left, and a sticky
 * DETAIL pane on the right that elevates the selected case (preconditions,
 * per-step action→expect→PROOF, sign-off, grounded edit/re-point). All data,
 * handlers and governance features are preserved; this is presentation only.
 */
import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  AlertTriangle, ArrowRight, Bot, Camera, CheckCircle2, ChevronDown, Download, FileCode2, FileSpreadsheet,
  FlaskConical, Loader2, MousePointerClick, Rocket, Send, ShieldAlert, Sparkles, Upload,
} from 'lucide-react';
import { api } from '../services/api';
import { useAuth } from '../contexts/AuthContext';
import TriagePanel from './TriagePanel';

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
  demonstrated: { label: 'Observed', bg: 'rgba(5,150,105,0.12)', fg: '#047857' },
  available: { label: 'Option', bg: 'rgba(217,162,58,0.16)', fg: '#92661d' },
  inferred: { label: 'Inferred', bg: 'rgba(38,112,163,0.12)', fg: '#164465' },
};
interface ProductionTestCase {
  name?: string; description?: string; steps?: Step[];
  priority?: string; type?: string; tags?: string[];
  expected_outcome?: string;
  preconditions?: Array<{ description?: string; setup_action?: string } | string>;
}
interface CaseRow {
  test_case_id: string; name: string; description: string; priority: string;
  type: string; confidence: string; status: string; step_count: number;
  tags: string[]; test_case: ProductionTestCase;
}

const CAP = 5;

// Categories keep their grouping (genuinely useful) but drop the per-section
// accent rainbow — one calm navy system, the gold tick marks the section.
const SECTIONS: { type: string; label: string }[] = [
  { type: 'functional', label: 'Demonstrated' },
  { type: 'combination', label: 'Suggested combinations' },
  { type: 'negative', label: 'Negative' },
  { type: 'boundary', label: 'Boundary' },
  { type: 'error_state', label: 'Error-state' },
];

// Single navy accent for every card (replaces the 5 per-section hexes).
const NAVY = '#2670a3';

// ── Small navy dropdown (groups secondary actions, kills the button salad) ──
function Menu(
  { label, icon, items, disabled }:
  { label: string; icon: React.ReactNode; items: { label: string; icon?: React.ReactNode; onClick: () => void }[]; disabled?: boolean },
) {
  const [open, setOpen] = useState(false);
  return (
    <div className="relative">
      <button type="button" onClick={() => setOpen((o) => !o)} disabled={disabled}
        className="flex items-center gap-1.5 rounded-lg border border-nexus-200 bg-white px-3 py-1.5 text-xs font-semibold text-nexus-700 hover:bg-nexus-50 disabled:opacity-50">
        {icon}{label}<ChevronDown className="h-3.5 w-3.5 opacity-60" />
      </button>
      {open && (
        <>
          <div className="fixed inset-0 z-20" onClick={() => setOpen(false)} />
          <div className="absolute right-0 z-30 mt-1 min-w-[200px] rounded-xl border border-nexus-200 bg-white py-1 shadow-card">
            {items.map((it, i) => (
              <button key={i} type="button" onClick={() => { setOpen(false); it.onClick(); }}
                className="flex w-full items-center gap-2 px-3 py-1.5 text-left text-xs font-medium text-nexus-700 hover:bg-nexus-50">
                {it.icon}{it.label}
              </button>
            ))}
          </div>
        </>
      )}
    </div>
  );
}

export default function TestCasesPanel(
  { artifactId, onOpenPlaywright }: { artifactId: string; onOpenPlaywright?: () => void },
) {
  const { user } = useAuth();
  const [summary, setSummary] = useState<any>(null);
  const [bySection, setBySection] = useState<Record<string, CaseRow[]>>({});
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [redactPII, setRedactPII] = useState(false);
  const [selectedId, setSelectedId] = useState<string | null>(null);

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

  // Flatten loaded cases (capped) and keep a valid selection.
  const allRows = useMemo(() => SECTIONS.flatMap((s) => bySection[s.type] || []), [bySection]);
  useEffect(() => {
    if (allRows.length === 0) { if (selectedId) setSelectedId(null); return; }
    if (!selectedId || !allRows.some((r) => r.test_case_id === selectedId)) {
      setSelectedId(allRows[0].test_case_id);
    }
  }, [allRows, selectedId]);
  const selectedRow = allRows.find((r) => r.test_case_id === selectedId) || null;
  const toReview = allRows.reduce(
    (n, r) => n + (r.test_case?.steps || []).filter((st) => st.confidence === 'review').length, 0,
  );

  const run = async (label: string, fn: () => Promise<any>, ok?: string) => {
    setBusy(label); setError(null); setNotice(null);
    try { await fn(); if (ok) setNotice(ok); await refresh(); }
    catch (e: any) { setError(e?.response?.data?.detail || String(e)); }
    finally { setBusy(''); }
  };

  const download = async (format: string) => {
    setBusy(`export:${format}`); setError(null);
    try {
      const blob = await api.exportTestFactory(artifactId, format, showDetails, redactPII);
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `nexus-testcases-${artifactId.slice(0, 8)}.${format === 'excel' ? 'xlsx' : format}`;
      a.click(); URL.revokeObjectURL(url);
    } catch (e: any) { setError(e?.response?.data?.detail || String(e)); }
    finally { setBusy(''); }
  };

  const downloadPlaywright = async (category = '', testCaseId = '') => {
    const key = testCaseId ? `playwright:tc:${testCaseId}` : (category ? `playwright:${category}` : 'playwright');
    setBusy(key); setError(null);
    try {
      const blob = await api.getPlaywrightBundle(artifactId, { category, testCaseId });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      const suffix = testCaseId ? `-${testCaseId.slice(0, 8)}` : (category ? `-${category}` : '');
      a.download = `nexus-playwright-${artifactId.slice(0, 8)}${suffix}.zip`;
      a.click(); URL.revokeObjectURL(url);
    } catch (e: any) { setError(e?.response?.data?.detail || String(e)); }
    finally { setBusy(''); }
  };

  const total = summary?.total ?? 0;

  // ── Header + decluttered toolbar (one gold CTA + grouped navy menus) ──
  const header = (
    <div className="flex flex-col gap-3">
      <div className="flex items-start gap-3 flex-wrap">
        <div className="flex items-center gap-2.5 min-w-0">
          <span className="h-9 w-1 rounded-full bg-gradient-to-b from-gold-400 to-gold-600 shrink-0" />
          <div className="min-w-0">
            <h2 className="text-[15px] font-bold tracking-tight text-nexus-900 leading-tight">Test Cases</h2>
            <p className="text-[12px] text-nexus-500">
              {total} grounded {total === 1 ? 'case' : 'cases'} · no assumptions
              {toReview > 0 && <span className="text-amber-700 font-semibold"> · {toReview} step{toReview === 1 ? '' : 's'} to review</span>}
            </p>
          </div>
        </div>
        <div className="ml-auto flex items-center gap-2 flex-wrap justify-end">
          <button onClick={() => run('generate', () => api.generateTestFactory(artifactId))} disabled={!!busy}
            className="btn-primary btn-gold text-xs px-3.5 py-1.5 font-semibold shadow-sm ring-1 ring-gold-300/40 disabled:opacity-50">
            {busy === 'generate' ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Sparkles className="h-3.5 w-3.5" />} Generate
          </button>
          <Menu label="Add coverage" icon={<ShieldAlert className="h-3.5 w-3.5" />} disabled={!!busy} items={[
            { label: 'Negative tests', icon: <ShieldAlert className="h-3.5 w-3.5 text-nexus-500" />, onClick: () => run('negative', () => api.generateTestFactoryCategory(artifactId, 'negative')) },
            { label: 'Boundary tests', icon: <ShieldAlert className="h-3.5 w-3.5 text-nexus-500" />, onClick: () => run('boundary', () => api.generateTestFactoryCategory(artifactId, 'boundary')) },
            { label: 'Error-state tests', icon: <ShieldAlert className="h-3.5 w-3.5 text-nexus-500" />, onClick: () => run('error_state', () => api.generateTestFactoryCategory(artifactId, 'error_state')) },
            ...(showDetails ? [{ label: 'Enrich (anchors + outcomes)', icon: <Sparkles className="h-3.5 w-3.5 text-nexus-500" />, onClick: () => run('enrich', () => api.enrichTestFactory(artifactId), 'Enriched: captured options, element anchors & outcomes, then regenerated.') }] : []),
          ]} />
          <label className="flex items-center gap-1.5 text-[11px] font-semibold text-nexus-600 cursor-pointer select-none" title="Redact detected PII (SSN/DOB/email/phone/policy) from exports + external pushes">
            <input type="checkbox" checked={redactPII} onChange={() => setRedactPII((v) => !v)} className="h-3.5 w-3.5 accent-nexus-600" /> Redact PII
          </label>
          <Menu label="Export" icon={<Download className="h-3.5 w-3.5" />} disabled={!!busy} items={[
            { label: 'Excel (.xlsx)', icon: <FileSpreadsheet className="h-3.5 w-3.5 text-nexus-500" />, onClick: () => download('excel') },
            { label: 'CSV', icon: <Download className="h-3.5 w-3.5 text-nexus-500" />, onClick: () => download('csv') },
            { label: 'Push to qTest', icon: <Upload className="h-3.5 w-3.5 text-nexus-500" />, onClick: () => run('push', () => api.pushTestFactory(artifactId, 'qtest'), 'Pushed to qTest.') },
            ...(showDetails ? [{ label: 'Playwright bundle (.zip)', icon: <FileCode2 className="h-3.5 w-3.5 text-nexus-500" />, onClick: () => downloadPlaywright() }] : []),
          ]} />
        </div>
      </div>
      <label className="flex items-center gap-1.5 text-[11px] font-semibold text-nexus-500 cursor-pointer select-none w-fit">
        <input type="checkbox" checked={showDetails} onChange={toggleDetails} className="h-3.5 w-3.5 accent-nexus-600" />
        Show automation evidence
        <span className="text-[10px] text-nexus-400 font-normal">— observed-in-recording proof for automation engineers (always stored)</span>
      </label>
    </div>
  );

  return (
    <section className="space-y-4">
      {header}

      <CoArchitectChat artifactId={artifactId} onAdded={refresh} />

      {notice && <div className="rounded-lg border border-emerald-200 bg-emerald-50 px-3 py-2 text-[12px] text-emerald-800">{notice}</div>}
      {error && <div className="rounded-lg border border-amber-300 bg-amber-50 px-3 py-2 text-[12px] text-amber-800 flex items-center gap-2"><AlertTriangle className="h-3.5 w-3.5" />{error}</div>}

      {/* Honest extraction health — never present degraded data as trustworthy. */}
      {summary?.extraction_health?.degraded && (
        <div className="rounded-lg border border-red-300 bg-red-50 px-3 py-2.5 text-[12px] text-red-800 flex items-start gap-2">
          <ShieldAlert className="h-4 w-4 flex-shrink-0 mt-0.5 text-red-600" />
          <div>
            <div className="font-bold">Extraction degraded — review before trusting these cases</div>
            <div className="mt-0.5 text-red-700">{summary.extraction_health.reason}</div>
            {summary.extraction_health.by_model && (
              <div className="mt-1 text-[10px] text-red-500 font-mono">
                models: {Object.entries(summary.extraction_health.by_model).map(([m, c]) => `${m}×${c}`).join(' · ')}
              </div>
            )}
          </div>
        </div>
      )}

      {loading ? (
        <div className="flex items-center gap-2 text-nexus-400 text-sm px-2 py-10 justify-center">
          <Loader2 className="h-4 w-4 animate-spin" /> Loading test cases…
        </div>
      ) : total === 0 ? (
        <div className="rounded-2xl border border-dashed border-nexus-200 bg-white px-4 py-12 text-center">
          <div className="mx-auto mb-3 flex h-12 w-12 items-center justify-center rounded-2xl bg-nexus-50 ring-1 ring-nexus-100">
            <FlaskConical className="h-6 w-6 text-nexus-500" />
          </div>
          <p className="text-sm font-semibold text-nexus-800">No test cases yet</p>
          {summary?.no_cases_reason ? (
            <p className="text-[12px] text-nexus-500 mt-1.5 max-w-xl mx-auto leading-relaxed">{summary.no_cases_reason}</p>
          ) : (
            <p className="text-[12px] text-nexus-500 mt-1 max-w-xl mx-auto">Click <b className="text-nexus-700">Generate</b> to build the demonstrated functional E2E, then <b className="text-nexus-700">Add coverage</b> for negative / boundary / error-state.</p>
          )}
          <button onClick={() => run('generate', () => api.generateTestFactory(artifactId))} disabled={!!busy}
            className="btn-primary btn-gold text-xs px-4 py-2 font-semibold mt-4 inline-flex ring-1 ring-gold-300/40 disabled:opacity-50">
            {busy === 'generate' ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Sparkles className="h-3.5 w-3.5" />} Generate test cases
          </button>
        </div>
      ) : (
        // ── Two-pane studio: calm list (left) + sticky detail/proof (right) ──
        <div className="lg:grid lg:grid-cols-[1fr_1.25fr] lg:gap-6 lg:items-start">
          {/* LEFT — grouped, scannable list */}
          <div className="space-y-5">
            {SECTIONS.map((s) => {
              const items = bySection[s.type] || [];
              const count = summary?.by_type?.[s.type] ?? items.length;
              if (count === 0) return null;
              const more = count - items.length;
              return (
                <div key={s.type}>
                  <div className="flex items-center gap-2 px-0.5 pb-1.5">
                    <span className="h-3.5 w-1 rounded-full bg-gradient-to-b from-gold-400 to-gold-600" />
                    <span className="text-[12px] font-bold tracking-tight text-nexus-900">{s.label}</span>
                    <span className="rounded-full bg-nexus-50 px-1.5 py-0.5 text-[10px] font-bold text-nexus-600 ring-1 ring-nexus-100">{count}</span>
                    {showDetails && (
                      <button onClick={() => downloadPlaywright(s.type)} disabled={!!busy}
                        title={`Generate Playwright for ${s.label} only`}
                        className="ml-auto flex items-center gap-1 rounded-md px-2 py-0.5 text-[10px] font-semibold text-nexus-600 hover:bg-nexus-50 disabled:opacity-50">
                        {busy === `playwright:${s.type}` ? <Loader2 className="h-3 w-3 animate-spin" /> : <FileCode2 className="h-3 w-3" />} Playwright
                      </button>
                    )}
                  </div>
                  {items.map((row) => (
                    <TestCaseCard key={row.test_case_id} variant="row" row={row} accent={NAVY}
                      showDetails={showDetails} busy={busy} artifactId={artifactId}
                      selected={row.test_case_id === selectedId}
                      onSelect={() => setSelectedId(row.test_case_id)}
                      onPlaywright={(id) => downloadPlaywright('', id)} />
                  ))}
                  {more > 0 && (
                    <div className="rounded-lg border border-dashed border-nexus-200 px-3 py-2 flex items-center gap-2 flex-wrap mt-1.5">
                      <span className="text-[11px] font-semibold text-nexus-600">
                        Showing {items.length} of {count} — {more} more available.
                      </span>
                      <button onClick={() => download('excel')} disabled={!!busy}
                        className="ml-auto flex items-center gap-1 rounded-md px-2 py-1 text-[10px] font-semibold text-nexus-600 hover:bg-nexus-50 disabled:opacity-50">
                        <Download className="h-3 w-3" /> Export all
                      </button>
                    </div>
                  )}
                </div>
              );
            })}
          </div>

          {/* RIGHT — sticky detail pane (the elevated proof surface) */}
          <div className="mt-5 lg:mt-0 lg:sticky lg:top-4">
            {selectedRow ? (
              <TestCaseCard key={selectedRow.test_case_id} variant="detail" row={selectedRow} accent={NAVY}
                showDetails={showDetails} busy={busy} artifactId={artifactId}
                onPlaywright={(id) => downloadPlaywright('', id)} />
            ) : (
              <div className="rounded-2xl border border-dashed border-nexus-200 bg-white px-4 py-16 text-center">
                <MousePointerClick className="h-6 w-6 text-nexus-300 mx-auto mb-2" />
                <p className="text-[12px] text-nexus-500">Select a test case to see its steps, expected results and grounded proof.</p>
              </div>
            )}
          </div>
        </div>
      )}

      {total > 0 && (
        onOpenPlaywright ? (
          <button
            onClick={onOpenPlaywright}
            className="w-full flex items-center gap-3 rounded-xl border border-nexus-200 bg-white px-4 py-3 mt-1 text-left transition-all hover:bg-nexus-50 hover:-translate-y-px shadow-card"
          >
            <span className="flex h-9 w-9 items-center justify-center rounded-xl bg-gradient-to-br from-[#0c2c4d] to-[#0a2540] ring-1 ring-gold-400/30 shrink-0">
              <Rocket className="h-4 w-4 text-gold-400" />
            </span>
            <span className="min-w-0">
              <span className="block text-[13px] font-bold text-nexus-900">Playwright Execution &rarr;</span>
              <span className="block text-[11px] text-nexus-500 font-medium">
                View &amp; run the generated scripts by category, then triage failures against their recorded baseline.
              </span>
            </span>
            <ArrowRight className="h-4 w-4 text-nexus-400 ml-auto shrink-0" />
          </button>
        ) : (
          <div className="pt-3 mt-1 border-t border-nexus-100">
            <div className="flex items-center gap-2 px-0.5 pb-2">
              <span className="h-3.5 w-1 rounded-full bg-gradient-to-b from-gold-400 to-gold-600" />
              <span className="text-[12px] font-bold tracking-tight text-nexus-900">Playwright Execution</span>
              <span className="text-[10px] text-nexus-400 font-semibold">run the suite → grounded failure triage</span>
            </div>
            <TriagePanel artifactId={artifactId} />
          </div>
        )
      )}
    </section>
  );
}

// A value-conflict (keystroke reading ≠ form-snapshot reading) the human resolves.
// Records ONLY the de-identified CHOICE enum server-side (never the value); the
// flywheel persists it only when capture is enabled. Additive — the chip is
// unchanged when unresolved.
function ValueConflictResolve(
  { artifactId, testId, conflict }: { artifactId: string; testId: string; conflict: { typed?: string; committed?: string } },
) {
  const [done, setDone] = useState<string>('');
  const [sending, setSending] = useState(false);
  const resolve = async (choice: 'typed' | 'committed' | 'other') => {
    setSending(true);
    try { await api.resolveValueConflict(artifactId, testId, choice); setDone(choice); }
    catch { /* best-effort; leave unresolved so the user can retry */ }
    finally { setSending(false); }
  };
  return (
    <span className="self-start rounded px-1.5 py-0.5 text-[10px] font-semibold leading-tight flex flex-col gap-1" style={{ background: 'rgba(217,162,58,0.16)', color: '#92661d' }}>
      <span title="The keystroke reading and the form-snapshot reading disagree — confirm the intended value.">
        ⚠ typed '{conflict.typed}' vs snapshot '{conflict.committed}'
      </span>
      {done ? (
        <span className="text-emerald-700">✓ resolved: {done === 'committed' ? 'snapshot' : done}</span>
      ) : (
        <span className="flex items-center gap-1">
          <button disabled={sending} onClick={() => resolve('typed')} className="rounded px-1 py-0.5 bg-white/70 hover:bg-white font-bold disabled:opacity-50">Use typed</button>
          <button disabled={sending} onClick={() => resolve('committed')} className="rounded px-1 py-0.5 bg-white/70 hover:bg-white font-bold disabled:opacity-50">Use snapshot</button>
          <button disabled={sending} onClick={() => resolve('other')} className="rounded px-1 py-0.5 bg-white/70 hover:bg-white font-bold disabled:opacity-50">Other</button>
        </span>
      )}
    </span>
  );
}

function TestCaseCard(
  { row, accent, showDetails, busy, artifactId, onPlaywright, variant = 'card', selected = false, onSelect }:
  { row: CaseRow; accent: string; showDetails: boolean; busy?: string; artifactId: string; onPlaywright?: (id: string) => void;
    variant?: 'card' | 'row' | 'detail'; selected?: boolean; onSelect?: () => void },
) {
  const [open, setOpen] = useState(variant === 'detail');
  const steps = row.test_case?.steps || [];
  const piiRe = /\d{3}-\d{2}-\d{4}|[\w.+-]+@[\w-]+\.[\w.-]+|\(\d{3}\)\s?\d{3}-?\d{4}|\b\d{2}\/\d{2}\/\d{4}\b/;
  const hasPII = steps.some((s: any) => piiRe.test(JSON.stringify(s)));
  const [editing, setEditing] = useState(false);
  const [savingEdit, setSavingEdit] = useState(false);
  const [editErr, setEditErr] = useState<string | null>(null);
  const [edited, setEdited] = useState<Record<number, { action?: string; expected_result?: string }>>({});
  const [draft, setDraft] = useState<Record<number, { action: string; expected_result: string }>>({});
  // Grounded RE-POINT: the captured controls a step may be re-targeted to, and the
  // per-step choice. Re-pointing edits the binding (not just the description text),
  // so Regenerate actually changes which control the script clicks.
  const [controls, setControls] = useState<Array<{ label: string; kind: string; page?: string }>>([]);
  const [ctrlDraft, setCtrlDraft] = useState<Record<number, { label: string; kind: string } | null>>({});
  const dispAction = (s: any, i: number): string => (edited[s.step_number ?? i + 1]?.action ?? s.action ?? '');
  const dispExpected = (s: any, i: number): string => (edited[s.step_number ?? i + 1]?.expected_result ?? s.expected_result ?? s.expected ?? '');
  const beginEdit = async () => {
    setEditErr(null);
    const d: Record<number, { action: string; expected_result: string }> = {};
    steps.forEach((s: any, i: number) => { const n = s.step_number ?? i + 1; d[n] = { action: dispAction(s, i), expected_result: dispExpected(s, i) }; });
    setDraft(d); setCtrlDraft({}); setEditing(true);
    try { const r = await api.getStepControls(artifactId, row.test_case_id); setControls(r.controls || []); } catch { setControls([]); }
  };
  const saveEdit = async () => {
    setSavingEdit(true); setEditErr(null);
    try {
      const stepsPatch = Object.entries(draft).map(([n, v]) => {
        const sn = Number(n);
        const p: any = { step_number: sn, action: v.action, expected_result: v.expected_result };
        const c = ctrlDraft[sn];
        if (c && c.label) p.control = { label: c.label, kind: c.kind };
        return p;
      });
      await api.editTestCase(artifactId, row.test_case_id, { steps: stepsPatch });
      setEdited((prev) => { const m = { ...prev }; Object.entries(draft).forEach(([n, v]) => { m[Number(n)] = { action: v.action, expected_result: v.expected_result }; }); return m; });
      // ALWAYS regenerate so "Save (updates Playwright)" is honest: a text edit refreshes the
      // step description/oracle comments; a control re-point changes the actual locator. Cheap + idempotent.
      try { await api.regenerateScript(artifactId, row.test_case_id); } catch { /* surfaced on next audit */ }
      setEditing(false);
    } catch (e: any) {
      setEditErr(e?.response?.data?.detail ?? e?.message ?? 'Save failed');
    } finally { setSavingEdit(false); }
  };
  const rv: any = (row.test_case as any)?.review || {};
  const [reviewState, setReviewState] = useState<string>(rv.state || 'draft');
  const [signing, setSigning] = useState(false);
  const [sig, setSig] = useState('');
  const [reviewBusy, setReviewBusy] = useState(false);
  const [reviewErr, setReviewErr] = useState<string | null>(null);
  const doReview = async (action: string, signature?: string) => {
    setReviewBusy(true); setReviewErr(null);
    try {
      const r = await api.reviewTestCase(artifactId, row.test_case_id, { action, signature });
      setReviewState(r.state); setSigning(false); setSig('');
    } catch (e: any) { setReviewErr(e?.response?.data?.detail ?? e?.message ?? 'Failed'); }
    finally { setReviewBusy(false); }
  };
  const demo = row.confidence === 'demonstrated';
  const reviewCount = steps.filter((s) => s.confidence === 'review').length;
  const scored = steps.some((s) => s.confidence);
  const bodyVisible = variant === 'detail' || (variant === 'card' && open);

  // Container styling per variant — flat hairline list rows vs. an elevated detail card.
  const containerCls =
    variant === 'row'
      ? `relative mb-1.5 rounded-lg border transition-colors overflow-hidden before:absolute before:left-0 before:top-1.5 before:bottom-1.5 before:w-[3px] before:rounded-full ${
          selected ? 'bg-nexus-50 border-nexus-200 before:bg-gold-500' : 'border-nexus-100 bg-white hover:bg-nexus-50/60 before:bg-transparent'
        }`
      : variant === 'detail'
      ? 'rounded-2xl border border-nexus-200 bg-white shadow-card overflow-hidden'
      : 'rounded-xl mb-2 overflow-hidden';
  const headerOnClick = variant === 'card' ? () => setOpen((o) => !o) : (variant === 'row' ? (onSelect || (() => {})) : undefined);

  return (
    <div className={containerCls} style={variant === 'card' ? { background: 'rgba(255,255,255,0.7)', border: `1px solid ${accent}33` } : undefined}>
      <div className="flex items-center">
      <button onClick={headerOnClick} className={`flex-1 min-w-0 flex items-center gap-2 px-3 ${variant === 'detail' ? 'py-3 cursor-default' : 'py-2.5'} text-left`}>
        {demo ? <CheckCircle2 className="h-4 w-4 shrink-0 text-nexus-500" />
              : <Sparkles className="h-4 w-4 shrink-0 text-nexus-500" />}
        <span className={`${variant === 'detail' ? 'text-[14px]' : 'text-[13px]'} font-semibold text-nexus-900 break-words`}>{row.name}</span>
        {hasPII && (
          <span className="shrink-0 inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[10px] font-bold" style={{ background: 'rgba(239,68,68,0.12)', color: '#b91c1c' }} title="Detected PII (SSN/DOB/email/phone) - redact on export/push">PII</span>
        )}
        {reviewState && reviewState !== 'draft' && (
          <span className="shrink-0 rounded-full px-2 py-0.5 text-[10px] font-bold uppercase"
            style={reviewState === 'approved' ? { background: 'rgba(5,150,105,0.14)', color: '#047857' }
              : reviewState === 'rejected' ? { background: 'rgba(239,68,68,0.14)', color: '#b91c1c' }
              : { background: 'rgba(217,162,58,0.16)', color: '#92661d' }}>
            {reviewState === 'in_review' ? 'in review' : reviewState}
          </span>
        )}
        {scored && (
          reviewCount > 0
            ? <span className="shrink-0 inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[10px] font-bold" style={{ background: 'rgba(217,162,58,0.16)', color: '#92661d' }}><AlertTriangle className="h-3 w-3" /> {reviewCount} to review</span>
            : <span className="shrink-0 inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[10px] font-bold" style={{ background: 'rgba(5,150,105,0.12)', color: '#047857' }}><CheckCircle2 className="h-3 w-3" /> all solid</span>
        )}
        <span className="shrink-0 ml-auto text-[10px] text-nexus-400 font-semibold">{row.step_count} steps · {row.priority}</span>
      </button>
      {variant === 'detail' && showDetails && onPlaywright && (
        <button onClick={() => onPlaywright(row.test_case_id)} disabled={!!busy}
          title="Generate Playwright for this test case"
          className="shrink-0 mr-2 flex items-center gap-1 rounded-md px-2 py-1 text-[10px] font-semibold text-nexus-600 hover:bg-nexus-50 disabled:opacity-50">
          {busy === `playwright:tc:${row.test_case_id}` ? <Loader2 className="h-3 w-3 animate-spin" /> : <FileCode2 className="h-3 w-3" />} Playwright
        </button>
      )}
      </div>
      {bodyVisible && (
        <div className="px-3 pb-3 overflow-x-auto">
          <div className="flex flex-wrap items-center gap-1 mb-1.5 pb-1.5 border-b border-nexus-100">
            <span className="text-[10px] font-bold text-nexus-500 mr-1">Sign-off:</span>
            {reviewState === 'approved' ? (
              <>
                <span className="text-[10px] font-bold text-emerald-700">Approved{rv.approved_email ? ' · ' + rv.approved_email : (rv.approved_by ? ' · ' + rv.approved_by : '')}</span>
                <button onClick={() => void doReview('reopen')} disabled={reviewBusy} className="text-[10px] rounded border border-nexus-200 px-1.5 py-0.5 text-nexus-600 hover:bg-nexus-50 disabled:opacity-50">Reopen</button>
              </>
            ) : reviewState === 'in_review' ? (
              <>
                {!signing ? (
                  <button onClick={() => setSigning(true)} disabled={reviewBusy} className="text-[10px] rounded border border-emerald-300 bg-emerald-50 px-1.5 py-0.5 text-emerald-700 hover:bg-emerald-100 disabled:opacity-50">Approve…</button>
                ) : (
                  <>
                    <input value={sig} onChange={(e) => setSig(e.target.value)} placeholder="Type your full name to sign" className="text-[10px] rounded border border-emerald-300 px-1.5 py-0.5 w-44" />
                    <button onClick={() => void doReview('approve', sig)} disabled={reviewBusy || !sig.trim()} className="text-[10px] rounded bg-emerald-600 px-1.5 py-0.5 text-white hover:bg-emerald-500 disabled:opacity-50">Sign &amp; Approve</button>
                    <button onClick={() => { setSigning(false); setSig(''); }} className="text-[10px] rounded border border-nexus-200 px-1.5 py-0.5 text-nexus-600 hover:bg-nexus-50">Cancel</button>
                  </>
                )}
                <button onClick={() => void doReview('reject')} disabled={reviewBusy} className="text-[10px] rounded border border-rose-200 px-1.5 py-0.5 text-rose-600 hover:bg-rose-50 disabled:opacity-50">Reject</button>
              </>
            ) : (
              <button onClick={() => void doReview('submit')} disabled={reviewBusy} className="text-[10px] rounded border border-nexus-300 bg-nexus-50 px-1.5 py-0.5 text-nexus-700 hover:bg-nexus-100 disabled:opacity-50">Submit for review</button>
            )}
            {reviewBusy && <Loader2 className="h-3 w-3 animate-spin text-nexus-400" />}
            {reviewErr && <span className="text-[10px] text-red-500">{reviewErr}</span>}
          </div>
          <div className="flex items-center justify-end gap-1 mb-1">
            {!editing ? (
              <button onClick={beginEdit} className="text-[10px] rounded border border-nexus-200 px-1.5 py-0.5 text-nexus-600 hover:bg-nexus-50">Edit values</button>
            ) : (
              <>
                <button onClick={saveEdit} disabled={savingEdit} className="text-[10px] rounded border border-emerald-300 bg-emerald-50 px-1.5 py-0.5 text-emerald-700 hover:bg-emerald-100 disabled:opacity-50">{savingEdit ? 'Saving…' : 'Save (updates Playwright)'}</button>
                <button onClick={() => { setEditing(false); setEditErr(null); }} disabled={savingEdit} className="text-[10px] rounded border border-nexus-200 px-1.5 py-0.5 text-nexus-600 hover:bg-nexus-50">Cancel</button>
              </>
            )}
          </div>
          {editErr && <p className="text-[10px] text-red-500 mb-1">{editErr}</p>}
          {row.description && <p className="text-[12px] text-nexus-600 mb-2 leading-snug">{row.description}</p>}
          {Array.isArray(row.test_case?.preconditions) && row.test_case.preconditions.length > 0 && (
            <div className="mb-2 rounded-lg border border-nexus-100 bg-nexus-50 px-3 py-2 text-[12px] leading-snug">
              <span className="text-[10px] font-bold uppercase tracking-wide text-nexus-500">Preconditions</span>
              <ul className="mt-1 space-y-0.5">
                {row.test_case.preconditions.map((p: any, i: number) => {
                  const text = typeof p === 'string' ? p : (p?.description || p?.setup_action || '');
                  if (!text) return null;
                  return <li key={i} className="text-nexus-700">• {text}</li>;
                })}
              </ul>
            </div>
          )}
          <table className="w-full text-[12px] border-collapse">
            <thead><tr className="text-nexus-400 text-left">
              <th className="font-semibold py-1 pr-2 w-8">#</th>
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
                <tr key={i} className="border-t border-nexus-100 align-top">
                  <td className="py-2 pr-2 text-nexus-400 font-mono">{s.step_number ?? i + 1}</td>
                  <td className="py-2 pr-2 text-nexus-900 font-medium">{editing ? (
                    <input value={(draft[s.step_number ?? i + 1]?.action) ?? ''} onChange={(e) => setDraft((d) => ({ ...d, [s.step_number ?? i + 1]: { action: e.target.value, expected_result: d[s.step_number ?? i + 1]?.expected_result ?? '' } }))} className="w-full text-[12px] rounded border border-nexus-200 px-1 py-0.5" />
                  ) : dispAction(s, i)}</td>
                  <td className="py-2 pr-2">{s.data_ref ? <span className="rounded px-1.5 py-0.5 font-mono font-medium" style={{ background: 'rgba(38,112,163,0.10)', color: '#164465' }}>{s.data_ref}</span> : <span className="text-nexus-300">—</span>}</td>
                  <td className="py-2 pr-2 text-nexus-600">{editing ? (
                    <input value={(draft[s.step_number ?? i + 1]?.expected_result) ?? ''} onChange={(e) => setDraft((d) => ({ ...d, [s.step_number ?? i + 1]: { action: d[s.step_number ?? i + 1]?.action ?? '', expected_result: e.target.value } }))} className="w-full text-[12px] rounded border border-nexus-200 px-1 py-0.5" />
                  ) : dispExpected(s, i)}</td>
                  {showDetails && (
                  <td className="py-2 pr-2">
                    <div className="flex flex-col gap-0.5">
                      {editing && (s.observed as any)?.label && (
                        <select
                          value={ctrlDraft[s.step_number ?? i + 1]?.label ?? ((s.observed as any).label ?? '')}
                          onChange={(e) => {
                            const lab = e.target.value;
                            const cur = (s.observed as any).label ?? '';
                            const c = controls.find((cc) => cc.label === lab);
                            setCtrlDraft((d) => ({ ...d, [s.step_number ?? i + 1]: (c && lab !== cur) ? { label: c.label, kind: c.kind } : null }));
                          }}
                          title="Re-point this step to a control the recording captured — regenerates the Playwright to click it"
                          className="self-start max-w-[200px] text-[10px] rounded border border-nexus-300 bg-nexus-50 px-1 py-0.5 text-nexus-800">
                          <option value={(s.observed as any).label}>{(s.observed as any).label} — current</option>
                          {controls.filter((c) => c.label !== ((s.observed as any).label ?? '')).map((c, ci) => (
                            <option key={ci} value={c.label}>↳ {c.label} ({c.kind}{c.page ? ` · ${c.page}` : ''})</option>
                          ))}
                        </select>
                      )}
                      {ev ? (
                        <>
                          {prov && <span className="self-start rounded px-1.5 py-0.5 text-[10px] font-bold uppercase tracking-wide" style={{ background: prov.bg, color: prov.fg }}>{prov.label}</span>}
                          <span className="font-mono text-[11px] text-nexus-500 leading-tight">{ev}</span>
                        </>
                      ) : (
                        prov ? <span className="self-start rounded px-1.5 py-0.5 text-[10px] font-bold uppercase" style={{ background: prov.bg, color: prov.fg }}>{prov.label}</span> : !s.screenshot && <span className="text-nexus-300">—</span>
                      )}
                      {(s.observed as any)?.value_conflict && (
                        <ValueConflictResolve artifactId={artifactId} testId={row.test_case_id} conflict={(s.observed as any).value_conflict} />
                      )}
                      {s.screenshot && (
                        <a href={api.getFrameImageUrl(s.screenshot)} target="_blank" rel="noopener noreferrer"
                          className="self-start inline-flex items-center gap-1 text-[11px] font-semibold text-nexus-600 hover:text-nexus-800">
                          <Camera className="h-3 w-3" /> screenshot
                        </a>
                      )}
                    </div>
                  </td>
                  )}
                  {showDetails && (
                  <td className="py-2" title={s.confidence_reason || ''}>
                    {s.confidence === 'high' ? (
                      <span className="inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-[10px] font-bold" style={{ background: 'rgba(5,150,105,0.12)', color: '#047857' }}><CheckCircle2 className="h-3 w-3" /> Solid</span>
                    ) : s.confidence === 'review' ? (
                      <span className="inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-[10px] font-bold cursor-help" style={{ background: 'rgba(217,162,58,0.16)', color: '#92661d' }}><AlertTriangle className="h-3 w-3" /> Review</span>
                    ) : s.confidence === 'confirm' ? (
                      <span className="inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-[10px] font-bold cursor-help" style={{ background: 'rgba(217,162,58,0.20)', color: '#92661d' }}><AlertTriangle className="h-3 w-3" /> Confirm value</span>
                    ) : <span className="text-nexus-300">—</span>}
                  </td>
                  )}
                </tr>
              );})}
            </tbody>
          </table>
          {row.test_case?.expected_outcome && (
            <div className="mt-2 rounded-lg border border-emerald-200 bg-emerald-50/60 px-3 py-2 text-[12px] leading-snug">
              <span className="font-bold text-emerald-700">Expected outcome: </span>
              <span className="text-nexus-700">{row.test_case.expected_outcome}</span>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function CoArchitectChat({ artifactId, onAdded }: { artifactId: string; onAdded?: () => void }) {
  const [open, setOpen] = useState(false);
  const [messages, setMessages] = useState<{ role: string; content: string }[]>([]);
  const [input, setInput] = useState('');
  const [sending, setSending] = useState(false);
  const [proposing, setProposing] = useState(false);
  const [committing, setCommitting] = useState(false);
  const [proposal, setProposal] = useState<any | null>(null);
  const [addNotice, setAddNotice] = useState<string | null>(null);

  const propose = async () => {
    const q = input.trim();
    if (!q || proposing || committing) return;
    setProposing(true); setProposal(null); setAddNotice(null);
    try {
      const res = await api.proposeTestCase(artifactId, q, messages.slice(-6));
      if (res?.error) setAddNotice(res.answer || 'Could not ground that request to your captured pages.');
      else setProposal({ ...res, request: q });
    } catch (e: any) { setAddNotice('Error: ' + (e?.response?.data?.detail || String(e))); }
    finally { setProposing(false); }
  };

  const commit = async () => {
    if (!proposal || committing) return;
    setCommitting(true); setAddNotice(null);
    try {
      const res = await api.addTestCase(artifactId, { name: proposal.name, message: proposal.request || proposal.name, steps: proposal.grounded_steps || [] });
      setAddNotice('Added "' + proposal.name + '" (' + (res.steps ?? 0) + ' steps) to your test cases below.');
      setProposal(null); setInput('');
      onAdded?.();
    } catch (e: any) { setAddNotice('Error: ' + (e?.response?.data?.detail || String(e))); }
    finally { setCommitting(false); }
  };

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
    <div className="rounded-xl border border-nexus-200 bg-white overflow-hidden shadow-card">
      <button onClick={() => setOpen((o) => !o)} className="w-full flex items-center gap-2 px-4 py-2.5 text-left hover:bg-nexus-50/60 transition-colors">
        <span className="flex h-6 w-6 items-center justify-center rounded-lg bg-nexus-50 ring-1 ring-nexus-100">
          <Bot className="h-3.5 w-3.5 text-nexus-600" />
        </span>
        <span className="text-[13px] font-bold text-nexus-900">Co-Architect</span>
        <span className="text-[11px] text-nexus-400 font-medium">· grounded in your recording · GPT-4o</span>
        <span className="ml-auto inline-flex items-center gap-1 text-[11px] text-nexus-600 font-semibold">{open ? 'Hide' : 'Ask / Add'} <ChevronDown className={`h-3.5 w-3.5 transition-transform ${open ? 'rotate-180' : ''}`} /></span>
      </button>
      {open && (
        <div className="px-3 pb-3 space-y-2 border-t border-nexus-100 pt-2">
          <div className="max-h-64 overflow-y-auto space-y-2">
            {messages.length === 0 && (
              <p className="text-[12px] text-nexus-400 px-1">
                Ask about your recording or the generated tests — e.g. “What did the user fill in?”, “Explain the negative tests”, “Which tests cover the traveler page?”
              </p>
            )}
            {messages.map((m, i) => (
              <div key={i}
                className={`rounded-lg px-3 py-2 text-[12px] ${m.role === 'user' ? 'bg-nexus-100 text-nexus-900 ml-8' : 'bg-white border border-nexus-200 text-nexus-700 mr-8'}`}
                style={{ whiteSpace: 'pre-wrap' }}>{m.content}</div>
            ))}
            {sending && <div className="flex items-center gap-1.5 text-[12px] text-nexus-400 px-1"><Loader2 className="h-3 w-3 animate-spin" /> thinking…</div>}
          </div>
          <div className="flex items-center gap-2">
            <input value={input} onChange={(e) => setInput(e.target.value)}
              onKeyDown={(e) => { if (e.key === 'Enter') void send(); }}
              placeholder="Ask Co-Architect…" disabled={sending}
              className="flex-1 rounded-lg border border-nexus-200 px-3 py-1.5 text-[12px] focus:outline-none focus:border-nexus-400" />
            <button onClick={() => void propose()} disabled={proposing || committing || !input.trim()}
              title="Draft a NEW grounded test case from your description"
              className="btn-primary btn-gold flex items-center gap-1 rounded-lg px-3 py-1.5 text-xs font-semibold ring-1 ring-gold-300/40 disabled:opacity-50">
              {proposing ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Sparkles className="h-3.5 w-3.5" />} Propose case
            </button>
            <button onClick={() => void send()} disabled={sending || !input.trim()}
              className="flex items-center gap-1 rounded-lg border border-nexus-200 px-3 py-1.5 text-xs font-semibold text-nexus-700 hover:bg-nexus-50 disabled:opacity-50">
              <Send className="h-3.5 w-3.5" /> Send
            </button>
          </div>
          {addNotice && (
            <div className="rounded-lg px-3 py-2 text-[12px] font-semibold" style={{ background: 'rgba(5,150,105,0.10)', color: '#047857' }}>{addNotice}</div>
          )}
          {proposal && (
            <div className="rounded-xl border border-emerald-300 p-3 space-y-2" style={{ background: 'rgba(5,150,105,0.05)' }}>
              <div className="flex items-center gap-2">
                <Sparkles className="h-3.5 w-3.5 text-emerald-600" />
                <span className="text-[12px] font-bold text-nexus-900">{proposal.name}</span>
                <span className="text-[10px] text-nexus-400 font-semibold">proposed - {proposal.case_pages ?? 0} page(s), {(proposal.proposed_case?.steps || []).length} steps</span>
              </div>
              <div className="max-h-56 overflow-y-auto rounded-lg border border-nexus-200 bg-white">
                <table className="w-full text-[12px]">
                  <tbody>
                    {(proposal.proposed_case?.steps || []).map((s: any, i: number) => (
                      <tr key={i} className="border-b border-nexus-100 last:border-0">
                        <td className="py-1 px-2 text-nexus-400 w-8 align-top">{s.step_number ?? i + 1}</td>
                        <td className="py-1 px-2 text-nexus-900">{s.action}</td>
                        <td className="py-1 px-2 text-nexus-500">{s.expected_result || s.expected || ''}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              {proposal.notes && <p className="text-[10px] text-nexus-500">{proposal.notes}</p>}
              {(proposal.dropped?.length || 0) > 0 && (
                <p className="text-[10px] text-amber-600">{proposal.dropped.length} step(s) could not be grounded and were left out.</p>
              )}
              <div className="flex items-center gap-2">
                <button onClick={() => void commit()} disabled={committing}
                  className="flex items-center gap-1 rounded-lg px-3 py-1.5 text-xs font-bold bg-emerald-600 text-white hover:bg-emerald-500 disabled:opacity-50">
                  {committing ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <CheckCircle2 className="h-3.5 w-3.5" />} Commit - add to test cases
                </button>
                <button onClick={() => setProposal(null)} disabled={committing}
                  className="rounded-lg px-3 py-1.5 text-xs font-medium bg-white border border-nexus-200 text-nexus-600 hover:bg-nexus-50">Discard</button>
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
