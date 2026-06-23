/**
 * PlaywrightExecutionPanel — the Playwright Execution page for one artifact.
 *
 * Lists every deterministically-generated script grouped by category
 * (Demonstrated / Suggested combinations / Negative / Boundary / Error-state),
 * shows each script's actual source, and lets you download it or copy the exact
 * run command. Below the list: how to run the suite (local + reporter env) and
 * the Grounded Triage results board.
 *
 * Source of truth is the backend manifest (same compilation as the downloaded
 * zip) — no assumptions, no client-side code generation.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  AlertTriangle, Check, CheckCircle2, CheckSquare, ChevronDown, ChevronRight, Copy, Database, Download,
  FileCode2, Gauge, Globe, History, Loader2, Lock, Pencil, Play, RefreshCw, RotateCcw, Rocket,
  Save, Server, ShieldAlert, ShieldCheck, SlidersHorizontal, Square, Terminal, Wand2,
} from 'lucide-react';
import { api } from '../services/api';
import TriagePanel from './TriagePanel';
import StepTimeline from './StepTimeline';

const RUN_CMD = 'npm install && npx playwright install --with-deps && npx playwright test';

// Category order + styling — mirrors TestCasesPanel SECTIONS so the two tabs
// group and colour identically.
const CATEGORY_ORDER: { type: string; label: string; accent: string; badge: string }[] = [
  { type: 'functional', label: 'Demonstrated', accent: '#059669', badge: 'rgba(16,185,129,0.15)' },
  { type: 'combination', label: 'Suggested combinations', accent: '#d97706', badge: 'rgba(245,158,11,0.15)' },
  { type: 'negative', label: 'Negative', accent: '#e11d48', badge: 'rgba(225,29,72,0.13)' },
  { type: 'boundary', label: 'Boundary', accent: '#7c3aed', badge: 'rgba(124,58,237,0.13)' },
  { type: 'error_state', label: 'Error-state', accent: '#dc2626', badge: 'rgba(220,38,38,0.13)' },
];

interface ScriptStats { total: number; solid: number; review: number; skipped: number; }
interface DataField { key: string; label: string; default: string; kind: string; }
interface Script {
  test_id: string; name: string; description: string; category: string;
  category_label: string; priority: string; path: string; code: string;
  lines: number; stats: ScriptStats; data_fields: DataField[]; base_url: string;
}
interface Manifest {
  artifact_id: string;
  scripts: Script[];
  project_files: { path: string; code: string }[];
  recorded_base_url: string;
  totals: { scripts: number; solid_steps: number; review_steps: number };
  run: {
    install: string; all: string; headed: string; ui: string; report: string;
    reporter_env: Record<string, string>;
  };
}

function downloadText(filename: string, text: string, mime = 'text/plain') {
  const url = URL.createObjectURL(new Blob([text], { type: mime }));
  const a = document.createElement('a');
  a.href = url; a.download = filename; a.click();
  URL.revokeObjectURL(url);
}

function runStatusStyle(status: string): Record<string, string> {
  if (status === 'passed') return { background: 'rgba(34,197,94,0.16)', color: '#15803d' };
  if (status === 'failed') return { background: 'rgba(239,68,68,0.16)', color: '#b91c1c' };
  if (status === 'timed_out' || status === 'error') return { background: 'rgba(245,158,11,0.18)', color: '#b45309' };
  return { background: 'rgba(100,116,139,0.14)', color: '#475569' };
}

// Auto-Heal Run panel — live re-run stream + the step-by-step heal trace + the
// terminal badge (Clean Run - V1 ✓ / stopped — needs human).
function AutoHealPanel({ job, live, healing, err }: { job: any; live: string | null; healing: boolean; err: string | null }) {
  const trace: any[] = job?.heal_trace || [];
  const terminal = job?.terminal_state;
  const label = (e: any) => {
    if (e.event === 'run_started') return `Run #${e.iteration} — executing ${e.scripts} script(s)…`;
    if (e.event === 'heal_applied') return `Fixed step ${e.step}${e.label ? ` ('${e.label}')` : ''} — ${e.fix}; re-running…`;
    if (e.event === 'stop_needs_human') return `Stopped at step ${e.step} — ${e.cause} needs a human.`;
    if (e.event === 'stop_no_progress') return `Stopped at step ${e.step} — the fix didn't make it pass.`;
    if (e.event === 'clean_run_v1') return `All green — saved Clean Run - V1${e.version_no ? ` (v${e.version_no})` : ''}.`;
    return e.event;
  };
  return (
    <div className="mt-3 rounded-lg border-2 border-violet-200 bg-violet-50/40 p-3">
      <p className="flex items-center gap-1.5 text-[12px] font-black text-violet-900 mb-2">
        <Wand2 className="h-4 w-4" /> Auto-Heal {healing && <Loader2 className="h-3.5 w-3.5 animate-spin" />}
        <span className="text-[10px] font-semibold text-slate-500 normal-case">diagnose → fix → re-run → continue → Clean Run V1</span>
      </p>
      {live && healing && (
        <div className="mb-2 rounded-md overflow-hidden border-2 border-violet-300 bg-black">
          <div className="px-2 py-0.5 text-[9px] font-bold uppercase text-white bg-violet-600 flex items-center gap-1.5">
            <span className="inline-block h-2 w-2 rounded-full bg-red-400 animate-pulse" /> Live — watching the auto-heal re-runs
          </div>
          <iframe title="Auto-heal live" src={live} className="w-full" style={{ height: 460, border: 0 }} />
        </div>
      )}
      {trace.length > 0 && (
        <ol className="space-y-1 mb-1">
          {trace.map((e, i) => (
            <li key={i} className="flex items-start gap-1.5 text-[10.5px] text-slate-700">
              <span className="mt-0.5 text-violet-400">{e.event === 'clean_run_v1' ? '✓' : e.event.startsWith('stop') ? '■' : '•'}</span>
              <span>{label(e)}</span>
            </li>
          ))}
        </ol>
      )}
      {err && <p className="text-[10px] text-amber-700">{err}</p>}
      {terminal === 'clean_run_v1' && (
        <div className="mt-1 flex items-center gap-2 rounded-md border border-emerald-200 bg-emerald-50 px-2.5 py-1.5">
          <CheckCircle2 className="h-4 w-4 text-emerald-600 shrink-0" />
          <span className="text-[11px] font-bold text-emerald-800">
            Clean Run - V1 ✓{job?.clean_run_version ? ` (v${job.clean_run_version})` : ''} — healed {job?.healed_count ?? 0} script(s), verified green and saved as PROPOSED. Approve it to make V1 the active version a run uses — nothing auto-promotes.
          </span>
        </div>
      )}
      {(terminal === 'needs_human' || terminal === 'error') && (
        <div className="mt-1 flex items-start gap-2 rounded-md border border-amber-200 bg-amber-50 px-2.5 py-1.5">
          <AlertTriangle className="h-4 w-4 text-amber-600 shrink-0 mt-0.5" />
          <span className="text-[11px] font-semibold text-amber-800">
            Stopped — {job?.stop_reason || 'needs a human.'} Nothing was changed.
          </span>
        </div>
      )}
    </div>
  );
}

// Per-script fidelity scorecard — does the compiled Playwright faithfully
// implement the test case + verify its Expected Results?
function FidelityCard({ rep }: { rep: any }) {
  if (rep?.error) return <div className="border-t border-slate-200 px-3 py-2 text-[10px] text-amber-700">Audit error: {rep.error}</div>;
  const g = rep.grade === 'strong'
    ? { bg: 'rgba(34,197,94,0.14)', fg: '#15803d' }
    : rep.grade === 'partial'
      ? { bg: 'rgba(245,158,11,0.16)', fg: '#b45309' }
      : { bg: 'rgba(239,68,68,0.14)', fg: '#b91c1c' };
  const llm = rep.llm_review;
  return (
    <div className="border-t border-sky-100 bg-sky-50/40 px-3 py-2.5">
      <div className="flex items-center gap-2 flex-wrap mb-1.5">
        <ShieldAlert className="h-3.5 w-3.5 text-sky-600" />
        <span className="text-[11px] font-black text-sky-900">Fidelity</span>
        <span className="rounded px-2 py-0.5 text-[10px] font-black uppercase" style={{ background: g.bg, color: g.fg }}>{rep.grade} · {rep.score}%</span>
        <span className="text-[10px] text-slate-500 font-semibold">{rep.covered}/{rep.steps} steps covered · {rep.assertions} assertions / {rep.expected_results} expected</span>
        {rep.drift && <span className="rounded px-1.5 py-0.5 text-[9px] font-bold bg-amber-100 text-amber-700">stale — regenerate</span>}
      </div>
      {(rep.gaps || []).length > 0 && (
        <ul className="list-disc pl-4 space-y-0.5 mb-1">
          {rep.gaps.map((x: string, i: number) => <li key={i} className="text-[10px] text-slate-600">{x}</li>)}
        </ul>
      )}
      {llm && llm.reviewed && (
        <div className="mt-1 rounded-md border border-violet-200 bg-white/70 px-2 py-1.5">
          <p className="text-[10px] font-bold text-violet-800 mb-0.5">
            AI review: {llm.faithful ? 'faithful ✓' : 'gaps found'}{typeof llm.confidence === 'number' ? ` · ${Math.round(llm.confidence * 100)}%` : ''}
          </p>
          {(llm.gaps || []).slice(0, 6).map((x: string, i: number) => <p key={i} className="text-[10px] text-slate-600 leading-snug">• {x}</p>)}
        </div>
      )}
    </div>
  );
}

// Live Preflight — deterministic Playwright probe (no LLM) vs the live app:
// per-step locator resolution (1 good / 0 broken-or-renamed / >1 ambiguous) +
// select-option presence. The "proof it will run" before a full run.
// Status-driven render: groups rep.steps by the engine's honest status so EVERY bucket
// (and any future one) surfaces with its own remediation. Order = most-actionable first.
const PF_META: Record<string, { glyph: string; color: string; label: string }> = {
  absent: { glyph: '✗', color: '#b91c1c', label: 'not found — renamed or removed (re-anchor / re-capture)' },
  kind_mismatch: { glyph: '⟳', color: '#7c3aed', label: 'wrong control kind — a control with this name exists but as a different kind (heal / Add-select)' },
  strict_ambiguous: { glyph: '≣', color: '#be123c', label: 'ambiguous in strict mode — matches more than one element, so the run errors (tighten locator)' },
  value_mismatch: { glyph: '≠', color: '#be123c', label: 'value/option gone — the recorded selection no longer exists (re-capture the value)' },
  disabled: { glyph: '⊘', color: '#64748b', label: 'present but not actionable — disabled / read-only (a precondition may be unmet)' },
  hidden: { glyph: '◫', color: '#0369a1', label: 'present but hidden — behind a collapsed/conditional section (the test needs a reveal step)' },
  blocked: { glyph: '⤼', color: '#475569', label: 'not reached — the step’s page never loaded (fix an earlier step first; not a locator failure)' },
  ambiguous: { glyph: '⚠', color: '#b45309', label: 'ambiguous — matches more than one element (tighten locator)' },
};
const PF_ORDER = ['absent', 'kind_mismatch', 'strict_ambiguous', 'value_mismatch', 'disabled', 'hidden', 'blocked', 'ambiguous'];

function PreflightCard({ rep }: { rep: any }) {
  if (rep?.error) return (
    <div className="rounded-md border border-rose-200 bg-rose-50/50 px-3 py-2 text-[10px] text-rose-700">
      Live preflight error: {String(rep.error)}
    </div>
  );
  const probed = rep?.probed ?? 0;
  const resolved = rep?.resolved ?? 0;
  const pct = rep?.resolve_pct ?? 0;
  const authed = rep?.authenticated;
  const norm = (st: string) => (st || '').replace(/-/g, '_');
  // Primary path: group the per-step objects (each carries a `status`). Legacy fallback:
  // use the bucket arrays if `steps` is absent. Never count 'resolved' as a problem.
  const groups: Record<string, any[]> = {};
  const steps: any[] = Array.isArray(rep?.steps)
    ? rep.steps.filter((s: any) => s && s.kind !== 'nav' && (s.status || s.count !== undefined)) : [];
  if (steps.length) {
    for (const s of steps) {
      const st = norm(s.status || (s.count === 1 ? 'resolved' : (s.count > 1 ? 'ambiguous' : 'absent')));
      if (st === 'resolved') continue;
      (groups[st] = groups[st] || []).push(s);
    }
  } else {
    for (const k of PF_ORDER) { const arr = rep?.[k]; if (arr && arr.length) groups[k] = arr; }
    if (rep?.broken && !groups.absent && !groups.kind_mismatch) groups.absent = rep.broken;
  }
  const blockedN = (groups.blocked || []).length;
  const ok = probed > 0 && PF_ORDER.every((k) => !(groups[k] && groups[k].length));
  return (
    <div className="border-t border-emerald-100 bg-emerald-50/40 px-3 py-2.5">
      <div className="flex items-center gap-2 flex-wrap mb-1">
        <Globe className="h-3.5 w-3.5 text-emerald-600" />
        <span className="text-[11px] font-black text-emerald-900">Live preflight</span>
        <span className="rounded px-2 py-0.5 text-[10px] font-black"
          style={ok ? { background: 'rgba(16,185,129,0.16)', color: '#047857' } : { background: 'rgba(244,63,94,0.14)', color: '#be123c' }}>
          {resolved}/{probed} resolve · {pct}%
        </span>
        {blockedN > 0 && (
          <span className="text-[9px] text-slate-500" title="Resolution among the steps actually reached (excludes blocked).">
            {rep?.resolve_pct_reachable ?? pct}% of reached
          </span>
        )}
        {authed !== undefined && (
          <span className="text-[9px] font-bold" style={{ color: authed ? '#047857' : '#b45309' }}>
            {authed ? '🔓 authenticated' : '🔒 no saved session'}
          </span>
        )}
        {rep?.runner_status && <span className="text-[9px] text-slate-400">runner: {rep.runner_status}</span>}
      </div>
      {rep?.note && <p className="text-[10px] text-amber-700 mb-1">{rep.note}</p>}

      {PF_ORDER.map((k) => {
        const arr = groups[k]; if (!arr || !arr.length) return null;
        const m = PF_META[k];
        return (
          <div key={k} className="mb-1">
            <p className="text-[10px] font-bold mb-0.5" style={{ color: m.color }}>{m.glyph} {arr.length} {m.label}:</p>
            {arr.map((s: any, i: number) => (
              <div key={i} className="text-[10px] text-slate-600 pl-2">
                step {s.step}: <b>{s.label}</b> <span className="text-slate-400">({s.kind})</span>
                {s.foundRole ? <span className="text-violet-500"> — found as <code>{s.foundRole}</code></span> : null}
                {typeof s.count === 'number' && s.count > 1 ? <span className="text-slate-400"> — {s.count} matches</span> : null}
              </div>
            ))}
          </div>
        );
      })}
      {ok && <p className="text-[10px] text-emerald-700">✓ Every probed locator resolves to exactly one visible, actionable element — this test is runnable.</p>}
    </div>
  );
}

// Grounded Oracle, MEASURED — surfaces how grounded the board's verdicts are:
// VERIFIED (positive proof) vs ASSUMED (inference), a design-confidence rollup,
// and heal integrity (the engine never green-washes an unproven fix). Read-only;
// self-fetches and refreshes with the run key. Honesty-first throughout.
// Requirements Traceability Matrix (Phase 2 provenance) — recorded requirement →
// step → the assertion the compiler ACTUALLY emits. Lazy (fetches on first expand);
// 'unproven' = the compiler skips the step (inferred / needs-review) or nothing
// grounded backs it, so coverage is never overclaimed. Read-only, $0, reuses the
// same compiler lines as the downloaded script (anti-drift).
function RtmCard({ artifactId }: { artifactId: string }) {
  const [open, setOpen] = useState(false);
  const [rtm, setRtm] = useState<any>(null);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true); setErr(null);
    try { setRtm(await api.getRtm(artifactId)); }
    catch (e: any) { setErr(e?.response?.data?.detail || e?.message || 'failed to load'); }
    finally { setLoading(false); }
  }, [artifactId]);

  const toggle = () => {
    const next = !open; setOpen(next);
    if (next && !rtm && !loading) void load();
  };

  const tests: any[] = rtm?.tests || [];
  const totals = tests.reduce((acc, t) => {
    for (const r of (t.rows || [])) { acc.total++; if (r.unproven) acc.unproven++; else acc.proven++; }
    return acc;
  }, { total: 0, proven: 0, unproven: 0 });

  return (
    <div className="mt-3 rounded-xl border border-slate-200 bg-white/70 overflow-hidden">
      <button onClick={toggle} className="w-full flex items-center gap-2 px-3 py-2 text-left hover:bg-slate-50/70">
        <CheckSquare className="h-4 w-4 text-indigo-500" />
        <span className="text-[12px] font-bold text-slate-700">Requirements Traceability (RTM)</span>
        <span className="text-[10px] text-slate-400">recorded requirement → step → emitted assertion; proven vs unproven</span>
        {open ? <ChevronDown className="h-4 w-4 text-slate-400 ml-auto" /> : <ChevronRight className="h-4 w-4 text-slate-400 ml-auto" />}
      </button>
      {open && (
        <div className="px-3 pb-3 pt-1">
          {loading && <div className="flex items-center gap-2 text-[11px] text-indigo-700 py-2"><Loader2 className="h-3.5 w-3.5 animate-spin" /> Building the matrix…</div>}
          {err && <div className="rounded-lg border border-amber-200 bg-amber-50/60 px-3 py-2 text-[10px] text-amber-700">{err}</div>}
          {rtm && !loading && (
            tests.length === 0 ? (
              <p className="text-[11px] text-slate-500 py-2">No generated tests yet — generate test cases and the matrix fills in.</p>
            ) : (
              <>
                <div className="flex items-center gap-2 flex-wrap mb-2 text-[10px] font-bold">
                  <span className="rounded-full px-2 py-0.5" style={{ background: 'rgba(16,185,129,0.14)', color: '#047857' }}>{totals.proven} proven</span>
                  <span className="rounded-full px-2 py-0.5" style={{ background: 'rgba(245,158,11,0.16)', color: '#b45309' }}>{totals.unproven} unproven</span>
                  <span className="text-slate-400 font-semibold">· {tests.length} test{tests.length === 1 ? '' : 's'}, {totals.total} steps</span>
                </div>
                <p className="text-[9px] text-slate-400 leading-snug mb-2">Proven = the step has a GROUNDED assertion the compiler actually emits. Unproven = the compiler skips it (inferred / needs-review) or nothing grounded backs it — never claimed as covered.</p>
                <div className="space-y-2">
                  {tests.map((t) => (
                    <div key={t.test_id} className="rounded-lg border border-slate-100 bg-white">
                      <div className="px-2.5 py-1.5 border-b border-slate-100">
                        <span className="text-[11px] font-bold text-slate-800">{t.name}</span>
                        {t.expected_outcome && <span className="block text-[9px] text-slate-500 leading-snug mt-0.5">Expected: {t.expected_outcome}</span>}
                      </div>
                      <table className="w-full text-[10px]">
                        <tbody>
                          {(t.rows || []).map((r: any, i: number) => (
                            <tr key={i} className="border-t border-slate-50 align-top">
                              <td className="px-2 py-1 text-slate-400 font-mono w-6">{r.step_number ?? i + 1}</td>
                              <td className="px-1 py-1 text-slate-700">{r.action || '—'}{r.observed_label ? <span className="text-slate-400"> · {r.observed_label}</span> : ''}</td>
                              <td className="px-1 py-1 w-20">
                                {r.unproven
                                  ? <span className="rounded px-1.5 py-0.5 text-[9px] font-bold" style={{ background: 'rgba(245,158,11,0.16)', color: '#b45309' }}>unproven</span>
                                  : <span className="rounded px-1.5 py-0.5 text-[9px] font-bold" style={{ background: 'rgba(16,185,129,0.14)', color: '#047857' }}>proven</span>}
                              </td>
                              <td className="px-1 py-1 text-slate-400 w-16 text-right">{(r.emitted_assertions || []).length} assert{(r.emitted_assertions || []).length === 1 ? '' : 's'}</td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  ))}
                </div>
              </>
            )
          )}
        </div>
      )}
    </div>
  );
}


function OracleScorecardCard({ artifactId, refreshKey }: { artifactId: string; refreshKey: number }) {
  const [sc, setSc] = useState<any>(null);
  const [err, setErr] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    let alive = true;
    setLoading(true);
    setErr(null);
    api.getOracleScorecard(artifactId)
      .then((d) => { if (alive) setSc(d); })
      .catch((e) => { if (alive) setErr(e?.response?.data?.detail || e?.message || 'failed to load'); })
      .finally(() => { if (alive) setLoading(false); });
    return () => { alive = false; };
  }, [artifactId, refreshKey]);

  if (err) return (
    <div className="mb-3 rounded-xl border border-amber-200 bg-amber-50/60 px-3 py-2 text-[10px] text-amber-700">Oracle scorecard: {err}</div>
  );
  if (!sc) return loading ? (
    <div className="mb-3 flex items-center gap-2 rounded-xl border border-indigo-100 bg-indigo-50/40 px-3 py-2 text-[11px] text-indigo-700">
      <Loader2 className="h-3.5 w-3.5 animate-spin" /> Measuring the oracle…
    </div>
  ) : null;

  const g = sc.grounding || {};
  const oc = sc.oracle_confidence || {};
  const heal = sc.heal || {};
  const att = heal.attempts || {};
  const failures = g.failures || 0;
  const pctOf = (n: number) => (failures > 0 ? (n / failures) * 100 : 0);

  return (
    <div className="mb-3 rounded-2xl border border-indigo-200 bg-gradient-to-br from-indigo-50/80 to-white px-3.5 py-3">
      <div className="flex items-center gap-2 mb-2.5">
        <ShieldCheck className="h-4 w-4 text-indigo-600" />
        <span className="text-[12px] font-black text-indigo-900">Grounded Oracle</span>
        <span className="text-[10px] font-semibold text-slate-400">how grounded these verdicts are — measured, not claimed</span>
      </div>

      {!sc.has_runs ? (
        <p className="text-[11px] text-slate-500 leading-snug">No runs ingested yet — run the suite and the oracle scorecard fills in. Nothing is assumed.</p>
      ) : (
        <div className="space-y-3">
          {/* 1. GROUNDING — proven vs inferred over failures; green shown separately */}
          <div>
            <div className="flex items-baseline gap-2 mb-1">
              <span className="text-[11px] font-bold text-slate-700">Failure grounding</span>
              {failures > 0 ? (
                <>
                  <span className="text-[13px] font-black text-emerald-700">{g.proven_pct == null ? '—' : `${g.proven_pct}%`}</span>
                  <span className="text-[10px] text-slate-400 font-semibold">proven</span>
                </>
              ) : (
                <span className="text-[11px] font-semibold text-emerald-700">all green — no failed verdicts to ground</span>
              )}
            </div>
            {failures > 0 && (
              <>
                <div className="flex h-2 w-full overflow-hidden rounded-full bg-slate-100">
                  {g.proven > 0 && <div style={{ width: `${pctOf(g.proven)}%`, background: '#10b981' }} />}
                  {g.inferred > 0 && <div style={{ width: `${pctOf(g.inferred)}%`, background: '#f59e0b' }} />}
                  {g.not_measured > 0 && <div style={{ width: `${pctOf(g.not_measured)}%`, background: '#cbd5e1' }} />}
                </div>
                <div className="flex items-center gap-2 flex-wrap mt-1.5 text-[10px] font-semibold">
                  <span className="rounded-full px-2 py-0.5" style={{ background: 'rgba(16,185,129,0.14)', color: '#047857' }}>{g.proven} proven</span>
                  <span className="rounded-full px-2 py-0.5" style={{ background: 'rgba(245,158,11,0.16)', color: '#b45309' }}>{g.inferred} inferred</span>
                  {g.not_measured > 0 && <span className="rounded-full px-2 py-0.5" style={{ background: 'rgba(148,163,184,0.16)', color: '#64748b' }}>{g.not_measured} not measured</span>}
                </div>
              </>
            )}
            <div className="flex items-center gap-2 flex-wrap mt-1.5 text-[10px] font-semibold">
              <span className="rounded-full px-2 py-0.5" style={{ background: 'rgba(34,197,94,0.12)', color: '#15803d' }}>{g.green ?? 0} green</span>
            </div>
            <p className="text-[9px] text-slate-400 leading-snug mt-1">Proven = a real regression caught by a failed <code>toHaveURL</code> against the recorded page. Inferred = drift / flake / needs-review. Green ran without error — its outcome oracle isn’t separately re-proven here, so it’s not counted as verified.</p>
          </div>

          {/* 2. ORACLE CONFIDENCE */}
          <div className="border-t border-indigo-100 pt-2.5">
            <div className="flex items-center gap-2 mb-1">
              <Gauge className="h-3.5 w-3.5 text-indigo-500" />
              <span className="text-[11px] font-bold text-slate-700">Oracle confidence</span>
              <span className="text-[13px] font-black text-indigo-700">{typeof oc.avg_confidence_failures_only === 'number' ? `${Math.round(oc.avg_confidence_failures_only * 100)}%` : '—'}</span>
              <span className="text-[10px] text-slate-400 font-semibold">{typeof oc.avg_confidence_failures_only === 'number' ? `on ${oc.failures_scored} failure${oc.failures_scored === 1 ? '' : 's'}` : 'no failures to score'}</span>
            </div>
            <div className="flex items-center gap-2 flex-wrap text-[10px] font-semibold">
              <span className="rounded-full px-2 py-0.5" style={{ background: 'rgba(16,185,129,0.14)', color: '#047857' }}>{oc.distribution?.high ?? 0} high</span>
              <span className="rounded-full px-2 py-0.5" style={{ background: 'rgba(245,158,11,0.16)', color: '#b45309' }}>{oc.distribution?.medium ?? 0} medium</span>
              <span className="rounded-full px-2 py-0.5" style={{ background: 'rgba(239,68,68,0.14)', color: '#b91c1c' }}>{oc.distribution?.low ?? 0} low</span>
            </div>
            <p className="text-[9px] text-slate-400 leading-snug mt-1">Design confidence (heuristic) — fixed per-verdict precision beliefs, not a learned accuracy rate.</p>
          </div>

          {/* 3. HEAL INTEGRITY */}
          <div className="border-t border-indigo-100 pt-2.5">
            <div className="flex items-center gap-2 mb-1">
              <ShieldAlert className="h-3.5 w-3.5 text-indigo-500" />
              <span className="text-[11px] font-bold text-slate-700">Heal integrity</span>
              <span className="text-[10px] text-slate-400 font-semibold">never green-washes</span>
            </div>
            <div className="flex items-center gap-2 flex-wrap text-[10px] font-semibold mb-1">
              <span className="rounded-full px-2 py-0.5" style={{ background: 'rgba(99,102,241,0.12)', color: '#4338ca' }}>{att.applied_proposed ?? 0} verified-green → proposed (human-gated)</span>
              {(att.not_promoted ?? 0) > 0 && <span className="rounded-full px-2 py-0.5" style={{ background: 'rgba(148,163,184,0.16)', color: '#64748b' }}>{att.not_promoted} not promoted</span>}
              {(att.in_progress ?? 0) > 0 && <span className="rounded-full px-2 py-0.5" style={{ background: 'rgba(245,158,11,0.16)', color: '#b45309' }}>{att.in_progress} in progress</span>}
              {(att.attempted ?? 0) === 0 && <span className="text-slate-400">no heal attempts yet</span>}
            </div>
            <p className="text-[10px] text-slate-600 leading-snug">
              <span className="font-bold">False-heal proxy: </span>
              {heal.insufficient_data
                ? <span className="text-slate-500">insufficient data ({heal.approved ?? 0} approved heal{(heal.approved ?? 0) === 1 ? '' : 's'})</span>
                : <span>{heal.approved_then_contradicted}/{heal.approved_with_later_run} approved heals later contradicted{heal.approved_then_contradicted_pct != null ? ` (${heal.approved_then_contradicted_pct}%)` : ''}</span>}
            </p>
            <p className="text-[9px] text-slate-400 leading-snug mt-0.5">Best-effort proxy, not a true rate — only catches regressions that fail a <code>toHaveURL</code>, and can’t prove the heal (vs an unrelated change) caused it. {heal.population}.</p>
            {sc.false_heal_rate && (
              <p className="text-[10px] text-slate-600 leading-snug mt-1">
                <span className="font-bold">Published false-heal rate: </span>
                {sc.false_heal_rate.status === 'insufficient_data'
                  ? <span className="text-slate-500">insufficient data — n={sc.false_heal_rate.denominator_evaluated}, need ≥{sc.false_heal_rate.min_n_to_publish} to publish</span>
                  : <span className="font-black text-indigo-700">{sc.false_heal_rate.rate_pct}%<span className="font-semibold text-slate-400"> on n={sc.false_heal_rate.denominator_evaluated}</span></span>}
                <span className="text-slate-400"> · proxy until calibrated</span>
              </p>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

export default function PlaywrightExecutionPanel({ artifactId }: { artifactId: string }) {
  const [data, setData] = useState<Manifest | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [openCode, setOpenCode] = useState<Record<string, boolean>>({});
  const [copied, setCopied] = useState<string>('');
  const [busy, setBusy] = useState<string>('');
  const [howToRun, setHowToRun] = useState(false);

  // ── Run console state ──────────────────────────────────
  const [target, setTarget] = useState<'local' | 'sauce' | 'ci'>('local');
  const [selectedCats, setSelectedCats] = useState<Set<string>>(new Set());
  const [selectedScriptIds, setSelectedScriptIds] = useState<Set<string>>(new Set());
  const [expandedRow, setExpandedRow] = useState<string>('');   // A.1 row inline-actions drawer (keyed by test_id|path)
  const [baseUrl, setBaseUrl] = useState('');
  const [dataOverrides, setDataOverrides] = useState<Record<string, string>>({});
  const [runBusy, setRunBusy] = useState(false);
  const [runStatus, setRunStatus] = useState<any>(null);
  const [runErr, setRunErr] = useState<string | null>(null);
  const [liveUrl, setLiveUrl] = useState<string | null>(null);
  const [triageKey, setTriageKey] = useState(0);
  const [runs, setRuns] = useState<any>(null);
  const [timeline, setTimeline] = useState<any>(null);
  const [historyOpen, setHistoryOpen] = useState(false);
  // Console vs Run view — clicking "view results" after a run flips to a dedicated
  // results/triage page; "Back to Run Console" returns. Additive (no routing yet).
  const [view, setView] = useState<'console' | 'run'>('console');
  const [autoHealing, setAutoHealing] = useState(false);
  const [autoHealJob, setAutoHealJob] = useState<any>(null);
  const [autoHealLive, setAutoHealLive] = useState<string | null>(null);
  const [autoHealErr, setAutoHealErr] = useState<string | null>(null);
  const [fidelity, setFidelity] = useState<Record<string, any>>({});   // test_id -> scorecard
  const [suiteFid, setSuiteFid] = useState<any>(null);
  const [fidBusy, setFidBusy] = useState<string>('');                  // test_id | 'suite' | 'all'
  const [preflight, setPreflight] = useState<Record<string, any>>({}); // test_id -> live resolution report
  const [pfBusy, setPfBusy] = useState<string>('');                    // test_id being preflighted

  // ── Authentication (capture-once session) state ──────────
  const [authStatus, setAuthStatus] = useState<any>(null);   // { profile, capturing, encryption_available }
  const [authBusy, setAuthBusy] = useState<string>('');       // 'capture' | 'save' | 'cancel' | 'clear'
  const [captureLive, setCaptureLive] = useState<string | null>(null);
  const [authErr, setAuthErr] = useState<string | null>(null);

  const auditScript = async (testId: string) => {
    if (!testId) return;
    setFidBusy(testId);
    try {
      const rep = await api.getScriptFidelity(artifactId, testId, true);
      setFidelity((m) => ({ ...m, [testId]: rep }));
    } catch (e: any) {
      setFidelity((m) => ({ ...m, [testId]: { error: e?.response?.data?.detail || String(e) } }));
    } finally { setFidBusy(''); }
  };
  // Live Preflight — deterministic Playwright probe vs the live app (no LLM).
  const runPreflight = async (testId: string) => {
    if (!testId) return;
    const bu = (baseUrl || '').trim() || (data?.recorded_base_url || '');
    setPfBusy(testId);
    try {
      const rep = await api.runScriptPreflight(artifactId, testId, bu);
      setPreflight((m) => ({ ...m, [testId]: rep }));
    } catch (e: any) {
      setPreflight((m) => ({ ...m, [testId]: { error: e?.response?.data?.detail || String(e) } }));
    } finally { setPfBusy(''); }
  };
  const regenScript = async (testId: string) => {
    if (!testId) return;
    setFidBusy(testId + ':regen');
    try { await api.regenerateScript(artifactId, testId); await auditScript(testId); }
    catch { /* ignore */ }
    finally { setFidBusy(''); }
  };
  const auditSuite = async () => {
    setFidBusy('suite');
    try { setSuiteFid(await api.getSuiteFidelity(artifactId)); }
    catch (e: any) { setSuiteFid({ error: e?.response?.data?.detail || String(e) }); }
    finally { setFidBusy(''); }
  };
  const regenAll = async () => {
    setFidBusy('all');
    try {
      const body: any = {};
      if (selectedScripts.length) body.test_ids = selectedScripts.map((s: Script) => s.test_id).filter(Boolean);
      await api.regenerateAll(artifactId, body);
      await auditSuite();
    } catch { /* ignore */ }
    finally { setFidBusy(''); }
  };
  const resultsRef = useRef<HTMLDivElement | null>(null);
  const scrollToResults = () => resultsRef.current?.scrollIntoView({ behavior: 'smooth', block: 'start' });
  const liveRef = useRef<HTMLDivElement | null>(null);
  const [browsers, setBrowsers] = useState<Set<string>>(new Set(['chromium']));
  const [headed, setHeaded] = useState(false);
  const [workers, setWorkers] = useState(4);
  const [retries, setRetries] = useState(0);
  const [runningTestId, setRunningTestId] = useState<string | null>(null);
  const [perTestData, setPerTestData] = useState<Record<string, Record<string, string>>>({});
  const [openData, setOpenData] = useState<Record<string, boolean>>({});
  // Phase C — per-test script editor + versions
  const [openEdit, setOpenEdit] = useState<Record<string, boolean>>({});
  const [editSource, setEditSource] = useState<Record<string, string>>({});
  const [editDirty, setEditDirty] = useState<Record<string, boolean>>({});
  const [versions, setVersions] = useState<Record<string, any[]>>({});
  const [editedTests, setEditedTests] = useState<Record<string, number>>({});
  const [editBusy, setEditBusy] = useState<string>('');

  const refreshVersions = async (tid: string) => {
    try {
      const v = await api.listScriptVersions(artifactId, tid);
      setVersions((m) => ({ ...m, [tid]: v.versions || [] }));
    } catch { /* ignore */ }
  };

  const openEditor = async (s: Script) => {
    const id = s.test_id || s.path;
    const willOpen = !openEdit[id];
    setOpenEdit((m) => ({ ...m, [id]: willOpen }));
    if (!willOpen || !s.test_id || editSource[s.test_id] !== undefined) return;
    setEditBusy(id);
    try {
      const src = await api.getScriptSource(artifactId, s.test_id);
      setEditSource((m) => ({ ...m, [s.test_id]: src.script_source || '' }));
      if (src.edited) setEditedTests((m) => ({ ...m, [s.test_id]: src.version_no }));
      await refreshVersions(s.test_id);
    } catch (e: any) { setError(e?.response?.data?.detail || String(e)); }
    finally { setEditBusy(''); }
  };

  const saveVersion = async (tid: string) => {
    setEditBusy(`save:${tid}`); setError(null);
    try {
      const r = await api.saveScriptVersion(artifactId, {
        test_id: tid, script_source: editSource[tid] || '', data: perTestData[tid] || {},
      });
      setEditedTests((m) => ({ ...m, [tid]: r.version_no }));
      setEditDirty((m) => ({ ...m, [tid]: false }));
      await refreshVersions(tid);
    } catch (e: any) { setError(e?.response?.data?.detail || String(e)); }
    finally { setEditBusy(''); }
  };

  const restoreVersion = async (tid: string, versionNo: number) => {
    setEditBusy(`restore:${tid}`); setError(null);
    try {
      await api.restoreScriptVersion(artifactId, { test_id: tid, version_no: versionNo });
      const src = await api.getScriptSource(artifactId, tid);
      setEditSource((m) => ({ ...m, [tid]: src.script_source || '' }));
      if (src.edited) setEditedTests((m) => ({ ...m, [tid]: src.version_no }));
      setEditDirty((m) => ({ ...m, [tid]: false }));
      await refreshVersions(tid);
    } catch (e: any) { setError(e?.response?.data?.detail || String(e)); }
    finally { setEditBusy(''); }
  };

  // Per-test overrides with any value set (blank cells inherit global/observed).
  const buildDataByTest = () => {
    const out: Record<string, Record<string, string>> = {};
    for (const [tid, fields] of Object.entries(perTestData)) {
      const row: Record<string, string> = {};
      for (const [k, v] of Object.entries(fields)) if (v !== '' && v != null) row[k] = v;
      if (Object.keys(row).length) out[tid] = row;
    }
    return out;
  };

  const toggleBrowser = (b: string) => setBrowsers((prev) => {
    const next = new Set(prev);
    if (next.has(b)) { if (next.size > 1) next.delete(b); } else next.add(b);
    return next;
  });

  const refresh = useCallback(async () => {
    setLoading(true); setError(null);
    try { setData(await api.getPlaywrightManifest(artifactId)); }
    catch (e: any) { setError(e?.response?.data?.detail || String(e)); }
    finally { setLoading(false); }
  }, [artifactId]);
  useEffect(() => { void refresh(); }, [refresh]);

  // One-click Enrich from the cockpit — captures real control types (dropdowns/
  // radios) so the generated scripts run correctly; then reloads the manifest.
  const runEnrich = async () => {
    setBusy('enrich'); setError(null);
    try { await api.enrichTestFactory(artifactId); await refresh(); }
    catch (e: any) { setError(e?.response?.data?.detail || String(e)); }
    finally { setBusy(''); }
  };

  // A.4 — load the active version per script on mount so the version badge is
  // correct immediately (not only after opening an editor). Bounded + best-effort.
  useEffect(() => {
    const scripts = data?.scripts || [];
    if (!scripts.length || scripts.length > 30) return;
    scripts.forEach(async (s: Script) => {
      try {
        const src = await api.getScriptSource(artifactId, s.test_id);
        if (src?.edited) setEditedTests((m) => ({ ...m, [s.test_id]: src.version_no }));
      } catch { /* ignore */ }
    });
  }, [data, artifactId]);

  // Phase D — run history + flake (refetched after each run via triageKey).
  const refreshRuns = useCallback(async () => {
    try { setRuns(await api.getRunsSummary(artifactId)); } catch { /* ignore */ }
  }, [artifactId]);
  useEffect(() => { void refreshRuns(); }, [refreshRuns, triageKey]);

  // THIS RUN — per-scenario, per-step timeline for the single latest run. Its
  // header counts come off one run row, so they always agree with the steps
  // below (this is what resolves the old "1 vs 6" contradiction).
  const refreshTimeline = useCallback(async () => {
    try { setTimeline(await api.getLatestRunTimeline(artifactId)); } catch { /* ignore */ }
  }, [artifactId]);
  useEffect(() => { void refreshTimeline(); }, [refreshTimeline, triageKey]);

  // Auth profile (capture-once session) — refetched on load.
  const refreshAuth = useCallback(async () => {
    try { setAuthStatus(await api.getAuthStatus(artifactId)); } catch { /* optional surface */ }
  }, [artifactId]);
  useEffect(() => { void refreshAuth(); }, [refreshAuth]);

  const startCapture = async () => {
    setAuthBusy('capture'); setAuthErr(null);
    try {
      const r = await api.startAuthCapture(artifactId, baseUrl || data?.recorded_base_url || '');
      setCaptureLive(r.live_url || null);
      void refreshAuth();
    } catch (e: any) { setAuthErr(e?.response?.data?.detail || String(e)); }
    finally { setAuthBusy(''); }
  };
  const saveCapture = async () => {
    setAuthBusy('save'); setAuthErr(null);
    try { await api.saveAuthCapture(artifactId); setCaptureLive(null); void refreshAuth(); }
    catch (e: any) { setAuthErr(e?.response?.data?.detail || String(e)); }
    finally { setAuthBusy(''); }
  };
  const cancelCapture = async () => {
    setAuthBusy('cancel');
    try { await api.cancelAuthCapture(artifactId); } catch { /* best-effort */ }
    finally { setCaptureLive(null); setAuthBusy(''); void refreshAuth(); }
  };
  const clearAuth = async () => {
    setAuthBusy('clear'); setAuthErr(null);
    try { await api.clearAuthProfile(artifactId); void refreshAuth(); }
    catch (e: any) { setAuthErr(e?.response?.data?.detail || String(e)); }
    finally { setAuthBusy(''); }
  };

  // Auto-Heal Run — fire the orchestrator, stream the live re-runs, and poll the
  // heal trace until it freezes a Clean Run - V1 or stops toward a human.
  const runAutoHeal = async () => {
    setAutoHealing(true); setAutoHealErr(null); setAutoHealJob(null); setAutoHealLive(null);
    setView('run');   // jump to the dedicated results/live view
    try {
      const body: any = { base_url: baseUrl.trim(), data: buildData() };
      if (selectedScripts.length) body.test_ids = selectedScripts.map((s: Script) => s.test_id).filter(Boolean);
      else body.categories = Array.from(selectedCats);
      const r = await api.autoHealRun(artifactId, body);
      setAutoHealLive(r.live_url || null);
      const runId = r.run_id;
      for (let i = 0; i < 320; i++) {                 // ~13 min ceiling (matches loop)
        await new Promise((res) => setTimeout(res, 2500));
        const st = await api.getNexusRunStatus(artifactId, runId);
        setAutoHealJob(st);
        if (st.terminal_state) break;                 // clean_run_v1 | needs_human | error
      }
    } catch (e: any) {
      setAutoHealErr(e?.response?.data?.detail || String(e));
    } finally {
      setAutoHealing(false); setAutoHealLive(null); setTriageKey((k) => k + 1);
    }
  };

  // After a run completes, bring the (refreshed) results/triage board into view —
  // it renders below the scripts list and is easy to miss.
  useEffect(() => {
    if (triageKey > 0) { const t = setTimeout(scrollToResults, 450); return () => clearTimeout(t); }
  }, [triageKey]);

  // When a live run starts (e.g. from a per-script Live button below), bring the
  // streamed browser into view.
  useEffect(() => {
    if (liveUrl) { const t = setTimeout(() => liveRef.current?.scrollIntoView({ behavior: 'smooth', block: 'center' }), 300); return () => clearTimeout(t); }
  }, [liveUrl]);

  // Initialise the run console from the manifest (all categories on, recorded env).
  useEffect(() => {
    if (!data) return;
    setSelectedCats(new Set((data.scripts || []).map((s: Script) => s.category)));
    setSelectedScriptIds(new Set((data.scripts || []).map((s: Script) => s.test_id)));
    void auditSuite();
    setBaseUrl(data.recorded_base_url || '');
    setDataOverrides({});
    setPerTestData({});
  }, [data]);

  const toggleScript = (tid: string) => setSelectedScriptIds((prev) => {
    const next = new Set(prev);
    if (next.has(tid)) next.delete(tid); else next.add(tid);
    return next;
  });
  const toggleCatItems = (items: Script[]) => setSelectedScriptIds((prev) => {
    const next = new Set(prev);
    const allOn = items.every((s) => next.has(s.test_id));
    items.forEach((s) => { if (allOn) next.delete(s.test_id); else next.add(s.test_id); });
    return next;
  });

  const selectedScripts = useMemo(
    () => (data?.scripts || []).filter((s: Script) => selectedScriptIds.has(s.test_id)),
    [data, selectedScriptIds],
  );

  // Merge the overridable data fields across the selected scripts (first default wins).
  const mergedFields = useMemo(() => {
    const seen = new Set<string>(); const fields: DataField[] = [];
    for (const s of selectedScripts) for (const f of (s.data_fields || [])) {
      if (seen.has(f.key)) continue; seen.add(f.key); fields.push(f);
    }
    return fields;
  }, [selectedScripts]);

  const buildData = () => {
    const out: Record<string, string> = {};
    for (const f of mergedFields) out[f.key] = dataOverrides[f.key] ?? f.default;
    return out;
  };

  const downloadConfigured = async () => {
    setRunBusy(true); setError(null);
    try {
      const blob = await api.getRunBundle(artifactId, {
        categories: Array.from(selectedCats),
        base_url: baseUrl.trim(),
        data: buildData(),
        data_by_test: buildDataByTest(),
        browsers: Array.from(browsers), headed, workers, retries,
      });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url; a.download = `nexus-playwright-run-${artifactId.slice(0, 8)}.zip`;
      a.click(); URL.revokeObjectURL(url);
    } catch (e: any) { setError(e?.response?.data?.detail || String(e)); }
    finally { setRunBusy(false); }
  };

  // CI/CD: same configured bundle + GitHub Actions / GitLab / Jenkins pipelines.
  const downloadCiBundle = async () => {
    setRunBusy(true); setError(null);
    try {
      const blob = await api.getCiBundle(artifactId, {
        categories: Array.from(selectedCats), base_url: baseUrl.trim(),
        data: buildData(), data_by_test: buildDataByTest(),
        browsers: Array.from(browsers), headed, workers, retries,
      });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url; a.download = `nexus-ci-bundle-${artifactId.slice(0, 8)}.zip`;
      a.click(); URL.revokeObjectURL(url);
    } catch (e: any) { setError(e?.response?.data?.detail || String(e)); }
    finally { setRunBusy(false); }
  };

  // Hard confirm before a server run that would start UNAUTHENTICATED. The
  // recording began with a login that is NOT replayed (it's captured as a
  // precondition, never scripted), so a cold run can fail at the login screen.
  // Only blocks the no-saved-session case — with a captured session it's silent.
  const confirmColdRun = (): boolean => {
    if (authStatus?.profile?.present) return true;  // saved session → starts logged in
    return window.confirm(
      'No saved login session.\n\n'
      + 'This recording starts with a login that is NOT replayed, so the run will start '
      + 'unauthenticated and may fail at the login screen.\n\n'
      + 'Tip: click "Capture login session" above first (one-time, encrypted).\n\n'
      + 'Run unauthenticated anyway?',
    );
  };

  // One-click: execute server-side on the Nexus runner, then refresh the triage
  // board. With no scope it runs the selected categories; { test_ids:[id] } runs
  // a single script.
  const runOnNexus = async (scope?: { test_ids?: string[] }) => {
    setRunErr(null);
    if (!confirmColdRun()) return;
    setView('run');   // jump to the dedicated results/live view
    const single = scope?.test_ids?.length === 1 ? scope.test_ids[0] : null;
    setRunningTestId(single);
    try {
      const body: any = {
        base_url: baseUrl.trim(), data: buildData(),
        data_by_test: buildDataByTest(),
        browsers: Array.from(browsers), headed, workers, retries,
      };
      if (scope?.test_ids) body.test_ids = scope.test_ids;
      else body.categories = Array.from(selectedCats);
      const r = await api.startNexusRun(artifactId, body);
      setRunStatus({ run_id: r.run_id, status: r.status, target: r.target, scripts: r.scripts });
      for (let i = 0; i < 160; i++) {
        await new Promise((res) => setTimeout(res, 2500));
        let s: any;
        try { s = await api.getNexusRunStatus(artifactId, r.run_id); } catch { continue; }
        setRunStatus((prev: any) => (prev && prev.run_id === r.run_id ? { ...prev, ...s } : prev));
        if (s.status && !['running', 'queued'].includes(s.status) && (s.status !== 'unknown' || i > 4)) break;
      }
      setTriageKey((k) => k + 1);
    } catch (e: any) {
      setRunErr(e?.response?.data?.detail || String(e));
      setRunStatus(null);
    } finally {
      setRunningTestId(null);
    }
  };
  const running = runStatus?.status === 'running' || runStatus?.status === 'queued';

  // Live: headed run on the runner, streamed into the portal via noVNC.
  const runLive = async (scope?: { test_ids?: string[] }) => {
    setRunErr(null); setLiveUrl(null);
    if (!confirmColdRun()) return;
    setView('run');   // jump to the dedicated results/live view
    const single = scope?.test_ids?.length === 1 ? scope.test_ids[0] : null;
    setRunningTestId(single);
    try {
      const body: any = {
        base_url: baseUrl.trim(), data: buildData(), data_by_test: buildDataByTest(),
        browsers: Array.from(browsers), retries,
      };
      if (scope?.test_ids) body.test_ids = scope.test_ids;
      else body.categories = Array.from(selectedCats);
      const r = await api.startNexusLiveRun(artifactId, body);
      setLiveUrl(r.live_url);
      setRunStatus({ run_id: r.run_id, status: r.status, target: r.target, scripts: r.scripts });
      for (let i = 0; i < 260; i++) {
        await new Promise((res) => setTimeout(res, 2500));
        let s: any;
        try { s = await api.getNexusRunStatus(artifactId, r.run_id); } catch { continue; }
        setRunStatus((prev: any) => (prev && prev.run_id === r.run_id ? { ...prev, ...s } : prev));
        if (s.status && !['running', 'queued'].includes(s.status) && (s.status !== 'unknown' || i > 4)) break;
      }
      setLiveUrl(null);
      setTriageKey((k) => k + 1);
    } catch (e: any) {
      setRunErr(e?.response?.data?.detail || String(e));
      setRunStatus(null); setLiveUrl(null);
    } finally {
      setRunningTestId(null);
    }
  };

  const copy = async (key: string, text: string) => {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(key);
      window.setTimeout(() => setCopied((c) => (c === key ? '' : c)), 1400);
    } catch { /* clipboard blocked — no-op */ }
  };

  // Download a category (or whole suite) as the real reproducible zip.
  const downloadZip = async (category = '') => {
    const key = category ? `zip:${category}` : 'zip:all';
    setBusy(key); setError(null);
    try {
      const blob = await api.getPlaywrightBundle(artifactId, { category });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `nexus-playwright-${artifactId.slice(0, 8)}${category ? `-${category}` : ''}.zip`;
      a.click(); URL.revokeObjectURL(url);
    } catch (e: any) { setError(e?.response?.data?.detail || String(e)); }
    finally { setBusy(''); }
  };

  const grouped = useMemo(() => {
    const scripts = data?.scripts || [];
    return CATEGORY_ORDER
      .map((c) => ({ ...c, items: scripts.filter((s) => s.category === c.type) }))
      .filter((g) => g.items.length > 0);
  }, [data]);

  const fidByTest = useMemo(() => {
    const m: Record<string, any> = {};
    for (const sc of (suiteFid?.scripts || [])) if (sc && sc.test_id) m[sc.test_id] = sc;
    return m;
  }, [suiteFid]);
  const lastByTest = useMemo(() => {
    const sc: any = runs?.scripts;
    if (Array.isArray(sc)) {
      const m: Record<string, any> = {};
      for (const x of sc) if (x && x.test_id) m[x.test_id] = x;
      return m;
    }
    return (sc && typeof sc === 'object') ? sc : {};
  }, [runs]);

  const totals = data?.totals || { scripts: 0, solid_steps: 0, review_steps: 0 };

  return (
    <section className="space-y-4">
      {/* ── Header ──────────────────────────────────────────── */}
      <div className="rounded-2xl px-4 py-3 flex items-center gap-3 flex-wrap"
        style={{ background: 'linear-gradient(135deg, rgba(99,102,241,0.07), rgba(56,189,248,0.05))', border: '1px solid rgba(99,102,241,0.22)' }}>
        <Rocket className="h-5 w-5" style={{ color: '#4f46e5' }} />
        <div className="min-w-0">
          <h2 className="text-sm font-black text-slate-900">Playwright Execution</h2>
          <p className="text-[11px] text-slate-500 font-medium">
            Deterministic, ownable scripts — generated from your recording. Run locally or in CI; failures land in the triage board below.
          </p>
        </div>
        <div className="ml-auto flex items-center gap-2 flex-wrap">
          <span className="text-[11px] font-bold text-slate-600">
            {totals.scripts} script{totals.scripts === 1 ? '' : 's'}
          </span>
          <span className="text-[10px] text-slate-400">·</span>
          <span className="text-[11px] font-semibold" style={{ color: '#059669' }}>{totals.solid_steps} solid steps</span>
          {totals.review_steps > 0 && (
            <span className="text-[11px] font-semibold" style={{ color: '#b45309' }}>{totals.review_steps} need review</span>
          )}
          <button onClick={refresh} disabled={loading}
            className="flex items-center gap-1 rounded-lg px-2.5 py-1.5 text-[11px] font-semibold bg-white text-slate-600 border border-slate-200 hover:bg-slate-50 disabled:opacity-50">
            {loading ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <RefreshCw className="h-3.5 w-3.5" />} Refresh
          </button>
          <button onClick={() => downloadZip('')} disabled={!!busy || totals.scripts === 0}
            className="flex items-center gap-1 rounded-lg px-3 py-1.5 text-[11px] font-semibold text-white disabled:opacity-50"
            style={{ background: '#4f46e5' }}>
            {busy === 'zip:all' ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Download className="h-3.5 w-3.5" />} Download full bundle
          </button>
        </div>
      </div>

      {error && <div className="rounded-lg border border-amber-300 bg-amber-50 px-3 py-2 text-[11px] text-amber-800">{error}</div>}

      {/* ── Run console ─────────────────────────────────────── */}
      {view === 'console' && data && totals.scripts > 0 && (
        <div className="rounded-xl border border-indigo-200 bg-white/70 overflow-hidden">
          <div className="px-4 py-2.5 flex items-center gap-2 bg-indigo-50/70 border-b border-indigo-100">
            <SlidersHorizontal className="h-4 w-4 text-indigo-600" />
            <span className="text-[12px] font-black text-slate-800">Run console</span>
            <span className="text-[10px] text-slate-400">configure once → run anywhere, no code edits</span>
          </div>
          <div className="p-4 space-y-4">
            {/* B.1 — Runnability preflight: surface what a cold run needs BEFORE it fails */}
            {(() => {
              const hasAuth = !!authStatus?.profile?.present;
              return (
                <div className="rounded-lg border px-3 py-2" style={hasAuth ? { borderColor: 'rgba(16,185,129,0.30)', background: 'rgba(16,185,129,0.05)' } : { borderColor: 'rgba(245,158,11,0.45)', background: 'rgba(245,158,11,0.07)' }}>
                  <div className="flex items-center gap-2 flex-wrap text-[11px]">
                    <span className="font-black text-slate-700">Before you run</span>
                    {hasAuth ? (
                      <span className="font-semibold text-emerald-700">✓ Login session ready — runs start authenticated</span>
                    ) : (
                      <>
                        <span className="font-semibold text-amber-800">⚠ No saved login — a cold run starts logged out and may stop at the login screen (fails at step 1).</span>
                        <button onClick={startCapture} disabled={!!authBusy || !authStatus?.encryption_available}
                          className="rounded-md px-2 py-0.5 text-[10px] font-bold bg-amber-600 text-white hover:bg-amber-500 disabled:opacity-50">
                          {authBusy === 'capture' ? 'Opening…' : 'Capture login session'}
                        </button>
                      </>
                    )}
                    <span className="ml-auto flex items-center gap-1.5 text-[10px] text-slate-400">
                      Accuracy: capture real control types (dropdowns / radios), not text.
                      <button onClick={runEnrich} disabled={busy === 'enrich'}
                        title="Vision pass that captures each control\u2019s real type + options so dropdowns/radios run correctly"
                        className="rounded-md px-2 py-0.5 text-[10px] font-bold bg-indigo-100 text-indigo-700 hover:bg-indigo-200 disabled:opacity-50">
                        {busy === 'enrich' ? 'Enriching\u2026' : 'Enrich now'}
                      </button>
                    </span>
                  </div>
                </div>
              );
            })()}
            {/* target */}
            <div className="flex items-center gap-2 flex-wrap">
              <span className="text-[10px] font-bold uppercase text-slate-400 w-14 shrink-0">Run on</span>
              {[
                { k: 'local', label: 'Local', icon: <Terminal className="h-3.5 w-3.5" />, on: true, dl: false },
                { k: 'ci', label: 'CI/CD', icon: <Server className="h-3.5 w-3.5" />, on: true, dl: true },
              ].map((t) => (
                <button key={t.k}
                  disabled={!t.on || (t.dl && (runBusy || selectedScripts.length === 0))}
                  title={t.dl ? 'Download a CI/CD bundle (GitHub Actions / GitLab / Jenkins) with the suite + reporter wired' : undefined}
                  onClick={() => { if (t.dl) { void downloadCiBundle(); } else if (t.on) setTarget(t.k as any); }}
                  className={`flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-[11px] font-semibold border transition-colors ${
                    !t.on ? 'border-slate-150 text-slate-300 cursor-not-allowed'
                    : target === t.k ? 'border-indigo-300 bg-indigo-600 text-white'
                    : 'border-slate-200 text-slate-600 hover:bg-slate-50'}`}>
                  {t.dl && runBusy ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : t.icon} {t.label}
                  {t.dl ? <Download className="h-3 w-3 opacity-60" /> : null}
                  {!t.on && <span className="rounded bg-slate-100 px-1 py-0.5 text-[8px] font-bold uppercase text-slate-400">soon</span>}
                </button>
              ))}
            </div>

            {/* 1 · scripts */}
            <div>
              <p className="text-[10px] font-bold uppercase text-slate-400 mb-1.5">1 · Scripts to run</p>
              <div className="space-y-1.5">
                {grouped.map((g) => {
                  const allOn = g.items.every((s) => selectedScriptIds.has(s.test_id));
                  return (
                    <div key={g.type} className="rounded-lg border border-slate-200 overflow-hidden">
                      <div className="flex items-center bg-slate-50/70">
                        <button onClick={() => toggleCatItems(g.items)}
                          className="flex-1 flex items-center gap-2 px-2.5 py-1.5 text-left hover:bg-slate-100/70">
                          {allOn ? <CheckSquare className="h-3.5 w-3.5" style={{ color: g.accent }} /> : <Square className="h-3.5 w-3.5 text-slate-400" />}
                          <span className="text-[11px] font-black" style={{ color: g.accent }}>{g.label}</span>
                          <span className="text-[10px] text-slate-400 font-semibold">{g.items.length}</span>
                          <span className="ml-auto text-[9px] uppercase font-bold text-slate-400">{allOn ? 'all selected' : 'select all'}</span>
                        </button>
                        <button onClick={() => downloadZip(g.type)} disabled={!!busy}
                          title="Download this category as a runnable zip"
                          className="shrink-0 flex items-center px-2 py-1.5 text-slate-500 hover:bg-slate-100/70 disabled:opacity-50">
                          {busy === `zip:${g.type}` ? <Loader2 className="h-3 w-3 animate-spin" /> : <Download className="h-3 w-3" />}
                        </button>
                      </div>
                      <div className="divide-y divide-slate-100">
                        {g.items.map((s) => {
                          const on = selectedScriptIds.has(s.test_id);
                          const fid = fidByTest[s.test_id];
                          const ver = editedTests[s.test_id];
                          const last = lastByTest[s.test_id] || {};
                          const lastSt = last.status || last.latest_status || last.verdict || (Array.isArray(last.runs) ? (last.runs[0] && last.runs[0].status) : '') || '';
                          const rid = s.test_id || s.path;
                          const xOpen = expandedRow === rid;
                          return (
                            <div key={s.test_id} className="hover:bg-indigo-50/40">
                              <div className="flex items-center gap-2 px-2.5 py-1.5">
                                <input type="checkbox" checked={on} onChange={() => toggleScript(s.test_id)} className="h-3.5 w-3.5 accent-indigo-600 shrink-0 cursor-pointer" />
                                <span onClick={() => toggleScript(s.test_id)} className="text-[11px] text-slate-700 font-medium truncate flex-1 min-w-0 cursor-pointer">{s.name}</span>
                                <span className="shrink-0 text-[9px] text-slate-400 flex items-center gap-1.5">
                                  <span>{s.stats?.total ?? s.lines} steps</span>
                                  {s.stats?.solid != null && <span style={{ color: '#059669' }}>{s.stats.solid} solid</span>}
                                  {(s.stats?.review || 0) > 0 && <span style={{ color: '#b45309' }}>{s.stats.review} review</span>}
                                  {(s.stats?.skipped || 0) > 0 && <span style={{ color: '#64748b' }}>{s.stats.skipped} skipped</span>}
                                </span>
                                {fid && (
                                  <span className="shrink-0 rounded px-1.5 py-0.5 text-[9px] font-bold"
                                    style={fid.grade === 'strong' ? { background: 'rgba(16,185,129,0.15)', color: '#047857' }
                                      : fid.grade === 'weak' ? { background: 'rgba(244,63,94,0.12)', color: '#be123c' }
                                      : { background: 'rgba(245,158,11,0.15)', color: '#b45309' }}
                                    title="Script fidelity — how faithfully the Playwright matches the test case (NOT whether the app passed; that is the Run/verdict).">
                                    fidelity {fid.score}%
                                  </span>
                                )}
                                <span className="shrink-0 rounded px-1.5 py-0.5 text-[9px] font-semibold"
                                  style={ver ? { background: 'rgba(99,102,241,0.12)', color: '#4338ca' } : { background: 'rgba(100,116,139,0.10)', color: '#64748b' }}
                                  title={ver ? 'an edited / healed version runs (not the auto-generated one)' : 'runs the auto-generated version'}>
                                  {ver ? `edited · v${ver}` : 'generated'}
                                </span>
                                {lastSt && (
                                  <span className="shrink-0 text-[10px]" title={`last run: ${lastSt}`}>
                                    {lastSt === 'passed' ? '🟢' : lastSt === 'failed' ? '🔴' : '⚪'}
                                  </span>
                                )}
                                <button onClick={() => setExpandedRow((cur) => (cur === rid ? '' : rid))}
                                  title="View code · Edit · Audit · Regenerate · Run just this"
                                  className="shrink-0 flex items-center gap-0.5 rounded px-1.5 py-0.5 text-[9px] font-bold bg-slate-100 text-slate-600 hover:bg-slate-200">
                                  {xOpen ? <ChevronDown className="h-3 w-3" /> : <ChevronRight className="h-3 w-3" />} actions
                                </button>
                              </div>
                              {xOpen && (
                                <div className="border-t border-indigo-100 bg-slate-50/70 px-2.5 py-2 space-y-2">
                                  <div className="flex items-center gap-1.5 flex-wrap">
                                    <button onClick={() => runOnNexus({ test_ids: [s.test_id] })} disabled={running || !s.test_id}
                                      title="Run just this script on the Nexus runner (headless)"
                                      className="flex items-center gap-1 rounded-md px-2 py-1 text-[10px] font-bold text-white disabled:opacity-50" style={{ background: '#059669' }}>
                                      {runningTestId === s.test_id ? <Loader2 className="h-3 w-3 animate-spin" /> : <Play className="h-3 w-3" />} Run this
                                    </button>
                                    <button onClick={() => runLive({ test_ids: [s.test_id] })} disabled={running || !s.test_id}
                                      title="Run this script HEADED and watch it live (streamed into the portal)"
                                      className="flex items-center gap-1 rounded-md px-2 py-1 text-[10px] font-bold text-white disabled:opacity-50" style={{ background: '#7c3aed' }}>
                                      {runningTestId === s.test_id && liveUrl ? <Loader2 className="h-3 w-3 animate-spin" /> : <Play className="h-3 w-3" />} Live
                                    </button>
                                    <button onClick={() => setOpenCode((m) => ({ ...m, [rid]: !m[rid] }))}
                                      className="flex items-center gap-1 rounded-md px-2 py-1 text-[10px] font-semibold bg-slate-100 text-slate-700 hover:bg-slate-200">
                                      {openCode[rid] ? <ChevronDown className="h-3 w-3" /> : <ChevronRight className="h-3 w-3" />} {openCode[rid] ? 'Hide' : 'View'} code
                                    </button>
                                    {s.test_id && (
                                      <button onClick={() => openEditor(s)}
                                        title="Edit this test's script, save a new version, runs use it"
                                        className="flex items-center gap-1 rounded-md px-2 py-1 text-[10px] font-semibold bg-violet-50 text-violet-700 hover:bg-violet-100">
                                        <Pencil className="h-3 w-3" /> Edit{editedTests[s.test_id] ? ` · v${editedTests[s.test_id]}` : ''}
                                      </button>
                                    )}
                                    {s.test_id && (
                                      <button onClick={() => auditScript(s.test_id)} disabled={fidBusy === s.test_id}
                                        title="Audit: does this script faithfully implement the test case + verify its Expected Results? (coverage + assertions + AI review)"
                                        className="flex items-center gap-1 rounded-md px-2 py-1 text-[10px] font-semibold bg-sky-50 text-sky-700 hover:bg-sky-100 disabled:opacity-50">
                                        {fidBusy === s.test_id ? <Loader2 className="h-3 w-3 animate-spin" /> : <ShieldAlert className="h-3 w-3" />} Audit
                                      </button>
                                    )}
                                    {s.test_id && (
                                      <button onClick={() => regenScript(s.test_id)} disabled={fidBusy === `${s.test_id}:regen`}
                                        title="Regenerate this script from the current test case as a new immutable version (v+1)"
                                        className="flex items-center gap-1 rounded-md px-2 py-1 text-[10px] font-semibold bg-teal-50 text-teal-700 hover:bg-teal-100 disabled:opacity-50">
                                        {fidBusy === `${s.test_id}:regen` ? <Loader2 className="h-3 w-3 animate-spin" /> : <RotateCcw className="h-3 w-3" />} Regenerate
                                      </button>
                                    )}
                                    {s.test_id && (
                                      <button onClick={() => runPreflight(s.test_id)} disabled={pfBusy === s.test_id}
                                        title="Live preflight: open the live app and check every locator resolves (0 = broken/renamed, >1 = ambiguous). Proof it will run. Uses the Environment URL above."
                                        className="flex items-center gap-1 rounded-md px-2 py-1 text-[10px] font-semibold bg-emerald-50 text-emerald-700 hover:bg-emerald-100 disabled:opacity-50">
                                        {pfBusy === s.test_id ? <Loader2 className="h-3 w-3 animate-spin" /> : <Globe className="h-3 w-3" />} Live preflight
                                      </button>
                                    )}
                                    {(s.data_fields?.length || 0) > 0 && (
                                      <button onClick={() => setOpenData((m) => ({ ...m, [rid]: !m[rid] }))}
                                        title="Set this test's own data (overrides the global defaults)"
                                        className="flex items-center gap-1 rounded-md px-2 py-1 text-[10px] font-semibold bg-amber-50 text-amber-700 hover:bg-amber-100">
                                        <Database className="h-3 w-3" /> Data ({s.data_fields.length}){perTestData[s.test_id] && Object.values(perTestData[s.test_id]).some((v) => v) ? ' ✎' : ''}
                                      </button>
                                    )}
                                    <button onClick={() => copy(`cmd:${rid}`, `npx playwright test ${s.path}`)}
                                      className="flex items-center gap-1 rounded-md px-2 py-1 text-[10px] font-semibold bg-indigo-50 text-indigo-700 hover:bg-indigo-100">
                                      {copied === `cmd:${rid}` ? <Check className="h-3 w-3 text-emerald-500" /> : <Copy className="h-3 w-3" />} Copy command
                                    </button>
                                    <button onClick={() => downloadText((s.path.split('/').pop() || 'test.spec.ts'), s.code, 'text/typescript')}
                                      className="flex items-center gap-1 rounded-md px-2 py-1 text-[10px] font-semibold bg-slate-100 text-slate-700 hover:bg-slate-200">
                                      <Download className="h-3 w-3" /> .spec
                                    </button>
                                  </div>
                                  {fidelity[s.test_id] && <FidelityCard rep={fidelity[s.test_id]} />}
                                  {preflight[s.test_id] && <PreflightCard rep={preflight[s.test_id]} />}
                                  {openData[rid] && (s.data_fields?.length || 0) > 0 && (
                                    <div className="rounded-md border border-amber-200 bg-amber-50/40 px-2.5 py-2">
                                      <p className="text-[10px] text-slate-500 mb-1.5"><span className="font-bold text-amber-700">This test's data</span> — blank inherits the global default / observed value (never invented). Text, dates &amp; dropdowns are overridable; radio/checkbox/toggle replay the recording.</p>
                                      <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
                                        {s.data_fields.map((f) => (
                                          <label key={f.key} className="flex items-center gap-2">
                                            <span className="text-[11px] text-slate-500 w-28 shrink-0 truncate" title={f.label}>{f.label || f.key}</span>
                                            <input value={perTestData[s.test_id]?.[f.key] ?? ''} placeholder={f.default || '(observed)'}
                                              onChange={(e) => setPerTestData((d) => ({ ...d, [s.test_id]: { ...(d[s.test_id] || {}), [f.key]: e.target.value } }))}
                                              className="flex-1 min-w-0 rounded-md border border-amber-200 px-2 py-1 text-[11px] font-mono text-slate-700 focus:outline-none focus:border-amber-400" />
                                          </label>
                                        ))}
                                      </div>
                                    </div>
                                  )}
                                  {openEdit[rid] && s.test_id && (
                                    <div className="rounded-md border border-violet-200 bg-violet-50/40 px-2.5 py-2">
                                      <div className="flex items-center gap-2 mb-1.5 flex-wrap">
                                        <Pencil className="h-3 w-3 text-violet-600" />
                                        <span className="text-[10px] font-bold text-violet-700">Edit script</span>
                                        <span className="text-[10px] text-slate-400">you own this code — Save creates a new version; runs use the latest</span>
                                        <button onClick={() => saveVersion(s.test_id)} disabled={!editDirty[s.test_id] || editBusy === `save:${s.test_id}`}
                                          className="ml-auto flex items-center gap-1 rounded-md px-2.5 py-1 text-[10px] font-bold text-white disabled:opacity-50" style={{ background: '#7c3aed' }}>
                                          {editBusy === `save:${s.test_id}` ? <Loader2 className="h-3 w-3 animate-spin" /> : <Save className="h-3 w-3" />}
                                          Save &rarr; v{(editedTests[s.test_id] || 0) + 1}
                                        </button>
                                      </div>
                                      {editBusy === rid ? (
                                        <div className="flex items-center gap-2 text-[11px] text-slate-400 py-6 justify-center">
                                          <Loader2 className="h-3.5 w-3.5 animate-spin" /> loading source…
                                        </div>
                                      ) : (
                                        <textarea
                                          value={editSource[s.test_id] ?? ''}
                                          onChange={(e) => { setEditSource((m) => ({ ...m, [s.test_id]: e.target.value })); setEditDirty((m) => ({ ...m, [s.test_id]: true })); }}
                                          spellCheck={false}
                                          className="w-full rounded-md bg-slate-950 text-slate-200 font-mono text-[11px] leading-relaxed px-3 py-2 focus:outline-none border border-slate-800"
                                          style={{ minHeight: '16rem' }} />
                                      )}
                                      {(versions[s.test_id]?.length || 0) > 0 && (
                                        <div className="mt-2">
                                          <p className="text-[10px] font-bold text-slate-500 mb-1 flex items-center gap-1"><History className="h-3 w-3" /> Versions</p>
                                          <div className="space-y-1">
                                            {versions[s.test_id].map((v: any, i: number) => (
                                              <div key={v.script_version_id} className="flex items-center gap-2 text-[10px] text-slate-500 rounded bg-white/70 border border-slate-200 px-2 py-1">
                                                <span className="font-bold text-slate-700">v{v.version_no}</span>
                                                {i === 0 && <span className="rounded bg-emerald-100 text-emerald-700 px-1 font-bold">active</span>}
                                                <span className="truncate">{v.author || 'unknown'}{v.note ? ` · ${v.note}` : ''}</span>
                                                <span className="ml-auto shrink-0 text-slate-400">{v.created_at ? new Date(v.created_at).toLocaleString() : ''}</span>
                                                {i !== 0 && (
                                                  <button onClick={() => restoreVersion(s.test_id, v.version_no)} disabled={editBusy === `restore:${s.test_id}`}
                                                    className="shrink-0 flex items-center gap-1 rounded px-1.5 py-0.5 font-semibold bg-slate-100 text-slate-600 hover:bg-slate-200 disabled:opacity-50">
                                                    <RotateCcw className="h-2.5 w-2.5" /> Restore
                                                  </button>
                                                )}
                                              </div>
                                            ))}
                                          </div>
                                        </div>
                                      )}
                                    </div>
                                  )}
                                  {openCode[rid] && (
                                    <div className="rounded-md overflow-hidden border border-slate-200">
                                      <div className="flex items-center justify-between px-3 py-1.5 bg-slate-900">
                                        <span className="text-[10px] font-mono text-slate-400">{s.path} · {s.lines} lines</span>
                                        <button onClick={() => copy(`code:${rid}`, s.code)}
                                          className="flex items-center gap-1 text-[10px] font-semibold text-slate-300 hover:text-white">
                                          {copied === `code:${rid}` ? <Check className="h-3 w-3 text-emerald-400" /> : <Copy className="h-3 w-3" />} Copy
                                        </button>
                                      </div>
                                      <pre className="overflow-x-auto bg-slate-950 px-3 py-3 text-[11px] leading-relaxed font-mono text-slate-200" style={{ maxHeight: '24rem' }}>
                                        <code>{s.code}</code>
                                      </pre>
                                    </div>
                                  )}
                                </div>
                              )}
                            </div>
                          );
                        })}
                      </div>
                    </div>
                  );
                })}
                <p className="text-[10px] text-slate-500 font-semibold mt-1">→ {selectedScripts.length} of {totals.scripts} script{totals.scripts === 1 ? '' : 's'} selected · ready to run</p>
              </div>
            </div>

            {/* 2 · environment */}
            <div>
              <p className="text-[10px] font-bold uppercase text-slate-400 mb-1.5">2 · Environment</p>
              <div className="flex items-center gap-2">
                <Globe className="h-4 w-4 text-slate-400 shrink-0" />
                <input value={baseUrl} onChange={(e) => setBaseUrl(e.target.value)}
                  placeholder="https://staging.your-app.com"
                  className="flex-1 min-w-0 rounded-md border border-slate-200 px-2.5 py-1.5 text-[12px] font-mono text-slate-700 focus:outline-none focus:border-indigo-300" />
              </div>
              {data.recorded_base_url && (
                <p className="text-[10px] text-slate-400 mt-1">
                  recorded on <span className="font-mono">{data.recorded_base_url}</span> — change it to run the same scripts against any environment
                </p>
              )}
              <p className="text-[10px] text-slate-400 mt-0.5">
                Only the <span className="font-semibold text-slate-500">host</span> is swapped — recorded paths and the login step are replayed exactly as captured (re-point ≠ log in).
              </p>
            </div>

            {/* authentication — capture-once session so cold runs start logged in */}
            <div>
              <p className="text-[10px] font-bold uppercase text-slate-400 mb-1.5 flex items-center gap-1.5">
                <Lock className="h-3 w-3" /> Authentication
                <span className="normal-case font-medium text-slate-300">log in once; we save the session (encrypted) and reuse it for every run</span>
              </p>
              {captureLive ? (
                <div className="rounded-md border-2 border-indigo-300 overflow-hidden bg-black">
                  <div className="px-2 py-1 flex items-center gap-2 bg-indigo-600">
                    <span className="text-[10px] font-bold uppercase text-white">Log in below, then save the session</span>
                    <button onClick={saveCapture} disabled={authBusy === 'save'}
                      className="ml-auto flex items-center gap-1 rounded px-2 py-0.5 text-[10px] font-bold bg-emerald-500 text-white disabled:opacity-50">
                      {authBusy === 'save' ? <Loader2 className="h-3 w-3 animate-spin" /> : <Check className="h-3 w-3" />} I've logged in — Save session
                    </button>
                    <button onClick={cancelCapture} disabled={authBusy === 'cancel'}
                      className="rounded px-2 py-0.5 text-[10px] font-semibold bg-white/20 text-white">Cancel</button>
                  </div>
                  <iframe title="Log in to capture a session" src={captureLive} className="w-full" style={{ height: 360, border: 0 }} />
                </div>
              ) : authStatus?.profile?.present ? (
                <div className="flex items-center gap-2 flex-wrap rounded-md border border-emerald-200 bg-emerald-50 px-2.5 py-1.5">
                  <CheckCircle2 className="h-4 w-4 text-emerald-600 shrink-0" />
                  <span className="text-[11px] font-semibold text-emerald-800">
                    Authenticated session saved{authStatus.profile.captured_at ? ` · ${new Date(authStatus.profile.captured_at).toLocaleString()}` : ''} — runs start logged in.
                  </span>
                  <button onClick={() => void startCapture()} disabled={!!authBusy}
                    className="ml-auto rounded px-2 py-0.5 text-[10px] font-semibold bg-white border border-slate-200 text-slate-600 hover:bg-slate-50 disabled:opacity-50">Re-capture</button>
                  <button onClick={clearAuth} disabled={authBusy === 'clear'}
                    className="rounded px-2 py-0.5 text-[10px] font-semibold bg-white border border-rose-200 text-rose-600 hover:bg-rose-50 disabled:opacity-50">Clear</button>
                </div>
              ) : (
                <div className="flex items-center gap-2 flex-wrap">
                  <span className="text-[10px] text-slate-400">
                    No saved session — a cold run starts unauthenticated (it may land on the login screen).
                  </span>
                  <button onClick={() => void startCapture()} disabled={!!authBusy || authStatus?.encryption_available === false}
                    title={authStatus?.encryption_available === false ? 'Encryption unavailable on this deployment — a session cannot be stored securely' : 'Open a browser, log in once; we save the session (encrypted) for every run'}
                    className="flex items-center gap-1.5 rounded-md px-2.5 py-1 text-[11px] font-semibold bg-indigo-600 text-white hover:bg-indigo-500 disabled:opacity-50">
                    {authBusy === 'capture' ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Lock className="h-3.5 w-3.5" />} Capture login session
                  </button>
                  {authStatus?.encryption_available === false && (
                    <span className="text-[10px] text-amber-600">encryption unavailable — can't store a session</span>
                  )}
                </div>
              )}
              {authErr && <p className="text-[10px] text-rose-600 mt-1">{authErr}</p>}
            </div>

            {/* 3 · data — comparison table: Global + one column per selected script, diffs highlighted */}
            {mergedFields.length > 0 && selectedScripts.length > 0 && (
              <div>
                <p className="text-[10px] font-bold uppercase text-slate-400 mb-1.5 flex items-center gap-1.5">
                  <Database className="h-3 w-3" /> 3 · Test data · per scenario
                  <span className="normal-case font-medium text-slate-300">Global applies to all; a scenario cell overrides just that script. Differences are highlighted.</span>
                </p>
                <p className="text-[9px] text-slate-400 mb-1.5 -mt-0.5">
                  Only text / dates / dropdowns are editable — radio / checkbox / toggle replay the recorded value (not shown here).
                </p>
                <div className="overflow-x-auto rounded-lg border border-slate-200">
                  <table className="w-full text-[11px] border-collapse">
                    <thead>
                      <tr className="bg-slate-50/80 text-slate-500">
                        <th className="text-left font-bold px-2 py-1.5 sticky left-0 bg-slate-50/80">Field</th>
                        <th className="text-left font-bold px-2 py-1.5">Global</th>
                        {selectedScripts.map((s) => (
                          <th key={s.test_id} className="text-left font-bold px-2 py-1.5 max-w-[140px] truncate" title={s.name}>{s.name}</th>
                        ))}
                      </tr>
                    </thead>
                    <tbody>
                      {mergedFields.map((f) => (
                        <tr key={f.key} className="border-t border-slate-100">
                          <td className="px-2 py-1 text-slate-600 font-medium sticky left-0 bg-white max-w-[160px] truncate" title={f.label || f.key}>{f.label || f.key}</td>
                          <td className="px-2 py-1">
                            <input value={dataOverrides[f.key] ?? f.default}
                              onChange={(e) => setDataOverrides((d) => ({ ...d, [f.key]: e.target.value }))}
                              className="w-full min-w-[90px] rounded border border-slate-200 px-1.5 py-0.5 text-[11px] font-mono text-slate-700 focus:outline-none focus:border-indigo-300" />
                          </td>
                          {selectedScripts.map((s) => {
                            const recorded = ((s.data_fields || []).find((x) => x.key === f.key) || {}).default || '';
                            const override = perTestData[s.test_id]?.[f.key] ?? '';
                            const inheritedEff = (dataOverrides[f.key] ?? recorded) || '';
                            const differs = recorded !== '' && recorded !== f.default;
                            return (
                              <td key={s.test_id} className={'px-2 py-1 ' + (differs ? 'bg-amber-50/60' : '')}>
                                <input value={override}
                                  placeholder={inheritedEff || '(observed)'}
                                  onChange={(e) => setPerTestData((d) => ({ ...d, [s.test_id]: { ...(d[s.test_id] || {}), [f.key]: e.target.value } }))}
                                  title={override ? 'overridden for this scenario' : (differs ? ('recorded for this scenario: ' + recorded + ' (differs from Global)') : 'inherits Global / recorded value')}
                                  className={'w-full min-w-[90px] rounded border px-1.5 py-0.5 text-[11px] font-mono focus:outline-none focus:border-indigo-300 ' + (differs ? 'border-amber-300 text-amber-800 placeholder-amber-600/70' : 'border-slate-200 text-slate-500')} />
                              </td>
                            );
                          })}
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
                <p className="text-[9px] text-slate-400 mt-1">A blank scenario cell shows its effective value as faint placeholder (inherits Global, then the recorded value). Amber = this scenario recorded a value different from Global.</p>
              </div>
            )}

            {/* browsers & options */}
            <div>
              <p className="text-[10px] font-bold uppercase text-slate-400 mb-1.5">Browsers &amp; options</p>
              <div className="flex items-center gap-2.5 flex-wrap text-[11px]">
                {(['chromium', 'firefox', 'webkit'] as const).map((b) => {
                  const on = browsers.has(b);
                  return (
                    <button key={b} onClick={() => toggleBrowser(b)}
                      className={`flex items-center gap-1 rounded-md px-2 py-1 font-semibold border capitalize ${
                        on ? 'bg-slate-800 text-white border-slate-800' : 'bg-white text-slate-500 border-slate-200 hover:bg-slate-50'}`}>
                      {on ? <CheckSquare className="h-3 w-3" /> : <Square className="h-3 w-3" />}{b}
                    </button>
                  );
                })}
                <span className="text-slate-300">·</span>
                <label className="flex items-center gap-1 text-slate-500">workers
                  <select value={workers} onChange={(e) => setWorkers(Number(e.target.value))}
                    className="rounded border border-slate-200 px-1 py-0.5 text-[11px]">
                    {[1, 2, 4, 6, 8].map((n) => <option key={n} value={n}>{n}</option>)}
                  </select>
                </label>
                <label className="flex items-center gap-1 text-slate-500">retries
                  <select value={retries} onChange={(e) => setRetries(Number(e.target.value))}
                    className="rounded border border-slate-200 px-1 py-0.5 text-[11px]">
                    {[0, 1, 2, 3].map((n) => <option key={n} value={n}>{n}</option>)}
                  </select>
                </label>
                <label className="flex items-center gap-1 text-slate-500"
                  title="Applies to the downloaded local bundle — the Nexus runner is headless">
                  <input type="checkbox" checked={headed} onChange={(e) => setHeaded(e.target.checked)} /> headed <span className="text-slate-300">(local)</span>
                </label>
              </div>
            </div>

            {/* 4 · run */}
            <div className="rounded-lg border border-indigo-100 bg-indigo-50/50 p-3 space-y-3">
              {/* 0 · audit + version */}
              <div>
                <p className="text-[10px] font-bold uppercase text-sky-700 mb-2">Audit &amp; version
                  <span className="text-slate-400 normal-case font-medium"> — do the scripts faithfully implement the test cases?</span>
                </p>
                <div className="flex items-center gap-2 flex-wrap">
                  <button onClick={auditSuite} disabled={fidBusy === 'suite'}
                    className="flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-[11px] font-bold text-white disabled:opacity-50"
                    style={{ background: '#0284c7' }}>
                    {fidBusy === 'suite' ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <ShieldAlert className="h-3.5 w-3.5" />} Audit suite
                  </button>
                  <button onClick={regenAll} disabled={fidBusy === 'all'}
                    title="Regenerate the selected scripts (or all) from their current test cases to new versions (v+1)"
                    className="flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-[11px] font-bold text-white disabled:opacity-50"
                    style={{ background: '#0d9488' }}>
                    {fidBusy === 'all' ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <RotateCcw className="h-3.5 w-3.5" />} Regenerate {selectedScripts.length ? `(${selectedScripts.length})` : 'all'} → v+1
                  </button>
                  {suiteFid?.rollup && (
                    <span className="text-[10px] font-semibold text-slate-600">
                      {suiteFid.rollup.scripts} scripts · avg {suiteFid.rollup.avg_score}% ·
                      <span className="text-emerald-700"> {suiteFid.rollup.strong} strong</span> ·
                      <span className="text-amber-700"> {suiteFid.rollup.partial} partial</span> ·
                      <span className="text-rose-700"> {suiteFid.rollup.weak} weak</span>
                      {suiteFid.rollup.drifted ? <span className="text-amber-700"> · {suiteFid.rollup.drifted} stale</span> : null}
                    </span>
                  )}
                  {suiteFid?.error && <span className="text-[10px] text-amber-700">{suiteFid.error}</span>}
                </div>
              </div>
              {/* one-click, server-side */}
              <div>
                <p className="text-[10px] font-bold uppercase text-emerald-700 mb-2">
                  4 · Run on Nexus <span className="text-slate-400 normal-case font-medium">— one click, server-side</span>
                </p>
                <div className="flex items-center gap-2 flex-wrap">
                  <button onClick={() => runOnNexus()} disabled={running || selectedScripts.length === 0}
                    className="flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-[11px] font-bold text-white disabled:opacity-50"
                    style={{ background: '#059669' }}>
                    {running ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Play className="h-3.5 w-3.5" />}
                    {running ? 'Running on Nexus…' : `Run on Nexus runner (${selectedScripts.length})`}
                  </button>
                  <button onClick={() => runLive()} disabled={running || selectedScripts.length === 0}
                    title="Run headed on the Nexus runner and WATCH it live in the portal (view-only stream)"
                    className="flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-[11px] font-bold text-white disabled:opacity-50"
                    style={{ background: '#7c3aed' }}>
                    {running && liveUrl ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Play className="h-3.5 w-3.5" />}
                    {running && liveUrl ? 'Live…' : 'Run live ▸ watch'}
                  </button>
                  <button onClick={runAutoHeal} disabled={autoHealing || running || selectedScripts.length === 0}
                    title="Run headed and AUTO-HEAL on failure — diagnose, fix, re-run, continue, and freeze a Clean Run - V1 when green"
                    className="flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-[11px] font-bold text-white disabled:opacity-50"
                    style={{ background: 'linear-gradient(135deg,#7c3aed,#4f46e5)' }}>
                    {autoHealing ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Wand2 className="h-3.5 w-3.5" />}
                    {autoHealing ? 'Auto-healing…' : '⚡ Auto-Heal Run'}
                  </button>
                  <span className="text-[10px] text-slate-400">
                    runs the highest-approved version of each selected script against {baseUrl.trim() || 'the recorded site'} — results flow to the board below
                  </span>
                </div>
                {runStatus && (
                  <div className="mt-2 flex items-center gap-2 text-[11px] flex-wrap">
                    {running ? (
                      <span className="flex items-center gap-1.5 text-emerald-700 font-semibold">
                        <Loader2 className="h-3 w-3 animate-spin" /> running {runStatus.scripts ?? ''} script(s) → {runStatus.target || 'recorded site'}
                        {runStatus.total_tests ? <span className="text-slate-500 font-medium">· {runStatus.steps_completed ?? 0}/{runStatus.total_tests} done</span> : null}
                      </span>
                    ) : (
                      <>
                        <span className="rounded px-2 py-0.5 font-black uppercase text-[9px]" style={runStatusStyle(runStatus.status)}>{runStatus.status}</span>
                        {runStatus.status !== 'unknown' && (
                          <button onClick={() => setView('run')} className="text-indigo-600 text-[10px] font-bold underline hover:text-indigo-800">→ view results &amp; triage</button>
                        )}
                      </>
                    )}
                  </div>
                )}
                {runErr && <p className="mt-2 text-[10px] text-amber-700">{runErr}</p>}
              </div>

              {/* or download + run locally */}
              <div className="border-t border-indigo-100 pt-2.5">
                <p className="text-[10px] font-bold uppercase text-slate-400 mb-2">or download &amp; run locally</p>
                <div className="flex items-center gap-2 flex-wrap">
                  <button onClick={downloadConfigured} disabled={runBusy || selectedScripts.length === 0}
                    className="flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-[11px] font-bold text-white disabled:opacity-50"
                    style={{ background: '#4f46e5' }}>
                    {runBusy ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Download className="h-3.5 w-3.5" />}
                    Download configured bundle ({selectedScripts.length})
                  </button>
                  <code className="flex-1 min-w-0 truncate rounded-md bg-slate-900 px-2.5 py-1.5 text-[11px] font-mono text-emerald-300">{RUN_CMD}</code>
                  <button onClick={() => copy('runcmd', RUN_CMD)}
                    className="shrink-0 rounded-md p-1.5 text-slate-400 hover:bg-slate-200 hover:text-slate-700" title="Copy">
                    {copied === 'runcmd' ? <Check className="h-3.5 w-3.5 text-emerald-500" /> : <Copy className="h-3.5 w-3.5" />}
                  </button>
                </div>
              </div>
            </div>
          </div>
        </div>
      )}

      {/* A.3 - GROUNDED VERDICT, front-and-center (the differentiator) */}
      {data && totals.scripts > 0 && (
        <div className="rounded-xl border-2 p-3" style={{ borderColor: 'rgba(16,185,129,0.30)', background: 'linear-gradient(135deg, rgba(16,185,129,0.06), rgba(99,102,241,0.04))' }}>
          <div className="flex items-center gap-2 flex-wrap">
            <span className="text-[13px]">🛡️</span>
            <span className="text-[12px] font-black text-slate-900">Grounded verdict</span>
            <span className="text-[10px] text-slate-500 font-medium max-w-[560px]">
              🟢 = the app reached the same outcome the recorded human did · 🔴 = a real deviation (not a layout change). Every verdict links to the recorded evidence.
            </span>
            {runs?.board?.last_run_at ? (
              <span className="ml-auto flex items-center gap-1.5 text-[11px] font-bold flex-wrap">
                <span className="rounded-full px-2 py-0.5" style={{ background: 'rgba(34,197,94,0.16)', color: '#15803d' }}>{runs.board.passed ?? 0} passed</span>
                {(runs.board.failed ?? 0) > 0 && <span className="rounded-full px-2 py-0.5" style={{ background: 'rgba(244,63,94,0.14)', color: '#be123c' }}>{runs.board.failed} failed</span>}
                {(runs.board.flaky ?? 0) > 0 && <span className="rounded-full px-2 py-0.5" style={{ background: 'rgba(245,158,11,0.16)', color: '#b45309' }}>{runs.board.flaky} flaky</span>}
                <button onClick={() => setView('run')} className="text-indigo-600 underline text-[10px] font-bold hover:text-indigo-800">see proof →</button>
              </span>
            ) : (
              <span className="ml-auto text-[10px] text-slate-400 italic">Run a script to get a grounded verdict - green means the app is actually right, with proof.</span>
            )}
          </div>
        </div>
      )}

      {/* Run / Results view — return to the Run Console */}
      {view === 'run' && (
        <button onClick={() => setView('console')}
          className="flex items-center gap-1 text-[12px] font-bold text-indigo-700 hover:text-indigo-900">
          ‹ Back to Run Console
        </button>
      )}
      {/* Live execution — auto-heal trace + headed run stream (results view only;
          mounts here so the noVNC websocket is torn down when you go Back) */}
      {view === 'run' && ((autoHealing || autoHealJob) || (liveUrl && running)) && (
        <div className="space-y-3">
          {(autoHealing || autoHealJob) && (
            <AutoHealPanel job={autoHealJob} live={autoHealLive} healing={autoHealing} err={autoHealErr} />
          )}
          {liveUrl && running && (
            <div ref={liveRef} className="rounded-lg overflow-hidden border-2 border-violet-300 bg-black">
              <div className="px-2 py-1 bg-violet-600 text-white text-[10px] font-bold flex items-center gap-1.5">
                <span className="inline-block h-2 w-2 rounded-full bg-red-400 animate-pulse" />
                LIVE — headed Chromium on the Nexus runner (view-only stream)
              </div>
              <iframe title="Nexus live run" src={liveUrl} className="w-full" style={{ height: 520, border: 0 }} />
            </div>
          )}
          {runStatus && running && (
            <div className="flex items-center gap-1.5 text-[11px] text-emerald-700 font-semibold">
              <Loader2 className="h-3 w-3 animate-spin" /> running {runStatus.scripts ?? ''} script(s) → {runStatus.target || 'recorded site'}
              {runStatus.total_tests ? <span className="text-slate-500 font-medium">· {runStatus.steps_completed ?? 0}/{runStatus.total_tests} done</span> : null}
            </div>
          )}
        </div>
      )}
      {/* ── This run (per-step timeline) + History (right under the run console) ─ */}
      {view === 'run' && data && totals.scripts > 0 && (() => {
        const th = timeline?.run_header;
        return (
        <div ref={resultsRef} className="rounded-xl border-2 border-indigo-200 bg-indigo-50/30 p-3">
          {/* This run — header counts come off ONE run row, so they always agree
              with the per-step timeline below (no 1-vs-6 mismatch). */}
          <div className="flex items-center gap-2 px-1 pb-2 flex-wrap">
            {th && th.failed_steps > 0 ? (
              <>
                <ShieldAlert className="h-4 w-4" style={{ color: '#b91c1c' }} />
                <span className="text-[13px] font-black text-rose-700">This run — where it broke</span>
              </>
            ) : th ? (
              <>
                <span className="text-base font-black leading-none text-emerald-600">✓</span>
                <span className="text-[13px] font-black text-emerald-700">This run — all green</span>
              </>
            ) : (
              <>
                <span className="text-[11px] font-black uppercase tracking-wide" style={{ color: '#4f46e5' }}>Run results</span>
                <span className="text-[10px] text-slate-400 font-semibold">your latest run appears here, step by step</span>
              </>
            )}
            {th && (
              <span className="ml-auto flex items-center gap-1.5 flex-wrap text-[11px] font-bold">
                <span className="rounded-full px-2.5 py-1" style={{ background: 'rgba(34,197,94,0.16)', color: '#15803d' }}>{th.passed_steps} passed</span>
                {th.failed_steps > 0 && <span className="rounded-full px-2.5 py-1" style={{ background: 'rgba(239,68,68,0.16)', color: '#b91c1c' }}>{th.failed_steps} failed</span>}
                {th.skipped_steps > 0 && <span className="rounded-full px-2.5 py-1" style={{ background: 'rgba(148,163,184,0.16)', color: '#64748b' }}>{th.skipped_steps} skipped</span>}
                <span className="text-slate-400 font-semibold">· {(th.duration_ms / 1000).toFixed(1)}s{th.started_at ? ` · ${new Date(th.started_at).toLocaleString()}` : ''}</span>
              </span>
            )}
          </div>

          {th ? (
            <StepTimeline scenarios={timeline.scenarios || []} artifactId={artifactId} baseUrl={baseUrl} />
          ) : (
            <div className="rounded-xl border border-dashed border-slate-300 bg-white/60 px-4 py-8 text-center">
              <p className="text-sm font-semibold text-slate-600">No runs yet</p>
              <p className="text-[11px] text-slate-400 mt-1 max-w-md mx-auto leading-relaxed">
                Run a script above (or wire the bundled Nexus reporter) and this run's step-by-step pass/fail timeline appears here — each failing step shown beside its known-good baseline.
              </p>
            </div>
          )}

          {/* History — the per-scenario-latest accumulation + flake trend across
              the last N runs. Kept separate + collapsed so it never contradicts
              the single run above. */}
          <div className="mt-3 rounded-xl border border-slate-200 bg-white/70 overflow-hidden">
            <button onClick={() => setHistoryOpen((v) => !v)}
              className="w-full flex items-center gap-2 px-3 py-2 text-left hover:bg-slate-50/70">
              <History className="h-4 w-4 text-slate-500" />
              <span className="text-[12px] font-bold text-slate-700">History &amp; flake</span>
              <span className="text-[10px] text-slate-400">across the last runs — per-scenario verdicts + flake trend</span>
              {historyOpen ? <ChevronDown className="h-4 w-4 text-slate-400 ml-auto" /> : <ChevronRight className="h-4 w-4 text-slate-400 ml-auto" />}
            </button>
            {historyOpen && (
              <div className="px-3 pb-3 pt-1">
                <OracleScorecardCard artifactId={artifactId} refreshKey={triageKey} />
                <RtmCard artifactId={artifactId} />
                {runs?.board?.last_run_at ? (
                  <div className="flex items-center gap-2 flex-wrap mb-2 px-1 text-[11px] font-bold">
                    <span className="rounded-full px-2.5 py-1" style={{ background: 'rgba(34,197,94,0.16)', color: '#15803d' }}>{runs.board.passed} passed</span>
                    <span className="rounded-full px-2.5 py-1" style={{ background: 'rgba(239,68,68,0.16)', color: '#b91c1c' }}>{runs.board.failed} failed</span>
                    {runs.board.flaky > 0 && <span className="rounded-full px-2.5 py-1" style={{ background: 'rgba(100,116,139,0.14)', color: '#475569' }}>{runs.board.flaky} flaky</span>}
                    {runs.board.skipped > 0 && <span className="rounded-full px-2.5 py-1" style={{ background: 'rgba(148,163,184,0.16)', color: '#64748b' }}>{runs.board.skipped} skipped</span>}
                  </div>
                ) : null}
                <TriagePanel key={triageKey} artifactId={artifactId} scopeIds={selectedScripts.map((s: Script) => s.test_id).filter(Boolean)} />
              </div>
            )}
          </div>
        </div>
        );
      })()}

      {/* ── How to run ──────────────────────────────────────── */}
      {view === 'console' && data && (
        <div className="rounded-xl border border-slate-200 bg-slate-50/60 overflow-hidden">
          <button onClick={() => setHowToRun((v) => !v)}
            className="w-full flex items-center gap-2 px-4 py-2.5 text-left hover:bg-slate-100/60">
            <Terminal className="h-4 w-4 text-slate-500" />
            <span className="text-[12px] font-bold text-slate-700">How to run</span>
            <span className="text-[10px] text-slate-400">local · CI · the optional Nexus reporter feeds the triage board</span>
            {howToRun ? <ChevronDown className="h-4 w-4 text-slate-400 ml-auto" /> : <ChevronRight className="h-4 w-4 text-slate-400 ml-auto" />}
          </button>
          {howToRun && (
            <div className="px-4 pb-4 pt-1 space-y-2">
              {[
                { k: 'install', label: '1 · Install', cmd: data.run.install },
                { k: 'all', label: '2 · Run the suite', cmd: data.run.all },
                { k: 'headed', label: 'Watch it run', cmd: data.run.headed },
                { k: 'ui', label: 'Pick & debug (UI mode)', cmd: data.run.ui },
              ].map((row) => (
                <div key={row.k} className="flex items-center gap-2">
                  <span className="text-[10px] font-semibold text-slate-400 w-36 shrink-0">{row.label}</span>
                  <code className="flex-1 min-w-0 truncate rounded-md bg-slate-900 px-2.5 py-1.5 text-[11px] font-mono text-emerald-300">{row.cmd}</code>
                  <button onClick={() => copy(`run:${row.k}`, row.cmd)}
                    className="shrink-0 rounded-md p-1.5 text-slate-400 hover:bg-slate-200 hover:text-slate-700" title="Copy">
                    {copied === `run:${row.k}` ? <Check className="h-3.5 w-3.5 text-emerald-500" /> : <Copy className="h-3.5 w-3.5" />}
                  </button>
                </div>
              ))}
              <div className="mt-2 rounded-lg border border-indigo-100 bg-indigo-50/60 px-3 py-2">
                <p className="text-[10px] font-bold text-indigo-700 mb-1">Feed the Grounded Triage board (optional)</p>
                <p className="text-[10px] text-slate-500 leading-relaxed mb-1.5">
                  Set these env vars before running and the bundled <span className="font-mono">nexus-reporter</span> uploads each run — every failure then appears below, classified and shown beside its known-good baseline.
                </p>
                <div className="flex flex-wrap gap-1.5">
                  {Object.entries(data.run.reporter_env).map(([k, v]) => (
                    <button key={k} onClick={() => copy(`env:${k}`, `${k}=${v}`)}
                      className="flex items-center gap-1 rounded-md bg-white border border-slate-200 px-2 py-1 text-[10px] font-mono text-slate-600 hover:bg-slate-50">
                      {copied === `env:${k}` ? <Check className="h-3 w-3 text-emerald-500" /> : <Copy className="h-3 w-3 text-slate-400" />}
                      {k}=<span className="text-slate-400">{v}</span>
                    </button>
                  ))}
                </div>
              </div>
            </div>
          )}
        </div>
      )}

      {/* ── Scripts by category ─────────────────────────────── */}
      {loading && !data && (
        <div className="flex items-center justify-center py-16 text-slate-400 gap-2 text-sm">
          <Loader2 className="h-4 w-4 animate-spin" /> Compiling scripts…
        </div>
      )}

      {data && totals.scripts === 0 && (
        <div className="rounded-xl border border-dashed border-slate-300 bg-white/60 px-4 py-12 text-center">
          <FileCode2 className="h-7 w-7 text-slate-300 mx-auto mb-2" />
          <p className="text-sm font-semibold text-slate-600">No scripts yet</p>
          <p className="text-[11px] text-slate-400 mt-1 max-w-md mx-auto leading-relaxed">
            Generate test cases on the <span className="font-semibold">Test Cases</span> tab first — every active test case compiles into a runnable Playwright spec here, grouped by category.
          </p>
        </div>
      )}

    </section>
  );
}
