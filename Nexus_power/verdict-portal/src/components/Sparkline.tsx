/**
 * Sparkline — a tiny inline trend line (hand-rolled SVG for crispness + null-gap
 * support). `null` values break the line (an honest gap, not a zero). Used in the
 * fleet tiles and situation panels.
 */
import { useId } from 'react';

import { cn } from '../lib/format';

export type SparkTone = 'teal' | 'good' | 'warn' | 'crit' | 'gold';

export interface SparklineProps {
  data: Array<number | null>;
  width?: number;
  height?: number;
  tone?: SparkTone;
  strokeWidth?: number;
  fill?: boolean;
  /** Emphasise the final point with a dot. */
  showDot?: boolean;
  min?: number;
  max?: number;
  className?: string;
  ariaLabel?: string;
}

const TONE_VAR: Record<SparkTone, string> = {
  teal: '--teal',
  good: '--good',
  warn: '--warn',
  crit: '--crit',
  gold: '--gold',
};

export function Sparkline({
  data,
  width = 96,
  height = 28,
  tone = 'teal',
  strokeWidth = 1.75,
  fill = false,
  showDot = true,
  min,
  max,
  className,
  ariaLabel,
}: SparklineProps) {
  const gradId = useId();
  const color = `rgb(var(${TONE_VAR[tone]}))`;
  const nums = data.filter((d): d is number => d !== null && !Number.isNaN(d));

  if (nums.length === 0) {
    return (
      <svg width={width} height={height} className={className} role="img" aria-label={ariaLabel ?? 'no trend data'}>
        <line x1={0} y1={height / 2} x2={width} y2={height / 2} stroke="rgb(var(--line-strong))" strokeDasharray="3 4" />
      </svg>
    );
  }

  const lo = min ?? Math.min(...nums);
  const hi = max ?? Math.max(...nums);
  const span = hi - lo || 1;
  const pad = strokeWidth + 1;
  const stepX = data.length > 1 ? (width - pad * 2) / (data.length - 1) : 0;

  const xy = (v: number, i: number): [number, number] => {
    const x = pad + i * stepX;
    const y = pad + (height - pad * 2) * (1 - (v - lo) / span);
    return [x, y];
  };

  // Build line segments, breaking on nulls.
  let d = '';
  let started = false;
  data.forEach((v, i) => {
    if (v === null || Number.isNaN(v)) {
      started = false;
      return;
    }
    const [x, y] = xy(v, i);
    d += `${started ? ' L' : ' M'} ${x.toFixed(1)} ${y.toFixed(1)}`;
    started = true;
  });

  // Area fill (only for the last contiguous span, from baseline).
  const lastIdx = (() => {
    for (let i = data.length - 1; i >= 0; i--) if (data[i] !== null) return i;
    return -1;
  })();
  const [lastX, lastY] = lastIdx >= 0 ? xy(data[lastIdx] as number, lastIdx) : [0, 0];

  const first = nums[0];
  const last = nums[nums.length - 1];
  const trend = last > first ? 'up' : last < first ? 'down' : 'flat';

  return (
    <svg
      width={width}
      height={height}
      viewBox={`0 0 ${width} ${height}`}
      className={cn('overflow-visible', className)}
      role="img"
      aria-label={ariaLabel ?? `trend ${trend}, latest ${last}`}
    >
      {fill && (
        <>
          <defs>
            <linearGradient id={gradId} x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor={color} stopOpacity={0.22} />
              <stop offset="100%" stopColor={color} stopOpacity={0} />
            </linearGradient>
          </defs>
          <path d={`${d} L ${lastX.toFixed(1)} ${height} L ${pad} ${height} Z`} fill={`url(#${gradId})`} stroke="none" />
        </>
      )}
      <path d={d.trim()} fill="none" stroke={color} strokeWidth={strokeWidth} strokeLinecap="round" strokeLinejoin="round" />
      {showDot && lastIdx >= 0 && <circle cx={lastX} cy={lastY} r={strokeWidth + 0.6} fill={color} />}
    </svg>
  );
}

export default Sparkline;
