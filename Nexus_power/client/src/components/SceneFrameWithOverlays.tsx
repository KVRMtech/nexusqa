// ═══════════════════════════════════════════════════════════════
//  SceneFrameWithOverlays — representative frame + control overlays
// ═══════════════════════════════════════════════════════════════
//
// Renders the scene's representative frame and overlays a clickable
// rectangle for every extracted control whose ``bounding_box`` has
// real coordinates.  Hovering a rectangle highlights it; clicking it
// invokes ``onSelectControl`` so the parent can scroll the Evidence
// Inspector to that control.
//
// Why this matters for Phase 3:
//   * Every assertion the platform makes about a scene becomes
//     visually verifiable.  Reviewers see the exact pixels behind
//     a "Click Submit" test step.
//   * Confidence-coloured rectangles let a reviewer scan a screen
//     and instantly count weak (red) selectors that need work.
//   * No SVG mathing — overlays are percentage-positioned so they
//     scale with the rendered image at any width.
//
// Coordinate model:
//   The backend writes ``bounding_box`` as ``{x1, y1, x2, y2}`` in
//   *source-frame pixels*.  We need the source frame's intrinsic
//   width and height to convert to percentages.  The component reads
//   that from the rendered <img> element's ``naturalWidth`` /
//   ``naturalHeight`` once the image loads.
//
// Edge cases handled:
//   * Empty / null bounding_box → control is skipped silently
//   * Image load failure        → overlays hidden, plain frame shown
//   * Out-of-range coords       → clamped to [0,1]

import { clsx } from 'clsx';
import { useMemo, useRef, useState } from 'react';
import type { CSSProperties } from 'react';
import type { BoundingBox, EvidenceControl, VisualScene } from '../types/canonical';
import { ConfidenceChip, bandForScore } from './ConfidenceChip';

interface Props {
  scene: VisualScene;
  /** Public/signed URL for the representative frame. */
  frameUrl: string;
  controls: EvidenceControl[];
  selectedControlId?: string | null;
  onSelectControl?: (control: EvidenceControl) => void;
  /** When true, only show controls that are automation_ready=True. */
  filterAutomationReadyOnly?: boolean;
  /** Maximum rendered height — defaults to a tall inspector panel. */
  maxHeight?: number;
  className?: string;
}

interface NormalisedBox {
  x: number;    // 0..1 left
  y: number;    // 0..1 top
  w: number;    // 0..1 width
  h: number;    // 0..1 height
}

/** Coerce the variant shapes of bounding_box into the
 *  ``{x, y, w, h}`` normalised form, or ``null`` if no usable
 *  geometry is present.  The backend stores ``{x1, y1, x2, y2}``
 *  in source pixels; some legacy rows used ``{x, y, width, height}``.
 */
function normaliseBox(
  raw: BoundingBox | null | undefined,
  naturalWidth: number,
  naturalHeight: number,
): NormalisedBox | null {
  if (!raw || typeof raw !== 'object') return null;
  if (naturalWidth <= 0 || naturalHeight <= 0) return null;

  const x1 = typeof raw.x1 === 'number' ? raw.x1
    : typeof raw.x === 'number' ? raw.x : null;
  const y1 = typeof raw.y1 === 'number' ? raw.y1
    : typeof raw.y === 'number' ? raw.y : null;
  let x2: number | null = null;
  let y2: number | null = null;

  if (typeof raw.x2 === 'number') x2 = raw.x2;
  else if (typeof raw.width === 'number' && x1 !== null) x2 = x1 + raw.width;

  if (typeof raw.y2 === 'number') y2 = raw.y2;
  else if (typeof raw.height === 'number' && y1 !== null) y2 = y1 + raw.height;

  if (x1 === null || y1 === null || x2 === null || y2 === null) return null;
  // Some pipelines emit zero-area boxes for vision-only guesses — drop them.
  if (x2 <= x1 || y2 <= y1) return null;

  const clamp = (v: number) => Math.max(0, Math.min(1, v));
  return {
    x: clamp(x1 / naturalWidth),
    y: clamp(y1 / naturalHeight),
    w: clamp((x2 - x1) / naturalWidth),
    h: clamp((y2 - y1) / naturalHeight),
  };
}

function bandColour(level: ReturnType<typeof bandForScore>): string {
  switch (level) {
    case 'strong':   return 'border-emerald-400 bg-emerald-400/10 hover:bg-emerald-400/25';
    case 'degraded': return 'border-amber-400   bg-amber-400/10   hover:bg-amber-400/25';
    case 'weak':     return 'border-red-400     bg-red-400/10     hover:bg-red-400/25';
    case 'unknown':  return 'border-slate-300   bg-slate-300/10   hover:bg-slate-300/25';
  }
}

export function SceneFrameWithOverlays({
  scene,
  frameUrl,
  controls,
  selectedControlId,
  onSelectControl,
  filterAutomationReadyOnly = false,
  maxHeight = 480,
  className,
}: Props) {
  const imgRef = useRef<HTMLImageElement | null>(null);
  const [naturalSize, setNaturalSize] = useState<{ w: number; h: number } | null>(null);
  const [imageError, setImageError] = useState(false);

  const visibleControls = useMemo(
    () => controls.filter(c => !filterAutomationReadyOnly || c.automation_ready),
    [controls, filterAutomationReadyOnly],
  );

  // Compute overlay positions only after we know the natural size of
  // the source frame.  Until then, the image renders without overlays.
  const overlays = useMemo(() => {
    if (!naturalSize) return [];
    return visibleControls
      .map(c => {
        const box = normaliseBox(c.bounding_box, naturalSize.w, naturalSize.h);
        if (!box) return null;
        return { control: c, box };
      })
      .filter((x): x is { control: EvidenceControl; box: NormalisedBox } => x !== null);
  }, [visibleControls, naturalSize]);

  if (!frameUrl || imageError) {
    return (
      <div
        className={clsx(
          'rounded-xl border border-gray-200 bg-gray-50 flex items-center justify-center text-[11px] text-slate-500',
          className,
        )}
        style={{ minHeight: 120 }}
      >
        No representative frame available
      </div>
    );
  }

  return (
    <div
      className={clsx(
        'relative rounded-xl border border-gray-200 bg-gray-900 overflow-hidden',
        className,
      )}
    >
      <img
        ref={imgRef}
        src={frameUrl}
        alt={`Scene ${scene.scene_index + 1}`}
        className="block w-full object-contain bg-gray-100"
        style={{ maxHeight }}
        loading="lazy"
        onLoad={e => {
          const img = e.currentTarget;
          setNaturalSize({ w: img.naturalWidth, h: img.naturalHeight });
        }}
        onError={() => setImageError(true)}
      />

      {/* Overlay layer — uses the same width/height as the image
          because both share the parent's content box.  Pointer events
          are scoped to the individual rectangles so clicks on empty
          regions don't spuriously change selection. */}
      <div className="pointer-events-none absolute inset-0">
        {overlays.map(({ control, box }) => {
          const level = bandForScore(control.selector_confidence);
          const isSelected = selectedControlId === control.control_id;
          const style: CSSProperties = {
            left:   `${box.x * 100}%`,
            top:    `${box.y * 100}%`,
            width:  `${box.w * 100}%`,
            height: `${box.h * 100}%`,
          };
          return (
            <button
              key={control.control_id}
              type="button"
              className={clsx(
                'pointer-events-auto absolute border-2 rounded-sm transition-colors',
                bandColour(level),
                isSelected && 'ring-2 ring-nexus-500 z-10',
                !control.automation_ready && 'border-dashed',
              )}
              style={style}
              onClick={() => onSelectControl?.(control)}
              title={
                (control.label_text || control.display_label || control.element_type || 'control')
                + (control.automation_ready ? '' : ' (not automation-ready)')
              }
              aria-label={control.label_text || control.element_type || 'control'}
            />
          );
        })}
      </div>

      {/* Caption strip — scene id + control count + quality */}
      <div className="absolute bottom-0 left-0 right-0 flex items-center justify-between gap-2 bg-gradient-to-t from-black/80 to-transparent px-3 py-2 text-[10px] text-white">
        <span>
          Scene #{scene.scene_index + 1}
          {scene.scene_state_summary?.screen_title && (
            <> · {scene.scene_state_summary.screen_title}</>
          )}
        </span>
        <span className="flex items-center gap-1.5">
          {overlays.length > 0 && (
            <span className="opacity-90">{overlays.length} control{overlays.length === 1 ? '' : 's'}</span>
          )}
          {scene.scene_quality && (
            <ConfidenceChip
              level={scene.scene_quality}
              className="!bg-black/40 !border-white/30 !text-white"
            />
          )}
        </span>
      </div>
    </div>
  );
}

export type { NormalisedBox };
export { normaliseBox };
