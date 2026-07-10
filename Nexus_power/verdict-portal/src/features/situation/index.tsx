/**
 * APP SITUATION — the per-app panel (/apps/:id): the approval queue (the 1%),
 * the coverage scorecard with its P0 possible-deletion gap, the certified
 * invariants (refuse-proof), per-band autonomy, recent cycles, and the
 * app-scoped Verdict Ledger + Honesty Feed.
 *
 * Scaffold note: complete + running. The `situation` feature agent owns the
 * deeper flows (scenario detail, gap adjudication UI, cycle drill-in). Export
 * `AppSituation`.
 */
import { useState } from 'react';
import { Link, useParams } from 'react-router-dom';
import { toast } from 'sonner';
import {
  ArrowLeft,
  FileCheck2,
  GitBranch,
  Layers,
  PlayCircle,
  ScrollText,
  ShieldAlert,
} from 'lucide-react';

import { api, QecApiError } from '../../lib/api';
import { cn, formatCount, humanize, timeAgo } from '../../lib/format';
import { useAsync } from '../../lib/useAsync';
import {
  Bar,
  Button,
  EmptyState,
  ErrorState,
  Gauge,
  Loading,
  Panel,
  Pill,
  SectionHead,
  SkeletonRows,
  StatusDot,
  VerdictBadge,
} from '../../components';
import type { CriticalityBand, ScenarioView } from '../../types/qec';
import VerdictLedger from '../ledger';
import HonestyFeed from '../honesty';

const BAND_TONE: Record<CriticalityBand, 'crit' | 'warn' | 'teal' | 'neutral'> = {
  P0: 'crit',
  P1: 'warn',
  P2: 'teal',
  P3: 'neutral',
};

// ── header ───────────────────────────────────────────────────────────────────

function SituationHeader({ appId }: { appId: string }) {
  const state = useAsync((signal) => api.getApp(appId, { signal }), [appId]);
  const [triggering, setTriggering] = useState(false);

  const runCycle = async () => {
    setTriggering(true);
    try {
      const res = await api.triggerCycle(appId, { mode: 'auto' });
      toast.success(`Cycle started (${res.mode})`, { description: res.cycle_id });
    } catch (err) {
      const e = err as QecApiError;
      toast.error('Could not start cycle', { description: e.message });
    } finally {
      setTriggering(false);
    }
  };

  if (state.isLoading) return <Loading label="Loading app…" />;
  if (state.isError) return <ErrorState error={state.error} onRetry={state.reload} />;
  const app = state.data!;

  return (
    <div className="flex items-start justify-between gap-4">
      <div className="min-w-0">
        <Link to="/" className="inline-flex items-center gap-1.5 text-2xs text-ink-low hover:text-ink mb-2 transition-colors">
          <ArrowLeft size={13} aria-hidden /> Command Center
        </Link>
        <div className="flex items-center gap-2.5">
          <StatusDot tone={app.status === 'active' ? 'good' : app.status === 'paused' ? 'warn' : 'crit'} label={app.status} />
          <h1 className="text-lg font-semibold text-ink tracking-tight truncate">{app.name}</h1>
          {app.tier && (
            <Pill tone={app.tier === 'behaves' ? 'teal' : 'warn'} size="sm" variant="outline">
              {app.tier === 'behaves' ? 'Behaves' : 'Renders'}
            </Pill>
          )}
        </div>
        <p className="text-2xs text-ink-low font-mono mt-1 truncate">{app.base_url}</p>
      </div>
      <Button variant="primary" loading={triggering} onClick={runCycle} icon={<PlayCircle size={15} />}>
        Run cycle
      </Button>
    </div>
  );
}

// ── approval queue (the 1%) ──────────────────────────────────────────────────

function ApprovalQueue({ appId }: { appId: string }) {
  const state = useAsync((signal) => api.listScenarios(appId, { state: 'needs_approval' }, { signal }), [appId]);
  const [signature, setSignature] = useState('');
  const [busy, setBusy] = useState<string | null>(null);

  const approve = async (scn: ScenarioView) => {
    if (!signature.trim()) {
      toast.error('An e-signature (your full name) is required to approve');
      return;
    }
    setBusy(scn.scenario_id);
    try {
      await api.approveScenario(scn.scenario_id, signature.trim());
      toast.success('Scenario certified', { description: scn.name });
      state.reload();
    } catch (err) {
      toast.error('Approval refused', { description: (err as QecApiError).message });
    } finally {
      setBusy(null);
    }
  };

  const rows = state.data?.scenarios ?? [];

  return (
    <Panel tone="elevated">
      <SectionHead
        title="Approval queue"
        subtitle="the 1% — NEW / CHANGED scenarios awaiting a human sign-off"
        icon={<ScrollText size={16} className="text-gold" />}
        right={state.data && <Pill tone={rows.length ? 'warn' : 'good'} size="sm">{rows.length} pending</Pill>}
      />

      <div className="mt-3">
        {state.isLoading && <SkeletonRows rows={3} />}
        {state.isError && <ErrorState error={state.error} onRetry={state.reload} />}
        {state.isSuccess &&
          (rows.length > 0 ? (
            <>
              <div className="mb-3">
                <label htmlFor="sig" className="block text-2xs text-ink-low mb-1">
                  E-signature (typed full name) — required to certify
                </label>
                <input
                  id="sig"
                  value={signature}
                  onChange={(e) => setSignature(e.target.value)}
                  placeholder="e.g. Dana Whitfield, Chief Actuary"
                  className="w-full rounded-lg bg-inset text-ink text-xs ring-1 ring-line focus-visible:ring-teal/60 px-3 py-2"
                />
              </div>
              <ul className="divide-y divide-line">
                {rows.map((scn) => (
                  <li key={scn.scenario_id} className="py-2.5 flex items-center gap-3">
                    <Pill tone={BAND_TONE[scn.criticality_band]} size="sm">{scn.criticality_band}</Pill>
                    <div className="min-w-0 flex-1">
                      <p className="text-sm text-ink truncate">{scn.name}</p>
                      <p className="text-2xs text-ink-low">
                        {humanize(scn.diff_state)} · {humanize(scn.review_state)} · {scn.tier}
                      </p>
                    </div>
                    <Button
                      size="sm"
                      variant="secondary"
                      loading={busy === scn.scenario_id}
                      disabled={!signature.trim()}
                      onClick={() => approve(scn)}
                    >
                      Certify
                    </Button>
                  </li>
                ))}
              </ul>
            </>
          ) : (
            <EmptyState
              title="Queue clear"
              hint="Every NEW / CHANGED scenario is signed off — UNCHANGED scenarios auto-carry their approval (zero touch)."
            />
          ))}
      </div>
    </Panel>
  );
}

// ── coverage scorecard ───────────────────────────────────────────────────────

function CoverageCard({ appId }: { appId: string }) {
  const state = useAsync((signal) => api.getCoverage(appId, { signal }), [appId]);
  const cov = state.data;

  return (
    <Panel tone="elevated">
      <SectionHead
        title="Coverage"
        subtitle="enumerable atoms measured against certified invariants"
        icon={<Layers size={16} className="text-teal" />}
        right={
          cov && (
            <Pill tone={cov.verdict === 'ok' ? 'good' : 'crit'} size="sm" variant="soft">
              {cov.verdict === 'ok' ? 'all green' : 'blocked · P0'}
            </Pill>
          )
        }
      />
      <div className="mt-3">
        {state.isLoading && <SkeletonRows rows={3} />}
        {state.isError && <ErrorState error={state.error} onRetry={state.reload} />}
        {state.isSuccess && cov && (
          <>
            <div className="grid grid-cols-3 gap-3 text-center">
              <div>
                <div className="text-xl font-semibold text-ink tabular">{formatCount(cov.atoms.count)}</div>
                <div className="text-2xs text-ink-low">atoms</div>
              </div>
              <div>
                <div className="text-xl font-semibold text-ink tabular">{formatCount(cov.invariants.total)}</div>
                <div className="text-2xs text-ink-low">invariants</div>
              </div>
              <div>
                <div className={cn('text-xl font-semibold tabular', cov.blocking_gaps ? 'text-crit' : 'text-good')}>
                  {formatCount(cov.blocking_gaps)}
                </div>
                <div className="text-2xs text-ink-low">blocking gaps</div>
              </div>
            </div>

            {cov.gaps.length > 0 && (
              <ul className="mt-3 space-y-2">
                {cov.gaps.map((gap) => (
                  <li
                    key={gap.gap_id}
                    className={cn(
                      'rounded-lg px-3 py-2 ring-1',
                      gap.blocking ? 'ring-crit/25 bg-crit/[0.06]' : 'ring-line bg-inset',
                    )}
                  >
                    <div className="flex items-center gap-2">
                      <ShieldAlert size={13} className={gap.blocking ? 'text-crit' : 'text-ink-low'} aria-hidden />
                      <span className="text-xs font-semibold text-ink">{humanize(gap.kind)}</span>
                      <Pill tone={gap.band === 'P0' ? 'crit' : 'neutral'} size="sm">{gap.band}</Pill>
                      <Pill tone="neutral" size="sm">{humanize(gap.status)}</Pill>
                    </div>
                    {typeof gap.detail.reason === 'string' && (
                      <p className="text-2xs text-ink-low mt-1 leading-snug">{gap.detail.reason}</p>
                    )}
                  </li>
                ))}
              </ul>
            )}
          </>
        )}
      </div>
    </Panel>
  );
}

// ── certified invariants ─────────────────────────────────────────────────────

function InvariantsCard({ appId }: { appId: string }) {
  const state = useAsync((signal) => api.listInvariants(appId, { signal }), [appId]);
  const rows = state.data?.invariants ?? [];

  return (
    <Panel tone="elevated">
      <SectionHead
        title="Certified invariants"
        subtitle="the non-enumerable half — executed + e-signed, never auto-discovered"
        icon={<FileCheck2 size={16} className="text-gold" />}
        right={state.data && <Pill tone="gold" size="sm">{rows.length}</Pill>}
      />
      <div className="mt-3">
        {state.isLoading && <SkeletonRows rows={3} />}
        {state.isError && <ErrorState error={state.error} onRetry={state.reload} />}
        {state.isSuccess &&
          (rows.length > 0 ? (
            <ul className="space-y-2">
              {rows.map((inv) => (
                <li key={inv.invariant_id} className="rounded-lg bg-inset ring-1 ring-line px-3 py-2">
                  <div className="flex items-start gap-2">
                    <Pill tone={inv.criticality_band === 'P0' ? 'crit' : 'warn'} size="sm">{inv.criticality_band}</Pill>
                    <p className="text-xs text-ink leading-snug flex-1">{inv.statement}</p>
                  </div>
                  <p className="text-2xs text-ink-faint mt-1 font-mono">✍ {inv.signature}</p>
                </li>
              ))}
            </ul>
          ) : (
            <EmptyState title="No certified invariants" hint="Author + e-sign the P0 truths this app must never violate." />
          ))}
      </div>
    </Panel>
  );
}

// ── autonomy ─────────────────────────────────────────────────────────────────

function AutonomyCard({ appId }: { appId: string }) {
  const state = useAsync((signal) => api.getAutonomy(appId, {}, { signal }), [appId]);
  const data = state.data;
  const bands: CriticalityBand[] = ['P0', 'P1', 'P2', 'P3'];
  const p0 = data?.by_band?.P0?.autonomy_pct ?? null;

  return (
    <Panel tone="elevated">
      <SectionHead
        title="Autonomy"
        subtitle="per band — deliberately never averaged"
        icon={<GitBranch size={16} className="text-teal" />}
      />
      <div className="mt-3">
        {state.isLoading && <SkeletonRows rows={3} />}
        {state.isError && <ErrorState error={state.error} onRetry={state.reload} />}
        {state.isSuccess && data && (
          <div className="flex items-center gap-5">
            <Gauge value={p0} autoTone size={104} label="P0 autonomy" />
            <div className="flex-1 space-y-2.5 min-w-0">
              {bands.map((b) => {
                const band = data.by_band?.[b];
                if (!band) return null;
                return (
                  <Bar
                    key={b}
                    label={`${b} · ${band.human_touches}/${band.governed_scenarios} touched`}
                    value={band.autonomy_pct}
                    autoTone
                  />
                );
              })}
            </div>
          </div>
        )}
      </div>
    </Panel>
  );
}

// ── cycles ───────────────────────────────────────────────────────────────────

function CyclesCard({ appId }: { appId: string }) {
  const state = useAsync((signal) => api.listCycles(appId, { limit: 8 }, { signal }), [appId]);
  const rows = state.data?.cycles ?? [];

  return (
    <Panel tone="elevated">
      <SectionHead title="Recent cycles" subtitle="incremental regression runs" icon={<PlayCircle size={16} className="text-teal" />} />
      <div className="mt-3">
        {state.isLoading && <SkeletonRows rows={3} />}
        {state.isError && <ErrorState error={state.error} onRetry={state.reload} />}
        {state.isSuccess &&
          (rows.length > 0 ? (
            <ul className="divide-y divide-line">
              {rows.map((c) => {
                const tone = c.state === 'done' ? 'good' : c.state === 'budget_stopped' || c.state === 'failed' ? 'crit' : 'warn';
                return (
                  <li key={c.cycle_id} className="py-2 flex items-center gap-3">
                    <StatusDot tone={tone} pulse={!c.terminal} label={c.state} />
                    <div className="min-w-0 flex-1">
                      <p className="text-xs text-ink">
                        {humanize(c.state)} · <span className="text-ink-low">{humanize(c.trigger)}</span>
                      </p>
                      <p className="text-2xs text-ink-faint tabular">
                        {c.selected_count} selected · {c.carried_count} carried
                      </p>
                    </div>
                    {c.possible_deletion && <Pill tone="crit" size="sm">deletion?</Pill>}
                    <time className="text-2xs text-ink-faint shrink-0" dateTime={c.created_at ?? undefined}>
                      {timeAgo(c.created_at)}
                    </time>
                  </li>
                );
              })}
            </ul>
          ) : (
            <EmptyState title="No cycles yet" hint="Run a cycle to begin regression coverage." />
          ))}
      </div>
    </Panel>
  );
}

// ── the situation ────────────────────────────────────────────────────────────

export function AppSituation() {
  const { id } = useParams<{ id: string }>();
  if (!id) return <ErrorState title="No app selected" error="Missing app id in the route." />;

  return (
    <div className="space-y-6 max-w-[1600px]">
      <SituationHeader appId={id} />
      <div className="grid grid-cols-1 xl:grid-cols-2 gap-4 items-start">
        <div className="space-y-4">
          <ApprovalQueue appId={id} />
          <CoverageCard appId={id} />
          <InvariantsCard appId={id} />
        </div>
        <div className="space-y-4">
          <AutonomyCard appId={id} />
          <CyclesCard appId={id} />
          <VerdictLedger appId={id} title="App verdict ledger" limit={20} />
          <HonestyFeed appId={id} />
        </div>
      </div>
    </div>
  );
}

export default AppSituation;
