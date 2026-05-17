// ═══════════════════════════════════════════════════════════════
//  NEXUS AI ENGINE FACTORY — Progress Bar Component
// ═══════════════════════════════════════════════════════════════
import clsx from 'clsx';

interface ProgressBarProps {
  /** Percentage (0-100) */
  value: number;
  /** Color variant */
  variant?: 'nexus' | 'green' | 'yellow' | 'red' | 'blue';
  /** Bar height */
  size?: 'sm' | 'md' | 'lg';
  /** Show percentage label */
  showLabel?: boolean;
  /** Whether to animate the bar */
  animate?: boolean;
  /** Label text override (instead of percentage) */
  label?: string;
  className?: string;
}

const colorMap = {
  nexus: 'bg-nexus-500',
  green: 'bg-green-500',
  yellow: 'bg-yellow-500',
  red: 'bg-red-500',
  blue: 'bg-blue-500',
};

const sizeMap = {
  sm: 'h-1.5',
  md: 'h-2.5',
  lg: 'h-4',
};

export function ProgressBar({
  value,
  variant = 'nexus',
  size = 'md',
  showLabel = false,
  animate = true,
  label,
  className,
}: ProgressBarProps) {
  const clamped = Math.min(100, Math.max(0, value));

  return (
    <div className={clsx('w-full', className)}>
      {showLabel && (
        <div className="flex justify-between text-xs text-slate-500 mb-1">
          <span>{label || 'Progress'}</span>
          <span className="tabular-nums">{Math.round(clamped)}%</span>
        </div>
      )}
      <div className={clsx('w-full rounded-full bg-white/[0.06] overflow-hidden', sizeMap[size])}>
        <div
          className={clsx(
            'h-full rounded-full transition-all',
            colorMap[variant],
            animate && 'duration-700 ease-out',
          )}
          style={{ width: `${clamped}%` }}
          role="progressbar"
          aria-valuenow={clamped}
          aria-valuemin={0}
          aria-valuemax={100}
        />
      </div>
    </div>
  );
}
