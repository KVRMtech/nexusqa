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
  default: 'bg-panel ring-1 ring-line',
  elevated: 'bg-panel-2 ring-1 ring-line-strong shadow-panel',
  inset: 'bg-inset ring-1 ring-line',
  ghost: 'bg-transparent ring-1 ring-line',
};

const GLOW: Record<'teal' | 'gold' | 'none', string> = {
  teal: 'shadow-glow-teal',
  gold: 'shadow-glow-gold',
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
