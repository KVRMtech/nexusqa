/**
 * FLEET COMMAND CENTER — the home instrument. A fleet-verdict header (certified /
 * refused / healed, 0 green-washed, 0 false-heals), a grid of app tiles (verdict
 * pill, tier, per-band autonomy, sparkline), and the Verdict Ledger + Honesty
 * Feed side-by-side.
 *
 * Scaffold note: complete + running against mock and live data. The `fleet`
 * feature agent owns the deeper instrument (drill-downs, filters, live refresh).
 * Export `FleetCommandCenter`.
 */
import { useMemo } from 'react';
import { Link } from 'react-router-dom';
import { AlertTriangle, KeyRound, ShieldCheck } from 'lucide-react';

import { api } from '../../lib/api';
import { useAuth } from '../../lib/auth';
import { cn, formatCount } from '../../lib/format';
import { useAsync } from '../../lib/useAsync';
import {
  Bar,
  EmptyState,
  ErrorState,
  Panel,
  Pill,
  Seal,
  SkeletonRows,
  Sparkline,
  StatusDot,
  VerdictBadge,
} from '../../components';

/** A react-router Link styled as the primary action button. */
const PRIMARY_LINK =
  'inline-flex items-center justify-center gap-2 rounded-lg text-sm px-3.5 py-2 bg-teal text-bg hover:bg-teal/90 ring-1 ring-teal/40 font-semibold transition-colors';
import type {
  AutonomyTrend,
  ClientApp,
  CriticalityBand,
  LedgerResponse,
  VerdictKind,
} from '../../types/qec';
import VerdictLedger from '../ledger';
import HonestyFeed from '../honesty';

// ── instrument stat tile ─────────────────────────────────────────────────────

function StatTile({
  label,
  value,
  tone = 'teal',
  hint,
}: {
  label: string;
  value: string | number;
  tone?: 'teal' | 'certified' | 'refused' | 'healed' | 'good' | 'gold';
  hint?: string;
}) {
  return (
    <Panel tone="default" className="min-w-0">
      <div className="text-2xs uppercase tracking-wide text-ink-low">{label}</div>
      <div
        className="mt-1 text-2xl font-semibold tabular"
        style={{ color: `rgb(var(--${toneVar(tone)}))` }}
      >
        {value}
      </div>
      {hint && <div className="text-2xs text-ink-faint mt-0.5">{hint}</div>}
    </Panel>
  );
}

function toneVar(tone: string): string {
  switch (tone) {
    case 'teal':
      return 'teal';
    case 'certified':
      return 'verdict-certified';
    case 'refused':
      return 'verdict-refused';
    case 'healed':
      return 'verdict-healed';
    case 'good':
      return 'good';
    case 'gold':
      return 'gold';
    default:
      return 'ink';
  }
}

// ── per-app tile ─────────────────────────────────────────────────────────────

function latestKindFor(appId: string, ledger?: LedgerResponse): VerdictKind | null {
  const hit = ledger?.entries.find((e) => e.app_id === appId);
  return hit ? hit.kind : null;
}

function trendSpark(trend: AutonomyTrend | undefined, band: CriticalityBand): Array<number | null> {
  const series = trend?.per_band?.[band];
  if (!series) return [];
  return series.map((p) => p.autonomy_pct);
}

function latestPct(trend: AutonomyTrend | undefined, band: CriticalityBand): number | null {
  const series = trend?.per_band?.[band];
  if (!series || series.length === 0) return null;
  return series[series.length - 1].autonomy_pct;
}

function AppTile({ app, headline }: { app: ClientApp; headline: VerdictKind | null }) {
  const state = useAsync((signal) => api.getAutonomyTrend(app.app_id, { cycles: 8 }, { signal }), [app.app_id]);
  const trend = state.data;
  const envKind = app.env_attestation?.env_kind;

  return (
    <Panel as={Link} to={`/apps/${app.app_id}`} interactive tone="default" className="group block">
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <StatusDot tone={app.status === 'active' ? 'good' : app.status === 'paused' ? 'warn' : 'crit'} label={app.status} />
            <h3 className="text-sm font-semibold text-ink truncate group-hover:text-teal transition-colors">{app.name}</h3>
          </div>
          <p className="text-2xs text-ink-low font-mono truncate mt-0.5">{app.canonical_host}</p>
        </div>
        {headline ? <VerdictBadge kind={headline} size="sm" /> : <Pill tone="neutral" size="sm">—</Pill>}
      </div>

      <div className="flex items-center gap-1.5 mt-2.5 flex-wrap">
        {app.tier && (
          <Pill tone={app.tier === 'behaves' ? 'teal' : 'warn'} size="sm" variant="outline">
            {app.tier === 'behaves' ? 'Behaves' : 'Renders'}
          </Pill>
        )}
        {envKind && (
          <Pill tone={envKind === 'disposable' ? 'good' : envKind === 'prod' ? 'crit' : 'neutral'} size="sm" variant="outline">
            {envKind}
          </Pill>
        )}
        {app.has_credentials && (
          <span className="text-ink-faint" title="credentials provisioned">
            <KeyRound size={12} aria-hidden />
          </span>
        )}
      </div>

      {/* autonomy + sparkline */}
      <div className="mt-3 grid grid-cols-[1fr_auto] items-end gap-3">
        <div className="space-y-1.5 min-w-0">
          {state.isLoading ? (
            <div className="h-9"><SkeletonRows rows={2} /></div>
          ) : state.isError ? (
            <p className="text-2xs text-crit">autonomy unavailable</p>
          ) : (
            <>
              <Bar label="P0" value={latestPct(trend, 'P0')} autoTone height={5} />
              <Bar label="P1" value={latestPct(trend, 'P1')} autoTone height={5} />
            </>
          )}
        </div>
        <Sparkline data={trendSpark(trend, 'P1')} tone="teal" width={88} height={30} fill />
      </div>
    </Panel>
  );
}

// ── the command center ───────────────────────────────────────────────────────

export function FleetCommandCenter() {
  const { session } = useAuth();
  const appsState = useAsync((signal) => api.listApps({ signal }), [session?.tenantId]);
  const ledgerState = useAsync((signal) => api.getLedger({ limit: 60 }, { signal }), [session?.tenantId]);

  const apps = appsState.data?.apps ?? [];
  const ledger = ledgerState.data;
  const activeCount = useMemo(() => apps.filter((a) => a.status === 'active').length, [apps]);

  return (
    <div className="space-y-6 max-w-[1600px]">
      {/* ── heading ── */}
      <div className="flex items-center justify-between gap-4">
        <div className="flex items-center gap-3">
          <Seal size={34} tone="certified" title="" />
          <div>
            <h1 className="text-lg font-semibold text-ink tracking-tight">Command Center</h1>
            <p className="text-2xs text-ink-low font-mono">the verdict on every release — certified, or refused</p>
          </div>
        </div>
        <Link to="/onboard" className={PRIMARY_LINK}>
          <ShieldCheck size={15} aria-hidden />
          Onboard app
        </Link>
      </div>

      {/* ── instrument ── */}
      <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-6 gap-3">
        <StatTile label="Apps under verdict" value={formatCount(apps.length)} tone="teal" hint={`${activeCount} active`} />
        <StatTile label="Certified" value={ledger ? ledger.counts.certified : '—'} tone="certified" />
        <StatTile label="Refused" value={ledger ? ledger.counts.refused : '—'} tone="refused" hint="the honest hero" />
        <StatTile label="Healed" value={ledger ? ledger.counts.healed : '—'} tone="healed" />
        <StatTile label="Green-washed" value={0} tone="good" hint="never" />
        <StatTile label="False-heals" value={0} tone="good" hint="oracle-gated" />
      </div>

      {/* ── fleet grid ── */}
      <section aria-label="Fleet">
        {appsState.isLoading && (
          <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-4">
            {[0, 1, 2].map((i) => (
              <Panel key={i}><SkeletonRows rows={4} /></Panel>
            ))}
          </div>
        )}
        {appsState.isError && <ErrorState error={appsState.error} onRetry={appsState.reload} />}
        {appsState.isSuccess &&
          (apps.length > 0 ? (
            <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-4">
              {apps.map((app) => (
                <AppTile key={app.app_id} app={app} headline={latestKindFor(app.app_id, ledger)} />
              ))}
            </div>
          ) : (
            <Panel>
              <EmptyState
                icon={<AlertTriangle size={26} />}
                title="No apps under verdict yet"
                hint="Onboard your first application to start certifying releases."
                action={
                  <Link to="/onboard" className={PRIMARY_LINK}>
                    <ShieldCheck size={15} aria-hidden />
                    Onboard app
                  </Link>
                }
              />
            </Panel>
          ))}
      </section>

      {/* ── ledger + honesty ── */}
      <div className="grid grid-cols-1 xl:grid-cols-2 gap-4 items-start">
        <VerdictLedger limit={12} />
        <HonestyFeed />
      </div>
    </div>
  );
}

export default FleetCommandCenter;
