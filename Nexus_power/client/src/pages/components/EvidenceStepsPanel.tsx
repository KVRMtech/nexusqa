// ═══════════════════════════════════════════════════════════════
//  EvidenceStepsPanel — detail panel for evidence_steps
//
//  Renders the visual-evidence user-action timeline produced by the
//  spine engine's persist-action-evidence stage. Visual evidence is
//  purely visual: the timeline is derived from OCR-diff, cursor
//  tracking, and LLaVA scene description deltas. The SME's audio
//  narration is NOT part of evidence_steps — it lives on the session
//  transcript panel instead. Each step shows:
//
//    * action verb icon (matches canonical action_kind)
//    * target label and observed value (no fabrication)
//    * confidence ribbon and agreement-score chips
//    * before/after frame thumbnails
//
//  When a scene has no evidence_steps (legacy artifact, or all
//  signals were too weak to corroborate) the panel surfaces an
//  explicit "Awaiting analysis" message rather than inventing one.
// ═══════════════════════════════════════════════════════════════

import { useMemo, useState } from 'react';
import {
  AlertTriangle, CheckCircle, Eye, FormInput, GitBranch,
  Image as ImageIcon, Layers, Maximize2, MousePointer, Navigation, Search,
} from 'lucide-react';
import clsx from 'clsx';

import type { EvidenceStep } from '../../types/canonical';

// ── Action-kind iconography ────────────────────────────────────────

const _ACTION_ICON: Record<string, React.ElementType> = {
  click_cta: MousePointer,
  click: MousePointer,
  enter_text: FormInput,
  select_option: FormInput,
  submit_form: CheckCircle,
  navigate: Navigation,
  review: Eye,
  scroll: GitBranch,
  scroll_or_repaint: GitBranch,
  open_overlay: Maximize2,
  close_overlay: Maximize2,
  state_change: Layers,
  user_interaction: Layers,
  ui_interaction: Layers,
};

const _ACTION_LABEL: Record<string, string> = {
  click_cta: 'Click',
  click: 'Click',
  enter_text: 'Type',
  select_option: 'Select',
  submit_form: 'Submit',
  navigate: 'Navigate',
  review: 'Review',
  scroll: 'Scroll',
  scroll_or_repaint: 'Scroll',
  open_overlay: 'Open',
  close_overlay: 'Close',
  state_change: 'State change',
  user_interaction: 'Interaction',
  ui_interaction: 'Interaction',
};

function fmtTimestamp(ms: number): string {
  const total = Math.max(0, Math.round(ms / 1000));
  const m = Math.floor(total / 60);
  const s = total % 60;
  return `${m}:${s.toString().padStart(2, '0')}`;
}

// ── Confidence ribbon ──────────────────────────────────────────────

function ConfidenceRibbon({ confidence, agreement }: { confidence: number; agreement: number }) {
  const pct = Math.round(Math.max(0, Math.min(1, confidence)) * 100);
  const tone =
    confidence >= 0.7 ? 'bg-emerald-500'
    : confidence >= 0.4 ? 'bg-amber-500'
    : 'bg-rose-500';
  const tier =
    agreement >= 0.5 ? 'multi-signal'
    : agreement >= 0.25 ? 'single-signal'
    : 'thin evidence';

  return (
    <div className="flex flex-col gap-1">
      <div className="h-1.5 w-24 bg-slate-200 rounded-full overflow-hidden">
        <div
          className={clsx('h-full transition-all', tone)}
          style={{ width: `${pct}%` }}
          aria-label={`Confidence ${pct}%`}
        />
      </div>
      <span className="text-[9px] uppercase tracking-wider text-slate-500">
        {pct}% · {tier}
      </span>
    </div>
  );
}

// ── Evidence-signal chips ──────────────────────────────────────────

function SignalChip({ name }: { name: string }) {
  // Visual evidence is purely visual — audio_intent is no longer emitted
  // by the backend triangulator. filled_control / flow_edge are synthetic
  // step sources derived from real visual facts (a populated form field;
  // a confirmed scene-to-scene transition) and are colour-coded so the
  // user can see WHY each step exists at a glance.
  const palette: Record<string, string> = {
    cursor: 'bg-sky-100 text-sky-700 border-sky-300',
    cursor_click: 'bg-sky-100 text-sky-700 border-sky-300',
    cursor_hover: 'bg-sky-50 text-sky-600 border-sky-200',
    ocr_diff: 'bg-amber-100 text-amber-700 border-amber-300',
    llava_delta: 'bg-emerald-100 text-emerald-700 border-emerald-300',
    filled_control: 'bg-teal-100 text-teal-700 border-teal-300',
    flow_edge: 'bg-indigo-100 text-indigo-700 border-indigo-300',
    ocr_form_match: 'bg-fuchsia-100 text-fuchsia-700 border-fuchsia-300',
    frame_captured: 'bg-slate-100 text-slate-600 border-slate-300',
  };
  const cls = palette[name] || 'bg-slate-100 text-slate-600 border-slate-300';
  return (
    <span className={clsx(
      'inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[9px] uppercase tracking-wider border',
      cls,
    )}>
      {name === 'cursor' && <MousePointer className="w-2.5 h-2.5" />}
      {name === 'filled_control' && <FormInput className="w-2.5 h-2.5" />}
      {name === 'flow_edge' && <Navigation className="w-2.5 h-2.5" />}
      {name === 'ocr_form_match' && <Search className="w-2.5 h-2.5" />}
      {name.replace('_', ' ')}
    </span>
  );
}

// ── Single step card ───────────────────────────────────────────────

function StepCard({
  step,
  onSelect,
  isSelected,
  thumbnailUrl,
}: {
  step: EvidenceStep;
  onSelect?: (step: EvidenceStep) => void;
  isSelected?: boolean;
  thumbnailUrl?: string | null;
}) {
  const Icon = _ACTION_ICON[step.action_kind] || Layers;
  const verb = _ACTION_LABEL[step.action_kind] || step.action_kind;
  const hasTarget = (step.target_label ?? '').trim().length > 0;
  const hasValue = (step.observed_value ?? '').trim().length > 0;
  // Track image load failure so we render a clean placeholder instead
  // of the browser's ugly fallback (which surfaces the alt text "Frame
  // at 16800 ms" — looks broken to the user).
  const [imgFailed, setImgFailed] = useState(false);

  return (
    <button
      type="button"
      onClick={() => onSelect?.(step)}
      className={clsx(
        'w-full text-left flex gap-3 p-3 rounded-lg border transition-all',
        'hover:border-indigo-300 hover:bg-indigo-50/40',
        isSelected
          ? 'border-indigo-500 bg-indigo-50 ring-1 ring-indigo-300'
          : 'border-slate-200 bg-white',
      )}
    >
      {/* Thumbnail (after-frame) */}
      {thumbnailUrl && !imgFailed ? (
        <img
          src={thumbnailUrl}
          alt=""
          className="w-16 h-12 object-cover rounded border border-slate-300 flex-shrink-0"
          loading="lazy"
          onError={() => setImgFailed(true)}
        />
      ) : (
        <div className="w-16 h-12 rounded border border-dashed border-slate-300 bg-slate-50 flex items-center justify-center flex-shrink-0">
          <ImageIcon className="w-4 h-4 text-slate-300" aria-hidden />
        </div>
      )}

      {/* Body */}
      <div className="flex-1 min-w-0">
        <div className="flex items-center gap-2 mb-1">
          <Icon className="w-3.5 h-3.5 text-indigo-600" />
          <span className="text-xs font-bold text-slate-800 uppercase tracking-wider">
            {verb}
          </span>
          <span className="text-[10px] text-slate-400 ml-auto">
            {fmtTimestamp(step.start_ms)} → {fmtTimestamp(step.end_ms)}
          </span>
        </div>

        {/* Target + value */}
        <div className="text-sm text-slate-700 mb-1 truncate">
          {hasTarget ? (
            <>
              <span className="text-slate-500">target:</span>{' '}
              <span className="font-medium">{step.target_label}</span>
            </>
          ) : (
            <span className="text-amber-700 inline-flex items-center gap-1">
              <AlertTriangle className="w-3 h-3" />
              No target captured
            </span>
          )}
        </div>
        {hasValue && (
          <div className="text-sm text-slate-700 mb-1 truncate">
            <span className="text-slate-500">value:</span>{' '}
            <span className="font-mono text-xs bg-slate-100 px-1.5 py-0.5 rounded">
              {step.observed_value}
            </span>
          </div>
        )}

        {/* Confidence + signals */}
        <div className="flex items-center gap-3 mt-1">
          <ConfidenceRibbon confidence={step.confidence} agreement={step.agreement_score} />
          <div className="flex flex-wrap gap-1">
            {step.evidence_signals.map((s) => (
              <SignalChip key={s} name={s} />
            ))}
          </div>
        </div>
      </div>
    </button>
  );
}

// ── Empty state ────────────────────────────────────────────────────

function EmptyState({ reason }: { reason: 'no_steps_for_scene' | 'no_steps_in_artifact' }) {
  if (reason === 'no_steps_in_artifact') {
    return (
      <div className="rounded-lg border border-dashed border-slate-300 p-4 text-center text-slate-500 text-sm">
        <Search className="w-5 h-5 mx-auto mb-2 text-slate-400" />
        <p className="font-medium">No triangulated steps yet</p>
        <p className="text-xs mt-1">
          The action evidence pipeline has not produced steps for this artifact.
          Re-run with per-frame LLaVA + cursor tracking enabled to populate this view.
        </p>
      </div>
    );
  }
  return (
    <div className="rounded-lg border border-dashed border-amber-300 bg-amber-50/50 p-4 text-center text-amber-800 text-sm">
      <AlertTriangle className="w-5 h-5 mx-auto mb-2 text-amber-600" />
      <p className="font-medium">Awaiting analysis for this scene</p>
      <p className="text-xs mt-1">
        Other scenes have triangulated steps but this one is awaiting reliable
        signal. Avoiding fabricated step labels — see the visual flow page
        for the scene-level summary.
      </p>
    </div>
  );
}

// ── Public component ───────────────────────────────────────────────

export function EvidenceStepsPanel({
  steps,
  selectedStepId,
  onSelectStep,
  thumbnailFor,
  totalArtifactSteps,
  sceneId,
  minConfidence,
}: {
  /** All steps for the artifact, optionally pre-filtered to the selected scene. */
  steps: EvidenceStep[];
  /** Step that should render with the highlighted selected state. */
  selectedStepId?: string | null;
  /** Callback when the user clicks a step card (used by Phase D.2 step-replay). */
  onSelectStep?: (step: EvidenceStep) => void;
  /** Resolves a frame_id to its image URL.  Caller wires this via
   *  getFrameImageUrl(scene.representative_frame_asset_path). */
  thumbnailFor?: (frameId: string | null) => string | null;
  /** Total step count across the whole artifact — used to differentiate
   *  "no steps for this scene" from "no steps in artifact at all". */
  totalArtifactSteps?: number;
  /** sceneId for which the panel was opened.  Empty when showing all. */
  sceneId?: string;
  /** Min confidence to display. Steps below the threshold are hidden by
   *  default with a "show all" toggle to surface them if the user wants. */
  minConfidence?: number;
}) {
  const [showLowConfidence, setShowLowConfidence] = useState(false);
  const threshold = typeof minConfidence === 'number' ? minConfidence : 0.6;

  const { highConfidenceSteps, lowConfidenceCount } = useMemo(() => {
    const sorted = [...(steps || [])].sort((a, b) => {
      const sa = a.scene_index ?? 0;
      const sb = b.scene_index ?? 0;
      if (sa !== sb) return sa - sb;
      return a.step_index - b.step_index;
    });
    if (showLowConfidence) {
      return { highConfidenceSteps: sorted, lowConfidenceCount: 0 };
    }
    const high = sorted.filter((s) => (s.confidence ?? 0) >= threshold);
    return {
      highConfidenceSteps: high,
      lowConfidenceCount: sorted.length - high.length,
    };
  }, [steps, threshold, showLowConfidence]);

  if (highConfidenceSteps.length === 0 && lowConfidenceCount === 0) {
    if (totalArtifactSteps && totalArtifactSteps > 0 && sceneId) {
      return <EmptyState reason="no_steps_for_scene" />;
    }
    return <EmptyState reason="no_steps_in_artifact" />;
  }

  return (
    <div className="flex flex-col gap-2">
      {highConfidenceSteps.map((step) => (
        <StepCard
          key={step.step_id}
          step={step}
          isSelected={selectedStepId === step.step_id}
          onSelect={onSelectStep}
          thumbnailUrl={thumbnailFor?.(step.after_frame_id) ?? thumbnailFor?.(step.before_frame_id) ?? null}
        />
      ))}
      {lowConfidenceCount > 0 && (
        <button
          type="button"
          onClick={() => setShowLowConfidence(true)}
          className="text-[10px] text-slate-500 hover:text-indigo-600 underline self-start mt-1"
        >
          Show {lowConfidenceCount} weaker step{lowConfidenceCount === 1 ? '' : 's'} (low confidence)
        </button>
      )}
      {showLowConfidence && (
        <button
          type="button"
          onClick={() => setShowLowConfidence(false)}
          className="text-[10px] text-slate-500 hover:text-indigo-600 underline self-start mt-1"
        >
          Hide weaker steps
        </button>
      )}
    </div>
  );
}
