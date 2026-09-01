/**
 * Gauge — the autonomy ring. A circular meter for a 0–100 value; `null` renders
 * an honest "n/a" with a dashed track (never a misleading 0 or full ring), which
 * matches the per-band autonomy contract (no denominator ⇒ not computable).
 */
import type { ReactNode } from 'react';

import { cn } from '../lib/format';

export type GaugeTone = 'teal' | 'good' | 'warn' | 'crit' | 'gold' | 'certified';

export interface GaugeProps {
  /** 0–100, or null for "not computable". */
  value: number | null;
  size?: number;
  thickness?: number;
  tone?: GaugeTone;
  /** Auto-pick the tone from the value (autonomy thresholds). Overrides `tone`. */
  autoTone?: boolean;
  label?: ReactNode;
  sublabel?: ReactNode;
  className?: string;
  ariaLabel?: string;
}

const TONE_VAR: Record<GaugeTone, string> = {
  teal: '--teal',
  good: '--good',
  warn: '--warn',
  crit: '--crit',
  gold: '--gold',
  certified: '--verdict-certified',
};

function pickTone(value: number | null): GaugeTone {
  if (value === null) return 'teal';
  if (value >= 99) return 'good';
  if (value >= 90) return 'teal';
  if (value >= 75) return 'warn';
  return 'crit';
}

export function Gauge({
  value,
  size = 92,
  thickness = 8,
  tone = 'teal',
  autoTone,
  label,
  sublabel,
  className,
  ariaLabel,
}: GaugeProps) {
  const resolvedTone = autoTone ? pickTone(value) : tone;
  const color = `rgb(var(${TONE_VAR[resolvedTone]}))`;
  const r = (size - thickness) / 2;
  const circ = 2 * Math.PI * r;
  const clamped = value === null ? 0 : Math.max(0, Math.min(100, value));
  const offset = circ * (1 - clamped / 100);
  const na = value === null;
  const valueText = na ? 'n/a' : `${Math.round(value)}%`;

  return (
    <div
      className={cn('relative inline-flex items-center justify-center', className)}
      style={{ width: size, height: size }}
      role={na ? 'img' : 'meter'}
      aria-label={ariaLabel ?? (na ? 'autonomy not computable' : `autonomy ${valueText}`)}
      aria-valuenow={na ? undefined : Math.round(value)}
      aria-valuemin={na ? undefined : 0}
      aria-valuemax={na ? undefined : 100}
    >
      <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`} className="-rotate-90">
        <circle
          cx={size / 2}
          cy={size / 2}
          r={r}
          fill="none"
          stroke="rgb(var(--line-strong))"
          strokeWidth={thickness}
          strokeDasharray={na ? '3 5' : undefined}
          strokeLinecap="round"
        />
        {!na && (
          <circle
            cx={size / 2}
            cy={size / 2}
            r={r}
            fill="none"
            stroke={color}
            strokeWidth={thickness}
            strokeLinecap="round"
            strokeDasharray={circ}
            strokeDashoffset={offset}
            style={{ transition: 'stroke-dashoffset 700ms cubic-bezier(0.22,1,0.36,1)' }}
          />
        )}
      </svg>
      <div className="absolute inset-0 flex flex-col items-center justify-center text-center leading-tight">
        <span
          className={cn('tabular font-semibold', na ? 'text-ink-low' : 'text-ink')}
          style={{ fontSize: size * 0.24, color: na ? undefined : color }}
        >
          {valueText}
        </span>
        {label != null && <span className="text-2xs text-ink-low mt-0.5">{label}</span>}
        {sublabel != null && <span className="text-2xs text-ink-faint">{sublabel}</span>}
      </div>
    </div>
  );
}

export default Gauge;
