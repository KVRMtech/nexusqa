/** StatusDot — a small state indicator; `pulse` marks a live/active state. */
import { cn } from '../lib/format';

export type DotTone = 'good' | 'warn' | 'crit' | 'teal' | 'gold' | 'neutral' | 'certified' | 'refused' | 'healed';

export interface StatusDotProps {
  tone?: DotTone;
  pulse?: boolean;
  size?: number;
  /** Accessible label; when omitted the dot is decorative. */
  label?: string;
  className?: string;
}

const TONE_VAR: Record<DotTone, string> = {
  good: '--good',
  warn: '--warn',
  crit: '--crit',
  teal: '--teal',
  gold: '--gold',
  neutral: '--ink-low',
  certified: '--verdict-certified',
  refused: '--verdict-refused',
  healed: '--verdict-healed',
};

export function StatusDot({ tone = 'neutral', pulse = false, size = 8, label, className }: StatusDotProps) {
  const color = `rgb(var(${TONE_VAR[tone]}))`;
  return (
    <span
      className={cn('relative inline-flex shrink-0', className)}
      style={{ width: size, height: size }}
      role={label ? 'img' : 'presentation'}
      aria-label={label}
      aria-hidden={label ? undefined : true}
    >
      {pulse && (
        <span
          className="absolute inset-0 rounded-full animate-pulse-dot"
          style={{ backgroundColor: color, opacity: 0.55 }}
        />
      )}
      <span className="relative inline-block rounded-full" style={{ width: size, height: size, backgroundColor: color }} />
    </span>
  );
}

export default StatusDot;
