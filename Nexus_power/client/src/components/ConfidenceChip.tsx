// ═══════════════════════════════════════════════════════════════
//  ConfidenceChip — unified quality / confidence badge
// ═══════════════════════════════════════════════════════════════
//
// Replaces 4+ duplicated quality-badge JSX blocks across the
// E2E Architect workspace.  Used wherever the UI surfaces a
// strong / degraded / weak rating from the evidence pipeline:
//
//   * VisualScene.scene_quality
//   * VisualFlowEdge.action_quality
//   * EvidenceControl.selector_confidence  (mapped to band)
//   * Filmstrip thumbnails (compact "w" / "d" marker)
//
// Tone choices:
//   strong   → green   ("ready to ship")
//   degraded → amber   ("usable — verify before relying on it")
//   weak     → red     ("flag for review")
//   unknown  → slate   ("no signal yet")
//
// The component intentionally does NOT take colour / class overrides
// so the badge looks the same everywhere — that consistency is the
// "WOW" payoff for Phase 3 (every assertion in the UI has the same
// visual vocabulary of trust).

import { clsx } from 'clsx';
import type { ReactNode } from 'react';

export type ConfidenceLevel = 'strong' | 'degraded' | 'weak' | 'unknown';

interface Props {
  level: ConfidenceLevel | string | null | undefined;
  /** Optional explicit label.  Defaults to capitalised level. */
  label?: string;
  /** Compact one-letter marker — used on filmstrip thumbnails. */
  compact?: boolean;
  /** Optional 0.0–1.0 confidence number — rendered as a small percent suffix. */
  score?: number | null;
  /** Optional leading icon. */
  icon?: ReactNode;
  /** Title attribute for hover-tooltip. */
  title?: string;
  className?: string;
}

const _levelClass: Record<ConfidenceLevel, string> = {
  strong:   'border-emerald-300 text-emerald-700 bg-emerald-50',
  degraded: 'border-amber-300   text-amber-700   bg-amber-50',
  weak:     'border-red-300     text-red-700     bg-red-50',
  unknown:  'border-slate-300   text-slate-600   bg-slate-50',
};

const _compactClass: Record<ConfidenceLevel, string> = {
  strong:   'bg-emerald-500/80 text-white',
  degraded: 'bg-amber-500/80   text-white',
  weak:     'bg-red-500/80     text-white',
  unknown:  'bg-slate-500/80   text-white',
};

const _compactLetter: Record<ConfidenceLevel, string> = {
  strong:   's',
  degraded: 'd',
  weak:     'w',
  unknown:  '?',
};

function normaliseLevel(raw: Props['level']): ConfidenceLevel {
  if (raw === 'strong' || raw === 'degraded' || raw === 'weak') return raw;
  return 'unknown';
}

export function ConfidenceChip({
  level,
  label,
  compact = false,
  score,
  icon,
  title,
  className,
}: Props) {
  const lvl = normaliseLevel(level);

  if (compact) {
    return (
      <span
        className={clsx(
          'inline-flex items-center justify-center rounded-bl rounded-tr px-1 text-[8px] font-bold uppercase',
          _compactClass[lvl],
          className,
        )}
        title={title ?? label ?? lvl}
      >
        {_compactLetter[lvl]}
      </span>
    );
  }

  // Percent suffix only when an explicit score is provided AND the
  // level isn't "unknown" — for unknown we'd just be showing 0% noise.
  const suffix =
    typeof score === 'number' && lvl !== 'unknown'
      ? ` · ${Math.round(score * 100)}%`
      : '';

  return (
    <span
      className={clsx(
        'inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[10px] font-medium capitalize',
        _levelClass[lvl],
        className,
      )}
      title={title ?? `${label ?? lvl}${suffix}`}
    >
      {icon}
      {label ?? lvl}
      {suffix}
    </span>
  );
}

/** Map a 0.0–1.0 numeric confidence to a band — kept here so every
 *  consumer uses the same thresholds.  These match the eyes-engine
 *  quality_gate weighting (strong ≥ 0.80, degraded ≥ 0.55, else weak).
 */
export function bandForScore(score: number | null | undefined): ConfidenceLevel {
  if (typeof score !== 'number' || Number.isNaN(score)) return 'unknown';
  if (score >= 0.80) return 'strong';
  if (score >= 0.55) return 'degraded';
  return 'weak';
}
