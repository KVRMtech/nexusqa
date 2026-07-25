/**
 * Execution Evidence Report — the Certificate of Execution, in-product.
 *
 * Design rules inherited from the report itself (they are doctrine, not taste):
 *  - the Trust Block leads: the suite had to prove itself on the baseline
 *    BEFORE it was allowed to judge the application;
 *  - every rollup shows ALL SEVEN buckets including zeros — there is no code
 *    path here that renders a lone green badge;
 *  - a case that did not execute is shown as such, never folded into a pass;
 *  - "Completed with Defects" is a SUCCESS of the product (we caught a real
 *    application defect) and is styled apart from "Execution Error", which is
 *    our own automation failing.
 */
import { useCallback, useEffect, useState } from 'react';
import {
  AlertTriangle, ChevronRight, ClipboardCheck, Clock, Download, ExternalLink, FileSpreadsheet,
  FileText, Loader2, Package, RefreshCw, ShieldCheck,
} from 'lucide-react';
import { api } from './factoryApi';

type Props = { artifactId: string };

const BUCKETS: Array<{ key: string; label: string; cls: string }> = [
  { key: 'passed', label: 'Passed', cls: 'bg-emerald-50 text-emerald-700 border-emerald-200' },
  { key: 'defect_found', label: 'Defect Found', cls: 'bg-amber-50 text-amber-700 border-amber-200' },
  { key: 'execution_error', label: 'Execution Error', cls: 'bg-rose-50 text-rose-700 border-rose-200' },
  { key: 'blocked', label: 'Blocked', cls: 'bg-yellow-50 text-yellow-800 border-yellow-200' },
  { key: 'needs_review', label: 'Needs Review', cls: 'bg-indigo-50 text-indigo-700 border-indigo-200' },
  { key: 'skipped', label: 'Skipped', cls: 'bg-slate-100 text-slate-600 border-slate-300' },
  { key: 'cancelled', label: 'Cancelled', cls: 'bg-slate-100 text-slate-600 border-slate-300' },
];

function Counts({ counts }: { counts: Record<string, number> | undefined }) {
  if (!counts) return null;
  const notExec = Number(counts.not_executed || 0);
  return (
    <div className="flex flex-wrap gap-1.5">
      {BUCKETS.map((b) => {
        const n = Number(counts[b.key] || 0);
        return (
          <span
            key={b.key}
            className={`rounded-full border px-2.5 py-0.5 text-[11px] font-semibold tabular-nums ${b.cls} ${n === 0 ? 'opacity-40' : ''}`}
          >
            {b.label}: {n}
          </span>
        );
      })}
      {notExec > 0 && (
        <span className="rounded-full border border-slate-300 bg-slate-100 px-2.5 py-0.5 text-[11px] font-semibold tabular-nums text-slate-600">
          Not Executed: {notExec}
        </span>
      )}
    </div>
  );
}

function Stat({ label, value }: { label: string; value: any }) {
  return (
    <div className="rounded-lg bg-slate-50 px-3 py-2">
      <div className="text-[10px] uppercase tracking-wider text-slate-500">{label}</div>
      <div className="mt-0.5 text-[15px] tabular-nums text-slate-900">
        {value === null || value === undefined || value === '' ? '—' : String(value)}
      </div>
    </div>
  );
}

export default function EvidenceReportPanel({ artifactId }: Props) {
  const [runs, setRuns] = useState<any[]>([]);
  const [runId, setRunId] = useState<string>('');
  const [report, setReport] = useState<any>(null);
  const [analytics, setAnalytics] = useState<any>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [fStatus, setFStatus] = useState('');
  const [fType, setFType] = useState('');
  const [q, setQ] = useState('');
  const [openCase, setOpenCase] = useState<string>('');
  const [showTimeline, setShowTimeline] = useState(false);
  const [queue, setQueue] = useState<any>(null);
  const [forms, setForms] = useState<Record<string, any>>({});

  const setForm = (key: string, patch: any) =>
    setForms((prev) => ({ ...prev, [key]: { ...(prev[key] || {}), ...patch } }));

  const loadQueue = useCallback(async () => {
    try {
      setQueue(await api.getReviewQueue(artifactId, true));
    } catch {
      setQueue(null);   // the rest of the report is unaffected
    }
  }, [artifactId]);

  const submit = async (item: any, key: string) => {
    const f = forms[key] || {};
    setForm(key, { busy: true, error: '', done: false });
    try {
      const res = await api.recordReviewDisposition(artifactId, {
        scenario_id: item.scenario_id,
        step_number: item.step_number,
        disposition: f.disposition,
        reason: (f.reason || '').trim(),
        assignee: (f.assignee || '').trim() || undefined,
        signature_name: (f.signature_name || '').trim() || undefined,
        defect_signature: item.defect_signature,
      });
      setForm(key, { busy: false, done: true, signed: !!res?.electronically_signed });
      void loadQueue();
    } catch (e: any) {
      setForm(key, {
        busy: false,
        error: e?.response?.data?.detail || e?.message || 'could not record the disposition',
      });
    }
  };

  const loadRuns = useCallback(async () => {
    try {
      const r = await api.listEvidenceRuns(artifactId);
      setRuns(r?.runs || []);
    } catch {
      setRuns([]); // the report still works on its default run
    }
  }, [artifactId]);

  const load = useCallback(async (rid: string) => {
    setLoading(true);
    setError('');
    try {
      const [rep, an] = await Promise.all([
        api.getEvidenceReport(artifactId, rid || undefined),
        api.getEvidenceAnalytics(artifactId, rid || undefined).catch(() => null),
      ]);
      setReport(rep);
      setAnalytics(an);
    } catch (e: any) {
      setError(e?.response?.data?.detail || e?.message || 'could not load the report');
      setReport(null);
    } finally {
      setLoading(false);
    }
  }, [artifactId]);

  useEffect(() => { void loadRuns(); }, [loadRuns]);
  useEffect(() => { void loadQueue(); }, [loadQueue]);
  useEffect(() => { void load(runId); }, [load, runId]);

  const trust = report?.trust;
  const summary = report?.summary;
  const run = report?.run;
  const coverage = report?.coverage;
  const defects = report?.defects;
  const diff = report?.diff;

  const open = (kind: 'html' | 'zip' | 'csv' | 'junit' | 'xlsx' | 'pdf') => {
    window.open(api.getEvidenceUrl(artifactId, kind, runId || undefined), '_blank', 'noopener');
  };

  return (
    <div className="space-y-4">
      {/* header + run selector */}
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h3 className="text-[15px] font-semibold text-slate-900">Execution Evidence Report</h3>
          <p className="text-[12px] text-slate-500">
            The audit-grade Certificate of Execution — every number here resolves to a stored row.
          </p>
        </div>
        <div className="flex items-center gap-2">
          <select
            value={runId}
            onChange={(e) => setRunId(e.target.value)}
            className="max-w-[420px] rounded-md border border-slate-200 bg-white px-2 py-1.5 text-[12px] text-slate-700"
          >
            <option value="">Latest run (default)</option>
            {runs.map((r) => (
              <option key={r.run_id} value={r.run_id}>
                {(r.is_certification ? '★ certification · ' : `${r.environment} · `)}
                {r.started_at ? new Date(r.started_at).toLocaleString() : r.run_id.slice(0, 8)}
                {` — ${r.passed_steps}/${r.total_steps} steps passed`}
                {r.failed_steps ? `, ${r.failed_steps} failed` : ''}
                {r.skipped_steps ? `, ${r.skipped_steps} skipped` : ''}
              </option>
            ))}
          </select>
          <button
            onClick={() => { void loadRuns(); void load(runId); }}
            className="inline-flex items-center gap-1.5 rounded-md border border-slate-200 px-2.5 py-1.5 text-[12px] text-slate-600 hover:bg-slate-50"
          >
            <RefreshCw className="h-3.5 w-3.5" /> Refresh
          </button>
        </div>
      </div>

      {loading && (
        <div className="flex items-center gap-2 rounded-lg border border-slate-200 bg-white px-4 py-6 text-[13px] text-slate-600">
          <Loader2 className="h-4 w-4 animate-spin" /> Assembling the report…
        </div>
      )}

      {!loading && error && (
        <div className="flex items-start gap-2 rounded-lg border border-rose-200 bg-rose-50 px-4 py-3 text-[13px] text-rose-800">
          <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
          <div>{error}</div>
        </div>
      )}

      {!loading && report && (
        <>
          {/* ── Trust Block — opens the report, as in the document ── */}
          <div className="rounded-xl border border-slate-200 border-l-[3px] border-l-sky-500 bg-white p-4 shadow-sm">
            <div className="mb-1 flex items-center gap-2">
              <ShieldCheck className="h-4 w-4 text-sky-600" />
              <h4 className="text-[13px] font-semibold text-slate-900">Trust</h4>
              <span
                className={`rounded-full border px-2 py-0.5 text-[11px] font-semibold ${
                  trust?.certified
                    ? 'border-emerald-200 bg-emerald-50 text-emerald-700'
                    : 'border-amber-200 bg-amber-50 text-amber-700'
                }`}
              >
                {trust?.certified ? 'Certified' : 'Not yet certified'}
              </span>
            </div>
            <p className="text-[12px] leading-relaxed text-slate-500">{trust?.statement}</p>
            <div className="mt-3 grid grid-cols-2 gap-2 sm:grid-cols-4">
              <Stat label="Suite size" value={trust?.suite_size} />
              <Stat label="Quarantined" value={trust?.quarantined_count} />
              <Stat label="Uncertified exploratory" value={trust?.uncertified_exploratory_count} />
              <Stat label="Cert steps passed"
                    value={trust?.certification_run
                      ? `${trust.certification_run.passed_steps}/${trust.certification_run.total_steps}`
                      : '—'} />
            </div>
          </div>

          {/* ── Summary ── */}
          <div className="rounded-xl border border-slate-200 bg-white p-4 shadow-sm">
            <h4 className="mb-2 text-[13px] font-semibold text-slate-900">Execution summary</h4>
            <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
              <Stat label="Environment" value={run?.environment} />
              <Stat label="Run started" value={run?.started_at ? new Date(run.started_at).toLocaleString() : '—'} />
              <Stat label="Cases generated" value={summary?.total_cases_generated} />
              <Stat label="Steps executed" value={summary?.total_steps_executed} />
            </div>
            <div className="mt-3 space-y-2">
              <div className="text-[11px] uppercase tracking-wider text-slate-500">Test cases</div>
              <Counts counts={summary?.case_counts} />
              <div className="pt-1 text-[11px] uppercase tracking-wider text-slate-500">Steps</div>
              <Counts counts={summary?.step_counts} />
            </div>
            <p className="mt-3 text-[11px] text-slate-500">
              Every bucket is shown, including zeros. A step that did not execute is never counted as a pass.
            </p>
          </div>

          {/* ── Analytics ── */}
          {analytics && (
            <div className="rounded-xl border border-slate-200 bg-white p-4 shadow-sm">
              <h4 className="mb-2 text-[13px] font-semibold text-slate-900">Analytics</h4>
              <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
                <Stat label="Pass rate (executed)" value={analytics.pass_rate_pct != null ? `${analytics.pass_rate_pct}%` : '—'} />
                <Stat label="Defect rate" value={analytics.defect_rate_pct != null ? `${analytics.defect_rate_pct}%` : '—'} />
                <Stat label="Execution-error rate" value={analytics.execution_error_rate_pct != null ? `${analytics.execution_error_rate_pct}%` : '—'} />
                <Stat label="Avg case duration" value={analytics.avg_case_duration_ms != null ? `${analytics.avg_case_duration_ms} ms` : '—'} />
              </div>
              <div className="mt-2 flex flex-wrap gap-1.5">
                {Object.entries(analytics.evidence_class_distribution || {}).map(([k, v]) => (
                  <span key={k} className="rounded-md border border-slate-200 bg-slate-50 px-2 py-0.5 text-[11px] text-slate-600">
                    {k}: {String(v)}
                  </span>
                ))}
              </div>
              <p className="mt-2 text-[11px] text-slate-500">{analytics.honesty_note}</p>
            </div>
          )}

          {/* ── Defects + change ── */}
          {(defects?.unique_defects > 0 || diff?.available) && (
            <div className="grid gap-3 sm:grid-cols-2">
              {defects?.unique_defects > 0 && (
                <div className="rounded-xl border border-slate-200 bg-white p-4 shadow-sm">
                  <h4 className="mb-2 text-[13px] font-semibold text-slate-900">Defects (deduplicated)</h4>
                  <div className="grid grid-cols-3 gap-2">
                    <Stat label="Unique" value={defects.unique_defects} />
                    <Stat label="Occurrences" value={defects.total_occurrences} />
                    <Stat label="Runs in window" value={defects.window_runs} />
                  </div>
                  <div className="mt-2 flex flex-wrap gap-1.5">
                    {Object.entries(defects.by_lifecycle || {}).map(([k, v]) => (
                      <span key={k} className="rounded-md border border-slate-200 bg-slate-50 px-2 py-0.5 text-[11px] text-slate-600">
                        {k}: {String(v)}
                      </span>
                    ))}
                  </div>
                  {Object.keys(defects.by_severity || {}).length > 0 && (
                    <div className="mt-2 flex flex-wrap gap-1.5">
                      {Object.entries(defects.by_severity).map(([k, v]) => (
                        <span key={k} className={`rounded-md border px-2 py-0.5 text-[11px] font-semibold ${
                          k === 'critical' ? 'border-rose-200 bg-rose-50 text-rose-700'
                            : k === 'high' ? 'border-amber-200 bg-amber-50 text-amber-700'
                              : k === 'medium' ? 'border-indigo-200 bg-indigo-50 text-indigo-700'
                                : 'border-slate-200 bg-slate-100 text-slate-600'}`}>
                          severity {k}: {String(v)}
                        </span>
                      ))}
                    </div>
                  )}
                  <p className="mt-2 text-[11px] text-slate-500">
                    One signature = one defect with N occurrences, so a recurring defect is never counted twice.
                    Severity and priority are derived from countable signals (recurrence, regression, blast
                    radius, the case&apos;s own business priority) and are <b>suggested until a human confirms</b>.
                  </p>
                </div>
              )}
              {diff?.available && (
                <div className="rounded-xl border border-slate-200 bg-white p-4 shadow-sm">
                  <h4 className="mb-2 text-[13px] font-semibold text-slate-900">Change since previous run</h4>
                  <div className="grid grid-cols-2 gap-2">
                    <Stat label="Newly failing" value={diff.newly_failing_count} />
                    <Stat label="Fixed" value={diff.fixed_count} />
                    <Stat label="Still failing" value={diff.still_failing_count} />
                    <Stat label="Coverage lost" value={diff.coverage_lost_count} />
                  </div>
                  <p className="mt-2 text-[11px] text-slate-500">{diff.note}</p>
                </div>
              )}
            </div>
          )}

          {/* ── Coverage honesty ── */}
          <div className="rounded-xl border border-slate-200 bg-white p-4 shadow-sm">
            <h4 className="mb-1 text-[13px] font-semibold text-slate-900">Coverage honesty</h4>
            <p className="text-[12px] text-slate-500">{coverage?.note}</p>
            <div className="mt-2 grid grid-cols-3 gap-2">
              <Stat label="Not executed" value={coverage?.cases_not_executed_count} />
              <Stat label="Quarantined" value={coverage?.quarantined_count} />
              <Stat label="Uncertified exploratory" value={coverage?.uncertified_exploratory_count} />
            </div>
          </div>

          {/* ── §2.18 Needs-Review QUEUE — a queue with owners, not a label ── */}
          {queue && (queue.open_count > 0 || queue.resolved_count > 0) && (
            <div className="rounded-xl border border-slate-200 bg-white p-4 shadow-sm">
              <div className="mb-2 flex flex-wrap items-center gap-2">
                <ClipboardCheck className="h-4 w-4 text-indigo-600" />
                <h4 className="mr-auto text-[13px] font-semibold text-slate-900">
                  Review queue
                </h4>
                <span className="text-[11px] text-slate-500">
                  {queue.open_count} open · {queue.resolved_count} dispositioned
                </span>
              </div>
              <p className="mb-3 text-[11px] text-slate-500">{queue.note}</p>

              <div className="divide-y divide-slate-100 rounded-lg border border-slate-200">
                {(queue.open || []).map((it: any) => {
                  const key = `${it.scenario_id}:${it.step_number}`;
                  const f = forms[key] || {};
                  return (
                    <div key={key} className="p-3">
                      <div className="flex flex-wrap items-center gap-2">
                        <span className={`rounded-full border px-2 py-0.5 text-[10px] font-semibold ${
                          it.severity === 'critical' ? 'border-rose-200 bg-rose-50 text-rose-700'
                            : it.severity === 'high' ? 'border-amber-200 bg-amber-50 text-amber-700'
                              : it.severity === 'unset' ? 'border-slate-300 bg-slate-100 text-slate-600'
                                : 'border-indigo-200 bg-indigo-50 text-indigo-700'}`}>
                          severity {it.severity}
                        </span>
                        <span className="flex-1 truncate text-[12px] text-slate-800">
                          {it.case_name} · step {it.step_number}
                        </span>
                        <span className="text-[11px] text-slate-500">
                          {it.cause} · ×{it.occurrence_count} · blast {it.blast_radius}
                        </span>
                      </div>
                      {(it.assessment_reasons || []).slice(0, 2).map((r: string, i: number) => (
                        <p key={i} className="mt-1 border-l-2 border-slate-200 pl-2 text-[11px] text-slate-500">{r}</p>
                      ))}
                      <div className="mt-2 flex flex-wrap items-center gap-1.5">
                        <select
                          value={f.disposition || ''}
                          onChange={(e) => setForm(key, { disposition: e.target.value })}
                          className="rounded-md border border-slate-200 bg-white px-2 py-1 text-[11px] text-slate-700"
                        >
                          <option value="">Disposition…</option>
                          {(queue.dispositions || []).map((d: string) => (
                            <option key={d} value={d}>{d.replace(/_/g, ' ')}</option>
                          ))}
                        </select>
                        <input
                          value={f.assignee || ''}
                          onChange={(e) => setForm(key, { assignee: e.target.value })}
                          placeholder="Assignee"
                          className="w-32 rounded-md border border-slate-200 px-2 py-1 text-[11px]"
                        />
                        <input
                          value={f.reason || ''}
                          onChange={(e) => setForm(key, { reason: e.target.value })}
                          placeholder="Reason (required)"
                          className="min-w-[180px] flex-1 rounded-md border border-slate-200 px-2 py-1 text-[11px]"
                        />
                        <input
                          value={f.signature_name || ''}
                          onChange={(e) => setForm(key, { signature_name: e.target.value })}
                          placeholder="Type full name to sign"
                          className="w-44 rounded-md border border-slate-200 px-2 py-1 text-[11px]"
                        />
                        <button
                          disabled={!f.disposition || !(f.reason || '').trim() || f.busy}
                          onClick={() => void submit(it, key)}
                          className="rounded-md bg-indigo-600 px-2.5 py-1 text-[11px] font-medium text-white disabled:opacity-40"
                        >
                          {f.busy ? 'Recording…' : 'Record'}
                        </button>
                      </div>
                      {f.error && <p className="mt-1 text-[11px] text-rose-700">{f.error}</p>}
                      {f.done && (
                        <p className="mt-1 text-[11px] text-emerald-700">
                          Recorded on the audit chain{f.signed ? ' (electronically signed)' : ' (unsigned)'}.
                        </p>
                      )}
                    </div>
                  );
                })}
                {queue.open_count === 0 && (
                  <div className="p-3 text-[12px] text-slate-500">
                    Nothing awaiting a human decision.
                  </div>
                )}
              </div>
              <p className="mt-2 text-[11px] text-slate-500">
                A reason is required, and an unsigned disposition is recorded as unsigned —
                never presented as a sign-off.
              </p>
            </div>
          )}

          {/* ── §2.12 hierarchy + §2.13 filters — drill into the actual rows ── */}
          <div className="rounded-xl border border-slate-200 bg-white p-4 shadow-sm">
            <div className="mb-3 flex flex-wrap items-center gap-2">
              <h4 className="mr-auto text-[13px] font-semibold text-slate-900">
                Flows → cases → steps
              </h4>
              <input
                value={q}
                onChange={(e) => setQ(e.target.value)}
                placeholder="Search case or step…"
                className="w-48 rounded-md border border-slate-200 px-2 py-1 text-[12px] text-slate-700"
              />
              <select value={fStatus} onChange={(e) => setFStatus(e.target.value)}
                      className="rounded-md border border-slate-200 bg-white px-2 py-1 text-[12px] text-slate-700">
                <option value="">All statuses</option>
                {['passed', 'completed_with_defects', 'defect_found_halted', 'execution_error',
                  'needs_review', 'blocked', 'skipped', 'not_executed'].map((v) => (
                    <option key={v} value={v}>{v.replace(/_/g, ' ')}</option>
                  ))}
              </select>
              <select value={fType} onChange={(e) => setFType(e.target.value)}
                      className="rounded-md border border-slate-200 bg-white px-2 py-1 text-[12px] text-slate-700">
                <option value="">All types</option>
                <option value="functional">functional</option>
                <option value="combination">combination</option>
              </select>
            </div>

            <div className="space-y-3">
              {(report.flows || []).map((f: any) => {
                const cases = (f.cases || []).filter((c: any) => {
                  if (fStatus && c.status !== fStatus) return false;
                  if (fType && String(c.test_type || '').toLowerCase() !== fType) return false;
                  if (q) {
                    const hay = [c.name, c.description,
                      ...(c.steps || []).map((s: any) => `${s.action} ${s.actual}`)]
                      .join(' ').toLowerCase();
                    if (!hay.includes(q.toLowerCase())) return false;
                  }
                  return true;
                });
                if (!cases.length) return null;
                return (
                  <div key={f.flow_key}>
                    <div className="mb-1 flex flex-wrap items-center gap-2">
                      <span className="text-[12px] font-semibold text-slate-800">{f.flow_label}</span>
                      <span className="text-[11px] text-slate-500">
                        {cases.length} of {f.case_count} cases
                        {f.pass_percentage != null ? ` · pass ${f.pass_percentage}%` : ''}
                      </span>
                    </div>
                    <div className="divide-y divide-slate-100 rounded-lg border border-slate-200">
                      {cases.map((c: any) => {
                        const isOpen = openCase === c.test_case_id;
                        const chip = BUCKETS.find((b) => b.key === c.status)?.cls
                          || (c.status === 'completed_with_defects'
                            ? 'bg-amber-50 text-amber-700 border-amber-200'
                            : 'bg-slate-100 text-slate-600 border-slate-300');
                        return (
                          <div key={c.test_case_id}>
                            <button
                              onClick={() => setOpenCase(isOpen ? '' : c.test_case_id)}
                              className="flex w-full items-center gap-2 px-3 py-2 text-left hover:bg-slate-50"
                            >
                              <ChevronRight
                                className={`h-3.5 w-3.5 shrink-0 text-slate-400 transition-transform ${isOpen ? 'rotate-90' : ''}`}
                              />
                              <span className={`rounded-full border px-2 py-0.5 text-[10px] font-semibold ${chip}`}>
                                {String(c.status || '').replace(/_/g, ' ')}
                              </span>
                              <span className="flex-1 truncate text-[12px] text-slate-800">{c.name}</span>
                              <span className="shrink-0 text-[11px] tabular-nums text-slate-500">
                                {c.steps_executed}/{c.steps_declared} steps · {c.duration_ms} ms
                              </span>
                            </button>
                            {isOpen && (
                              <div className="bg-slate-50/60 px-3 pb-3 pt-1">
                                {!c.executed && (
                                  <p className="mb-2 text-[11px] text-slate-600">
                                    <b>Not executed:</b> {c.not_executed_reason}
                                  </p>
                                )}
                                <div className="overflow-x-auto">
                                  <table className="w-full text-[11px]">
                                    <thead>
                                      <tr className="text-left text-slate-500">
                                        <th className="py-1 pr-2">#</th>
                                        <th className="py-1 pr-2">Status</th>
                                        <th className="py-1 pr-2">Action</th>
                                        <th className="py-1 pr-2">Expected</th>
                                        <th className="py-1 pr-2">Actual / error</th>
                                        <th className="py-1">Evidence</th>
                                      </tr>
                                    </thead>
                                    <tbody>
                                      {(c.steps || []).map((s: any) => (
                                        <tr key={s.step_number} className="border-t border-slate-200 align-top">
                                          <td className="py-1 pr-2 tabular-nums text-slate-500">{s.step_number}</td>
                                          <td className="py-1 pr-2">
                                            <span className={`rounded px-1.5 py-0.5 text-[10px] font-semibold ${
                                              s.status === 'passed' ? 'bg-emerald-50 text-emerald-700'
                                                : s.status === 'defect_found' ? 'bg-amber-50 text-amber-700'
                                                  : s.status === 'execution_error' ? 'bg-rose-50 text-rose-700'
                                                    : s.status === 'needs_review' ? 'bg-indigo-50 text-indigo-700'
                                                      : 'bg-slate-100 text-slate-600'}`}>
                                              {String(s.status || '').replace(/_/g, ' ')}
                                            </span>
                                          </td>
                                          <td className="py-1 pr-2 text-slate-700">{s.action}</td>
                                          <td className="py-1 pr-2 text-slate-600">
                                            {s.expected}
                                            <div className="text-[10px] font-semibold text-slate-400">
                                              {s.evidence_class}
                                            </div>
                                          </td>
                                          <td className="py-1 pr-2 text-slate-600">
                                            <div className="max-w-[420px] whitespace-pre-wrap break-words">
                                              {s.actual}
                                            </div>
                                            {s.analysis && (
                                              <div className="mt-1 rounded border border-dashed border-indigo-300 bg-indigo-50/60 p-1.5">
                                                <div className="text-[9px] font-bold uppercase tracking-wide text-indigo-700">
                                                  AI-suggested — confirm before acting
                                                </div>
                                                <div className="text-slate-700">
                                                  <b>{s.analysis.cause}</b>
                                                  {s.analysis.category ? ` · ${s.analysis.category}` : ''}
                                                </div>
                                                <div className="text-slate-600">{s.analysis.detail}</div>
                                              </div>
                                            )}
                                          </td>
                                          <td className="py-1">
                                            {s.evidence?.screenshot_url && (
                                              <a className="text-sky-700 underline"
                                                 href={api.getRunScreenshotUrl(s.evidence.screenshot_url)}
                                                 target="_blank" rel="noreferrer">shot</a>
                                            )}
                                            {s.evidence?.trace_url && (
                                              <a className="ml-2 text-sky-700 underline"
                                                 href={api.getRunScreenshotUrl(s.evidence.trace_url)}
                                                 target="_blank" rel="noreferrer">trace</a>
                                            )}
                                          </td>
                                        </tr>
                                      ))}
                                    </tbody>
                                  </table>
                                </div>
                              </div>
                            )}
                          </div>
                        );
                      })}
                    </div>
                  </div>
                );
              })}
            </div>
            <p className="mt-2 text-[11px] text-slate-500">
              Filtering narrows this list only — the totals above still describe the whole
              execution, so a filtered view never reads as a smaller run.
            </p>
          </div>

          {/* ── §2.11 Execution timeline ── */}
          {report.timeline?.events?.length > 0 && (
            <div className="rounded-xl border border-slate-200 bg-white p-4 shadow-sm">
              <button onClick={() => setShowTimeline((v) => !v)}
                      className="flex w-full items-center gap-2 text-left">
                <Clock className="h-4 w-4 text-slate-500" />
                <h4 className="mr-auto text-[13px] font-semibold text-slate-900">Execution timeline</h4>
                <span className="text-[11px] text-slate-500">
                  {report.timeline.event_count} events{showTimeline ? '' : ' — show'}
                </span>
              </button>
              {showTimeline && (
                <div className="mt-2 max-h-96 overflow-y-auto">
                  <table className="w-full text-[11px]">
                    <tbody>
                      {report.timeline.events.map((e: any, i: number) => (
                        <tr key={i} className="border-t border-slate-100 align-top">
                          <td className="w-32 py-1 pr-2 tabular-nums text-slate-500">
                            {e.at ? new Date(e.at).toLocaleTimeString() : '—'}
                          </td>
                          <td className="w-20 py-1 pr-2">
                            {e.status && (
                              <span className={`rounded px-1.5 py-0.5 text-[10px] font-semibold ${
                                e.status === 'passed' ? 'bg-emerald-50 text-emerald-700'
                                  : e.status === 'failed' ? 'bg-rose-50 text-rose-700'
                                    : 'bg-slate-100 text-slate-600'}`}>{e.status}</span>
                            )}
                          </td>
                          <td className="py-1 text-slate-700">
                            {e.label}
                            {e.detail && (
                              <div className="whitespace-pre-wrap break-words text-slate-500">{e.detail}</div>
                            )}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                  <p className="mt-2 text-[11px] text-slate-500">{report.timeline.note}</p>
                </div>
              )}
            </div>
          )}

          {/* ── Open / export ── */}
          <div className="rounded-xl border border-slate-200 bg-white p-4 shadow-sm">
            <h4 className="mb-2 text-[13px] font-semibold text-slate-900">Open &amp; export</h4>
            <div className="flex flex-wrap gap-2">
              <button onClick={() => open('html')}
                      className="inline-flex items-center gap-1.5 rounded-md bg-sky-600 px-3 py-1.5 text-[12px] font-medium text-white hover:bg-sky-700">
                <ExternalLink className="h-3.5 w-3.5" /> Open full report
              </button>
              <button onClick={() => open('zip')}
                      className="inline-flex items-center gap-1.5 rounded-md border border-slate-200 px-3 py-1.5 text-[12px] text-slate-700 hover:bg-slate-50">
                <Package className="h-3.5 w-3.5" /> Evidence package (.zip)
              </button>
              <button onClick={() => open('pdf')}
                      className="inline-flex items-center gap-1.5 rounded-md border border-slate-200 px-3 py-1.5 text-[12px] text-slate-700 hover:bg-slate-50">
                <FileText className="h-3.5 w-3.5" /> PDF
              </button>
              <button onClick={() => open('xlsx')}
                      className="inline-flex items-center gap-1.5 rounded-md border border-slate-200 px-3 py-1.5 text-[12px] text-slate-700 hover:bg-slate-50">
                <FileSpreadsheet className="h-3.5 w-3.5" /> Excel
              </button>
              <button onClick={() => open('csv')}
                      className="inline-flex items-center gap-1.5 rounded-md border border-slate-200 px-3 py-1.5 text-[12px] text-slate-700 hover:bg-slate-50">
                <Download className="h-3.5 w-3.5" /> CSV
              </button>
              <button onClick={() => open('junit')}
                      className="inline-flex items-center gap-1.5 rounded-md border border-slate-200 px-3 py-1.5 text-[12px] text-slate-700 hover:bg-slate-50">
                <Download className="h-3.5 w-3.5" /> JUnit XML
              </button>
            </div>
            <p className="mt-2 text-[11px] text-slate-500">
              The evidence package carries a SHA-256 manifest and an offline verifier, so a reviewer can
              confirm nothing was altered after export. Exporting requires an editor, manager or admin
              role — viewing the report does not.
            </p>
          </div>
        </>
      )}
    </div>
  );
}
