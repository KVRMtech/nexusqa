/**
 * Bar — a labeled horizontal meter, used for per-band autonomy. `null` renders a
 * hatched "n/a" track (no denominator ⇒ not computable), never a misleading 0.
 */
import type { ReactNode } from 'react';

import { cn } from '../lib/format';
import { formatPct } from '../lib/format';

export type BarTone = 'teal' | 'good' | 'warn' | 'crit' | 'gold' | 'neutral';

export interface BarProps {
  value: number | null;
  label?: ReactNode;
  tone?: BarTone;
  autoTone?: boolean;
  /** Right-aligned trailing content (defaults to the formatted percentage). */
  right?: ReactNode;
  showValue?: boolean;
  height?: number;
  className?: string;
}

const TONE_VAR: Record<BarTone, string> = {
  teal: '--teal',
  good: '--good',
  warn: '--warn',
  crit: '--crit',
  gold: '--gold',
  neutral: '--ink-low',
};

function pickTone(value: number | null): BarTone {
  if (value === null) return 'neutral';
  if (value >= 99) return 'good';
  if (value >= 90) return 'teal';
  if (value >= 75) return 'warn';
  return 'crit';
}

export function Bar({
  value,
  label,
  tone = 'teal',
  autoTone,
  right,
  showValue = true,
  height = 6,
  className,
}: BarProps) {
  const resolved = autoTone ? pickTone(value) : tone;
  const color = `rgb(var(${TONE_VAR[resolved]}))`;
  const na = value === null;
  const clamped = na ? 0 : Math.max(0, Math.min(100, value));

  return (
    <div className={cn('w-full', className)}>
      {(label != null || showValue || right != null) && (
        <div className="flex items-baseline justify-between mb-1 gap-2">
          {label != null && <span className="text-2xs text-ink-low uppercase tracking-wide">{label}</span>}
          {right ?? (showValue && <span className={cn('text-2xs tabular', na ? 'text-ink-faint' : 'text-ink-mid')}>{formatPct(value)}</span>)}
        </div>
      )}
      <div
        className="w-full rounded-full bg-line-strong/60 overflow-hidden"
        style={{ height }}
        role="meter"
        aria-valuenow={na ? undefined : Math.round(clamped)}
        aria-valuemin={na ? undefined : 0}
        aria-valuemax={na ? undefined : 100}
        aria-label={typeof label === 'string' ? `${label} autonomy ${formatPct(value)}` : `autonomy ${formatPct(value)}`}
      >
        {na ? (
          <div
            className="h-full w-full opacity-50"
            style={{
              backgroundImage:
                'repeating-linear-gradient(45deg, rgb(var(--ink-faint)) 0 4px, transparent 4px 8px)',
            }}
          />
        ) : (
          <div
            className="h-full rounded-full"
            style={{ width: `${clamped}%`, backgroundColor: color, transition: 'width 600ms cubic-bezier(0.22,1,0.36,1)' }}
          />
        )}
      </div>
    </div>
  );
}

export default Bar;
