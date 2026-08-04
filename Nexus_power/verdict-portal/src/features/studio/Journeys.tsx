/**
 * JOURNEYS — the Journey Graph surface (Release C3).
 *
 * Answers the only question a business user actually asks — "did you get all
 * the way through Apply?" — PER PATH, per identity, per env, per time, with
 * every branch nobody walked shown as a first-class object.
 *
 * Honesty rules this component encodes (mirroring the API's):
 *   • counts, never percentages — no progress bars over uncounted spaces;
 *   • "discovered, not walked" is a visible chip, never an absence;
 *   • terminal reasons render VERBATIM; `oracle_unavailable` is labelled a
 *     platform failure and never reads as covered;
 *   • `branch_coverage` appears only when the server EARNED it (every
 *     enumerated option walked or attributably blocked);
 *   • path products above the server cap say "not enumerated" — no claim.
 */
import { useState } from 'react';
import { toast } from 'sonner';
import {
  AlertTriangle, CheckCircle2, ChevronDown, ChevronRight, CircleDashed,
  Footprints, GitBranch, Pencil, RefreshCw, Route as RouteIcon, ShieldCheck,
} from 'lucide-react';

import {
  api,
  type JourneyBranch,
  type JourneyDetail,
  type JourneySummary,
} from '../../lib/api';
import { useAsync } from '../../lib/useAsync';
import { timeAgo } from '../../lib/format';
import { Button, EmptyState, ErrorState, Pill, SkeletonRows } from '../../components';

const TERMINAL_COPY: Record<string, { label: string; tone: 'teal' | 'warn' | 'crit' | 'neutral' }> = {
  submit_boundary: { label: 'Walked to the submit boundary', tone: 'teal' },
  no_advance: { label: 'Walked to its natural end', tone: 'teal' },
  budget_exhausted: { label: 'Stopped: budget exhausted — more funnel existed', tone: 'warn' },
  loop: { label: 'Stopped: the app looped back to a seen step', tone: 'warn' },
  cancelled: { label: 'Stopped: the crawl was cancelled', tone: 'warn' },
  oracle_unavailable: {
    label: 'Stopped: platform advance service unavailable — NOT proven complete (not the app\'s fault)',
    tone: 'crit',
  },
};

const BRANCH_TONE: Record<JourneyBranch['status'], 'teal' | 'warn' | 'neutral' | 'crit'> = {
  walked: 'teal',
  discovered: 'warn',
  planned: 'neutral',
  blocked: 'crit',
};

const BRANCH_LABEL: Record<JourneyBranch['status'], string> = {
  walked: 'walked',
  discovered: 'discovered, not walked',
  planned: 'walk planned',
  blocked: 'blocked',
};

function Terminal({ terminal }: { terminal: string }) {
  const copy = TERMINAL_COPY[terminal] ?? { label: terminal, tone: 'neutral' as const };
  return (
    <span className="inline-flex items-center gap-1.5">
      <Pill tone={copy.tone} size="sm" variant="outline">{copy.label}</Pill>
      <span className="text-2xs text-ink-low font-mono">{terminal}</span>
    </span>
  );
}

function BranchChips({ s }: { s: JourneySummary }) {
  const b = s.branches;
  return (
    <span className="inline-flex flex-wrap items-center gap-1.5">
      {b.walked > 0 && <Pill tone="teal" size="sm">{b.walked} walked</Pill>}
      {b.discovered > 0 && (
        <Pill tone="warn" size="sm">{b.discovered} discovered, not walked</Pill>
      )}
      {b.planned > 0 && <Pill tone="neutral" size="sm">{b.planned} planned</Pill>}
      {b.blocked > 0 && <Pill tone="crit" size="sm">{b.blocked} blocked</Pill>}
      {b.walked + b.discovered + b.planned + b.blocked === 0 && (
        <span className="text-2xs text-ink-low">no decision points found yet</span>
      )}
    </span>
  );
}

function JourneyDetailView({ appId, journeyId }: { appId: string; journeyId: string }) {
  const state = useAsync(
    (signal) => api.getJourney(appId, journeyId, { signal }),
    [appId, journeyId],
  );
  if (state.isLoading) return <SkeletonRows rows={3} />;
  if (state.isError) return <ErrorState error={state.error} onRetry={state.reload} />;
  const d = state.data as JourneyDetail;

  const nodeTitle = (fp: string) =>
    d.nodes.find((n) => n.fingerprint === fp)?.title || fp.slice(0, 10);
  const branchesByControl = new Map<string, JourneyBranch[]>();
  for (const b of d.branch_list) {
    const key = `${b.node_fp}::${b.control_signature}`;
    branchesByControl.set(key, [...(branchesByControl.get(key) ?? []), b]);
  }

  return (
    <div className="space-y-4 pt-3">
      {/* enumeration honesty block */}
      <div className="text-2xs text-ink-low">
        {d.path_enumeration.enumerated ? (
          <>Paths through this journey's decision points: exactly{' '}
            <span className="font-semibold text-ink">{d.path_enumeration.path_product}</span>{' '}
            across {d.path_enumeration.decision_controls} decision control
            {d.path_enumeration.decision_controls === 1 ? '' : 's'}.</>
        ) : (
          <>Path space {d.path_enumeration.note} across{' '}
            {d.path_enumeration.decision_controls} decision controls — coverage is
            stated per option, never as a percentage of an uncounted space.</>
        )}
      </div>

      {/* walked paths — the per-path claims */}
      <div>
        <div className="text-xs font-semibold text-ink mb-1.5 inline-flex items-center gap-1.5">
          <Footprints size={13} aria-hidden /> Walked paths ({d.traversals.length})
        </div>
        <div className="space-y-2">
          {d.traversals.map((t) => (
            <div key={t.traversal_id} className="rounded-lg bg-inset/60 px-3 py-2 text-2xs">
              <div className="flex flex-wrap items-center gap-2">
                {t.completed
                  ? <CheckCircle2 size={13} className="text-teal" aria-hidden />
                  : <AlertTriangle size={13} className="text-warn" aria-hidden />}
                <Terminal terminal={t.terminal} />
                <span className="text-ink-low">{t.path_fps.length} steps</span>
                <span className="text-ink-low">{timeAgo(t.walked_at)}</span>
                {t.identity_ref && (
                  <span className="font-mono text-ink-low">as {t.identity_ref}</span>
                )}
                {t.env_ref && <span className="text-ink-low">on {t.env_ref}</span>}
                {t.pre_hardening && (
                  <Pill tone="neutral" size="sm" variant="outline">pre-hardening evidence</Pill>
                )}
              </div>
              <div className="mt-1 text-ink-low truncate">
                {t.path_fps.map(nodeTitle).join(' → ')}
              </div>
              {t.outcome_values.length > 0 && (
                <div className="mt-1 flex flex-wrap gap-1.5">
                  {t.outcome_values.map((o, i) => (
                    <Pill key={i} tone="neutral" size="sm" variant="outline">
                      {o.label}: {o.value}
                    </Pill>
                  ))}
                </div>
              )}
            </div>
          ))}
          {d.traversals.length === 0 && (
            <div className="text-2xs text-ink-low">No walked paths recorded yet.</div>
          )}
        </div>
      </div>

      {/* branches — walked AND not, grouped per decision control */}
      <div>
        <div className="text-xs font-semibold text-ink mb-1.5 inline-flex items-center gap-1.5">
          <GitBranch size={13} aria-hidden /> Decision points ({branchesByControl.size})
        </div>
        <div className="space-y-2">
          {[...branchesByControl.entries()].map(([key, list]) => (
            <div key={key} className="rounded-lg bg-inset/60 px-3 py-2 text-2xs">
              <div className="text-ink font-medium">
                {list[0].control_label || 'unnamed control'}
                <span className="text-ink-low font-normal"> on “{nodeTitle(list[0].node_fp)}”</span>
              </div>
              <div className="mt-1 flex flex-wrap gap-1.5">
                {list.map((b) => (
                  <span key={b.branch_id} className="inline-flex items-center gap-1">
                    <Pill tone={BRANCH_TONE[b.status]} size="sm">
                      {b.option_label} — {BRANCH_LABEL[b.status]}
                    </Pill>
                  </span>
                ))}
              </div>
              {list.some((b) => b.blocked_reason) && (
                <div className="mt-1 text-crit">
                  {list.filter((b) => b.blocked_reason).map((b) => (
                    <div key={b.branch_id}>{b.option_label}: {b.blocked_reason}</div>
                  ))}
                </div>
              )}
            </div>
          ))}
          {branchesByControl.size === 0 && (
            <div className="text-2xs text-ink-low">
              No enumerable decision points discovered on this journey yet.
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

function RenameForm({
  appId, journey, onDone,
}: { appId: string; journey: JourneySummary; onDone: () => void }) {
  const [name, setName] = useState(journey.business_name);
  const [busy, setBusy] = useState(false);
  return (
    <form
      className="inline-flex items-center gap-1.5"
      onSubmit={async (e) => {
        e.preventDefault();
        if (!name.trim()) return;
        setBusy(true);
        try {
          await api.renameJourney(appId, journey.journey_id, { business_name: name.trim() });
          toast.success('Journey renamed — the agent will never overwrite it.');
          onDone();
        } catch (err) {
          toast.error(err instanceof Error ? err.message : 'Rename failed');
        } finally {
          setBusy(false);
        }
      }}
    >
      <input
        value={name}
        onChange={(e) => setName(e.target.value)}
        maxLength={60}
        className="rounded-md bg-inset px-2 py-1 text-xs text-ink ring-1 ring-line w-64"
        aria-label="Journey name"
      />
      <Button type="submit" size="sm" disabled={busy}>Save</Button>
    </form>
  );
}

export default function Journeys({ appId }: { appId: string }) {
  const state = useAsync((signal) => api.listJourneys(appId, { signal }), [appId]);
  const [open, setOpen] = useState<string | null>(null);
  const [renaming, setRenaming] = useState<string | null>(null);
  const [busyAction, setBusyAction] = useState<string | null>(null);

  if (state.isLoading) return <SkeletonRows rows={4} />;
  if (state.isError) return <ErrorState error={state.error} onRetry={state.reload} />;
  const data = state.data!;

  const act = async (label: string, run: () => Promise<unknown>) => {
    setBusyAction(label);
    try {
      await run();
      state.reload();
    } catch (err) {
      toast.error(err instanceof Error ? err.message : `${label} failed`);
    } finally {
      setBusyAction(null);
    }
  };

  return (
    <div className="rounded-2xl bg-panel text-ink ring-1 ring-line shadow-card p-5 space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="inline-flex items-center gap-2">
          <RouteIcon size={16} className="text-teal" aria-hidden />
          <span className="text-sm font-semibold">
            {data.journeys_found} business journey{data.journeys_found === 1 ? '' : 's'}
          </span>
          {data.branch_coverage && (
            <Pill tone="teal" size="sm">
              <ShieldCheck size={12} aria-hidden /> branch coverage earned
            </Pill>
          )}
        </div>
        <div className="inline-flex items-center gap-2">
          <Button
            size="sm" variant="secondary" disabled={busyAction !== null}
            onClick={() => act('Re-fold', async () => {
              const r = await api.refoldJourneys(appId);
              toast.success(`Re-folded ${r.explorations_folded} completed crawl(s) into the graph.`);
            })}
          >
            <RefreshCw size={13} aria-hidden /> Re-fold history
          </Button>
          <Button
            size="sm" variant="secondary" disabled={busyAction !== null}
            onClick={() => act('Branch walks', async () => {
              const r = await api.walkBranches(appId);
              toast.success(`${r.plans} branch walk plan(s) dispatched.`);
            })}
          >
            <GitBranch size={13} aria-hidden /> Walk unexplored branches
          </Button>
        </div>
      </div>

      {data.journeys.length === 0 ? (
        <EmptyState
          title="No journeys folded yet"
          hint="Run an End-to-end crawl (or Re-fold history) and the walked funnels appear here as named business journeys."
        />
      ) : (
        <div className="space-y-3">
          {data.journeys.map((j) => (
            <div key={j.journey_id} className="rounded-xl ring-1 ring-line bg-panel">
              <button
                type="button"
                onClick={() => setOpen(open === j.journey_id ? null : j.journey_id)}
                className="w-full text-left px-4 py-3"
                aria-expanded={open === j.journey_id}
              >
                <div className="flex flex-wrap items-center gap-2">
                  {open === j.journey_id
                    ? <ChevronDown size={14} aria-hidden />
                    : <ChevronRight size={14} aria-hidden />}
                  <span className="text-sm font-semibold text-ink">
                    {j.business_name || j.entry_title || 'Unnamed journey'}
                  </span>
                  <Pill tone={j.name_source === 'operator' ? 'teal' : 'neutral'} size="sm" variant="outline">
                    {j.name_source === 'operator' ? 'named by you'
                      : j.name_source === 'agent' ? 'agent-proposed' : 'auto title'}
                  </Pill>
                  {j.branch_coverage
                    ? <Pill tone="teal" size="sm"><ShieldCheck size={12} aria-hidden /> branch coverage earned</Pill>
                    : <Pill tone="neutral" size="sm" variant="outline"><CircleDashed size={12} aria-hidden /> branches remain</Pill>}
                </div>
                {j.description && (
                  <div className="mt-1 text-2xs text-ink-low">{j.description}</div>
                )}
                <div className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1.5 text-2xs text-ink-low">
                  <span>
                    <span className="font-semibold text-ink">{j.paths_completed}</span> of{' '}
                    <span className="font-semibold text-ink">{j.paths_walked}</span> walked path
                    {j.paths_walked === 1 ? '' : 's'} completed
                  </span>
                  <span>{j.deepest_steps} steps at deepest</span>
                  {j.last_proven_at && <span>last proven {timeAgo(j.last_proven_at)}</span>}
                  <BranchChips s={j} />
                </div>
              </button>
              <div className="px-4 pb-3">
                {renaming === j.journey_id ? (
                  <RenameForm
                    appId={appId} journey={j}
                    onDone={() => { setRenaming(null); state.reload(); }}
                  />
                ) : (
                  <button
                    type="button"
                    onClick={() => setRenaming(j.journey_id)}
                    className="inline-flex items-center gap-1 text-2xs text-ink-low hover:text-ink"
                  >
                    <Pencil size={11} aria-hidden /> Rename
                  </button>
                )}
                {open === j.journey_id && (
                  <JourneyDetailView appId={appId} journeyId={j.journey_id} />
                )}
              </div>
            </div>
          ))}
        </div>
      )}

      <p className="text-2xs text-ink-low">
        One row per path actually walked — nothing here is inferred. A journey
        earns “branch coverage” only when every enumerated option is walked or
        attributably blocked; a single “discovered, not walked” branch keeps the
        claim off.
      </p>
    </div>
  );
}
