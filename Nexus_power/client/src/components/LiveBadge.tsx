import clsx from 'clsx';
import { Wifi, WifiOff } from 'lucide-react';

/**
 * Small badge that shows whether the page is displaying live API data
 * or an unavailable backend state.
 */
export function LiveBadge({ isLive, className }: { isLive: boolean; className?: string }) {
  return (
    <span
      className={clsx(
        'inline-flex items-center gap-1.5 rounded-full px-2.5 py-0.5 text-[10px] font-semibold uppercase tracking-wider',
        isLive
          ? 'bg-green-500/10 text-green-400 ring-1 ring-green-500/20'
          : 'bg-yellow-500/10 text-yellow-400 ring-1 ring-yellow-500/20',
        className,
      )}
    >
      {isLive ? <Wifi className="h-3 w-3" /> : <WifiOff className="h-3 w-3" />}
      {isLive ? 'Live' : 'Offline'}
    </span>
  );
}

export default LiveBadge;
