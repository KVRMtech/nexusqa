/**
 * Seed Manifest panel (Phase 1 UI) — the answer to "how does the user know what to
 * provide?". After a crawl, it classifies every observed field into the six
 * dispositions and surfaces, PROMINENTLY, the short list of things only a human can
 * give: the real values the product must never invent (ASK) and permission to submit
 * + what "correct" means (APPROVE). Everything else is auto-filled and tucked under a
 * "show all" toggle. Provide the few, then re-crawl to drive + prove every flow.
 *
 * This closes the gap that made a bank-transfer crawl look broken: the crawl reached
 * the transfer but couldn't fill From Account / Payee or click Send, and nothing told
 * the user that seeding those was required.
 */
import { useMemo, useState } from 'react';
import { toast } from 'sonner';
import {
  CheckCircle2,
  Eye,
  KeyRound,
  ListChecks,
  Repeat,
  ShieldCheck,
  Sparkles,
} from 'lucide-react';

import { Button, Panel, Pill, SectionHead, SkeletonRows } from '../../components';
import { api } from '../../lib/api';
import { cn } from '../../lib/format';
import { useAsync } from '../../lib/useAsync';
import type { Disposition, SeedItem } from '../../types/qec';

const DISPOSITION_META: Record<
  Disposition,
  { label: string; tone: 'crit' | 'warn' | 'good' | 'teal' | 'neutral'; icon: typeof KeyRound; blurb: string }
> = {
  ASK: { label: 'You provide', tone: 'crit', icon: KeyRound, blurb: 'a real value the product must never invent' },
  APPROVE: { label: 'You approve', tone: 'warn', icon: ShieldCheck, blurb: 'permission to submit + what "correct" means' },
  SYNTHESIZE: { label: 'Auto-filled', tone: 'good', icon: Sparkles, blurb: 'a valid value generated for you' },
  PICK: { label: 'Auto-picked', tone: 'teal', icon: ListChecks, blurb: 'chosen from an option the crawl saw' },
  CARRY: { label: 'Reused', tone: 'neutral', icon: Repeat, blurb: 'a value you entered once before' },
  OBSERVE: { label: 'Checked', tone: 'neutral', icon: Eye, blurb: 'read and asserted, never filled' },
};

function Badge({ disposition }: { disposition: Disposition }) {
  const m = DISPOSITION_META[disposition];
  return (
    <Pill tone={m.tone} size="sm" variant="soft">
      {m.label}
    </Pill>
  );
}

export default function SeedManifestPanel({
  appId,
  onSeeded,
}: {
  appId: string;
  onSeeded?: () => void;
}) {
  const manifest = useAsync((signal) => api.getSeedManifest(appId, 'full', { signal }), [appId]);
  const appState = useAsync((signal) => api.getApp(appId, { signal }), [appId]);

  const [askValues, setAskValues] = useState<Record<string, string>>({});
  const [approvals, setApprovals] = useState<Record<string, boolean>>({});
  const [outcomes, setOutcomes] = useState<Record<string, string>>({});
  const [showAll, setShowAll] = useState(false);
  const [saving, setSaving] = useState<null | 'save' | 'recrawl'>(null);

  const m = manifest.data;

  const groups = useMemo(() => {
    const full = m?.full ?? [];
    return {
      ask: full.filter((i) => i.disposition === 'ASK'),
      approve: full.filter((i) => i.disposition === 'APPROVE'),
      observe: full.filter((i) => i.disposition === 'OBSERVE'),
      auto: full.filter((i) => ['SYNTHESIZE', 'PICK', 'CARRY'].includes(i.disposition)),
    };
  }, [m]);

  if (manifest.isLoading) {
    return (
      <Panel tone="elevated">
        <SectionHead title="Seed data" subtitle="what this app needs to test its flows" />
        <div className="mt-3"><SkeletonRows rows={2} /></div>
      </Panel>
    );
  }
  // Nothing to do until there is a crawl, and nothing to ASK for a fully-autonomous app.
  if (!m || m.status !== 'ready') return null;
  const humanCount = groups.ask.length + groups.approve.length;
  if (humanCount === 0) {
    return (
      <Panel tone="elevated">
        <div className="flex items-center gap-2.5">
          <CheckCircle2 size={16} className="text-good shrink-0" aria-hidden />
          <p className="text-xs text-ink">
            <span className="font-semibold">Nothing to seed.</span> Every field on this app is
            auto-handled — the crawl can drive all its flows on its own.
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
      toast.success(
        recrawl ? 'Saved — starting a seeded crawl to test these flows' : 'Seed values saved',
      );
      onSeeded?.();
    } catch (err) {
      toast.error('Could not save the seed values');
    } finally {
      setSaving(null);
    }
  }

  const canSubmit =
    Object.values(askValues).some((v) => v.trim()) ||
    Object.values(approvals).some(Boolean) ||
    Object.values(outcomes).some((v) => v.trim());

  return (
    <div className="rounded-xl ring-1 ring-amber-500/40 bg-amber-500/[0.04]">
      <Panel tone="ghost">
        <SectionHead
          title="A few real values needed to test every flow"
          subtitle="everything else is filled automatically — these are the things only you can provide"
          icon={<KeyRound size={16} className="text-amber-500" />}
          right={
            <Pill tone="warn" size="sm" variant="soft">
              {groups.ask.length} to provide · {groups.approve.length} to approve
            </Pill>
          }
        />

        <p className="mt-2 text-2xs text-ink-low leading-relaxed max-w-2xl">
          The product will not invent a real account number or click “Send” on its own —
          that is how it stays trustworthy. Provide the values below once and re-crawl, and
          it will drive and <span className="font-medium text-ink">prove</span> the full flow.
        </p>

        {/* ── ASK: real values ─────────────────────────────────────────────── */}
        {groups.ask.length > 0 && (
          <div className="mt-4 space-y-2">
            {groups.ask.map((item) => (
              <SeedRow key={item.label} item={item}>
                <input
                  type="text"
                  value={askValues[item.label] ?? ''}
                  onChange={(e) => setAskValues((s) => ({ ...s, [item.label]: e.target.value }))}
                  placeholder={`Enter a real ${item.label.toLowerCase()}…`}
                  className="w-full rounded-lg bg-inset ring-1 ring-line focus:ring-amber-500/60 px-3 py-1.5 text-xs text-ink outline-none transition"
                />
              </SeedRow>
            ))}
          </div>
        )}

        {/* ── APPROVE: submit permission + invariant ───────────────────────── */}
        {groups.approve.length > 0 && (
          <div className="mt-3 space-y-2">
            {groups.approve.map((item) => (
              <SeedRow key={item.label} item={item}>
                <label className="flex items-center gap-2 cursor-pointer select-none">
                  <input
                    type="checkbox"
                    checked={approvals[item.label] ?? false}
                    onChange={(e) => setApprovals((s) => ({ ...s, [item.label]: e.target.checked }))}
                    className="accent-amber-500 h-3.5 w-3.5"
                  />
                  <span className="text-xs text-ink">Allow the crawl to submit “{item.label}”</span>
                </label>
                <input
                  type="text"
                  value={outcomes[item.label] ?? ''}
                  onChange={(e) => setOutcomes((s) => ({ ...s, [item.label]: e.target.value }))}
                  placeholder="What does success look like? e.g. “a confirmation appears”"
                  className="mt-1.5 w-full rounded-lg bg-inset ring-1 ring-line focus:ring-amber-500/60 px-3 py-1.5 text-xs text-ink outline-none transition"
                />
              </SeedRow>
            ))}
          </div>
        )}

        {/* ── auto-handled fields (collapsed) ──────────────────────────────── */}
        {(groups.auto.length > 0 || groups.observe.length > 0) && (
          <div className="mt-4">
            <button
              type="button"
              onClick={() => setShowAll((s) => !s)}
              className="text-2xs text-ink-low hover:text-ink transition inline-flex items-center gap-1"
            >
              {showAll ? 'Hide' : 'Show'} the {groups.auto.length + groups.observe.length} fields the
              product handles automatically
            </button>
            {showAll && (
              <div className="mt-2 grid grid-cols-1 sm:grid-cols-2 gap-1.5">
                {[...groups.auto, ...groups.observe].map((item) => (
                  <div
                    key={item.label}
                    className="flex items-center justify-between gap-2 rounded-lg bg-inset ring-1 ring-line px-2.5 py-1.5"
                  >
                    <div className="min-w-0">
                      <div className="text-2xs font-medium text-ink truncate">{item.label}</div>
                      <div className="text-2xs text-ink-low truncate">
                        {item.disposition === 'OBSERVE'
                          ? 'read & asserted'
                          : item.default ?? DISPOSITION_META[item.disposition].blurb}
                      </div>
                    </div>
                    <Badge disposition={item.disposition} />
                  </div>
                ))}
              </div>
            )}
          </div>
        )}

        {/* ── actions ──────────────────────────────────────────────────────── */}
        <div className="mt-4 flex items-center gap-2">
          <Button
            variant="primary"
            loading={saving === 'recrawl'}
            disabled={saving !== null || !canSubmit}
            onClick={() => save(true)}
          >
            Save &amp; re-crawl
          </Button>
          <Button
            variant="secondary"
            loading={saving === 'save'}
            disabled={saving !== null || !canSubmit}
            onClick={() => save(false)}
          >
            Save only
          </Button>
          <span className="text-2xs text-ink-low">
            re-crawl drives the seeded flows end-to-end
          </span>
        </div>
      </Panel>
    </div>
  );
}

/** One field row: badge + label + why, with the caller's input rendered on the right. */
function SeedRow({ item, children }: { item: SeedItem; children: React.ReactNode }) {
  const m = DISPOSITION_META[item.disposition];
  const Icon = m.icon;
  return (
    <div className="rounded-lg ring-1 ring-line bg-inset/60 px-3 py-2.5">
      <div className="flex items-center gap-2 mb-1.5">
        <Icon size={13} className={cn('shrink-0', item.disposition === 'ASK' ? 'text-crit' : 'text-amber-500')} aria-hidden />
        <span className="text-xs font-semibold text-ink">{item.label}</span>
        {item.required && <Pill tone="neutral" size="sm">required</Pill>}
        <Badge disposition={item.disposition} />
        <span className="text-2xs text-ink-low ml-auto hidden sm:inline">{m.blurb}</span>
      </div>
      {children}
    </div>
  );
}
