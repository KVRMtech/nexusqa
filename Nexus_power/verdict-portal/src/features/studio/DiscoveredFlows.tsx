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

interface FailureAttribution {
  attribution: string;
  cause: string;
  blame: string;
  detail: string;
  /** Attribution Engine v1 category (P1.4): product_script_defect |
   *  application_defect | environment | configuration | test_data | unknown */
  category?: string;
  tier?: string;
}

interface ScriptRun {
  runs?: Array<{ status: string; at: string | null }>;
  is_flaky?: boolean;
  flake_rate_pct?: number;
  consecutive_failures?: number;
  last_run_status?: string;
  last_run_at?: string | null;
  /** P1.4 — ingest-time failure attribution: present only when the failure
   *  cause is PROVABLE from evidence; the UI never blames the client's
   *  application without it. */
  failure_attribution?: FailureAttribution | null;
  /** P0.2 — soft-oracle misses on the latest client run (non-fatal
   *  best-effort hints under the proven-oracle policy; visible, never silent). */
  soft_oracle_misses?: number;
  /** P0.3 — latest CERTIFICATION run (baseline self-proof; kept out of the
   *  client stats above). */
  certification?: {
    status: 'certified' | 'failed';
    at: string | null;
    attribution?: FailureAttribution | null;
  } | null;
  /** P0.3 — server truth: quarantined from client runs until re-certified. */
  quarantined?: boolean;
}

interface FlowsData {
  cases: CaseRow[];
  total: number;
  scripts: Record<string, ScriptRun>;
  /** P2.8 — the north-star quality metric (null when the endpoint is absent). */
  quality?: {
    client_visible_product_faults: number;
    caught_in_certification: number;
    window_days: number;
  } | null;
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
 *
 *  P0.1 (neutral-by-default blame): a failure NEVER implicitly points at the
 *  client's application. With evidence (P1.4 attribution) the reason names the
 *  proven cause; without it, the reason says "cause under analysis".
 *  P0.3: quarantined flows say the product is repairing them; certification
 *  state is surfaced for flows the client has not run yet.
 *  Exported for the never-green-wash unit test. */
export function classify(script: ScriptRun | undefined): { status: FlowStatus; reason: string } {
  const cert = script?.certification ?? null;
  const certWhen = cert?.at ? ` · ${timeAgo(cert.at)}` : '';

  // P0.3 — quarantined: failed certification for a product/unproven cause.
  if (script?.quarantined) {
    const cat = cert?.attribution?.category;
    const cause = cat === 'product_script_defect'
      ? `a product-side cause (${cert?.attribution?.cause ?? 'script defect'})`
      : 'a not-yet-attributed cause (under diagnosis)';
    return {
      status: 'attention',
      reason: `Quarantined — failed its certification run on the baseline for ${cause}${certWhen}. ` +
        'The product is repairing it; it returns automatically once re-certified. ' +
        'Your application is NOT implicated.',
    };
  }

  const hasRunEvidence = !!script && ((script.runs?.length ?? 0) > 0 || Boolean(script.last_run_status));
  if (!hasRunEvidence) {
    // No CLIENT runs yet — surface the certification state honestly.
    if (cert?.status === 'certified') {
      return {
        status: 'candidate',
        reason: `Certified — proved itself end-to-end on the baseline${certWhen}. Ready for its first client run.`,
      };
    }
    if (cert?.status === 'failed') {
      const cat = cert.attribution?.category;
      if (cat === 'application_defect') {
        return {
          status: 'attention',
          reason: `Certification found a grounded application regression on the baseline${certWhen} — ` +
            'review the run evidence (this is a real signal, not a script problem).',
        };
      }
      return {
        status: 'candidate',
        reason: `Certification was blocked by ${cat === 'environment' ? 'an environment outage' : 'a configuration issue'}${certWhen} — ` +
          'fix it and regenerate to re-certify. No verdict on the application was made.',
      };
    }
    return { status: 'candidate', reason: 'Discovered + compiled — never executed end-to-end.' };
  }
  const last = String(script.last_run_status ?? '').toLowerCase();
  const passed = PASS_STATES.has(last);
  const flaky = Boolean(script.is_flaky);
  const consec = script.consecutive_failures ?? 0;
  const when = script.last_run_at ? ` · ${timeAgo(script.last_run_at)}` : '';
  const soft = script.soft_oracle_misses ?? 0;
  const softNote = soft > 0 ? ` ${soft} soft oracle hint${soft === 1 ? '' : 's'} recorded (non-fatal).` : '';

  if (passed && !flaky && consec === 0) {
    return { status: 'proven', reason: `Passed end-to-end${when}.${softNote}` };
  }
  const bits: string[] = [];
  if (!passed) bits.push(`last run ${script.last_run_status || 'did not pass'}`);
  if (flaky) bits.push(`flaky (${Math.round(script.flake_rate_pct ?? 0)}%)`);
  if (consec > 0) bits.push(`${consec} consecutive failure${consec === 1 ? '' : 's'}`);

  // P1.4 — evidence-based blame, category by category. Without evidence the
  // wording stays NEUTRAL — never implicit application blame.
  const attr = script.failure_attribution;
  if (!passed && attr) {
    const cat = attr.category ??
      ((attr.blame === 'product' || attr.blame === 'product_probable') ? 'product_script_defect' : '');
    if (cat === 'product_script_defect') {
      const qualifier = attr.blame === 'product' ? 'a product-side script defect' : 'a probable product-side script defect';
      return {
        status: 'attention',
        reason: `Failed on ${qualifier} (${attr.cause}) — not an application failure. ${bits.join(', ')}${when}.${softNote}`,
      };
    }
    if (cat === 'environment') {
      return {
        status: 'attention',
        reason: `Failed — the target environment was unreachable (${attr.cause}). ` +
          `Not an application failure. ${bits.join(', ')}${when}.`,
      };
    }
    if (cat === 'configuration') {
      return {
        status: 'attention',
        reason: `Failed — test configuration blocked the run (${attr.cause}, e.g. auth/session). ` +
          `Not an application failure. ${bits.join(', ')}${when}.`,
      };
    }
    if (cat === 'application_defect') {
      return {
        status: 'attention',
        reason: `Failed — evidence points at an application change (${attr.cause}): a grounded oracle broke. ` +
          `${bits.join(', ')}${when}.${softNote}`,
      };
    }
    // unknown / test_data → fall through to the neutral wording below.
  }
  if (!passed) {
    return {
      status: 'attention',
      reason: `Failed — cause under analysis (not yet attributed to the application, the product, or the environment). ` +
        `${bits.join(', ')}${when}.${softNote}`,
    };
  }
  return { status: 'attention', reason: `Ran but not clean — ${bits.join(', ')}${when}.${softNote}` };
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
    const [list, summary, quality] = await Promise.all([
      studioApi.listTestFactoryCases(artifactId, 1, 200, 'active'),
      studioApi.getRunsSummary(artifactId, 10).catch(() => ({ scripts: {} })),
      studioApi.getProductFaults(artifactId).catch(() => null),
    ]);
    return {
      cases: (list?.items ?? []) as CaseRow[],
      total: Number(list?.total ?? (list?.items?.length ?? 0)),
      scripts: (summary?.scripts ?? {}) as Record<string, ScriptRun>,
      quality: quality as FlowsData['quality'],
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

      {/* P2.8 — honest-quality strip: the product grades ITSELF in front of the
          client. Client-visible product faults target ZERO; certification
          catches show the gate doing its job before any client run. */}
      {state.isSuccess && state.data?.quality && (
        <div className="mt-3 flex flex-wrap items-center gap-x-4 gap-y-1 rounded-lg bg-inset ring-1 ring-line px-3 py-2">
          <span className="text-2xs font-semibold text-ink-mid">
            Product honesty ({state.data.quality.window_days}d):
          </span>
          <span className={`text-2xs font-mono ${state.data.quality.client_visible_product_faults > 0 ? 'text-crit' : 'text-good'}`}>
            {state.data.quality.client_visible_product_faults} client-visible product fault{state.data.quality.client_visible_product_faults === 1 ? '' : 's'}
          </span>
          <span className="text-2xs font-mono text-ink-low">
            {state.data.quality.caught_in_certification} caught by certification before any client run
          </span>
        </div>
      )}

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
