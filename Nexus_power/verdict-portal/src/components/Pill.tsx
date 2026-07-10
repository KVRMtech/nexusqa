/** Pill — a compact status / verdict badge. Tone carries the meaning. */
import type { ReactNode } from 'react';

import { cn } from '../lib/format';

export type PillTone =
  | 'certified'
  | 'refused'
  | 'healed'
  | 'good'
  | 'warn'
  | 'crit'
  | 'teal'
  | 'gold'
  | 'neutral';

export type PillVariant = 'soft' | 'solid' | 'outline';
export type PillSize = 'sm' | 'md';

export interface PillProps {
  tone?: PillTone;
  variant?: PillVariant;
  size?: PillSize;
  icon?: ReactNode;
  uppercase?: boolean;
  mono?: boolean;
  className?: string;
  children: ReactNode;
}

const SOFT: Record<PillTone, string> = {
  certified: 'text-certified bg-certified/12 ring-certified/25',
  refused: 'text-refused bg-refused/12 ring-refused/25',
  healed: 'text-healed bg-healed/12 ring-healed/25',
  good: 'text-good bg-good/12 ring-good/25',
  warn: 'text-warn bg-warn/12 ring-warn/25',
  crit: 'text-crit bg-crit/12 ring-crit/25',
  teal: 'text-teal bg-teal/12 ring-teal/25',
  gold: 'text-gold bg-gold/12 ring-gold/25',
  neutral: 'text-ink-mid bg-ink/[0.05] ring-line',
};

const SOLID: Record<PillTone, string> = {
  certified: 'text-bg bg-certified ring-certified',
  refused: 'text-bg bg-refused ring-refused',
  healed: 'text-bg bg-healed ring-healed',
  good: 'text-bg bg-good ring-good',
  warn: 'text-bg bg-warn ring-warn',
  crit: 'text-bg bg-crit ring-crit',
  teal: 'text-bg bg-teal ring-teal',
  gold: 'text-bg bg-gold ring-gold',
  neutral: 'text-ink bg-panel-2 ring-line-strong',
};

const OUTLINE: Record<PillTone, string> = {
  certified: 'text-certified bg-transparent ring-certified/50',
  refused: 'text-refused bg-transparent ring-refused/50',
  healed: 'text-healed bg-transparent ring-healed/50',
  good: 'text-good bg-transparent ring-good/50',
  warn: 'text-warn bg-transparent ring-warn/50',
  crit: 'text-crit bg-transparent ring-crit/50',
  teal: 'text-teal bg-transparent ring-teal/50',
  gold: 'text-gold bg-transparent ring-gold/50',
  neutral: 'text-ink-mid bg-transparent ring-line',
};

const SIZE: Record<PillSize, string> = {
  sm: 'text-2xs px-2 py-[3px] gap-1',
  md: 'text-xs px-2.5 py-1 gap-1.5',
};

export function Pill({
  tone = 'neutral',
  variant = 'soft',
  size = 'md',
  icon,
  uppercase,
  mono,
  className,
  children,
}: PillProps) {
  const palette = variant === 'solid' ? SOLID : variant === 'outline' ? OUTLINE : SOFT;
  return (
    <span
      className={cn(
        'inline-flex items-center rounded-full font-semibold ring-1 leading-none whitespace-nowrap',
        SIZE[size],
        palette[tone],
        uppercase && 'uppercase tracking-wide',
        mono && 'font-mono tabular',
        className,
      )}
    >
      {icon != null && <span className="shrink-0 inline-flex items-center">{icon}</span>}
      {children}
    </span>
  );
}

export default Pill;
