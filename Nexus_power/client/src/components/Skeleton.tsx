// ═══════════════════════════════════════════════════════════════
//  VKPOWER AI ENGINE FACTORY — Loading Skeleton Component
// ═══════════════════════════════════════════════════════════════
import clsx from 'clsx';

interface SkeletonProps {
  className?: string;
  /** Width (e.g., "w-24", "w-full") */
  width?: string;
  /** Height (e.g., "h-4", "h-8") */
  height?: string;
  /** Make it circular */
  circle?: boolean;
}

/**
 * Animated skeleton placeholder for loading states.
 */
export function Skeleton({ className, width = 'w-full', height = 'h-4', circle }: SkeletonProps) {
  return (
    <div
      className={clsx(
        'animate-pulse bg-white/[0.06] rounded',
        circle ? 'rounded-full' : 'rounded-lg',
        width,
        height,
        className,
      )}
    />
  );
}

/** Pre-built skeleton layouts for common patterns */

export function StatCardSkeleton() {
  return (
    <div className="stat-card space-y-3">
      <Skeleton width="w-24" height="h-3" />
      <Skeleton width="w-16" height="h-7" />
      <Skeleton width="w-32" height="h-3" />
    </div>
  );
}

export function TableRowSkeleton({ cols = 4 }: { cols?: number }) {
  return (
    <tr>
      {Array.from({ length: cols }).map((_, i) => (
        <td key={i} className="px-4 py-3">
          <Skeleton width={i === 0 ? 'w-32' : 'w-20'} height="h-4" />
        </td>
      ))}
    </tr>
  );
}

export function CardSkeleton() {
  return (
    <div className="card p-5 space-y-3">
      <div className="flex items-center gap-3">
        <Skeleton circle width="w-10" height="h-10" />
        <div className="flex-1 space-y-2">
          <Skeleton width="w-32" height="h-4" />
          <Skeleton width="w-48" height="h-3" />
        </div>
      </div>
      <Skeleton height="h-3" />
      <Skeleton width="w-3/4" height="h-3" />
    </div>
  );
}

export function PageSkeleton() {
  return (
    <div className="space-y-6 animate-[fade-in_0.2s_ease-out]">
      {/* Header skeleton */}
      <div className="space-y-2">
        <Skeleton width="w-24" height="h-3" />
        <Skeleton width="w-48" height="h-7" />
        <Skeleton width="w-64" height="h-4" />
      </div>
      {/* Stat cards */}
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
        <StatCardSkeleton />
        <StatCardSkeleton />
        <StatCardSkeleton />
        <StatCardSkeleton />
      </div>
      {/* Content */}
      <div className="grid gap-4 lg:grid-cols-2">
        <CardSkeleton />
        <CardSkeleton />
      </div>
    </div>
  );
}
