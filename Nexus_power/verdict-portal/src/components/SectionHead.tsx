/** SectionHead — a titled section header row with optional icon + right actions. */
import type { ReactNode } from 'react';

import { cn } from '../lib/format';

export interface SectionHeadProps {
  title: ReactNode;
  subtitle?: ReactNode;
  icon?: ReactNode;
  /** Right-aligned actions (buttons, pills, filters). */
  right?: ReactNode;
  /** Heading level for the title (accessibility). Defaults to h2. */
  as?: 'h1' | 'h2' | 'h3' | 'h4';
  className?: string;
}

export function SectionHead({ title, subtitle, icon, right, as = 'h2', className }: SectionHeadProps) {
  const Heading = as;
  return (
    <div className={cn('flex items-start justify-between gap-3', className)}>
      <div className="flex items-start gap-2.5 min-w-0">
        {icon != null && <span className="mt-0.5 shrink-0 text-ink-mid">{icon}</span>}
        <div className="min-w-0">
          <Heading className="text-sm font-semibold text-ink tracking-tight truncate">{title}</Heading>
          {subtitle != null && <p className="text-2xs text-ink-low mt-0.5 leading-snug">{subtitle}</p>}
        </div>
      </div>
      {right != null && <div className="shrink-0 flex items-center gap-2">{right}</div>}
    </div>
  );
}

export default SectionHead;
