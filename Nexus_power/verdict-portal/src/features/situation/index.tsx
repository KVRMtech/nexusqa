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
import { useEffect, useState } from 'react';
import { Link, useNavigate, useParams } from 'react-router-dom';
import { toast } from 'sonner';
import {
  AlertTriangle,
  ArrowLeft,
  CheckCircle2,
  FileCheck2,
  FlaskConical,
  GitBranch,
  Info,
  KeyRound,
  Layers,
  PlayCircle,
  Radar,
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
import type { AppCrawlStatus, CriticalityBand, CrawlDiagnosis, ExplorationCoverage, ScenarioView } from '../../types/qec';
import VerdictLedger from '../ledger';
import HonestyFeed from '../honesty';
import SeedManifestPanel from './SeedManifestPanel';
import CoveragePanel from './CoveragePanel';

const BAND_TONE: Record<CriticalityBand, 'crit' | 'warn' | 'teal' | 'neutral'> = {
  P0: 'crit',
  P1: 'warn',
  P2: 'teal',
  P3: 'neutral',
};

// ── header ───────────────────────────────────────────────────────────────────

/**
 * Persistent, typed crawl-diagnosis card (Phase 0 — legible failure). Renders the
 * durable "what happened + what to do" the server computed, so a reloaded failed/
 * empty/seed-blocked crawl always states its reason instead of a blank Studio. Shown
 * only for terminal states that warrant attention — a clean `COMPLETED_OK`, an
 * in-progress crawl, or a never-crawled app render nothing here.
 */
function CrawlDiagnosisCard({ crawl }: { crawl?: AppCrawlStatus }) {
  const d: CrawlDiagnosis | undefined = crawl?.diagnosis;
  if (!d) return null;
  // Only surface terminal states the client should act on / notice.
  const HIDE = new Set(['COMPLETED_OK', 'RUNNING', 'QUEUED', 'NONE']);
  if (HIDE.has(d.code)) return null;

  const tone =
    d.severity === 'action'
      ? { box: 'border-amber-500/40 bg-amber-500/10', icon: 'text-amber-500' }
      : d.severity === 'ok'
        ? { box: 'border-teal-500/30 bg-teal-500/10', icon: 'text-teal-500' }
        : { box: 'border-rose-500/40 bg-rose-500/10', icon: 'text-rose-500' };
  const Icon =
    d.code === 'LOGIN_FAILED' ? KeyRound
      : d.severity === 'ok' ? CheckCircle2
        : d.severity === 'action' ? Info
          : AlertTriangle;

  return (
    <div className={cn('flex items-start gap-2.5 rounded-lg border px-3.5 py-2.5', tone.box)}>
      <Icon size={15} className={cn('mt-0.5 shrink-0', tone.icon)} aria-hidden />
      <div className="min-w-0">
        <p className="text-xs font-semibold text-ink">{d.title}</p>
        <p className="text-2xs text-ink-low mt-0.5">{d.human}</p>
        {d.remediation && (
          <p className="text-2xs text-ink mt-1 font-medium">{d.remediation}</p>
        )}
        {d.fields.length > 0 && (
          <div className="mt-1.5 flex flex-wrap gap-1">
            {d.fields.map((f) => (
              <span key={f} className="rounded bg-ink/5 px-1.5 py-0.5 text-2xs font-mono text-ink-low">
                {f}
              </span>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

function SituationHeader({ appId }: { appId: string }) {
  const state = useAsync((signal) => api.getApp(appId, { signal }), [appId]);
  const navigate = useNavigate();
  const [triggering, setTriggering] = useState(false);
  const [crawling, setCrawling] = useState(false);

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

  // Dispatch a live crawl, then poll to the terminal status so the operator sees
  // an honest result (pages/actions captured, or the refusal reason) and the
  // header reloads to pick up the freshly-minted latest_artifact_id.
  const crawl = async () => {
    setCrawling(true);
    try {
      const res = await api.triggerExploration(appId);
      toast.info('Crawl dispatched — exploring the app…', { description: res.crawl_id });
      let terminal: Awaited<ReturnType<typeof api.getExploration>> | null = null;
      // Poll up to ~6 min (covers the bounded first-pass crawl's 5-min ceiling); the
      // on-load crawl-status effect below is the durable signal if the operator
      // navigates away or reloads, so this loop is just the same-session convenience.
      for (let i = 0; i < 90; i += 1) {
        await new Promise((r) => setTimeout(r, 4000));
        const exp = await api.getExploration(res.exploration_id);
        if (exp.status === 'completed' || exp.status === 'failed' || exp.status === 'refused') {
          terminal = exp;
          break;
        }
      }
      if (!terminal) {
        toast.warning('Crawl still running', {
          description: 'Taking longer than expected — check back shortly, then Run cycle.',
        });
      } else if (terminal.status === 'completed') {
        const s = (terminal.stats ?? {}) as {
          visits?: number;
          actions?: number;
          coverage?: ExplorationCoverage;
        };
        toast.success('Crawl complete', {
          description: `${s.visits ?? 0} pages · ${s.actions ?? 0} actions captured. You can Run cycle now.`,
        });
        // Post-crawl seed-confirm nudge: name the fields that blocked deeper coverage,
        // so the operator's remediation is a targeted seed request, not blind guessing.
        const needsSeed = s.coverage?.fields_needing_seed ?? [];
        if (needsSeed.length > 0) {
          toast.warning(`${needsSeed.length} field(s) need a seed to crawl deeper`, {
            description: needsSeed.slice(0, 6).join(', ') + (needsSeed.length > 6 ? '…' : ''),
            duration: 12000,
          });
        }
        state.reload();
      } else {
        toast.error(`Crawl ${terminal.status}`, {
          description: terminal.error || 'Check the app’s onboarding attestation.',
        });
      }
    } catch (err) {
      toast.error('Could not start crawl', { description: (err as QecApiError).message });
    } finally {
      setCrawling(false);
    }
  };

  // Keep the app view LIVE while a crawl runs server-side: poll so it reflects
  // progress and flips to the ready state the instant the crawl completes — even
  // after a page reload, when the local `crawling` flag is gone. This is precisely
  // why a long crawl no longer leaves an empty Test Studio looking broken: the app
  // knows, from server truth, that a crawl is still in flight.
  const crawlActive = state.data?.crawl?.active ?? false;
  useEffect(() => {
    if (!crawlActive) return undefined;
    const t = setInterval(() => state.reload(), 5000);
    return () => clearInterval(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [crawlActive]);

  if (state.isLoading) return <Loading label="Loading app…" />;
  if (state.isError) return <ErrorState error={state.error} onRetry={state.reload} />;
  const app = state.data!;
  const isCrawling = crawling || (app.crawl?.active ?? false);

  return (
    <div className="space-y-3">
      {isCrawling && (
        <div className="flex items-center gap-2.5 rounded-lg border border-teal-500/30 bg-teal-500/10 px-3.5 py-2.5">
          <Radar size={15} className="text-teal-500 animate-pulse shrink-0" aria-hidden />
          <div className="min-w-0">
            <p className="text-xs font-medium text-ink">Crawling in progress — exploring the app…</p>
            <p className="text-2xs text-ink-low">
              {(app.crawl?.pages ?? 0) > 0 ? `${app.crawl!.pages} pages captured so far. ` : ''}
              Test Studio populates automatically when the crawl completes (usually a few minutes). You can leave this page — it keeps running.
            </p>
          </div>
        </div>
      )}
      {!isCrawling && <CrawlDiagnosisCard crawl={app.crawl} />}
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
      <div className="flex items-center gap-2 shrink-0">
        <Button
          variant="secondary"
          onClick={() => navigate(`/apps/${appId}/studio`)}
          disabled={!app.latest_artifact_id}
          title={
            app.latest_artifact_id
              ? 'Browse + run every discovered flow'
              : isCrawling
                ? 'Crawl in progress — Test Studio opens automatically when it completes'
                : 'Crawl first to populate the Studio'
          }
          icon={<FlaskConical size={15} />}
        >
          Test Studio
        </Button>
        <Button
          variant="secondary"
          loading={isCrawling}
          disabled={isCrawling}
          onClick={crawl}
          icon={<Radar size={15} />}
        >
          {isCrawling ? 'Crawling…' : 'Crawl'}
        </Button>
        <Button variant="primary" loading={triggering} onClick={runCycle} icon={<PlayCircle size={15} />}>
          Run cycle
        </Button>
      </div>
    </div>
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

// ── onboarding attestation (the fail-closed crawl gate, made legible) ─────────

function AttestationCard({ appId }: { appId: string }) {
  const state = useAsync((signal) => api.getApp(appId, { signal }), [appId]);
  const [busy, setBusy] = useState(false);
  const app = state.data;

  const reAttest = async () => {
    if (!app) return;
    setBusy(true);
    try {
      // One-click extend: spread-safe (api.reAttest keeps attested_by / RoE / preflight).
      const expires_at = new Date(Date.now() + 90 * 864e5).toISOString();
      await api.reAttest(app, { expires_at });
      toast.success('Re-attested', { description: 'Attestation window extended 90 days.' });
      state.reload();
    } catch (err) {
      toast.error('Could not re-attest', { description: (err as QecApiError).message });
    } finally {
      setBusy(false);
    }
  };

  const statusTone = (s?: string): 'good' | 'warn' | 'crit' =>
    s === 'live' ? 'good' : s === 'attested' ? 'warn' : 'crit';
  const att = app?.env_attestation ?? {};

  return (
    <Panel tone="elevated">
      <SectionHead
        title="Onboarding attestation"
        subtitle="the fail-closed crawl gate — signed RoE · non-prod · preflight"
        icon={<FileCheck2 size={16} className="text-teal" />}
        right={
          app && (
            <Pill tone={statusTone(app.onboarding_status)} size="sm" variant="soft">
              {app.onboarding_status ?? 'draft'}
            </Pill>
          )
        }
      />
      <div className="mt-3">
        {state.isLoading && <SkeletonRows rows={3} />}
        {state.isError && <ErrorState error={state.error} onRetry={state.reload} />}
        {state.isSuccess && app && (
          <>
            <dl className="grid grid-cols-2 gap-2 text-2xs">
              <div>
                <dt className="text-ink-low">Env kind</dt>
                <dd className="text-ink font-medium">{String(att.env_kind || '—')}</dd>
              </div>
              <div>
                <dt className="text-ink-low">Attested by</dt>
                <dd className="text-ink font-medium truncate">{String(att.attested_by || '—')}</dd>
              </div>
              <div>
                <dt className="text-ink-low">RoE signed</dt>
                <dd className="text-ink font-medium">{att.rules_of_engagement?.signed ? 'yes' : 'no'}</dd>
              </div>
              <div>
                <dt className="text-ink-low">Expires</dt>
                <dd className="text-ink font-medium">
                  {app.attestation_expires_at ? timeAgo(app.attestation_expires_at) : '—'}
                </dd>
              </div>
            </dl>
            {!app.onboarding_ready && (app.onboarding_reasons?.length ?? 0) > 0 && (
              <ul className="mt-3 space-y-1.5">
                {app.onboarding_reasons!.map((reason, i) => (
                  <li
                    key={i}
                    className="flex items-start gap-2 rounded-lg px-3 py-2 ring-1 ring-crit/25 bg-crit/[0.06]"
                  >
                    <ShieldAlert size={13} className="text-crit mt-0.5 shrink-0" aria-hidden />
                    <span className="text-2xs text-ink leading-snug">{reason}</span>
                  </li>
                ))}
              </ul>
            )}
            <div className="mt-3 flex items-center justify-between gap-2">
              <span className="text-2xs text-ink-low">
                {app.onboarding_ready ? 'Gate open — crawl allowed.' : 'Gate closed — resolve the reasons.'}
              </span>
              <Button variant="secondary" size="sm" loading={busy} disabled={!att.env_kind} onClick={reAttest}>
                Re-attest +90d
              </Button>
            </div>
          </>
        )}
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
      {/* Seed Manifest — surfaced full-width and prominent so a user always knows
          the few real values / approvals this app needs to test its flows. */}
      <SeedManifestPanel appId={id} />
      {/* Coverage Ledger — the measured "did we miss anything?" honesty spine. */}
      <CoveragePanel appId={id} />
      <div className="grid grid-cols-1 xl:grid-cols-2 gap-4 items-start">
        <div className="space-y-4">
          <AttestationCard appId={id} />
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
