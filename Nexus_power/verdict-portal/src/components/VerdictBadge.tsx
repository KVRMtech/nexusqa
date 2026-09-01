/**
 * VerdictBadge — the canonical rendering of a ledger verdict kind
 * (certified | refused | healed): the seal mark + a toned pill. Keeps the three
 * verdict states visually identical everywhere they appear.
 */
import type { VerdictKind } from '../types/qec';
import { Pill } from './Pill';
import type { PillSize, PillVariant } from './Pill';
import { Seal } from './Seal';

const LABEL: Record<VerdictKind, string> = {
  certified: 'Certified',
  refused: 'Refused',
  healed: 'Healed',
};

const TONE: Record<VerdictKind, 'certified' | 'refused' | 'healed'> = {
  certified: 'certified',
  refused: 'refused',
  healed: 'healed',
};

export interface VerdictBadgeProps {
  kind: VerdictKind;
  size?: PillSize;
  variant?: PillVariant;
  withSeal?: boolean;
  className?: string;
}

export function VerdictBadge({ kind, size = 'md', variant = 'soft', withSeal = true, className }: VerdictBadgeProps) {
  return (
    <Pill
      tone={TONE[kind]}
      size={size}
      variant={variant}
      uppercase
      className={className}
      icon={withSeal ? <Seal size={size === 'sm' ? 12 : 14} tone={kind} title="" /> : undefined}
    >
      {LABEL[kind]}
    </Pill>
  );
}

export default VerdictBadge;
