// ═══════════════════════════════════════════════════════════════
//  AppTimeline — Gantt-style horizontal bars for multi-app demos
// ═══════════════════════════════════════════════════════════════
//
// A 3-hour demo that hops between five apps (Salesforce → SQL
// Developer → CICS → Excel → Outlook) is the hardest thing to
// summarise visually.  This component renders a single-row "track"
// per app instance positioned on the scene-index axis, so the
// reviewer can:
//
//   * See how the demo distributed time across apps at a glance.
//   * Click a bar to scope the scenario panel to that app instance.
//   * Spot interleaved visits (the same app appearing twice) without
//     reading a sentence of text.
//
// We use the scene-index axis (not wall-clock time) for two reasons:
//   1. ``AppInstance.first_scene_index`` / ``last_scene_index`` are
//      always populated by the segmenter; ``start_ms`` is optional
//      on older artifacts and ``duration_quality`` may be "unknown".
//   2. The filmstrip elsewhere on the page uses the same axis, so
//      clicking a bar here lines up visually with the filmstrip.
//
// Each bar is coloured per app_type using a small deterministic
// palette so the same app gets the same colour every time the page
// is loaded — important because reviewers learn "blue = Salesforce".

import { clsx } from 'clsx';
import type { CSSProperties } from 'react';
import type { AppInstance } from '../types/canonical';

interface Props {
  instances: AppInstance[];
  /** Total scenes in the artifact — used to scale the x-axis. */
  totalScenes: number;
  selectedInstanceId?: string | null;
  onSelectInstance?: (instance: AppInstance) => void;
  className?: string;
}

// Deterministic colour mapping by app_type.  Order matters: we walk
// this array to pick a colour by index when an unknown app_type is
// encountered, so a new app type gets a stable colour even before
// it has a registered entry.
const APP_TYPE_PALETTE: Array<{ token: string; bar: string; text: string }> = [
  { token: 'web',         bar: 'bg-sky-500',     text: 'text-sky-700' },
  { token: 'mainframe',   bar: 'bg-violet-500',  text: 'text-violet-700' },
  { token: 'sap',         bar: 'bg-orange-500',  text: 'text-orange-700' },
  { token: 'database',    bar: 'bg-emerald-500', text: 'text-emerald-700' },
  { token: 'excel',       bar: 'bg-green-600',   text: 'text-green-700' },
  { token: 'outlook',     bar: 'bg-indigo-500',  text: 'text-indigo-700' },
  { token: 'word',        bar: 'bg-blue-600',    text: 'text-blue-700' },
  { token: 'terminal',    bar: 'bg-slate-700',   text: 'text-slate-700' },
  { token: 'pdf',         bar: 'bg-rose-500',    text: 'text-rose-700' },
  { token: 'desktop',     bar: 'bg-zinc-500',    text: 'text-zinc-700' },
];

function paletteFor(appType: string | null, fallbackIdx: number): { bar: string; text: string } {
  if (appType) {
    const norm = appType.toLowerCase();
    const hit = APP_TYPE_PALETTE.find(p => norm.includes(p.token));
    if (hit) return hit;
  }
  return APP_TYPE_PALETTE[fallbackIdx % APP_TYPE_PALETTE.length];
}

function readableLabel(inst: AppInstance, idx: number): string {
  return (
    inst.app_name ||
    inst.window_title ||
    inst.detected_url ||
    inst.app_type ||
    `App ${idx + 1}`
  );
}

export function AppTimeline({
  instances,
  totalScenes,
  selectedInstanceId,
  onSelectInstance,
  className,
}: Props) {
  if (!instances || instances.length === 0 || totalScenes <= 0) {
    return null;
  }

  // Compute interleaving: an app instance is "returning" when there
  // was a different app instance between its first appearance and
  // this one.  We detect that by walking sorted-by-first-scene order.
  const sortedByStart = [...instances].sort(
    (a, b) => a.first_scene_index - b.first_scene_index,
  );
  const returningKeys = new Set<string>();
  const seenKeys = new Set<string>();
  for (const inst of sortedByStart) {
    const key = `${inst.app_type ?? 'unknown'}::${inst.detected_url ?? inst.window_title ?? ''}`;
    if (seenKeys.has(key)) returningKeys.add(inst.instance_id);
    else seenKeys.add(key);
  }

  return (
    <div
      className={clsx(
        'rounded-xl border border-gray-200 bg-white p-3 space-y-2',
        className,
      )}
    >
      <div className="flex items-center justify-between">
        <h3 className="text-[10px] uppercase tracking-wider text-slate-500 font-semibold">
          App timeline · {instances.length} instance{instances.length === 1 ? '' : 's'}
        </h3>
        <span className="text-[10px] text-slate-400">
          {totalScenes} scene{totalScenes === 1 ? '' : 's'}
        </span>
      </div>

      <div className="space-y-1.5">
        {sortedByStart.map((inst, idx) => {
          const startPct = (inst.first_scene_index / totalScenes) * 100;
          // ``last_scene_index`` is inclusive; width covers (last - first + 1) scenes.
          const widthPct = Math.max(
            0.5,
            ((inst.last_scene_index - inst.first_scene_index + 1) / totalScenes) * 100,
          );
          const isSelected = selectedInstanceId === inst.instance_id;
          const { bar, text } = paletteFor(inst.app_type, idx);
          const label = readableLabel(inst, idx);
          const isReturning = returningKeys.has(inst.instance_id);
          const barStyle: CSSProperties = {
            left:  `${startPct}%`,
            width: `${widthPct}%`,
          };

          return (
            <div key={inst.instance_id} className="flex items-center gap-2">
              {/* Label column (fixed width so bars align across rows) */}
              <div className="w-32 shrink-0 text-[11px] flex items-center gap-1">
                <span className={clsx('w-1.5 h-1.5 rounded-full', bar)} />
                <span className={clsx('truncate', text)} title={label}>
                  {label}
                </span>
                {isReturning && (
                  <span
                    className="text-[8px] text-slate-500 border border-slate-300 rounded px-1"
                    title="User returned to this app after visiting another"
                  >
                    return
                  </span>
                )}
              </div>

              {/* Track */}
              <div className="relative flex-1 h-6 rounded bg-slate-100 overflow-hidden">
                <button
                  type="button"
                  onClick={() => onSelectInstance?.(inst)}
                  className={clsx(
                    'absolute top-0 bottom-0 rounded transition-all',
                    bar,
                    isSelected
                      ? 'ring-2 ring-nexus-500 opacity-100'
                      : 'opacity-80 hover:opacity-100',
                  )}
                  style={barStyle}
                  title={`${label} · scenes ${inst.first_scene_index + 1}–${inst.last_scene_index + 1} (${inst.scene_count})`}
                  aria-label={`${label}, ${inst.scene_count} scenes`}
                >
                  <span className="absolute inset-0 flex items-center justify-center text-[9px] font-semibold text-white truncate px-1">
                    {inst.scene_count >= 3 ? inst.scene_count : ''}
                  </span>
                </button>
              </div>
            </div>
          );
        })}
      </div>

      {/* Hover-hint footer */}
      <p className="text-[9px] text-slate-400 pt-1 border-t border-gray-100">
        Click a bar to filter the scenario panel to that app instance.
      </p>
    </div>
  );
}
