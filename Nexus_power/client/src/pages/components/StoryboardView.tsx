// ═══════════════════════════════════════════════════════════════
//  StoryboardView — picture-first comic-strip layout (Phase 1)
//
//  Renders the new Storyboard tab on VisualFlowDiagramPage and the
//  5-panel hero preview on CanonicalResultPage.  Every panel is a
//  large screenshot with a 5-8 word imperative caption, an app
//  badge, and a duration chip.  Picture-to-text ratio is ~90/10 —
//  the screenshot dominates, captions are tight.
//
//  Backend contract: GET /api/v1/artifacts/{id}/storyboard returns
//  the StoryboardPayload (see client/src/types/canonical.ts).
// ═══════════════════════════════════════════════════════════════

import { useMemo } from 'react';
import { MonitorPlay, AlertTriangle, Sparkles, Clock, Layers } from 'lucide-react';
import clsx from 'clsx';
import api from '../../services/api';
import type {
  StoryboardPanel,
  StoryboardPayload,
} from '../../types/canonical';

interface StoryboardViewProps {
  /** Parent passes the fetched payload; null while loading. */
  payload: StoryboardPayload | null;
  loading?: boolean;
  error?: string | null;
  /** Optional artifact id — needed only when the parent did not embed
   *  ``annotated_frame_url`` in panels (older composer versions). */
  artifactId?: string;
  /** Show noise panels (off by default — the storyboard filters them). */
  includeNoise?: boolean;
  /** Layout — "vertical" for the main tab, "hero" for the canonical
   *  result page mini-preview. */
  layout?: 'vertical' | 'hero';
  /** When user clicks a panel, fire this callback (parent can scroll
   *  to the underlying scene in the 3D map). */
  onPanelClick?: (panel: StoryboardPanel) => void;
}

function _formatDuration(ms: number): string {
  if (!ms || ms < 0) return '';
  const s = Math.round(ms / 1000);
  if (s < 60) return `${s}s`;
  return `${Math.floor(s / 60)}m ${s % 60}s`;
}

function _qualityClasses(quality: string): string {
  if (quality === 'strong') {
    return 'bg-emerald-50 text-emerald-700 border-emerald-200';
  }
  if (quality === 'degraded') {
    return 'bg-amber-50 text-amber-700 border-amber-200';
  }
  return 'bg-slate-50 text-slate-500 border-slate-200';
}

/** One panel card.  Used in both the vertical comic strip and the hero strip. */
function Panel({
  panel,
  artifactId,
  layout,
  onClick,
}: {
  panel: StoryboardPanel;
  artifactId: string;
  layout: 'vertical' | 'hero';
  onClick?: (panel: StoryboardPanel) => void;
}) {
  // Prefer the explicit URL the backend gave us; fall back to building
  // it from the artifact + frame id when missing (older composer).
  const frameUrl = useMemo(() => {
    if (panel.annotated_frame_url) {
      // Append the JWT so a plain <img> tag works.
      const token = sessionStorage.getItem('nexus_token') || '';
      const sep = panel.annotated_frame_url.includes('?') ? '&' : '?';
      return token
        ? `${panel.annotated_frame_url}${sep}token=${encodeURIComponent(token)}`
        : panel.annotated_frame_url;
    }
    if (panel.representative_frame_id) {
      return api.getAnnotatedFrameUrl(artifactId, panel.representative_frame_id);
    }
    return null;
  }, [panel, artifactId]);

  const caption = (panel.caption_short || '').trim();
  const isHero = layout === 'hero';
  const durationLabel = _formatDuration(panel.duration_ms);
  const appLabel = panel.app?.display_label || '';
  const qualityBadge = _qualityClasses(panel.panel_quality);

  return (
    <button
      type="button"
      onClick={() => onClick?.(panel)}
      className={clsx(
        'group relative w-full text-left overflow-hidden rounded-2xl',
        'transition-all duration-300 border bg-white',
        'hover:shadow-2xl hover:-translate-y-1 active:translate-y-0',
        'focus:outline-none focus-visible:ring-2 focus-visible:ring-indigo-400',
        isHero ? 'border-slate-200' : 'border-slate-200/80',
      )}
      style={{ minHeight: isHero ? 240 : 360 }}
      aria-label={`Panel ${panel.panel_index + 1}: ${caption || 'untitled'}`}
    >
      <div
        className="relative w-full bg-slate-100"
        style={{ aspectRatio: isHero ? '16 / 9' : '16 / 10' }}
      >
        {frameUrl ? (
          <img
            src={frameUrl}
            alt={caption || `Panel ${panel.panel_index + 1}`}
            loading="lazy"
            className={clsx(
              'w-full h-full object-cover',
              'transition-transform duration-500 group-hover:scale-[1.02]',
            )}
            onError={(e) => {
              // Hide the broken image so the placeholder underneath shows.
              (e.target as HTMLImageElement).style.opacity = '0';
            }}
          />
        ) : null}

        {/* Placeholder behind the image — shown when the image fails
            (annotated PNG not yet generated, or raw frame missing). */}
        <div className="absolute inset-0 flex flex-col items-center justify-center text-slate-400 text-xs gap-2 -z-0">
          <MonitorPlay className="h-8 w-8 opacity-40" />
          <span>{frameUrl ? 'Annotated frame pending…' : 'No representative frame'}</span>
        </div>

        {/* Top-right cluster: panel quality badge + app badge */}
        <div className="absolute top-3 right-3 flex items-center gap-1.5">
          {appLabel ? (
            <div className="px-2 py-0.5 rounded-md text-[10px] font-bold uppercase tracking-wider bg-white/85 text-slate-700 backdrop-blur-sm shadow-sm border border-white/60 max-w-[160px] truncate">
              {appLabel}
            </div>
          ) : null}
          <div
            className={clsx(
              'px-2 py-0.5 rounded-md text-[10px] font-black uppercase tracking-wider border',
              qualityBadge,
            )}
          >
            {panel.panel_quality || 'weak'}
          </div>
        </div>

        {/* Top-left cluster: panel index */}
        <div className="absolute top-3 left-3">
          <div className="px-2 py-0.5 rounded-md text-[10px] font-black uppercase tracking-wider bg-slate-900/80 text-white backdrop-blur-sm">
            #{panel.panel_index + 1}
          </div>
        </div>

        {/* Bottom-left: caption banner — picture-first design wants
            this short, big and high-contrast. */}
        <div
          className={clsx(
            'absolute bottom-0 left-0 right-0 px-4 py-3',
            'bg-gradient-to-t from-slate-900/90 via-slate-900/60 to-transparent',
            'text-white',
          )}
        >
          <div className="flex items-end justify-between gap-3">
            <div className="flex-1 min-w-0">
              <div
                className={clsx(
                  'font-black tracking-tight leading-tight truncate',
                  isHero ? 'text-sm' : 'text-lg',
                )}
              >
                {caption || (
                  <span className="text-slate-400 italic font-normal">
                    {panel.is_noise ? 'Noise panel — hidden by default' : 'Caption pending…'}
                  </span>
                )}
              </div>
              {!isHero && panel.caption_long && panel.caption_long !== caption ? (
                <div className="text-[11px] text-slate-300 mt-1 line-clamp-1">
                  {panel.caption_long}
                </div>
              ) : null}
            </div>
            {durationLabel ? (
              <div className="text-[10px] font-bold text-slate-200 whitespace-nowrap flex items-center gap-1 shrink-0">
                <Clock className="h-3 w-3" />
                {durationLabel}
              </div>
            ) : null}
          </div>
        </div>
      </div>

      {/* Below-the-image metadata strip — only on the vertical layout
          to keep the hero variant compact. */}
      {!isHero ? (
        <div className="px-4 py-2 flex items-center justify-between text-[11px] text-slate-500 bg-slate-50/70">
          <div className="flex items-center gap-3 min-w-0">
            <span className="flex items-center gap-1 shrink-0">
              <Layers className="h-3 w-3" />
              {panel.scene_count} scene{panel.scene_count === 1 ? '' : 's'}
            </span>
            {panel.in_scene_action_count > 0 ? (
              <span className="flex items-center gap-1 shrink-0">
                <Sparkles className="h-3 w-3" />
                {panel.in_scene_action_count} actions
              </span>
            ) : null}
          </div>
          {panel.caption_quality && panel.caption_quality !== 'strong' ? (
            <span
              className={clsx(
                'flex items-center gap-1 shrink-0 px-1.5 py-0.5 rounded',
                panel.caption_quality === 'degraded'
                  ? 'text-amber-700 bg-amber-50'
                  : 'text-slate-500 bg-slate-100',
              )}
            >
              <AlertTriangle className="h-3 w-3" />
              caption {panel.caption_quality}
            </span>
          ) : null}
        </div>
      ) : null}
    </button>
  );
}

/** Empty state used when storyboard derivation has not yet produced panels. */
function EmptyState({ reason }: { reason: string }) {
  return (
    <div className="flex flex-col items-center justify-center gap-3 py-16 px-6 text-center">
      <div className="w-14 h-14 rounded-2xl flex items-center justify-center bg-slate-100">
        <MonitorPlay className="h-7 w-7 text-slate-400" />
      </div>
      <h3 className="text-sm font-bold text-slate-900">Storyboard not ready</h3>
      <p className="text-xs text-slate-500 max-w-md leading-relaxed">{reason}</p>
    </div>
  );
}

/** The exported component — used by both the tab and the hero strip. */
export function StoryboardView({
  payload,
  loading,
  error,
  artifactId,
  includeNoise = false,
  layout = 'vertical',
  onPanelClick,
}: StoryboardViewProps) {
  if (loading) {
    return (
      <div className="flex items-center justify-center py-16 text-slate-400 text-xs">
        Loading storyboard…
      </div>
    );
  }
  if (error) {
    return <EmptyState reason={error} />;
  }
  if (!payload) {
    return <EmptyState reason="Storyboard payload missing." />;
  }
  const allPanels = payload.panels || [];
  const panels = includeNoise ? allPanels : allPanels.filter((p) => !p.is_noise);
  if (panels.length === 0) {
    return (
      <EmptyState
        reason={
          allPanels.length === 0
            ? 'No panels yet — the storyboard composer found no scenes to render.'
            : 'All panels are filtered as noise.  Toggle the noise filter to see them.'
        }
      />
    );
  }

  const id = artifactId || payload.artifact_id;

  if (layout === 'hero') {
    return (
      <div className="grid gap-4" style={{ gridTemplateColumns: `repeat(${Math.min(panels.length, 5)}, minmax(0, 1fr))` }}>
        {panels.slice(0, 5).map((panel) => (
          <Panel
            key={panel.panel_id}
            panel={panel}
            artifactId={id}
            layout="hero"
            onClick={onPanelClick}
          />
        ))}
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-6">
      {panels.map((panel) => (
        <Panel
          key={panel.panel_id}
          panel={panel}
          artifactId={id}
          layout="vertical"
          onClick={onPanelClick}
        />
      ))}
    </div>
  );
}

export default StoryboardView;
