/**
 * HONESTY FEED — refusals as the hero. A product that refuses to green-wash is
 * only credible if the refusals are the loudest thing in the room. This surfaces
 * every `refused` verdict (and the autonomous `healed` saves) with its honest
 * reason.
 *
 * Scaffold note: complete + running. The `honesty` feature agent owns deeper
 * treatment (per-rule REFUSE-matrix drill-in, trends). Export `HonestyFeed`.
 */
import { HeartHandshake, ShieldX, Sparkles } from 'lucide-react';

import { api } from '../../lib/api';
import { useAsync } from '../../lib/useAsync';
import { cn, timeAgo } from '../../lib/format';
import { EmptyState, ErrorState, Panel, SectionHead, SkeletonRows } from '../../components';
import type { VerdictLedgerEntry } from '../../types/qec';

export interface HonestyFeedProps {
  appId?: string;
  className?: string;
}

function RefusalRow({ entry }: { entry: VerdictLedgerEntry }) {
  const healed = entry.kind === 'healed';
  return (
    <li className="py-3 first:pt-0 last:pb-0 flex gap-3">
      <span className={cn('mt-0.5 shrink-0', healed ? 'text-healed' : 'text-refused')}>
        {healed ? <Sparkles size={16} aria-hidden /> : <ShieldX size={16} aria-hidden />}
      </span>
      <div className="min-w-0 flex-1">
        <div className="flex items-baseline justify-between gap-2">
          <p className="text-sm text-ink truncate">{entry.subject}</p>
          <time className="shrink-0 text-2xs text-ink-faint tabular" dateTime={entry.ts}>
            {timeAgo(entry.ts)}
          </time>
        </div>
        {entry.app_name && <p className="text-2xs text-ink-low mt-0.5 truncate">{entry.app_name}</p>}
        {entry.reason && (
          <p className={cn('mt-1 text-xs leading-snug', healed ? 'text-ink-mid' : 'text-refused/90')}>{entry.reason}</p>
        )}
      </div>
    </li>
  );
}

export function HonestyFeed({ appId, className }: HonestyFeedProps) {
  const state = useAsync((signal) => api.getLedger({ appId, limit: 60 }, { signal }), [appId]);
  const entries = (state.data?.entries ?? []).filter((e) => e.kind === 'refused' || e.kind === 'healed');
  const refusals = entries.filter((e) => e.kind === 'refused').length;

  return (
    <Panel tone="elevated" className={cn('flex flex-col', className)}>
      <SectionHead
        title="Honesty Feed"
        subtitle="refusals are the hero · 0 green-washed"
        icon={<HeartHandshake size={16} className="text-refused" />}
        right={
          state.data && (
            <span className="text-2xs text-ink-low tabular">
              <span className="text-refused font-semibold">{refusals}</span> refused
            </span>
          )
        }
      />

      <div className="mt-3 flex-1">
        {state.isLoading && <SkeletonRows rows={4} />}
        {state.isError && <ErrorState error={state.error} onRetry={state.reload} />}
        {state.isSuccess &&
          (entries.length > 0 ? (
            <ul className="divide-y divide-line">
              {entries.map((entry) => (
                <RefusalRow key={entry.id} entry={entry} />
              ))}
            </ul>
          ) : (
            <EmptyState
              title="Nothing refused — and nothing green-washed"
              hint="When the product refuses to certify (ungrounded locators, shrinking coverage, a budget breach) it lands here, loud."
            />
          ))}
      </div>
    </Panel>
  );
}

export default HonestyFeed;
