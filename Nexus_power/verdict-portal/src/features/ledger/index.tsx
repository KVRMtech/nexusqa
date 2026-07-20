/**
 * VERDICT LEDGER — the signature surface: a hash-chained stream of certified /
 * refused / healed verdicts. There is no single /ledger endpoint; `api.getLedger`
 * composes it (see the type + client docs) and flags entries that carry a real
 * chain link as `verified`.
 *
 * Scaffold note: this is a complete, running component. The `ledger` feature
 * agent owns any deeper interactions (drill-in, chain verification view, export);
 * the export name `VerdictLedger` + its props are the stable contract.
 */
import { Link2, ShieldCheck } from 'lucide-react';

import { api } from '../../lib/api';
import { useAsync } from '../../lib/useAsync';
import { cn, shortHash, timeAgo } from '../../lib/format';
import {
  EmptyState,
  ErrorState,
  Panel,
  Pill,
  SectionHead,
  SkeletonRows,
  VerdictBadge,
} from '../../components';
import type { VerdictLedgerEntry } from '../../types/qec';

export interface VerdictLedgerProps {
  /** Scope the ledger to one app (omit for the whole fleet). */
  appId?: string;
  limit?: number;
  title?: string;
  className?: string;
}

function LedgerRow({ entry }: { entry: VerdictLedgerEntry }) {
  return (
    <li className="py-3 first:pt-0 last:pb-0 flex gap-3">
      <div className="shrink-0 pt-0.5">
        <VerdictBadge kind={entry.kind} size="sm" />
      </div>
      <div className="min-w-0 flex-1">
        <div className="flex items-baseline justify-between gap-2">
          <p className="text-sm text-ink truncate">{entry.subject}</p>
          <time className="shrink-0 text-2xs text-ink-faint tabular" dateTime={entry.ts}>
            {timeAgo(entry.ts)}
          </time>
        </div>

        <div className="flex flex-wrap items-center gap-x-2 gap-y-1 mt-1 text-2xs text-ink-low">
          {entry.app_name && <span className="truncate max-w-[14rem]">{entry.app_name}</span>}
          {entry.band && <Pill tone="neutral" size="sm">{entry.band}</Pill>}
          {entry.signature && (
            <span className="inline-flex items-center gap-1 text-ink-mid">
              <ShieldCheck size={11} className="text-teal" aria-hidden />
              {entry.signature}
            </span>
          )}
        </div>

        {entry.reason && (
          <p className={cn('mt-1 text-2xs leading-snug', entry.kind === 'refused' ? 'text-crit/90' : 'text-ink-low')}>
            {entry.reason}
          </p>
        )}

        {/* hash chain link */}
        <div className="mt-1.5 flex items-center gap-2 text-2xs">
          {entry.verified ? (
            <span className="inline-flex items-center gap-1.5 hash">
              <Link2 size={11} className="text-teal shrink-0" aria-hidden />
              <span title={`prev ${entry.prev_hash}`}>{shortHash(entry.prev_hash)}</span>
              <span className="text-ink-faint">→</span>
              <span className="text-ink-mid" title={`chain ${entry.chain_hash}`}>{shortHash(entry.chain_hash)}</span>
            </span>
          ) : (
            <span className="text-ink-faint italic">composed signal · no chain link</span>
          )}
        </div>
      </div>
    </li>
  );
}

export function VerdictLedger({ appId, limit = 40, title = 'Verdict Ledger', className }: VerdictLedgerProps) {
  const state = useAsync((signal) => api.getLedger({ appId, limit }, { signal }), [appId, limit]);
  const { data } = state;

  // Honest subtitle: the LIVE ledger is COMPOSED from cycle + REFUSE-harness signals
  // that carry no per-row chain link (verified:false) — never claim 'hash-chained'
  // over composed rows. Only a non-composed (chain-linked) stream earns that label.
  const subtitle = data?.composed
    ? 'composed from live cycle + REFUSE-harness verdicts'
    : 'hash-chained · certified / refused / healed';

  return (
    <Panel tone="elevated" className={cn('flex flex-col', className)}>
      <SectionHead
        title={title}
        subtitle={subtitle}
        icon={<Link2 size={16} className="text-teal" />}
        right={
          data && (
            <div className="flex items-center gap-1.5">
              <Pill tone="certified" size="sm">{data.counts.certified}</Pill>
              <Pill tone="refused" size="sm">{data.counts.refused}</Pill>
              <Pill tone="healed" size="sm">{data.counts.healed}</Pill>
            </div>
          )
        }
      />

      <div className="mt-3 flex-1">
        {state.isLoading && <SkeletonRows rows={5} />}
        {state.isError && <ErrorState error={state.error} onRetry={state.reload} />}
        {state.isSuccess &&
          (data && data.entries.length > 0 ? (
            <ul className="divide-y divide-line">
              {data.entries.map((entry) => (
                <LedgerRow key={entry.id} entry={entry} />
              ))}
            </ul>
          ) : (
            <EmptyState title="No verdicts yet" hint="Verdicts appear here as cycles certify, refuse, or heal." />
          ))}
      </div>

      {data?.composed && (
        <p className="mt-3 pt-2.5 border-t border-line text-2xs text-ink-faint leading-snug">{data.note}</p>
      )}
    </Panel>
  );
}

export default VerdictLedger;
