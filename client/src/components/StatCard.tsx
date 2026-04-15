// ═══════════════════════════════════════════════════════════════
//  NEXUS AI ENGINE FACTORY — Stat Card Component
// ═══════════════════════════════════════════════════════════════
import type { ReactNode } from 'react';
import clsx from 'clsx';

interface StatCardProps {
  /** The metric label (e.g., "Knowledge Captured") */
  label: string;
  /** The primary value to display */
  value: string | number;
  /** Optional unit or suffix (e.g., "%", "hrs") */
  suffix?: string;
  /** Icon component shown on the left */
  icon?: ReactNode;
  /** Change indicator (e.g., "+12%") */
  change?: string;
  /** Whether the change is positive/negative */
  changeType?: 'positive' | 'negative' | 'neutral';
  /** Optional description beneath the value */
  description?: string;
  /** Additional CSS classes */
  className?: string;
  /** Click handler for the card */
  onClick?: () => void;
}

export function StatCard({
  label,
  value,
  suffix,
  icon,
  change,
  changeType = 'neutral',
  description,
  className,
  onClick,
}: StatCardProps) {
  return (
    <div
      className={clsx(
        'stat-card group',
        onClick && 'cursor-pointer hover:ring-nexus-500/30 hover:shadow-nexus-500/10',
        className,
      )}
      onClick={onClick}
      role={onClick ? 'button' : undefined}
      tabIndex={onClick ? 0 : undefined}
    >
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0 flex-1">
          <p className="text-xs font-medium text-gray-500 uppercase tracking-wider truncate">
            {label}
          </p>
          <div className="mt-1.5 flex items-baseline gap-1.5">
            <span className="text-2xl font-bold text-white tabular-nums">{value}</span>
            {suffix && <span className="text-sm text-gray-400">{suffix}</span>}
          </div>
          {change && (
            <span
              className={clsx('mt-1 inline-block text-xs font-semibold', {
                'text-green-400': changeType === 'positive',
                'text-red-400': changeType === 'negative',
                'text-gray-400': changeType === 'neutral',
              })}
            >
              {change}
            </span>
          )}
          {description && (
            <p className="mt-1 text-xs text-gray-500 truncate">{description}</p>
          )}
        </div>
        {icon && (
          <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-lg bg-white/[0.04] text-gray-400 group-hover:text-nexus-400 transition-colors">
            {icon}
          </div>
        )}
      </div>
    </div>
  );
}
