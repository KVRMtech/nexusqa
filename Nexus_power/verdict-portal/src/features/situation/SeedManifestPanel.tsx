/**
 * Seed Manifest panel — the answer to "how does the user know what to provide?".
 *
 * After a crawl, every observed field is classified into the six dispositions and
 * grouped BY FLOW so the panel reads the way the user thinks: "to test the **Transfer**
 * flow, provide these values" — leading with the flow the app was onboarded for, with
 * the shared login shown as already-set and other flows collapsed. This replaced a flat,
 * undifferentiated list that mixed Transfer, Loan and Login fields with identical copy
 * ("a real value the product must never invent" on every row), which read as noise.
 *
 * Only the human slice is actionable: the real values the product must never invent
 * (ASK) and permission to submit + what "correct" means (APPROVE). Everything else is
 * shown as auto-handled. Provide the few, re-crawl, and it drives + proves the flow.
 */
import { useMemo, useState } from 'react';
import { toast } from 'sonner';
import {
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  Eye,
  KeyRound,
  ListChecks,
  Lock,
  Repeat,
  ShieldCheck,
  Sparkles,
} from 'lucide-react';

import { Button, Panel, Pill, SectionHead, SkeletonRows } from '../../components';
import { api } from '../../lib/api';
import { cn } from '../../lib/format';
import { useAsync } from '../../lib/useAsync';
import type { Disposition, SeedFlow, SeedItem, SeedManifest } from '../../types/qec';

const DISPOSITION_META: Record<
  Disposition,
  { label: string; tone: 'crit' | 'warn' | 'good' | 'teal' | 'neutral'; icon: typeof KeyRound; blurb: string }
> = {
  ASK: { label: 'You provide', tone: 'crit', icon: KeyRound, blurb: 'a real value only you have' },
  APPROVE: { label: 'You approve', tone: 'warn', icon: ShieldCheck, blurb: 'permission to submit + what “correct” means' },
  SYNTHESIZE: { label: 'Auto-filled', tone: 'good', icon: Sparkles, blurb: 'a valid value generated for you' },
  PICK: { label: 'Auto-picked', tone: 'teal', icon: ListChecks, blurb: 'chosen from an option the crawl saw' },
  CARRY: { label: 'Reused', tone: 'neutral', icon: Repeat, blurb: 'a value you entered once before' },
  OBSERVE: { label: 'Checked', tone: 'neutral', icon: Eye, blurb: 'read & asserted, never filled' },
};

const ACTIONABLE: Disposition[] = ['ASK', 'APPROVE'];

function Badge({ disposition }: { disposition: Disposition }) {
  const m = DISPOSITION_META[disposition];
  return (
    <Pill tone={m.tone} size="sm" variant="soft">
      {m.label}
    </Pill>
  );
}

/** Derive a one-line "flows" fallback if the server didn't group (version skew). */
function fallbackFlows(m: SeedManifest): { flows: SeedFlow[]; auth: SeedFlow | null } {
  if (m.flows) return { flows: m.flows, auth: m.auth ?? null };
  const items = m.full ?? [];
  const actionable = items.filter((i) => ACTIONABLE.includes(i.disposition));
  return {
    flows: [
      {
        key: 'all',
        name: 'Fields to provide',
        kind: 'flow',
        primary: true,
        to_provide: actionable.length,
        actionable: actionable.length,
        total: items.length,
        items,
      },
    ],
    auth: null,
  };
}

export default function SeedManifestPanel({ appId, onSeeded }: { appId: string; onSeeded?: () => void }) {
  const manifest = useAsync((signal) => api.getSeedManifest(appId, 'full', { signal }), [appId]);
  const appState = useAsync((signal) => api.getApp(appId, { signal }), [appId]);

  const [askValues, setAskValues] = useState<Record<string, string>>({});
  const [approvals, setApprovals] = useState<Record<string, boolean>>({});
  const [outcomes, setOutcomes] = useState<Record<string, string>>({});
  const [saving, setSaving] = useState<null | 'save' | 'recrawl'>(null);

  const m = manifest.data;

  const { flows, auth } = useMemo(() => (m ? fallbackFlows(m) : { flows: [], auth: null }), [m]);

  // "ready" = a value is already saved OR the user has just typed/approved one.
  const isReady = (item: SeedItem): boolean => {
    if (item.provided) return true;
    if (item.disposition === 'ASK') return !!askValues[item.label]?.trim();
    if (item.disposition === 'APPROVE') return !!approvals[item.label];
    return true; // auto-handled dispositions are always "ready"
  };
  const readyCount = (f: SeedFlow) =>
    f.items.filter((i) => ACTIONABLE.includes(i.disposition) && isReady(i)).length;

  if (manifest.isLoading) {
    return (
      <Panel tone="elevated">
        <SectionHead title="Seed data" subtitle="what this app needs to test its flows" />
        <div className="mt-3"><SkeletonRows rows={2} /></div>
      </Panel>
    );
  }
  if (!m || m.status !== 'ready') return null;

  const totalToProvide =
    flows.reduce((n, f) => n + f.to_provide, 0) + (auth && !auth.satisfied ? auth.to_provide : 0);
  const flowsNeedingInput = flows.filter((f) => f.actionable > 0).length;
  const autoHandled = (m.autonomous_count ?? 0) + (m.counts?.OBSERVE ?? 0);

  // Fully autonomous app with nothing to seed and login handled — reassure, don't nag.
  if (totalToProvide === 0 && (!auth || auth.satisfied)) {
    return (
      <Panel tone="elevated">
        <div className="flex items-center gap-2.5">
          <CheckCircle2 size={16} className="text-good shrink-0" aria-hidden />
          <p className="text-xs text-ink">
            <span className="font-semibold">Nothing to seed.</span> Every field is auto-handled
            {auth ? ' and your login is set' : ''} — the crawl can drive all flows on its own.
          </p>
        </div>
      </Panel>
    );
  }

  async function save(recrawl: boolean) {
    const app = appState.data;
    if (!app) {
      toast.error('App not loaded — try again');
      return;
    }
    setSaving(recrawl ? 'recrawl' : 'save');
    try {
      const ak = (app.answer_key || {}) as Record<string, unknown>;
      const fill = { ...((ak.fill as Record<string, unknown> | undefined) || {}) };
      Object.entries(askValues).forEach(([k, v]) => {
        if (v.trim()) fill[k] = v.trim();
      });
      const outc = { ...((ak.outcomes as Record<string, unknown> | undefined) || {}) };
      Object.entries(outcomes).forEach(([k, v]) => {
        if (v.trim()) outc[k] = v.trim();
      });
      const answer_key = { ...ak, fill, outcomes: outc };

      const fen = { ...(app.fences || {}) } as Record<string, unknown>;
      const approved = Object.entries(approvals).filter(([, on]) => on).map(([l]) => l);
      if (approved.length) {
        const prior = (fen.submit_approvals as string[] | undefined) || [];
        fen.submit_approvals = Array.from(new Set([...prior, ...approved]));
        fen.allow_submit = true;
      }

      await api.updateApp(appId, { answer_key, fences: fen });
      if (recrawl) await api.triggerExploration(appId);
      toast.success(recrawl ? 'Saved — starting a seeded crawl to test these flows' : 'Seed values saved');
      onSeeded?.();
    } catch {
      toast.error('Could not save the seed values');
    } finally {
      setSaving(null);
    }
  }

  const canSubmit =
    Object.values(askValues).some((v) => v.trim()) ||
    Object.values(approvals).some(Boolean) ||
    Object.values(outcomes).some((v) => v.trim());

  const ctx = { askValues, setAskValues, approvals, setApprovals, outcomes, setOutcomes, isReady };

  return (
    <div className="rounded-xl ring-1 ring-amber-500/40 bg-amber-500/[0.04]">
      <Panel tone="ghost">
        <SectionHead
          title="A few real values to test your flows"
          subtitle={
            flowsNeedingInput > 1
              ? `analyzed ${flowsNeedingInput} flows — everything else is filled automatically`
              : 'everything else is filled automatically — these are the things only you can provide'
          }
          icon={<KeyRound size={16} className="text-amber-500" />}
          right={
            <Pill tone="warn" size="sm" variant="soft">
              {totalToProvide} to provide
            </Pill>
          }
        />

        <p className="mt-2 text-2xs text-ink-low leading-relaxed max-w-2xl">
          The product will not invent a real account number or click “Send” on its own — that is
          how it stays trustworthy. Provide the values below once and re-crawl, and it will drive
          and <span className="font-medium text-ink">prove</span> each flow end-to-end.
        </p>

        {/* Make the 99%-automated / 1%-you split visible — the "it did the hard work" moment. */}
        <div className="mt-2.5 flex flex-wrap items-center gap-x-4 gap-y-1 text-2xs">
          {autoHandled > 0 && (
            <span className="inline-flex items-center gap-1 text-good font-medium">
              <Sparkles size={12} aria-hidden /> {autoHandled} handled automatically
            </span>
          )}
          <span className="inline-flex items-center gap-1 text-amber-500 font-medium">
            <KeyRound size={12} aria-hidden /> {totalToProvide} only you can provide
          </span>
          {flowsNeedingInput > 0 && (
            <span className="inline-flex items-center gap-1 text-ink-low">
              <ListChecks size={12} aria-hidden /> across {flowsNeedingInput}{' '}
              {flowsNeedingInput === 1 ? 'flow' : 'flows'}
            </span>
          )}
        </div>

        {/* ── Sign in (shared) — satisfied strip or, if no creds, a normal flow ── */}
        {auth && auth.satisfied && (
          <div className="mt-4 flex items-center gap-2.5 rounded-lg ring-1 ring-good/30 bg-good/[0.06] px-3 py-2">
            <Lock size={13} className="text-good shrink-0" aria-hidden />
            <p className="text-2xs text-ink">
              <span className="font-semibold">Sign in — already set.</span> Using the login you
              provided at onboarding{' '}
              <span className="text-ink-low">
                ({auth.items.map((i) => i.label).slice(0, 3).join(', ')})
              </span>
              .
            </p>
          </div>
        )}

        {/* ── Flows — primary first, others collapsed ── */}
        <div className="mt-3 space-y-2.5">
          {auth && !auth.satisfied && (
            <FlowCard flow={auth} defaultOpen ready={readyCount(auth)} ctx={ctx} />
          )}
          {flows
            // Show the primary flow always; hide other flows that have nothing for the
            // user to do (all-auto) so the panel never shows a "0 of 0" card.
            .filter((f) => f.primary || f.actionable > 0)
            .map((f) => (
              <FlowCard key={f.key} flow={f} defaultOpen={!!f.primary} ready={readyCount(f)} ctx={ctx} />
            ))}
        </div>

        {/* ── actions ── */}
        <div className="mt-4 flex items-center gap-2 flex-wrap">
          <Button variant="primary" loading={saving === 'recrawl'} disabled={saving !== null || !canSubmit} onClick={() => save(true)}>
            Save &amp; re-crawl
          </Button>
          <Button variant="secondary" loading={saving === 'save'} disabled={saving !== null || !canSubmit} onClick={() => save(false)}>
            Save only
          </Button>
          <span className="text-2xs text-ink-low">re-crawl drives the seeded flows end-to-end</span>
        </div>
      </Panel>
    </div>
  );
}

type Ctx = {
  askValues: Record<string, string>;
  setAskValues: React.Dispatch<React.SetStateAction<Record<string, string>>>;
  approvals: Record<string, boolean>;
  setApprovals: React.Dispatch<React.SetStateAction<Record<string, boolean>>>;
  outcomes: Record<string, string>;
  setOutcomes: React.Dispatch<React.SetStateAction<Record<string, string>>>;
  isReady: (item: SeedItem) => boolean;
};

/** One flow: a header with progress, its actionable fields as inputs, auto ones summarized. */
function FlowCard({
  flow,
  defaultOpen,
  ready,
  ctx,
}: {
  flow: SeedFlow;
  defaultOpen: boolean;
  ready: number;
  ctx: Ctx;
}) {
  const [open, setOpen] = useState(defaultOpen);
  const actionable = flow.items.filter((i) => ACTIONABLE.includes(i.disposition));
  const auto = flow.items.filter((i) => !ACTIONABLE.includes(i.disposition));
  const complete = flow.actionable > 0 && ready >= flow.actionable;
  const Chevron = open ? ChevronDown : ChevronRight;

  return (
    <div className={cn('rounded-lg ring-1 overflow-hidden', flow.primary ? 'ring-amber-500/30 bg-inset/40' : 'ring-line bg-inset/20')}>
      <button
        type="button"
        onClick={() => setOpen((s) => !s)}
        className="w-full flex items-center gap-2 px-3 py-2 text-left hover:bg-inset/40 transition"
      >
        <Chevron size={14} className="text-ink-low shrink-0" aria-hidden />
        {flow.kind === 'auth' ? (
          <Lock size={13} className="text-ink-low shrink-0" aria-hidden />
        ) : (
          <ListChecks size={13} className="text-ink-low shrink-0" aria-hidden />
        )}
        <span className="text-xs font-semibold text-ink">{flow.name}</span>
        {flow.primary && <Pill tone="warn" size="sm" variant="soft">main flow</Pill>}
        <span className="ml-auto inline-flex items-center gap-2">
          {complete ? (
            <span className="inline-flex items-center gap-1 text-2xs text-good font-medium">
              <CheckCircle2 size={12} aria-hidden /> ready
            </span>
          ) : (
            <span className="text-2xs text-ink-low tabular-nums">
              {ready} of {flow.actionable} ready
            </span>
          )}
        </span>
      </button>

      {open && (
        <div className="px-3 pb-3 pt-0.5 space-y-2">
          {flow.kind === 'flow' && actionable.length > 0 && (
            <p className="text-2xs text-ink-low">
              To complete <span className="text-ink font-medium">{flow.name}</span>, provide{' '}
              {flow.actionable} {flow.actionable === 1 ? 'value' : 'values'} the product won’t invent:
            </p>
          )}

          {actionable.map((item) =>
            item.disposition === 'APPROVE' ? (
              <SeedRow key={item.label} item={item} ready={ctx.isReady(item)}>
                <label className="flex items-center gap-2 cursor-pointer select-none">
                  <input
                    type="checkbox"
                    checked={ctx.approvals[item.label] ?? false}
                    onChange={(e) => ctx.setApprovals((s) => ({ ...s, [item.label]: e.target.checked }))}
                    className="accent-amber-500 h-3.5 w-3.5"
                  />
                  <span className="text-xs text-ink">Allow the crawl to submit “{item.label}”</span>
                </label>
                <input
                  type="text"
                  value={ctx.outcomes[item.label] ?? ''}
                  onChange={(e) => ctx.setOutcomes((s) => ({ ...s, [item.label]: e.target.value }))}
                  placeholder="What does success look like? e.g. “a confirmation appears”"
                  className="mt-1.5 w-full rounded-lg bg-inset ring-1 ring-line focus:ring-amber-500/60 px-3 py-1.5 text-xs text-ink outline-none transition"
                />
              </SeedRow>
            ) : (
              <SeedRow key={item.label} item={item} ready={ctx.isReady(item)}>
                {item.options.length > 0 ? (
                  <select
                    value={ctx.askValues[item.label] ?? ''}
                    onChange={(e) => ctx.setAskValues((s) => ({ ...s, [item.label]: e.target.value }))}
                    className="w-full rounded-lg bg-inset ring-1 ring-line focus:ring-amber-500/60 px-3 py-1.5 text-xs text-ink outline-none transition"
                  >
                    <option value="">Choose {item.label.toLowerCase()}…</option>
                    {item.options.map((o) => (
                      <option key={o} value={o}>{o}</option>
                    ))}
                  </select>
                ) : (
                  <input
                    type="text"
                    value={ctx.askValues[item.label] ?? ''}
                    onChange={(e) => ctx.setAskValues((s) => ({ ...s, [item.label]: e.target.value }))}
                    placeholder={item.provided ? 'a value is saved — type to replace' : `Enter a real ${item.label.toLowerCase()}…`}
                    className="w-full rounded-lg bg-inset ring-1 ring-line focus:ring-amber-500/60 px-3 py-1.5 text-xs text-ink outline-none transition"
                  />
                )}
              </SeedRow>
            ),
          )}

          {auto.length > 0 && (
            <div className="flex flex-wrap items-center gap-1.5 pt-0.5">
              <span className="text-2xs text-ink-low">Handled automatically:</span>
              {auto.slice(0, 8).map((i) => (
                <span
                  key={i.label}
                  title={DISPOSITION_META[i.disposition].blurb}
                  className="inline-flex items-center gap-1 rounded-md bg-inset ring-1 ring-line px-1.5 py-0.5 text-2xs text-ink-low"
                >
                  <CheckCircle2 size={10} className="text-good/70" aria-hidden />
                  {i.label}
                </span>
              ))}
              {auto.length > 8 && <span className="text-2xs text-ink-low">+{auto.length - 8} more</span>}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

/** One field row: badge + label + why, with the caller's input rendered below. */
function SeedRow({ item, ready, children }: { item: SeedItem; ready: boolean; children: React.ReactNode }) {
  const meta = DISPOSITION_META[item.disposition];
  const Icon = meta.icon;
  return (
    <div className={cn('rounded-lg ring-1 px-3 py-2.5', ready ? 'ring-good/30 bg-good/[0.04]' : 'ring-line bg-inset/60')}>
      <div className="flex items-center gap-2 mb-1.5">
        <Icon size={13} className={cn('shrink-0', item.disposition === 'ASK' ? 'text-crit' : 'text-amber-500')} aria-hidden />
        <span className="text-xs font-semibold text-ink">{item.label}</span>
        {item.required && <Pill tone="neutral" size="sm">required</Pill>}
        <Badge disposition={item.disposition} />
        {ready && <CheckCircle2 size={13} className="text-good ml-auto" aria-hidden />}
        {!ready && <span className="text-2xs text-ink-low ml-auto hidden sm:inline">{meta.blurb}</span>}
      </div>
      {children}
    </div>
  );
}
