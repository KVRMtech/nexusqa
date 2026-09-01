// ═══════════════════════════════════════════════════════════════
//  VKPOWER AI ENGINE FACTORY — Stat Card Component
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
          <p className="text-[11px] font-semibold text-slate-500 uppercase tracking-wider truncate">
            {label}
          </p>
          <div className="mt-2 flex items-baseline gap-1.5">
            <span className="text-3xl font-bold text-[#0a2540] tabular-nums tracking-tight">{value}</span>
            {suffix && <span className="text-sm text-slate-500">{suffix}</span>}
          </div>
          {change && (
            <span
              className={clsx('mt-1 inline-block text-xs font-semibold', {
                'text-green-400': changeType === 'positive',
                'text-red-400': changeType === 'negative',
                'text-slate-500': changeType === 'neutral',
              })}
            >
              {change}
            </span>
          )}
          {description && (
            <p className="mt-1 text-xs text-slate-400 truncate">{description}</p>
          )}
        </div>
        {icon && (
          <div className="flex h-11 w-11 shrink-0 items-center justify-center rounded-xl bg-gradient-to-br from-nexus-50 to-nexus-100 text-nexus-700 ring-1 ring-nexus-200/60 group-hover:ring-gold-400/40 transition-all duration-200">
            {icon}
          </div>
        )}
      </div>
    </div>
  );
}
