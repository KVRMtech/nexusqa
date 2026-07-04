// ═══════════════════════════════════════════════════════════════
//  VKPOWER AI ENGINE FACTORY — Filter Bar Component
// ═══════════════════════════════════════════════════════════════
import clsx from 'clsx';

export interface FilterOption {
  label: string;
  value: string;
  /** Count badge */
  count?: number;
}

interface FilterBarProps {
  options: FilterOption[];
  value: string;
  onChange: (value: string) => void;
  /** Include an "All" option at the start */
  showAll?: boolean;
  allLabel?: string;
  className?: string;
}

export function FilterBar({
  options,
  value,
  onChange,
  showAll = true,
  allLabel = 'All',
  className,
}: FilterBarProps) {
  const allOptions: FilterOption[] = showAll
    ? [{ label: allLabel, value: 'all' }, ...options]
    : options;

  return (
    <div className={clsx('flex flex-wrap items-center gap-1.5', className)}>
      {allOptions.map((opt) => {
        const active = value === opt.value;
        return (
          <button
            key={opt.value}
            onClick={() => onChange(opt.value)}
            className={clsx(
              'inline-flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-xs font-medium transition-all duration-150',
              active
                ? 'bg-nexus-500/15 text-nexus-400 ring-1 ring-nexus-500/25'
                : 'text-slate-500 hover:bg-white/[0.04] hover:text-slate-700',
            )}
          >
            {opt.label}
            {typeof opt.count === 'number' && (
              <span
                className={clsx(
                  'rounded-full px-1.5 py-0.5 text-[10px] font-bold tabular-nums',
                  active ? 'bg-nexus-500/20 text-nexus-300' : 'bg-white/[0.06] text-slate-400',
                )}
              >
                {opt.count}
              </span>
            )}
          </button>
        );
      })}
    </div>
  );
}
