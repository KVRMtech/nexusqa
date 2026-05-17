// ═══════════════════════════════════════════════════════════════
//  Demo Diff Panel (P7a)
//
//  Slide-in panel that lets the user compare the current artifact's
//  visual evidence graph against a baseline (any other artifact in the
//  same session — typically the previous demo recording).
//
//  Renders:
//    - Summary chips (scenes added/removed/changed, controls etc.)
//    - Per-scene diff with control-level breakdown
//    - Flow-edge diff
//    - "Affected scenarios" list — which existing scenarios cite
//      scenes/controls/edges that changed
// ═══════════════════════════════════════════════════════════════
import { useCallback, useEffect, useMemo, useState } from 'react';
import clsx from 'clsx';
import {
  X,
  Loader2,
  GitCompare,
  AlertTriangle,
  CheckCircle2,
  Plus,
  Minus,
  RefreshCw,
  ChevronRight,
  ChevronDown,
} from 'lucide-react';
import api from '../services/api';
import type {
  ArtifactDiffResponse,
  CanonicalArtifact,
  ControlDiff,
  SceneDiff,
} from '../types/canonical';

interface DemoDiffPanelProps {
  artifactId: string;
  sessionId?: string;
  tenantId: string;
  open: boolean;
  onClose: () => void;
}

const CHANGE_KIND_STYLE: Record<string, { color: string; label: string }> = {
  added:             { color: 'text-emerald-700 bg-emerald-50 border-emerald-300', label: 'Added' },
  removed:           { color: 'text-red-700 bg-red-50 border-red-300',             label: 'Removed' },
  renamed:           { color: 'text-blue-700 bg-blue-50 border-blue-300',           label: 'Renamed' },
  selector_changed:  { color: 'text-indigo-700 bg-indigo-50 border-indigo-300',     label: 'Selector changed' },
  moved:             { color: 'text-amber-700 bg-amber-50 border-amber-300',       label: 'Moved' },
  automation_lost:   { color: 'text-orange-700 bg-orange-50 border-orange-300',     label: 'Lost automation' },
  automation_gained: { color: 'text-teal-700 bg-teal-50 border-teal-300',           label: 'Gained automation' },
  ocr_changed:       { color: 'text-violet-700 bg-violet-50 border-violet-300',     label: 'OCR changed' },
  controls_changed:  { color: 'text-blue-700 bg-blue-50 border-blue-300',           label: 'Controls changed' },
  unchanged:         { color: 'text-slate-500 bg-white border-gray-200',           label: 'Unchanged' },
};

function chipFor(kind: string) {
  return CHANGE_KIND_STYLE[kind] ?? { color: 'text-slate-600 bg-slate-50 border-slate-200', label: kind };
}

export function DemoDiffPanel({
  artifactId,
  sessionId,
  tenantId,
  open,
  onClose,
}: DemoDiffPanelProps) {
  const [siblings, setSiblings] = useState<CanonicalArtifact[]>([]);
  const [siblingsLoading, setSiblingsLoading] = useState(false);
  const [siblingsError, setSiblingsError] = useState<string | null>(null);
  const [baselineId, setBaselineId] = useState<string>('');
  const [diff, setDiff] = useState<ArtifactDiffResponse | null>(null);
  const [diffLoading, setDiffLoading] = useState(false);
  const [diffError, setDiffError] = useState<string | null>(null);
  const [expandedScenes, setExpandedScenes] = useState<Set<string>>(new Set());

  // Load sibling artifacts (same session, completed) for the picker
  useEffect(() => {
    if (!open || !sessionId) return;
    let cancelled = false;
    setSiblingsLoading(true);
    setSiblingsError(null);
    (async () => {
      try {
        const list = await api.listArtifacts(tenantId, {
          session_id: sessionId,
          limit: 50,
        });
        if (cancelled) return;
        // Exclude the current artifact + sort newest first
        const filtered = list
          .filter(a => a.artifact_id !== artifactId)
          .sort((a, b) => (b.created_at || '').localeCompare(a.created_at || ''));
        setSiblings(filtered);
        // Default baseline = most recent prior artifact, if any
        if (filtered.length > 0 && !baselineId) {
          setBaselineId(filtered[0].artifact_id);
        }
      } catch (err: unknown) {
        if (cancelled) return;
        setSiblingsError(err instanceof Error ? err.message : 'Failed to load artifacts');
      } finally {
        if (!cancelled) setSiblingsLoading(false);
      }
    })();
    return () => { cancelled = true; };
  }, [open, sessionId, tenantId, artifactId, baselineId]);

  const runDiff = useCallback(async () => {
    if (!baselineId) return;
    setDiffLoading(true);
    setDiffError(null);
    setDiff(null);
    setExpandedScenes(new Set());
    try {
      const resp = await api.diffArtifacts(baselineId, artifactId);
      setDiff(resp);
    } catch (err: unknown) {
      const detail = (err as { response?: { data?: { detail?: unknown } } })
        ?.response?.data?.detail;
      let msg: string;
      if (typeof detail === 'string') msg = detail;
      else if (detail && typeof detail === 'object') {
        msg = (detail as { message?: string }).message ?? JSON.stringify(detail);
      } else if (err instanceof Error) msg = err.message;
      else msg = 'Diff failed';
      setDiffError(msg);
    } finally {
      setDiffLoading(false);
    }
  }, [baselineId, artifactId]);

  const toggleScene = useCallback((pageKey: string) => {
    setExpandedScenes(prev => {
      const next = new Set(prev);
      if (next.has(pageKey)) next.delete(pageKey);
      else next.add(pageKey);
      return next;
    });
  }, []);

  const meaningfulSceneDiffs = useMemo(
    () => (diff?.diff.scenes ?? []).filter(s => s.change_kind !== 'unchanged'),
    [diff],
  );
  const unchangedScenesCount = useMemo(
    () => (diff?.diff.scenes ?? []).filter(s => s.change_kind === 'unchanged').length,
    [diff],
  );

  if (!open) return null;

  return (
    <div className="absolute inset-y-0 right-0 z-40 flex w-full sm:w-[560px] flex-col bg-white border-l border-gray-200 shadow-xl">
      {/* Header */}
      <div className="shrink-0 flex items-center gap-2 border-b border-gray-200 bg-gradient-to-r from-amber-50 to-orange-50 px-4 py-3">
        <div className="flex h-7 w-7 items-center justify-center rounded-full bg-orange-500 text-white">
          <GitCompare className="h-4 w-4" />
        </div>
        <div className="flex-1 min-w-0">
          <h2 className="text-sm font-semibold text-slate-800">Demo Diff</h2>
          <p className="text-[10px] text-slate-500">
            Compare this artifact's visual evidence graph against an earlier recording.
          </p>
        </div>
        <button
          onClick={onClose}
          className="text-slate-500 hover:text-slate-700"
          aria-label="Close Demo Diff"
        >
          <X className="h-4 w-4" />
        </button>
      </div>

      <div className="flex-1 min-h-0 overflow-y-auto p-4 space-y-3 bg-[#fafafa]">
        {/* Baseline picker */}
        <div className="rounded-md border border-gray-200 bg-white p-3 space-y-2">
          <label className="text-[10px] uppercase tracking-wider text-slate-500 font-semibold">
            Baseline artifact
          </label>
          {siblingsLoading ? (
            <p className="text-[11px] text-slate-500 inline-flex items-center gap-1">
              <Loader2 className="h-3 w-3 animate-spin" /> Loading artifacts…
            </p>
          ) : siblingsError ? (
            <p className="text-[11px] text-red-600">Failed: {siblingsError}</p>
          ) : siblings.length === 0 ? (
            <p className="text-[11px] text-slate-500 italic">
              No other artifacts in this session to compare against.
            </p>
          ) : (
            <select
              value={baselineId}
              onChange={e => setBaselineId(e.target.value)}
              className="w-full rounded-md border border-gray-200 bg-white px-2 py-1 text-[11px] text-slate-700 focus:outline-none focus:border-orange-500/50"
            >
              {siblings.map(a => (
                <option key={a.artifact_id} value={a.artifact_id}>
                  {a.artifact_id.slice(0, 8)} · {a.source_filename ?? a.status}
                  {a.created_at ? ` · ${new Date(a.created_at).toLocaleString()}` : ''}
                </option>
              ))}
            </select>
          )}
          <div className="flex items-center justify-between">
            <span className="text-[10px] text-slate-500">
              Current: <span className="font-mono">{artifactId.slice(0, 8)}</span>
            </span>
            <button
              onClick={() => void runDiff()}
              disabled={!baselineId || diffLoading}
              className="rounded-md bg-orange-500 text-white text-[11px] font-medium px-3 py-1 hover:bg-orange-400 disabled:opacity-50 disabled:cursor-not-allowed inline-flex items-center gap-1"
            >
              {diffLoading ? <Loader2 className="h-3 w-3 animate-spin" /> : <RefreshCw className="h-3 w-3" />}
              Compute diff
            </button>
          </div>
        </div>

        {diffError && (
          <div className="rounded-md border border-red-300 bg-red-50 px-3 py-2 flex items-start gap-2">
            <AlertTriangle className="h-3.5 w-3.5 text-red-500 mt-0.5 shrink-0" />
            <p className="text-[11px] text-red-700 whitespace-pre-wrap flex-1">{diffError}</p>
          </div>
        )}

        {diff && (
          <>
            {/* Summary chips */}
            <div className="rounded-md border border-gray-200 bg-white p-3 space-y-2">
              <h3 className="text-[10px] uppercase tracking-wider text-slate-500 font-semibold">
                Summary
              </h3>
              <div className="flex flex-wrap gap-1.5">
                {Object.entries(diff.diff.summary).map(([k, v]) => (
                  <span
                    key={k}
                    className="text-[10px] inline-flex items-center gap-1 rounded-full border border-gray-200 px-2 py-0.5 text-slate-700"
                  >
                    {k.replace(/_/g, ' ')}
                    <span className="font-mono font-semibold">{v}</span>
                  </span>
                ))}
              </div>
              {(diff.diff.app_instances_added.length > 0 || diff.diff.app_instances_removed.length > 0) && (
                <div className="text-[10px] text-slate-600 space-y-0.5">
                  {diff.diff.app_instances_added.length > 0 && (
                    <p>
                      <Plus className="inline h-3 w-3 text-emerald-600" /> Apps added:{' '}
                      <span className="font-mono">{diff.diff.app_instances_added.join(', ')}</span>
                    </p>
                  )}
                  {diff.diff.app_instances_removed.length > 0 && (
                    <p>
                      <Minus className="inline h-3 w-3 text-red-600" /> Apps removed:{' '}
                      <span className="font-mono">{diff.diff.app_instances_removed.join(', ')}</span>
                    </p>
                  )}
                </div>
              )}
            </div>

            {/* Affected scenarios */}
            {diff.affected_scenarios.length > 0 && (
              <div className="rounded-md border border-amber-300 bg-amber-50/40 p-3 space-y-2">
                <h3 className="text-[10px] uppercase tracking-wider text-amber-700 font-semibold inline-flex items-center gap-1">
                  <AlertTriangle className="h-3 w-3" />
                  Affected scenarios ({diff.affected_scenarios.length})
                </h3>
                <div className="space-y-1.5">
                  {diff.affected_scenarios.map(sc => (
                    <div
                      key={sc.scenario_id}
                      className="rounded border border-amber-200 bg-white px-2 py-1.5"
                    >
                      <p className="text-[11px] font-medium text-slate-700">
                        <span className="font-mono text-[10px] text-slate-400 mr-1">
                          {sc.scenario_id}
                        </span>
                        {sc.title}
                        {sc.state && (
                          <span className="ml-2 text-[9px] text-slate-500 font-normal">
                            ({sc.state})
                          </span>
                        )}
                      </p>
                      <p className="text-[10px] text-slate-500">
                        Steps {sc.affected_step_numbers.join(', ')}
                      </p>
                      <ul className="mt-0.5 space-y-0.5">
                        {sc.reasons.slice(0, 3).map((r, i) => (
                          <li key={i} className="text-[10px] text-slate-600">
                            • {r}
                          </li>
                        ))}
                        {sc.reasons.length > 3 && (
                          <li className="text-[9px] text-slate-400 italic">
                            …and {sc.reasons.length - 3} more
                          </li>
                        )}
                      </ul>
                    </div>
                  ))}
                </div>
              </div>
            )}

            {/* Scene-by-scene diff */}
            <div className="rounded-md border border-gray-200 bg-white p-3 space-y-2">
              <h3 className="text-[10px] uppercase tracking-wider text-slate-500 font-semibold">
                Scene diff ({meaningfulSceneDiffs.length} changed · {unchangedScenesCount} unchanged)
              </h3>
              {meaningfulSceneDiffs.length === 0 ? (
                <p className="text-[11px] text-emerald-700 inline-flex items-center gap-1">
                  <CheckCircle2 className="h-3 w-3" /> No scene-level changes detected.
                </p>
              ) : (
                <div className="space-y-1">
                  {meaningfulSceneDiffs.map(scene => (
                    <SceneDiffRow
                      key={scene.page_key}
                      scene={scene}
                      expanded={expandedScenes.has(scene.page_key)}
                      onToggle={() => toggleScene(scene.page_key)}
                    />
                  ))}
                </div>
              )}
            </div>

            {/* Edge diff */}
            {diff.diff.edges.length > 0 && (
              <div className="rounded-md border border-gray-200 bg-white p-3 space-y-1">
                <h3 className="text-[10px] uppercase tracking-wider text-slate-500 font-semibold">
                  Flow-edge changes ({diff.diff.edges.length})
                </h3>
                <div className="space-y-0.5">
                  {diff.diff.edges.map((edge, i) => {
                    const conf = chipFor(edge.change_kind);
                    return (
                      <div key={i} className="text-[10px] flex items-center gap-1.5">
                        <span className={clsx('inline-flex rounded-full border px-1.5 py-0', conf.color)}>
                          {conf.label}
                        </span>
                        <span className="text-slate-700">
                          <span className="font-mono">{edge.from_page_key}</span>
                          {' → '}
                          <span className="font-mono">{edge.to_page_key}</span>
                        </span>
                        <span className="text-slate-400">({edge.action_kind})</span>
                      </div>
                    );
                  })}
                </div>
              </div>
            )}
          </>
        )}
      </div>
    </div>
  );
}

// ─── Scene row component ──────────────────────────────────────────────

function SceneDiffRow({
  scene,
  expanded,
  onToggle,
}: {
  scene: SceneDiff;
  expanded: boolean;
  onToggle: () => void;
}) {
  const conf = chipFor(scene.change_kind);
  const meaningfulCtrls = (scene.control_diffs || []).filter(c => c.change_kind !== 'unchanged');
  return (
    <div className="rounded border border-gray-200 bg-white">
      <button
        onClick={onToggle}
        className="w-full flex items-start gap-2 px-2 py-1.5 text-left hover:bg-slate-50"
      >
        {expanded ? (
          <ChevronDown className="h-3 w-3 text-slate-400 mt-0.5 shrink-0" />
        ) : (
          <ChevronRight className="h-3 w-3 text-slate-400 mt-0.5 shrink-0" />
        )}
        <span className={clsx('inline-flex rounded-full border px-1.5 py-0 text-[10px]', conf.color)}>
          {conf.label}
        </span>
        <span className="flex-1 text-[11px] text-slate-700 truncate">
          {scene.title || scene.page_key}
        </span>
        {meaningfulCtrls.length > 0 && (
          <span className="text-[9px] text-slate-500 shrink-0">
            {meaningfulCtrls.length} control change{meaningfulCtrls.length !== 1 ? 's' : ''}
          </span>
        )}
      </button>
      {expanded && (
        <div className="px-3 pb-2 space-y-1.5 border-t border-gray-100">
          {scene.change_kind === 'ocr_changed' || scene.change_kind === 'controls_changed' ? (
            <div className="grid grid-cols-2 gap-2 pt-2">
              <div>
                <p className="text-[9px] uppercase text-slate-400">Baseline OCR</p>
                <p className="text-[10px] text-slate-600 font-mono whitespace-pre-wrap line-clamp-4">
                  {scene.baseline_ocr_preview || '(empty)'}
                </p>
              </div>
              <div>
                <p className="text-[9px] uppercase text-slate-400">Current OCR</p>
                <p className="text-[10px] text-slate-600 font-mono whitespace-pre-wrap line-clamp-4">
                  {scene.current_ocr_preview || '(empty)'}
                </p>
              </div>
            </div>
          ) : null}

          {meaningfulCtrls.length > 0 && (
            <div className="space-y-1 pt-1">
              <p className="text-[9px] uppercase text-slate-400">Controls</p>
              {meaningfulCtrls.map((ctrl, i) => (
                <ControlDiffRow key={i} ctrl={ctrl} />
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function ControlDiffRow({ ctrl }: { ctrl: ControlDiff }) {
  const conf = chipFor(ctrl.change_kind);
  return (
    <div className="flex items-start gap-2 text-[10px]">
      <span className={clsx('inline-flex rounded-full border px-1.5 py-0 shrink-0', conf.color)}>
        {conf.label}
      </span>
      <span className="flex-1">
        <span className="text-slate-700">{ctrl.label}</span>
        {ctrl.detail && (
          <span className="text-slate-500 italic"> — {ctrl.detail}</span>
        )}
      </span>
    </div>
  );
}
