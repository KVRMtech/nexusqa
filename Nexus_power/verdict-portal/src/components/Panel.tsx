/** Panel — the base surface container (card). Tone selects the elevation. */
import type { ElementType, ReactNode } from 'react';

import { cn } from '../lib/format';

export type PanelTone = 'default' | 'elevated' | 'inset' | 'ghost';

export interface PanelProps {
  as?: ElementType;
  tone?: PanelTone;
  /** Adds a hover lift + border brighten (for clickable cards). */
  interactive?: boolean;
  glow?: 'teal' | 'gold' | 'none';
  padded?: boolean;
  className?: string;
  children: ReactNode;
  /** Extra props forwarded to the underlying element (e.g. `to` for a Link,
   *  `href`, `onClick`, aria-*). Lets Panel be rendered as any tag via `as`. */
  [key: string]: unknown;
}

const TONE: Record<PanelTone, string> = {
  // White cards on a soft navy-tinted drop shadow — Video's .card / .card-glow.
  // Every solid card carries elevation (Video eliminated flat outlines); wells
  // and ghosts stay shadowless.
  default: 'bg-panel ring-1 ring-line shadow-card',
  elevated: 'bg-panel ring-1 ring-line-strong shadow-card-hover',
  inset: 'bg-inset ring-1 ring-line',
  ghost: 'bg-transparent ring-1 ring-line',
};

const GLOW: Record<'teal' | 'gold' | 'none', string> = {
  // Soft elevation instead of a colored glow halo (a dark-UI aesthetic); the
  // gold cue is carried by a hairline ring, matching Video's stat-card accent.
  teal: 'shadow-card-hover',
  gold: 'shadow-card-hover ring-1 ring-gold/30',
  none: '',
};

export function Panel({
  as,
  tone = 'default',
  interactive = false,
  glow = 'none',
  padded = true,
  className,
  children,
  ...rest
}: PanelProps) {
  const Tag = as ?? 'div';
  return (
    <Tag
      {...rest}
      className={cn(
        'rounded-xl',
        TONE[tone],
        GLOW[glow],
        padded && 'p-4',
        interactive &&
          'transition-colors transition-shadow hover:ring-line-strong hover:bg-panel-2 focus-within:ring-teal/40 cursor-pointer',
        className,
      )}
    >
      {children}
    </Tag>
  );
}

export default Panel;
