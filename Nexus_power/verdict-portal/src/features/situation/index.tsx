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
  Crosshair,
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
  ShieldCheck,
} from 'lucide-react';

import { api, QecApiError } from '../../lib/api';
import { useAuth } from '../../lib/auth';
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
import type { AppCrawlStatus, ClientApp, CriticalityBand, CrawlDiagnosis, ExplorationCoverage, ScenarioView } from '../../types/qec';
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

// ── the two dials that decide what a crawl does ────────────────────────
// SCOPE (where it walks) and DATA (who supplies the values) are independent, so
// they are two controls rather than one combined setting. Both live on the app's
// `schedule`; PATCH /apps whole-replaces it, so the existing object is spread and
// only the keys below are set or removed — cadence / run_environment survive.
//
// Everything defaults to the conservative option: no `scope_paths` means Explore,
// and no `data_mode` means "user", which is the behaviour that existed before the
// data agent. An app nobody has configured must never be silently upgraded.

type ScopeMode = 'explore' | 'target' | 'e2e';
type DataMode = 'user' | 'agent';

/** The path prefix a Target crawl would use if the operator sets nothing.
 *
 *  The client's own complaint: being asked to identify a deep URL AND then repeat
 *  its path as a scope is work the system can do itself. The Base URL already says
 *  which section they mean. */
function derivedScope(baseUrl: string): string {
  try {
    const p = new URL(baseUrl).pathname.replace(/\/+$/, '');
    return p && p !== '' ? p : '/';
  } catch {
    return '/';
  }
}

function RadioRow({
  checked, onSelect, disabled, title, note,
}: {
  checked: boolean; onSelect: () => void; disabled?: boolean; title: string; note: string;
}) {
  return (
    <button
      type="button"
      role="radio"
      aria-checked={checked}
      disabled={disabled}
      onClick={onSelect}
      className={cn(
        'w-full text-left flex items-start gap-2.5 rounded-lg px-2.5 py-2 ring-1 transition-colors',
        checked ? 'bg-teal/10 ring-teal/50' : 'bg-panel ring-line hover:ring-ink/20',
        disabled && 'opacity-55 cursor-not-allowed hover:ring-line',
      )}
    >
      <span
        aria-hidden
        className={cn(
          'mt-0.5 h-3.5 w-3.5 shrink-0 rounded-full ring-2 transition-colors',
          checked ? 'ring-teal bg-teal/70' : 'ring-ink-faint bg-transparent',
        )}
      />
      <span className="min-w-0">
        <span className="block text-xs font-semibold text-ink">{title}</span>
        <span className="block text-2xs text-ink-faint leading-snug">{note}</span>
      </span>
    </button>
  );
}

function CrawlModeControl({ app, onSaved }: { app: ClientApp; onSaved: () => void }) {
  const schedule = (app.schedule as Record<string, unknown> | undefined) ?? {};
  const rawScope = schedule.scope_paths;
  const currentPaths = Array.isArray(rawScope)
    ? rawScope.filter((p): p is string => typeof p === 'string')
    : [];
  const currentData: DataMode = schedule.data_mode === 'agent' ? 'agent' : 'user';
  const storedMode = typeof schedule.crawl_mode === 'string' ? schedule.crawl_mode : '';
  // Absent ⇒ derived from the scope, exactly as mode worked before this key
  // existed: a confined crawl is Target, an unconfined one Explore.
  const currentScope: ScopeMode =
    storedMode === 'e2e' ? 'e2e' : currentPaths.length ? 'target' : 'explore';

  const [editing, setEditing] = useState(false);
  const [scopeMode, setScopeMode] = useState<ScopeMode>(currentScope);
  const [dataMode, setDataMode] = useState<DataMode>(currentData);
  const [paths, setPaths] = useState(currentPaths.join('\n'));
  const [saving, setSaving] = useState(false);

  const open = () => {
    setScopeMode(currentScope);
    setDataMode(currentData);
    // Prefill Target with the section the Base URL already points at, so the
    // operator confirms a scope instead of retyping one.
    setPaths(currentPaths.length ? currentPaths.join('\n') : derivedScope(app.base_url));
    setEditing(true);
  };

  const cleanPaths = paths
    .split(/[\s,]+/)
    .map((p) => p.trim())
    .filter(Boolean)
    .map((p) => (p.startsWith('/') ? p : `/${p}`))
    .slice(0, 20);

  // The server refuses a Target crawl whose Base URL enters outside the scope — it
  // would start out-of-scope and capture nothing while reporting success. Catch it
  // here so the operator fixes it now rather than reading a 422 after dispatch.
  const entryPath = derivedScope(app.base_url);
  const entryOutOfScope =
    scopeMode === 'target' &&
    cleanPaths.length > 0 &&
    !cleanPaths.some((p) => entryPath === p || entryPath.startsWith(p.endsWith('/') ? p : `${p}/`));

  const save = async () => {
    setSaving(true);
    try {
      const next: Record<string, unknown> = { ...schedule };
      if (scopeMode === 'target' && cleanPaths.length) next.scope_paths = cleanPaths;
      else delete next.scope_paths;
      // Only 'e2e' is stored. Explore and Target stay derivable from the scope, so
      // an app configured before this key existed keeps behaving the same way.
      if (scopeMode === 'e2e') next.crawl_mode = 'e2e';
      else delete next.crawl_mode;
      if (dataMode === 'agent') next.data_mode = 'agent';
      else delete next.data_mode;   // absent = 'user' = the conservative default
      await api.updateApp(app.app_id, { schedule: next });
      toast.success(
        scopeMode === 'target'
          ? `Target — confined to ${cleanPaths.join(', ')}`
          : scopeMode === 'e2e'
            ? 'End-to-end — each journey walked to its end'
            : 'Explore — the whole app is crawled',
        {
          description:
            dataMode === 'agent'
              ? 'Data: the agent fills what it can, and asks for the rest.'
              : 'Data: you provide the values; the crawl asks for what it needs.',
        },
      );
      setEditing(false);
      onSaved();
    } catch (err) {
      toast.error('Could not update the crawl settings', {
        description: (err as QecApiError).message,
      });
    } finally {
      setSaving(false);
    }
  };

  if (!editing) {
    return (
      <button
        type="button"
        onClick={open}
        className="inline-flex items-center gap-1.5 rounded-md bg-ink/5 px-2 py-1 text-2xs text-ink-mid ring-1 ring-line hover:text-ink transition-colors"
        title="Set what the crawl walks, and who supplies the test data"
      >
        <Crosshair size={12} aria-hidden />
        <span className="font-semibold">
          {currentScope === 'target' ? 'Target' : currentScope === 'e2e' ? 'End-to-end' : 'Explore'}
        </span>
        {currentScope === 'target' && (
          <span className="font-mono text-ink-low">{currentPaths.join(' ')}</span>
        )}
        <span className="text-ink-faint">·</span>
        <span className="font-semibold">{currentData === 'agent' ? 'Agent data' : 'Your data'}</span>
        <span className="text-ink-faint">· edit</span>
      </button>
    );
  }

  return (
    <div className="rounded-lg bg-inset ring-1 ring-line px-3 py-3 space-y-3.5 w-full max-w-lg">
      {/* dial 1 — where it walks */}
      <div className="space-y-1.5" role="radiogroup" aria-label="Crawl scope">
        <p className="text-2xs font-semibold uppercase tracking-wide text-ink-faint">
          Scope — where the crawl walks
        </p>
        <RadioRow
          checked={scopeMode === 'explore'}
          onSelect={() => setScopeMode('explore')}
          title="Explore — the whole application"
          note="Every page reachable from the Base URL. Use this to discover what exists."
        />
        <RadioRow
          checked={scopeMode === 'target'}
          onSelect={() => setScopeMode('target')}
          title="Target — one section, thoroughly"
          note="Confined to the path below, taken from your Base URL. Nothing else on the host is crawled."
        />
        {scopeMode === 'target' && (
          <div className="pl-6 space-y-1">
            <input
              className="w-full rounded-lg bg-panel text-ink text-xs ring-1 ring-line focus-visible:ring-teal/60 px-2.5 py-1.5 font-mono"
              value={paths}
              onChange={(e) => setPaths(e.target.value)}
              placeholder={entryPath}
              aria-label="Target path prefix"
            />
            {entryOutOfScope ? (
              <p className="text-2xs text-amber-600 leading-snug">
                Your Base URL enters at <span className="font-mono">{entryPath}</span>, which is
                outside this scope — the crawl would start out of bounds and capture nothing.
                Use <span className="font-mono">{entryPath}</span>, or switch to Explore.
              </p>
            ) : (
              <p className="text-2xs text-ink-faint">Derived from your Base URL. Edit if you meant a different section.</p>
            )}
          </div>
        )}
        <RadioRow
          checked={scopeMode === 'e2e'}
          onSelect={() => setScopeMode('e2e')}
          title="End-to-end flow — walk each journey to its end"
          note="Follows a funnel all the way to its final step instead of sampling the first few, and reports which journeys actually finished."
        />
        {scopeMode === 'e2e' && (
          <p className="pl-6 text-2xs text-amber-600 leading-snug">
            One path per journey. At each decision point a single option is taken, so
            the business paths behind the other options are not visited — a different
            premium or a different eligibility outcome would not be seen. Branch
            coverage is the next phase.
          </p>
        )}
      </div>

      {/* dial 2 — who supplies the values */}
      <div className="space-y-1.5" role="radiogroup" aria-label="Test data">
        <p className="text-2xs font-semibold uppercase tracking-wide text-ink-faint">
          Data — who supplies the values
        </p>
        <RadioRow
          checked={dataMode === 'user'}
          onSelect={() => setDataMode('user')}
          title="You provide the data"
          note="The crawl fills what it can from your test data and names anything it could not, so you supply only what is actually missing."
        />
        <RadioRow
          checked={dataMode === 'agent'}
          onSelect={() => setDataMode('agent')}
          title="Let the agent fill what it can"
          note="A coherent fictional person answers every field it honestly can — including the choices that decide which path a funnel takes. Every choice is recorded. One-time codes and document uploads are still asked for."
        />
        {dataMode === 'agent' && (
          <p className="pl-6 text-2xs text-ink-faint leading-snug">
            The agent chooses answers that change what the application does — picking
            &ldquo;no&rdquo; on a smoker question selects the whole downstream funnel. The report
            says which path was taken, but it is the agent&rsquo;s choice, not yours.
          </p>
        )}
      </div>

      <div className="flex items-center gap-2 pt-0.5">
        <Button
          size="sm"
          variant="primary"
          loading={saving}
          onClick={save}
          disabled={entryOutOfScope || (scopeMode === 'target' && !cleanPaths.length)}
          title={scopeMode === 'e2e' ? 'Walks each journey to its end; one path per journey' : undefined}
        >
          Save
        </Button>
        <Button size="sm" variant="ghost" onClick={() => setEditing(false)} disabled={saving}>
          Cancel
        </Button>
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
        <div className="mt-2">
          <CrawlModeControl app={app} onSaved={state.reload} />
        </div>
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
                        {c.regression_review_count > 0 && ` · ${c.regression_review_count} need review`}
                      </p>
                    </div>
                    {c.regression_review_count > 0 && (
                      <Pill tone="warn" size="sm">
                        {c.regression_review_count} regression{c.regression_review_count > 1 ? 's' : ''}
                      </Pill>
                    )}
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
  const { session } = useAuth();
  const [busy, setBusy] = useState(false);
  const [affirmed, setAffirmed] = useState(false);
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

  // Record the operator's AUTHORIZATION to test this URL — the liability gate the
  // crawler enforces (env_attestation.authorization). Attributed to the signed-in
  // operator so a later allow/refusal is auditable; a blank identity is refused.
  const authorize = async () => {
    if (!app) return;
    const who = (session?.email || session?.sub || '').trim();
    if (!who) {
      toast.error('Sign in first', { description: 'Authorization must be attributed to an operator.' });
      return;
    }
    setBusy(true);
    try {
      await api.authorize(app, true, who);
      toast.success('Authorization recorded', {
        description: `Attributed to ${who}. The crawl gate for this URL is now open.`,
      });
      setAffirmed(false);
      state.reload();
    } catch (err) {
      toast.error('Could not record authorization', { description: (err as QecApiError).message });
    } finally {
      setBusy(false);
    }
  };

  const statusTone = (s?: string): 'good' | 'warn' | 'crit' =>
    s === 'live' ? 'good' : s === 'attested' ? 'warn' : 'crit';
  const att = app?.env_attestation ?? {};
  const authz = att.authorization;
  const isAuthorized = Boolean(authz?.authorized) && Boolean(String(authz?.authorized_by || '').trim());

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
              <div className="col-span-2">
                <dt className="text-ink-low">Authorized to test</dt>
                <dd className="text-ink font-medium truncate">
                  {isAuthorized ? `yes — ${authz?.authorized_by}` : 'not attested'}
                </dd>
              </div>
            </dl>
            {/* AUTHORIZATION affirm — the liability gate. Until the operator affirms
                they own or are permitted to test this URL, the crawler refuses. This is
                the self-serve control that clears the "authorization … not attested"
                reason, attributed to the signed-in operator. */}
            {!isAuthorized && (
              <div className="mt-3 rounded-lg px-3 py-2.5 ring-1 ring-warn/30 bg-warn/[0.06]">
                <label className="flex items-start gap-2 cursor-pointer">
                  <input
                    type="checkbox"
                    checked={affirmed}
                    onChange={(e) => setAffirmed(e.target.checked)}
                    className="mt-0.5 shrink-0 accent-teal"
                  />
                  <span className="text-2xs text-ink leading-snug">
                    I own or am permitted to test this URL, and I authorize this crawl.
                    <span className="block text-ink-low">
                      Recorded against {session?.email || session?.sub || 'the signed-in operator'} for audit.
                    </span>
                  </span>
                </label>
                <div className="mt-2.5 flex justify-end">
                  <Button
                    variant="primary"
                    size="sm"
                    loading={busy}
                    disabled={!affirmed}
                    onClick={authorize}
                  >
                    <ShieldCheck size={13} className="mr-1" aria-hidden />
                    Authorize testing
                  </Button>
                </div>
              </div>
            )}
            {isAuthorized && (
              <div className="mt-3 flex items-center gap-2 rounded-lg px-3 py-2 ring-1 ring-good/25 bg-good/[0.06]">
                <ShieldCheck size={13} className="text-good shrink-0" aria-hidden />
                <span className="text-2xs text-ink leading-snug">
                  Authorized to test by {authz?.authorized_by}
                  {authz?.authorized_at ? ` · ${timeAgo(authz.authorized_at)}` : ''}.
                </span>
              </div>
            )}
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
