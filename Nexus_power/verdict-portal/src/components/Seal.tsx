/**
 * Seal — the VKPower Verdict brand mark: a checkmark inside a jagged seal.
 * The jagged ring is generated procedurally so it stays crisp at any size. Tone
 * recolors the ring + inner glyph; `refused` swaps the check for a cross.
 */
import { useMemo } from 'react';

export type SealTone = 'certified' | 'healed' | 'refused' | 'teal' | 'gold' | 'muted';

export interface SealProps {
  size?: number;
  tone?: SealTone;
  /** Accessible label (rendered as <title>); pass '' for purely decorative. */
  title?: string;
  className?: string;
  strokeWidth?: number;
}

const TONE: Record<SealTone, { ring: string; glyph: string }> = {
  certified: { ring: 'var(--teal)', glyph: 'var(--gold)' },
  teal: { ring: 'var(--teal)', glyph: 'var(--teal)' },
  gold: { ring: 'var(--gold)', glyph: 'var(--gold)' },
  healed: { ring: 'var(--gold)', glyph: 'var(--gold)' },
  refused: { ring: 'var(--crit)', glyph: 'var(--crit)' },
  muted: { ring: 'var(--ink-low)', glyph: 'var(--ink-low)' },
};

/** Build the jagged (scalloped) seal ring path on a 64×64 grid. */
function ringPath(teeth = 12, outer = 27, inner = 22.5, cx = 32, cy = 32): string {
  const total = teeth * 2;
  const pts: string[] = [];
  for (let i = 0; i < total; i++) {
    const ang = (Math.PI * 2 * i) / total - Math.PI / 2;
    const r = i % 2 === 0 ? outer : inner;
    pts.push(`${(cx + r * Math.cos(ang)).toFixed(2)} ${(cy + r * Math.sin(ang)).toFixed(2)}`);
  }
  return `M${pts.join(' L ')} Z`;
}

export function Seal({ size = 28, tone = 'certified', title = 'Certified seal', className, strokeWidth = 2.4 }: SealProps) {
  const ring = useMemo(() => ringPath(), []);
  const colors = TONE[tone];
  const decorative = title === '';

  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 64 64"
      fill="none"
      className={className}
      role={decorative ? 'presentation' : 'img'}
      aria-hidden={decorative || undefined}
      aria-label={decorative ? undefined : title}
    >
      {!decorative && <title>{title}</title>}
      <path d={ring} fill="none" stroke={`rgb(${colors.ring})`} strokeWidth={strokeWidth} strokeLinejoin="round" />
      <circle cx="32" cy="32" r="17.5" fill="none" stroke={`rgb(${colors.ring})`} strokeWidth="1" strokeOpacity={0.4} />
      {tone === 'refused' ? (
        <path
          d="M25 25 L 39 39 M39 25 L 25 39"
          stroke={`rgb(${colors.glyph})`}
          strokeWidth={4}
          strokeLinecap="round"
        />
      ) : (
        <path
          d="M22.5 33 L 29.2 40 L 43 24.5"
          fill="none"
          stroke={`rgb(${colors.glyph})`}
          strokeWidth={4.2}
          strokeLinecap="round"
          strokeLinejoin="round"
        />
      )}
    </svg>
  );
}

export default Seal;
