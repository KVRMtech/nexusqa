/**
 * StepTimeline — the "where did it break" view for ONE run.
 *
 * Renders each scenario of the latest run as a card with a vertical step-by-step
 * timeline: every executed step shows a pass/fail glyph + its human label (the
 * recorded ProductionTestCase action) + duration. Failed scenarios auto-expand;
 * the failing step expands inline to the error, the expected outcome, and the
 * baseline-vs-actual two-up (Nexus's recorded known-good screenshot), plus the
 * ready-to-file defect export. Passed steps stay compact one-liners.
 *
 * Data comes straight from GET /test-factory/{id}/runs/latest — a single run, so
 * the header counts above it always agree with these rows (no 1-vs-6 mismatch).
 */
import { useState } from 'react';
import { AlertTriangle, CheckCircle2, ChevronDown, ChevronRight, Copy, Download, Loader2, Play, Wand2 } from 'lucide-react';
import { api } from '../services/api';
import { VERDICT, Frame, defectMarkdown, downloadDefect, type Scenario } from './TriagePanel';

// TrueFix cause → chip styling (mirrors the deterministic diagnosis labels).
const CAUSE_STYLE: Record<string, { bg: string; fg: string }> = {
  REAL_REGRESSION: { bg: 'rgba(239,68,68,0.18)', fg: '#991b1b' },
  WRONG_CONTROL_KIND: { bg: 'rgba(245,158,11,0.16)', fg: '#b45309' },
  SELECTOR_REANCHOR: { bg: 'rgba(34,197,94,0.16)', fg: '#15803d' },
  SELECTOR_DRIFT: { bg: 'rgba(56,189,248,0.16)', fg: '#0369a1' },
  LOCATOR_NOT_FOUND: { bg: 'rgba(239,68,68,0.14)', fg: '#b91c1c' },
  FLAKE: { bg: 'rgba(100,116,139,0.14)', fg: '#475569' },
  NEEDS_REVIEW: { bg: 'rgba(245,158,11,0.14)', fg: '#b45309' },
};

export interface TimelineStep {
  step_number: number;
  label: string;
  status: string;
  duration_ms: number;
  error_message: string;
  screenshot_url: string;
  video_url?: string;
  baseline_screenshot: string;
  expected: string;
  diagnosis?: any;   // Sentinel auto-diagnosis (cause + Triage source/route + opt-in Context)
}

export interface TimelineScenario {
  scenario_id: string;
  name: string;
  type: string;
  verdict: string;
  confidence: number;
  justification: string;
  status: string;
  baseline_available: boolean;
  failed_step_number: number | null;
  steps: TimelineStep[];
}

const FAIL = new Set(['failed', 'broken', 'timed_out']);

const STEP_ICON: Record<string, { glyph: string; color: string; bg: string }> = {
  passed: { glyph: '✓', color: '#15803d', bg: 'rgba(34,197,94,0.14)' },
  failed: { glyph: '✗', color: '#b91c1c', bg: 'rgba(239,68,68,0.14)' },
  broken: { glyph: '✗', color: '#b91c1c', bg: 'rgba(239,68,68,0.14)' },
  timed_out: { glyph: '⏱', color: '#b45309', bg: 'rgba(245,158,11,0.16)' },
  skipped: { glyph: '⊘', color: '#94a3b8', bg: 'rgba(148,163,184,0.16)' },
};

function fmtDur(ms: number): string {
  if (!ms || ms < 0) return '—';
  if (ms < 1000) return `${ms}ms`;
  return `${(ms / 1000).toFixed(1)}s`;
}

// Map a timeline scenario + its failing step into the Scenario shape that the
// shared defect exporter expects — so "Copy defect / Download .md" produce the
// exact same evidence-rich markdown as the triage board.
function asScenario(sc: TimelineScenario, st: TimelineStep): Scenario {
  return {
    scenario_id: sc.scenario_id,
    name: sc.name,
    type: sc.type,
    status: sc.status,
    verdict: sc.verdict,
    confidence: sc.confidence,
    justification: sc.justification,
    step_number: st.step_number,
    baseline_screenshot: st.baseline_screenshot,
    actual_screenshot: st.screenshot_url,
    expected: st.expected,
    error_message: st.error_message,
    is_flaky: false,
    flake_rate_pct: 0,
  };
}

// Sentinel — the always-on auto-diagnosis card. Renders with NO click: the run timeline
// already carries the grounded diagnosis (deterministic cause + Triage source/route, and
// the opt-in Context cross-field explanation). It never claims a fix is applied.
const _SRC_STYLE: Record<string, { bg: string; fg: string; icon: string }> = {
  product:     { bg: 'rgba(239,68,68,0.14)',  fg: '#b91c1c', icon: '🐞' },
  script:      { bg: 'rgba(245,158,11,0.16)', fg: '#b45309', icon: '🧪' },
  environment: { bg: 'rgba(56,189,248,0.16)', fg: '#0369a1', icon: '🌐' },
  unknown:     { bg: 'rgba(100,116,139,0.14)', fg: '#475569', icon: '❓' },
};

function AutoDiagnosis({ dx }: { dx: any }) {
  if (!dx) return null;
  const tri = dx.triage || {};
  const src = _SRC_STYLE[tri.source || 'unknown'] || _SRC_STYLE.unknown;
  const sem = dx.semantic;
  const prov = (sem && sem.provenance) || dx.provenance || {};
  const headline = dx.headline || dx.cause_label || 'Diagnosis';
  const evidence: string[] = (sem ? [] : (dx.evidence || [])).slice(0, 3);
  return (
    <div className="mb-2 rounded-lg border p-2" style={{ borderColor: '#c7d2fe', background: 'rgba(238,242,255,0.6)' }}>
      <div className="flex items-center gap-1.5 flex-wrap mb-1">
        <span className="text-[10px] font-black text-indigo-900">🤖 Auto-diagnosis</span>
        {tri.source && <span className="rounded px-1.5 py-0.5 text-[9px] font-black uppercase" style={{ background: src.bg, color: src.fg }}>{src.icon} {tri.source_label || tri.source}</span>}
        {tri.route && <span className="rounded px-1.5 py-0.5 text-[9px] font-bold uppercase" style={{ background: 'rgba(15,37,64,0.08)', color: '#0a2540' }}>{tri.route_label || tri.route}</span>}
        {typeof dx.confidence === 'number' && <span className="text-[9px] font-semibold text-slate-400">{Math.round((dx.confidence || 0) * 100)}%</span>}
      </div>
      <p className="text-[11px] text-slate-800 leading-snug">{headline}</p>
      {sem && sem.is_cross_field_inconsistency && (
        <div className="mt-1.5 rounded-md border border-indigo-100 bg-white/70 px-2 py-1.5">
          <p className="text-[10px] text-slate-800 leading-snug">{prov.claim || sem.human_explanation}</p>
          {(sem.suggested_value || sem.alternative_upstream_fix) && (
            <p className="text-[10px] text-emerald-700 font-semibold mt-1">
              Suggested: {sem.suggested_value
                ? `use the live option '${sem.suggested_value}'`
                : sem.alternative_upstream_fix}
            </p>
          )}
          <p className="text-[8.5px] text-slate-400 mt-1">
            {prov.confirmed_against_live ? '✓ confirmed against the live options' : 'inference'} · I did not change your data
          </p>
        </div>
      )}
      {evidence.length > 0 && (
        <ul className="list-disc pl-4 mt-1 space-y-0.5">
          {evidence.map((e, i) => <li key={i} className="text-[9.5px] text-slate-600 leading-snug">{e}</li>)}
        </ul>
      )}
      {dx.recommended_action && !sem && (
        <p className="text-[9.5px] text-slate-500 mt-1"><span className="font-bold">Next:</span> {dx.recommended_action}</p>
      )}
      <p className="text-[8px] text-slate-400 mt-1 italic">
        Auto-run · {prov.grounding === 'deterministic' ? 'deterministic ($0)' : 'grounded inference'} · the oracle still decides green
      </p>
    </div>
  );
}

export default function StepTimeline({ scenarios, artifactId, baseUrl }: { scenarios: TimelineScenario[]; artifactId: string; baseUrl?: string }) {
  if (!scenarios.length) return null;
  return (
    <div className="space-y-2">
      {scenarios.map((sc) => (
        <ScenarioCard key={sc.scenario_id || sc.name} sc={sc} artifactId={artifactId} baseUrl={baseUrl} />
      ))}
    </div>
  );
}

function ScenarioCard({ sc, artifactId, baseUrl }: { sc: TimelineScenario; artifactId: string; baseUrl?: string }) {
  const failed = FAIL.has(sc.status);
  const [open, setOpen] = useState(failed);
  const v = VERDICT[sc.verdict] || { label: sc.verdict, bg: 'rgba(100,116,139,0.12)', fg: '#475569', needYou: failed };
  const head = STEP_ICON[sc.status] || STEP_ICON.passed;
  const total = sc.steps.length;
  const passedN = sc.steps.filter((s) => s.status === 'passed').length;
  const dur = sc.steps.reduce((a, s) => a + (s.duration_ms || 0), 0);

  return (
    <div className="rounded-xl overflow-hidden" style={{ background: 'rgba(255,255,255,0.92)', border: `1px solid ${failed ? '#fecaca' : '#e2e8f0'}` }}>
      <button onClick={() => setOpen((o) => !o)} className="w-full flex items-center gap-2 px-3 py-2 text-left hover:bg-slate-50/70">
        <span className="text-base font-black leading-none" style={{ color: head.color }}>{head.glyph}</span>
        <span className="text-[12px] font-bold text-slate-900 break-words">{sc.name}</span>
        <span className="rounded px-2 py-0.5 text-[9px] font-black uppercase" style={{ background: v.bg, color: v.fg }}>{v.label}</span>
        <span className="ml-auto shrink-0 flex items-center gap-2 text-[10px] font-semibold text-slate-400">
          <span style={{ color: failed ? '#b91c1c' : '#15803d' }}>{passedN}/{total} steps</span>
          <span>· {fmtDur(dur)}</span>
          {open ? <ChevronDown className="h-3.5 w-3.5 text-slate-400" /> : <ChevronRight className="h-3.5 w-3.5 text-slate-400" />}
        </span>
      </button>
      {open && (
        <div className="px-3 pb-3 pt-0.5">
          <div className="relative pl-4">
            <div className="absolute left-[6px] top-2 bottom-2 w-px bg-slate-200" aria-hidden />
            {sc.steps.length === 0 ? (
              <p className="text-[11px] text-slate-400 py-2">No per-step detail recorded for this run.</p>
            ) : (
              sc.steps.map((st, i) => <StepRow key={`${st.step_number}-${i}`} st={st} sc={sc} artifactId={artifactId} baseUrl={baseUrl} />)
            )}
          </div>
        </div>
      )}
    </div>
  );
}

function StepRow({ st, sc, artifactId, baseUrl }: { st: TimelineStep; sc: TimelineScenario; artifactId: string; baseUrl?: string }) {
  const isFail = FAIL.has(st.status);
  const hasGreenShot = !isFail && !!st.screenshot_url; // final-frame success proof (last passed step on an evidence run)
  const [open, setOpen] = useState(isFail); // the failing step opens by default
  const ic = STEP_ICON[st.status] || STEP_ICON.passed;
  const s = asScenario(sc, st);

  const [diag, setDiag] = useState<any>(null);
  const [diagLoading, setDiagLoading] = useState(false);
  const [diagErr, setDiagErr] = useState<string | null>(null);
  const analyze = async () => {
    setDiagLoading(true); setDiagErr(null);
    try { setDiag(await api.analyzeStep(artifactId, sc.scenario_id, st.step_number)); }
    catch (e: any) { setDiagErr(e?.response?.data?.detail || String(e)); }
    finally { setDiagLoading(false); }
  };

  return (
    <div className="relative">
      <button
        onClick={() => (isFail || hasGreenShot) && setOpen((o) => !o)}
        className={`w-full flex items-center gap-2 py-1 text-left ${isFail ? 'hover:bg-rose-50/60 rounded-md cursor-pointer' : hasGreenShot ? 'hover:bg-emerald-50/60 rounded-md cursor-pointer' : 'cursor-default'}`}
      >
        <span className="relative z-10 -ml-4 grid place-items-center h-3.5 w-3.5 rounded-full text-[9px] font-black leading-none"
          style={{ background: ic.bg, color: ic.color }}>{ic.glyph}</span>
        <span className="text-[10px] font-bold text-slate-400 w-5 shrink-0 text-right">{st.step_number}</span>
        <span className={`text-[11px] break-words ${isFail ? 'font-bold text-rose-700' : 'text-slate-700'}`}>{st.label}</span>
        {isFail && <span className="shrink-0 text-[9px] font-black uppercase text-rose-500">◀ broke here</span>}
        {hasGreenShot && <span className="shrink-0 inline-flex items-center gap-0.5 text-[9px] font-bold text-emerald-600" title="Final-frame screenshot proof of the green run — click to view">📷 proof</span>}
        <span className="ml-auto shrink-0 text-[10px] font-mono" style={{ color: st.duration_ms >= 500 ? '#b45309' : '#94a3b8' }}>{fmtDur(st.duration_ms)}</span>
      </button>
      {isFail && open && (
        <div className="ml-1 mb-2 mt-1 rounded-lg border border-rose-200 bg-rose-50/50 p-2">
          {st.diagnosis && <AutoDiagnosis dx={st.diagnosis} />}
          {st.error_message && (
            <p className="text-[10px] font-mono text-rose-700 break-words mb-2" style={{ maxHeight: '6em', overflow: 'auto' }}>{st.error_message}</p>
          )}
          {st.expected && <p className="text-[10px] text-slate-500 mb-2">Expected: {st.expected}</p>}
          <div className="grid grid-cols-2 gap-2">
            <Frame label="Known-good (recorded)" src={st.baseline_screenshot ? api.getFrameImageUrl(st.baseline_screenshot) : ''} />
            <Frame label="This run" src={st.screenshot_url ? api.getRunScreenshotUrl(st.screenshot_url) : ''} awaiting={!st.screenshot_url} />
          </div>
          <div className="mt-2 flex items-center gap-2 flex-wrap">
            <button onClick={analyze} disabled={diagLoading}
              title="Nexus TrueFix — diagnose the real cause of this failure ($0, on-prem)"
              className="flex items-center gap-1 rounded-md px-2.5 py-1 text-[10px] font-bold text-white disabled:opacity-50"
              style={{ background: 'linear-gradient(135deg,#7c3aed,#4f46e5)' }}>
              {diagLoading ? <Loader2 className="h-3 w-3 animate-spin" /> : <Wand2 className="h-3 w-3" />} Analyze &amp; fix
            </button>
            <button onClick={() => { void navigator.clipboard?.writeText(defectMarkdown(s)).catch(() => {}); }}
              title="Copy a ready-to-file, evidence-rich defect (markdown)"
              className="flex items-center gap-1 rounded-md px-2 py-1 text-[10px] font-semibold bg-white border border-slate-200 text-slate-700 hover:bg-slate-50">
              <Copy className="h-3 w-3" /> Copy defect
            </button>
            <button onClick={() => downloadDefect(s)}
              className="flex items-center gap-1 rounded-md px-2 py-1 text-[10px] font-semibold bg-white border border-slate-200 text-slate-700 hover:bg-slate-50">
              <Download className="h-3 w-3" /> Download .md
            </button>
          </div>
          {diagErr && <p className="mt-2 text-[10px] text-amber-700">{diagErr}</p>}
          {diag && diag.found && <Diagnosis diag={diag} artifactId={artifactId} baseUrl={baseUrl} onReanalyze={analyze} />}
        </div>
      )}
      {hasGreenShot && open && (
        <div className="ml-1 mb-2 mt-1 rounded-lg border border-emerald-200 bg-emerald-50/50 p-2">
          <p className="text-[10px] font-semibold text-emerald-700 mb-1">✓ Final-frame proof — this run landed on the expected state.</p>
          <Frame label="This run (success)" src={api.getRunScreenshotUrl(st.screenshot_url || '')} />
          {st.video_url ? (
            <div className="mt-2">
              <p className="text-[10px] font-semibold text-emerald-700 mb-1">🎥 Run video</p>
              <video src={api.getRunScreenshotUrl(st.video_url)} controls preload="metadata"
                className="w-full max-w-md rounded border border-emerald-200" />
            </div>
          ) : null}
        </div>
      )}
    </div>
  );
}

// TrueFix diagnosis panel — grounded root cause + confidence + evidence +
// recommended action + the compiler-derived suggested fix (before/after) +
// one-click Apply & re-run (headed) that proves the fix green before saving it.
function Diagnosis({ diag, artifactId, baseUrl, onReanalyze }: { diag: any; artifactId: string; baseUrl?: string; onReanalyze?: () => void }) {
  const cs = CAUSE_STYLE[diag.cause] || { bg: 'rgba(100,116,139,0.14)', fg: '#475569' };
  const conf = Math.round((diag.confidence || 0) * 100);
  const fix = diag.suggested_fix;
  const canApply = !!(fix && fix.after);

  // Phase B — when a selector broke and there's no grounded fix yet, offer to
  // re-run with accessibility capture so TrueFix can find a RENAMED control.
  const [capturing, setCapturing] = useState(false);
  const [captureMsg, setCaptureMsg] = useState<string | null>(null);
  const canFindRenamed = !canApply && (diag.cause === 'LOCATOR_NOT_FOUND' || diag.cause === 'SELECTOR_DRIFT');
  const findRenamed = async () => {
    setCapturing(true); setCaptureMsg(null);
    try {
      const r = await api.captureFailureState(artifactId, diag.scenario_id, diag.step_number, { base_url: baseUrl || '' });
      if (r.reanchored) { setCaptureMsg(`Found a likely rename → '${r.reanchor?.name}'. Re-checking…`); onReanalyze?.(); }
      else if (r.captured) setCaptureMsg('No confident renamed-control match on the live page — needs a human (could be a real removal).');
      else setCaptureMsg('Could not capture the live page state (the re-run produced no accessibility snapshot).');
    } catch (e: any) {
      setCaptureMsg(e?.response?.data?.detail || String(e));
    } finally { setCapturing(false); }
  };

  const [healing, setHealing] = useState(false);
  const [liveUrl, setLiveUrl] = useState<string | null>(null);
  const [result, setResult] = useState<any>(null);
  const [healErr, setHealErr] = useState<string | null>(null);
  const [approving, setApproving] = useState(false);
  const [approved, setApproved] = useState(false);
  const [approveErr, setApproveErr] = useState<string | null>(null);

  const approve = async () => {
    setApproving(true); setApproveErr(null);
    try {
      await api.approveScriptVersion(artifactId, diag.scenario_id, result.heal_version);
      setApproved(true);
    } catch (e: any) {
      setApproveErr(e?.response?.data?.detail || String(e));
    } finally {
      setApproving(false);
    }
  };

  const applyFix = async () => {
    setHealing(true); setHealErr(null); setResult(null); setLiveUrl(null);
    try {
      const r = await api.healStep(artifactId, diag.scenario_id, diag.step_number, { base_url: baseUrl || '' });
      setLiveUrl(r.live_url || null);
      const runId = r.run_id;
      for (let i = 0; i < 160; i++) {                 // ~400s ceiling
        await new Promise((res) => setTimeout(res, 2500));
        const st = await api.getNexusRunStatus(artifactId, runId);
        if (typeof st.healed === 'boolean') { setResult(st); break; }  // _poll_heal decided
      }
    } catch (e: any) {
      setHealErr(e?.response?.data?.detail || String(e));
    } finally {
      setHealing(false); setLiveUrl(null);
    }
  };

  return (
    <div className="mt-2 rounded-lg border border-violet-200 bg-violet-50/50 p-2.5">
      <div className="flex items-center gap-2 flex-wrap mb-1.5">
        <Wand2 className="h-3.5 w-3.5" style={{ color: '#6d28d9' }} />
        <span className="text-[11px] font-black text-violet-900">Nexus TrueFix — root cause</span>
        <span className="rounded px-2 py-0.5 text-[9px] font-black uppercase" style={{ background: cs.bg, color: cs.fg }}>{diag.cause_label}</span>
        <span className="text-[10px] font-bold text-slate-500">{conf}% confidence</span>
        {diag.auto_fixable
          ? <span className="rounded px-1.5 py-0.5 text-[9px] font-bold" style={{ background: 'rgba(34,197,94,0.16)', color: '#15803d' }}>grounded · auto-fixable</span>
          : <span className="rounded px-1.5 py-0.5 text-[9px] font-bold" style={{ background: 'rgba(245,158,11,0.16)', color: '#b45309' }}>suggestion · confirm before apply</span>}
      </div>
      {(diag.evidence || []).length > 0 && (
        <ul className="list-disc pl-4 space-y-0.5 mb-2">
          {diag.evidence.map((e: string, i: number) => (
            <li key={i} className="text-[10px] text-slate-700 leading-snug">{e}</li>
          ))}
        </ul>
      )}
      {diag.recommended_action && (
        <p className="text-[10px] text-slate-800 mb-2"><span className="font-bold">Recommended:</span> {diag.recommended_action}</p>
      )}
      {fix && (fix.before || fix.after) && (
        <div className="rounded-md border border-slate-200 bg-white/80 p-2">
          <p className="text-[10px] font-bold text-slate-600 mb-1">Suggested fix{fix.summary ? ` — ${fix.summary}` : ''}</p>
          {fix.before && (
            <pre className="text-[9.5px] font-mono rounded bg-rose-50 border border-rose-100 text-rose-800 px-2 py-1 mb-1 overflow-x-auto whitespace-pre-wrap break-words">{fix.before.split('\n').map((l: string) => `- ${l}`).join('\n')}</pre>
          )}
          {fix.after && (
            <pre className="text-[9.5px] font-mono rounded bg-emerald-50 border border-emerald-100 text-emerald-800 px-2 py-1 overflow-x-auto whitespace-pre-wrap break-words">{fix.after.split('\n').map((l: string) => `+ ${l}`).join('\n')}</pre>
          )}
          {fix.needs && <p className="mt-1 text-[9.5px] text-slate-500 italic">{fix.needs}</p>}
        </div>
      )}

      {/* Phase B — find a renamed control: re-run with a11y capture, then re-analyze. */}
      {canFindRenamed && (
        <div className="mt-2">
          {!capturing ? (
            <button onClick={findRenamed}
              title="Re-run with accessibility capture to find a renamed / moved control"
              className="flex items-center gap-1.5 rounded-md px-2.5 py-1 text-[10px] font-bold text-white"
              style={{ background: 'linear-gradient(135deg,#0ea5e9,#6366f1)' }}>
              <Wand2 className="h-3 w-3" /> Find the renamed control
            </button>
          ) : (
            <p className="flex items-center gap-1.5 text-[10px] font-bold text-sky-800">
              <Loader2 className="h-3 w-3 animate-spin" /> Re-running with accessibility capture…
            </p>
          )}
          {captureMsg && <p className="mt-1 text-[10px] text-slate-600">{captureMsg}</p>}
        </div>
      )}

      {/* Apply & prove: recompile this test with the fix, re-run it headed, save
          a new version only if it goes green. */}
      {canApply && !result && (
        <div className="mt-2">
          {!healing ? (
            <button onClick={applyFix}
              title="Recompile this test with the fix and re-run it headed — saved only if it passes"
              className="flex items-center gap-1.5 rounded-md px-2.5 py-1 text-[10px] font-bold text-white"
              style={{ background: 'linear-gradient(135deg,#059669,#0d9488)' }}>
              <Play className="h-3 w-3" /> Apply &amp; re-run (headed)
            </button>
          ) : (
            <div className="rounded-md border border-violet-200 bg-white/80 p-2">
              <p className="flex items-center gap-1.5 text-[10px] font-bold text-violet-800">
                <Loader2 className="h-3 w-3 animate-spin" /> Verifying the fix on a headed re-run…
              </p>
              {liveUrl && (
                <div className="mt-2 rounded-md overflow-hidden border-2 border-violet-300 bg-black">
                  <div className="px-2 py-0.5 text-[9px] font-bold uppercase text-white bg-violet-600">Live — watching the re-run</div>
                  <iframe title="TrueFix live re-run" src={liveUrl} className="w-full" style={{ height: 280, border: 0 }} />
                </div>
              )}
            </div>
          )}
          {healErr && <p className="mt-1 text-[10px] text-amber-700">{healErr}</p>}
        </div>
      )}

      {result && (
        result.healed ? (
          <div className="mt-2 space-y-2">
            <div className="flex items-center gap-2 rounded-md border border-emerald-200 bg-emerald-50 px-2.5 py-1.5">
              <CheckCircle2 className="h-4 w-4 text-emerald-600 shrink-0" />
              <span className="text-[11px] font-bold text-emerald-800">
                Verified green on the headed re-run{result.heal_version ? ` — saved as v${result.heal_version}` : ''}{result.pending_approval && !approved ? ' (proposed)' : ''}.
              </span>
            </div>
            {result.pending_approval && !approved && (
              <div className="rounded-md border border-amber-200 bg-amber-50 px-2.5 py-2">
                <p className="text-[10px] text-amber-800 mb-1.5 font-semibold leading-snug">
                  Proposed fix — not active yet. A human approves before a machine-written fix becomes the source of truth for runs.
                </p>
                {approveErr && <p className="text-[10px] text-rose-700 mb-1">{approveErr}</p>}
                <button onClick={approve} disabled={approving}
                  title="Approve this fix — it becomes the active version for future runs"
                  className="flex items-center gap-1.5 rounded-md px-2.5 py-1 text-[10px] font-bold text-white disabled:opacity-50"
                  style={{ background: 'linear-gradient(135deg,#059669,#0d9488)' }}>
                  {approving ? <Loader2 className="h-3 w-3 animate-spin" /> : <CheckCircle2 className="h-3 w-3" />} Approve &amp; activate{result.heal_version ? ` v${result.heal_version}` : ''}
                </button>
              </div>
            )}
            {approved && (
              <div className="flex items-center gap-2 rounded-md border border-emerald-300 bg-emerald-100 px-2.5 py-1.5">
                <CheckCircle2 className="h-4 w-4 text-emerald-700 shrink-0" />
                <span className="text-[11px] font-bold text-emerald-900">
                  Approved — v{result.heal_version} is now the active source for runs.
                </span>
              </div>
            )}
          </div>
        ) : (
          <div className="mt-2 flex items-start gap-2 rounded-md border border-amber-200 bg-amber-50 px-2.5 py-1.5">
            <AlertTriangle className="h-4 w-4 text-amber-600 shrink-0 mt-0.5" />
            <span className="text-[11px] font-semibold text-amber-800">
              Not healed — {result.heal_reason || 'the re-run did not pass.'} Nothing was changed.
            </span>
          </div>
        )
      )}
    </div>
  );
}
