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
import { CheckCircle2, CircleDashed, PlayCircle, ShieldAlert, Sparkles } from 'lucide-react';

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
                  return (
                    <li key={f.test_case_id} className="py-2.5 flex items-center gap-3">
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
                      <div className="min-w-0 flex-1">
                        <div className="flex items-center gap-2">
                          <p className="text-sm text-ink truncate">{f.name}</p>
                          <Pill tone={meta.tone} size="sm" variant="soft">{meta.label}</Pill>
                          {f.type && <Pill tone="neutral" size="sm">{humanize(f.type)}</Pill>}
                        </div>
                        <p className="text-2xs text-ink-low mt-0.5">
                          {f.reason}
                          {typeof f.step_count === 'number' && f.step_count > 0 && ` · ${f.step_count} steps`}
                        </p>
                      </div>
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
