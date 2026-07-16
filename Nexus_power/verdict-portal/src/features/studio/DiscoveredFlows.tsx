/**
 * DISCOVERED FLOWS — the governance overlay on the reused Test Studio.
 *
 * Answers the operator's question directly: "the crawl found N flows, but a Run
 * cycle has only executed a few end-to-end — which ones, and why?" Every flow the
 * crawl discovered is listed with an HONEST status derived from real factory
 * signals (the case list + the run/verdict summary), never a fabricated one:
 *
 *   • Proven      — a real, non-flaky, green end-to-end run exists.
 *   • Needs attention — it ran, but the last run failed / is flaky / regressed.
 *   • Candidate   — discovered + compiled, but never executed end-to-end.
 *
 * The classification defaults to "not proven": a flow is only ever marked Proven
 * on a concrete passing run record. Absence of a run record is Candidate, never a
 * silent green. From here an authorised operator can promote any candidate to a
 * live, headed run (reusing the runner) — so a flow the cycle skipped can be
 * proven on demand without waiting for the next cycle.
 *
 * This composes the SAME artifact-keyed factory data the panels use (via the
 * Phase-1 bridge); it adds no backend and fabricates nothing.
 */
import { useMemo, useState } from 'react';
import { toast } from 'sonner';
import {
  CheckCircle2, ChevronDown, ChevronRight, CircleDashed, PlayCircle, ShieldAlert, Sparkles,
} from 'lucide-react';

import { useAuth } from '../../lib/auth';
import { useAsync } from '../../lib/useAsync';
import { cn, humanize, timeAgo } from '../../lib/format';
import { Button, EmptyState, ErrorState, Panel, Pill, SectionHead, SkeletonRows } from '../../components';
import { api as studioApi } from '../../studio/factoryApi';

type FlowStatus = 'proven' | 'attention' | 'candidate';

interface CaseRow {
  test_case_id: string;
  name: string;
  type: string;
  priority?: string;
  status?: string;
  step_count?: number;
}

interface ScriptRun {
  runs?: Array<{ status: string; at: string | null }>;
  is_flaky?: boolean;
  flake_rate_pct?: number;
  consecutive_failures?: number;
  last_run_status?: string;
  last_run_at?: string | null;
}

interface FlowsData {
  cases: CaseRow[];
  total: number;
  scripts: Record<string, ScriptRun>;
}

const STATUS_META: Record<FlowStatus, { label: string; tone: 'good' | 'crit' | 'neutral'; icon: React.ReactNode }> = {
  proven: { label: 'Proven', tone: 'good', icon: <CheckCircle2 size={14} /> },
  attention: { label: 'Needs attention', tone: 'crit', icon: <ShieldAlert size={14} /> },
  candidate: { label: 'Candidate', tone: 'neutral', icon: <CircleDashed size={14} /> },
};

const SORT_RANK: Record<FlowStatus, number> = { attention: 0, candidate: 1, proven: 2 };

const PASS_STATES = new Set(['passed', 'pass', 'success', 'green', 'ok']);

/** Classify ONE flow from its run record. Defaults to Candidate; only a concrete
 *  green, non-flaky, no-consecutive-failure run earns Proven — never green-wash.
 *  Exported for the never-green-wash unit test. */
export function classify(script: ScriptRun | undefined): { status: FlowStatus; reason: string } {
  const hasRunEvidence = !!script && ((script.runs?.length ?? 0) > 0 || Boolean(script.last_run_status));
  if (!hasRunEvidence) {
    return { status: 'candidate', reason: 'Discovered + compiled — never executed end-to-end.' };
  }
  const last = String(script.last_run_status ?? '').toLowerCase();
  const passed = PASS_STATES.has(last);
  const flaky = Boolean(script.is_flaky);
  const consec = script.consecutive_failures ?? 0;
  const when = script.last_run_at ? ` · ${timeAgo(script.last_run_at)}` : '';

  if (passed && !flaky && consec === 0) {
    return { status: 'proven', reason: `Passed end-to-end${when}.` };
  }
  const bits: string[] = [];
  if (!passed) bits.push(`last run ${script.last_run_status || 'did not pass'}`);
  if (flaky) bits.push(`flaky (${Math.round(script.flake_rate_pct ?? 0)}%)`);
  if (consec > 0) bits.push(`${consec} consecutive failure${consec === 1 ? '' : 's'}`);
  return { status: 'attention', reason: `Ran but not clean — ${bits.join(', ')}${when}.` };
}

// ── Per-step execution evidence (the client's "which step ran / failed / why /
//    where" question). Data already exists on the backend — a flow's LAST run,
//    step by step. Nothing is fabricated: a step with no run record shows nothing.
const STEP_FAIL = new Set(['failed', 'broken', 'timed_out', 'error']);
const STEP_GLYPH: Record<string, { g: string; cls: string }> = {
  passed: { g: '✓', cls: 'text-good' },
  failed: { g: '✗', cls: 'text-crit' },
  broken: { g: '✗', cls: 'text-crit' },
  timed_out: { g: '⏱', cls: 'text-warn' },
  error: { g: '✗', cls: 'text-crit' },
  skipped: { g: '–', cls: 'text-ink-low' },
};

interface RunStep {
  step_number?: number;
  status?: string;
  label?: string;
  action?: string;
  error_message?: string;
  resolved_selector?: string;
  expected_selector?: string;
  screenshot_url?: string;
  duration_ms?: number;
}

/** Expandable per-step evidence for ONE flow's last run. Answers, in place:
 *  which steps ran, which failed, WHY (error), and WHERE (selector) — plus the
 *  failure screenshot. No new backend: reads the flow's stored last-run steps. */
function FlowSteps({ artifactId, scenarioId }: { artifactId: string; scenarioId: string }) {
  const state = useAsync<any>(
    () => studioApi.getScenarioLastRun(artifactId, scenarioId),
    [artifactId, scenarioId],
  );

  if (state.isLoading) return <div className="pl-8 py-2"><SkeletonRows rows={2} /></div>;
  if (state.isError) {
    return (
      <p className="pl-8 py-2 text-2xs text-ink-low">
        No execution evidence recorded for this flow yet — run it to capture per-step results.
      </p>
    );
  }
  const data = state.data || {};
  const steps: RunStep[] = Array.isArray(data.steps) ? data.steps : [];
  if (steps.length === 0) {
    return (
      <p className="pl-8 py-2 text-2xs text-ink-low">
        No steps recorded for the last run{data.status ? ` (${humanize(String(data.status))})` : ''}.
      </p>
    );
  }
  const verdict = data.verdict || data.scenario_verdict;
  const justification = data.justification || data.root_cause_hints;
  return (
    <div className="pl-8 pr-2 pb-3 pt-1 space-y-1.5 min-w-0">
      {(verdict || justification) && (
        <div className="text-2xs text-ink-low mb-1">
          {verdict && <Pill tone="crit" size="sm" variant="soft">{humanize(String(verdict))}</Pill>}
          {justification && <span className="ml-2">{String(justification)}</span>}
        </div>
      )}
      {steps.map((st, i) => {
        const status = String(st.status || '').toLowerCase();
        const failed = STEP_FAIL.has(status);
        const glyph = STEP_GLYPH[status] || STEP_GLYPH.skipped;
        const where = st.resolved_selector || st.expected_selector || '';
        const shot = st.screenshot_url ? studioApi.getRunScreenshotUrl(st.screenshot_url) : '';
        return (
          <div key={st.step_number ?? i} className="flex items-start gap-2 min-w-0">
            <span className={cn('shrink-0 font-mono text-xs w-4 text-center', glyph.cls)} title={status}>
              {glyph.g}
            </span>
            <span className="shrink-0 text-2xs text-ink-low tabular-nums w-5 text-right">
              {st.step_number ?? i + 1}
            </span>
            <div className="min-w-0 flex-1">
              <p className={cn('text-xs truncate', failed ? 'text-crit' : 'text-ink')}>
                {st.label || st.action || `Step ${st.step_number ?? i + 1}`}
              </p>
              {failed && st.error_message && (
                <p className="text-2xs text-crit/90 mt-0.5 break-words font-mono">
                  {String(st.error_message).split('\n')[0].slice(0, 200)}
                </p>
              )}
              {failed && where && (
                <p className="text-2xs text-ink-low mt-0.5 break-all font-mono">where: {where}</p>
              )}
              {shot && (
                <a href={shot} target="_blank" rel="noopener noreferrer"
                   className="text-2xs text-teal hover:underline">view screenshot</a>
              )}
            </div>
            {typeof st.duration_ms === 'number' && (
              <span className="shrink-0 text-2xs text-ink-low tabular-nums">{Math.round(st.duration_ms)}ms</span>
            )}
          </div>
        );
      })}
    </div>
  );
}

export function DiscoveredFlows({
  artifactId,
  onOpenPlaywright,
}: {
  artifactId: string;
  onOpenPlaywright?: () => void;
}) {
  const { session } = useAuth();
  const canRun = ['admin', 'manager'].includes(String(session?.role ?? '').toLowerCase());
  const [running, setRunning] = useState<string | null>(null);
  const [expanded, setExpanded] = useState<string | null>(null);

  const state = useAsync<FlowsData>(async () => {
    // All discovered flows + the run/verdict summary, composed client-side.
    const [list, summary] = await Promise.all([
      studioApi.listTestFactoryCases(artifactId, 1, 200, 'active'),
      studioApi.getRunsSummary(artifactId, 10).catch(() => ({ scripts: {} })),
    ]);
    return {
      cases: (list?.items ?? []) as CaseRow[],
      total: Number(list?.total ?? (list?.items?.length ?? 0)),
      scripts: (summary?.scripts ?? {}) as Record<string, ScriptRun>,
    };
  }, [artifactId]);

  const flows = useMemo(() => {
    const data = state.data;
    if (!data) return [];
    return data.cases
      .map((c) => {
        const { status, reason } = classify(data.scripts[c.test_case_id]);
        return { ...c, status, reason };
      })
      .sort((a, b) => SORT_RANK[a.status] - SORT_RANK[b.status] || a.name.localeCompare(b.name));
  }, [state.data]);

  const counts = useMemo(() => {
    const c = { proven: 0, attention: 0, candidate: 0 };
    flows.forEach((f) => (c[f.status] += 1));
    return c;
  }, [flows]);

  const promote = async (flow: CaseRow) => {
    setRunning(flow.test_case_id);
    try {
      const res = await studioApi.startNexusLiveRun(artifactId, { test_ids: [flow.test_case_id] });
      toast.success('Headed run started', { description: flow.name });
      if (res?.live_url) window.open(res.live_url, '_blank', 'noopener,noreferrer');
      else onOpenPlaywright?.();
    } catch (err) {
      const detail = (err as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail;
      toast.error('Could not start run', { description: typeof detail === 'string' ? detail : String(err) });
    } finally {
      setRunning(null);
    }
  };

  return (
    <Panel tone="elevated">
      <SectionHead
        title="Discovered flows"
        subtitle="every flow the crawl found — proven end-to-end, or a candidate you can run on demand"
        icon={<Sparkles size={16} className="text-teal" />}
        right={
          state.isSuccess && (
            <div className="flex items-center gap-1.5">
              <Pill tone="good" size="sm">{counts.proven} proven</Pill>
              {counts.attention > 0 && <Pill tone="crit" size="sm">{counts.attention} attention</Pill>}
              <Pill tone="neutral" size="sm">{counts.candidate} candidate</Pill>
            </div>
          )
        }
      />

      <div className="mt-3">
        {state.isLoading && <SkeletonRows rows={4} />}
        {state.isError && <ErrorState error={state.error} onRetry={state.reload} />}
        {state.isSuccess &&
          (flows.length > 0 ? (
            <>
              <p className="text-2xs text-ink-low mb-3">
                {counts.proven} of {flows.length} discovered flow{flows.length === 1 ? '' : 's'} have a proven
                green end-to-end run. The rest are compiled and ready — run any on demand; nothing is marked green
                without a real passing run.
                {state.data && state.data.total > flows.length && (
                  <span className="text-warn"> Showing {flows.length} of {state.data.total} (active).</span>
                )}
              </p>
              <ul className="divide-y divide-line">
                {flows.map((f) => {
                  const meta = STATUS_META[f.status];
                  const isOpen = expanded === f.test_case_id;
                  // A flow with run evidence (proven/attention) can be expanded to
                  // its per-step results; a never-run candidate has nothing to show.
                  const hasEvidence = f.status !== 'candidate';
                  return (
                    <li key={f.test_case_id} className="py-1">
                      <div className="py-1.5 flex items-center gap-3">
                        <button
                          type="button"
                          className={cn('shrink-0 text-ink-low', !hasEvidence && 'invisible')}
                          title={isOpen ? 'Hide steps' : 'Show which steps ran / failed and why'}
                          onClick={() => setExpanded(isOpen ? null : f.test_case_id)}
                        >
                          {isOpen ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
                        </button>
                        <span
                          className={cn(
                            'inline-flex items-center gap-1 shrink-0 rounded-md px-1.5 py-0.5 text-2xs font-semibold',
                            meta.tone === 'good' && 'text-good',
                            meta.tone === 'crit' && 'text-crit',
                            meta.tone === 'neutral' && 'text-ink-low',
                          )}
                          title={meta.label}
                        >
                          {meta.icon}
                        </span>
                        <button
                          type="button"
                          className="min-w-0 flex-1 text-left"
                          disabled={!hasEvidence}
                          onClick={() => hasEvidence && setExpanded(isOpen ? null : f.test_case_id)}
                        >
                          <div className="flex items-center gap-2">
                            <p className="text-sm text-ink truncate">{f.name}</p>
                            <Pill tone={meta.tone} size="sm" variant="soft">{meta.label}</Pill>
                            {f.type && <Pill tone="neutral" size="sm">{humanize(f.type)}</Pill>}
                          </div>
                          <p className="text-2xs text-ink-low mt-0.5">
                            {f.reason}
                            {typeof f.step_count === 'number' && f.step_count > 0 && ` · ${f.step_count} steps`}
                            {hasEvidence && !isOpen && <span className="text-teal"> · view steps</span>}
                          </p>
                        </button>
                        <Button
                          size="sm"
                          variant={f.status === 'proven' ? 'ghost' : 'secondary'}
                          loading={running === f.test_case_id}
                          disabled={!canRun}
                          title={canRun ? 'Run this flow headed (live)' : 'Running requires an admin or manager role'}
                          icon={<PlayCircle size={14} />}
                          onClick={() => promote(f)}
                        >
                          {f.status === 'proven' ? 'Re-run' : 'Run'}
                        </Button>
                      </div>
                      {isOpen && hasEvidence && (
                        <FlowSteps artifactId={artifactId} scenarioId={f.test_case_id} />
                      )}
                    </li>
                  );
                })}
              </ul>
            </>
          ) : (
            <EmptyState
              title="No flows generated yet"
              hint="Open the Test Cases tab and Generate — the crawl's discovered flows become grounded test cases here."
            />
          ))}
      </div>
    </Panel>
  );
}

export default DiscoveredFlows;
