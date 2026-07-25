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
  AlertTriangle, Download, ExternalLink, FileSpreadsheet, FileText,
  Loader2, Package, RefreshCw, ShieldCheck,
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
                  <p className="mt-2 text-[11px] text-slate-500">
                    One signature = one defect with N occurrences, so a recurring defect is never counted twice.
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
