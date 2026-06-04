// ═══════════════════════════════════════════════════════════════
//  Test Studio — E2E Architect Workspace (Phase A)
//
//  Route: /sessions/:sessionId/e2e-architect?artifact_id=...
//
//  Layout (Phase A foundations):
//    Header:   Mode toggle (Engineer ⇄ Reviewer) + actions
//    LEFT:     System Model — Apps/Flows tree • Risk×Coverage heatmap •
//              Variables • Decision Points • Coverage stats
//    CENTER:   Test Canvas — scenario cards, click steps to inspect
//    RIGHT:    Evidence Inspector — frame thumb, OCR, transcript line,
//              selector + stability (Engineer), or plain-English evidence
//              (Reviewer)
//    BOTTOM:   Timeline / Filmstrip — scenes in order, click to inspect
// ═══════════════════════════════════════════════════════════════

import { useState, useEffect, useCallback, useMemo } from 'react';
import { useParams, useSearchParams, useNavigate } from 'react-router-dom';
import { useAuth } from '../contexts/AuthContext';
import api from '../services/api';
import { CoArchitectDock } from '../components/CoArchitectDock';
import { DemoDiffPanel } from '../components/DemoDiffPanel';
import { ConfidenceChip } from '../components/ConfidenceChip';
import { SceneFrameWithOverlays } from '../components/SceneFrameWithOverlays';
import { AppTimeline } from '../components/AppTimeline';
import TestCasesPanel from '../components/TestCasesPanel';
import { useArtifactProgress } from '../hooks/useArtifactProgress';
import type {
  E2EArchitectResponse,
  E2EScenario,
  TestStep,
  VisualEvidenceGraph,
  VisualScene,
  EvidenceControl,
  VisualFlowEdge,
  ScenarioLifecycleState,
} from '../types/canonical';
import clsx from 'clsx';
import {
  ArrowLeft,
  Sparkles,
  Target,
  AlertTriangle,
  CheckCircle2,
  Loader2,
  ChevronRight,
  ChevronDown,
  Eye,
  Mic,
  BarChart3,
  Layers,
  Download,
  Search,
  X,
  ArrowRight,
  GitBranch,
  Variable,
  Shuffle,
  Shield,
  Clock,
  Activity,
  Code2,
  Users,
  Film,
  Link2,
  Image as ImageIcon,
  ThumbsUp,
  Bot,
  GitCompare,
  Wrench,
} from 'lucide-react';

// ── Configs ─────────────────────────────────────────────────

type PageState = 'loading' | 'generating' | 'ready' | 'error' | 'prerequisites';
type ViewMode = 'engineer' | 'reviewer' | 'test-cases';

interface StepSelection {
  scenarioId: string;
  stepNumber: number;
}

const PRIORITY_CONFIG: Record<string, { label: string; color: string; bg: string; weight: number }> = {
  P0_critical: { label: 'P0 Critical', color: 'text-red-400', bg: 'bg-red-500/20 border-red-500/30', weight: 4 },
  P1_high:     { label: 'P1 High',     color: 'text-orange-400', bg: 'bg-orange-500/20 border-orange-500/30', weight: 3 },
  P2_medium:   { label: 'P2 Medium',   color: 'text-yellow-400', bg: 'bg-yellow-500/20 border-yellow-500/30', weight: 2 },
  P3_low:      { label: 'P3 Low',      color: 'text-blue-400', bg: 'bg-blue-500/20 border-blue-500/30', weight: 1 },
};

const CATEGORY_CONFIG: Record<string, { label: string; icon: React.ReactNode; color: string }> = {
  observed:              { label: 'Observed',        icon: <Eye className="h-3.5 w-3.5" />,        color: 'text-green-400 bg-green-500/20 border-green-500/30' },
  inferred_high_risk:    { label: 'Inferred Risk',   icon: <AlertTriangle className="h-3.5 w-3.5" />, color: 'text-red-400 bg-red-500/20 border-red-500/30' },
  boundary_assumption:   { label: 'Boundary',        icon: <Target className="h-3.5 w-3.5" />,     color: 'text-purple-400 bg-purple-500/20 border-purple-500/30' },
};

// P2: strategy chip styling for visual_strict mode
const STRATEGY_CONFIG: Record<string, { label: string; color: string }> = {
  happy_path:     { label: 'Happy path',     color: 'text-emerald-700 bg-emerald-50 border-emerald-300' },
  variant:        { label: 'Variant',        color: 'text-blue-700 bg-blue-50 border-blue-300' },
  negative:       { label: 'Negative',       color: 'text-red-700 bg-red-50 border-red-300' },
  boundary:       { label: 'Boundary',       color: 'text-purple-700 bg-purple-50 border-purple-300' },
  state_explorer: { label: 'State explorer', color: 'text-indigo-700 bg-indigo-50 border-indigo-300' },
  cross_app:      { label: 'Cross-app',      color: 'text-amber-700 bg-amber-50 border-amber-300' },
  error_state:    { label: 'Error state',    color: 'text-rose-700 bg-rose-50 border-rose-300' },
};

// P3: Lifecycle states + visuals.  Must mirror the backend state set in
// nexus_sdk/db/models.py (E2E_LIFECYCLE_STATES).
type LifecycleState =
  | 'draft' | 'reviewed' | 'approved' | 'rejected'
  | 'automated' | 'live' | 'stable' | 'failing';

const LIFECYCLE_STATE_CONFIG: Record<LifecycleState, { label: string; color: string; description: string }> = {
  draft:     { label: 'Draft',     color: 'text-slate-600 bg-slate-100 border-slate-300',     description: 'Newly generated; not yet reviewed' },
  reviewed:  { label: 'Reviewed',  color: 'text-blue-700 bg-blue-50 border-blue-300',         description: 'Seen by a reviewer; no decision yet' },
  approved:  { label: 'Approved',  color: 'text-emerald-700 bg-emerald-50 border-emerald-300', description: 'Approved for automation/export' },
  rejected:  { label: 'Rejected',  color: 'text-red-700 bg-red-50 border-red-300',             description: 'Explicitly rejected; kept for audit' },
  automated: { label: 'Automated', color: 'text-indigo-700 bg-indigo-50 border-indigo-300',   description: 'Code generated and committed' },
  live:      { label: 'Live',      color: 'text-violet-700 bg-violet-50 border-violet-300',   description: 'In CI and currently passing' },
  stable:    { label: 'Stable',    color: 'text-teal-700 bg-teal-50 border-teal-300',         description: 'Live + passing for a sustained period' },
  failing:   { label: 'Failing',   color: 'text-orange-700 bg-orange-50 border-orange-300',   description: 'Automated but currently failing' },
};

// Allowed transitions — must mirror _ALLOWED_TRANSITIONS in
// platform/api/app/services/e2e_lifecycle.py
const ALLOWED_NEXT_STATES: Record<LifecycleState, LifecycleState[]> = {
  draft:     ['reviewed', 'approved', 'rejected'],
  reviewed:  ['draft', 'approved', 'rejected'],
  approved:  ['reviewed', 'rejected', 'automated'],
  rejected:  ['draft', 'reviewed'],
  automated: ['approved', 'live', 'failing'],
  live:      ['stable', 'failing', 'automated'],
  stable:    ['live', 'failing'],
  failing:   ['live', 'automated', 'reviewed'],
};

// States that qualify for export (must mirror E2E_EXPORTABLE_STATES backend).
const EXPORTABLE_STATES: ReadonlySet<LifecycleState> = new Set<LifecycleState>([
  'approved', 'automated', 'live', 'stable',
]);

// P6: Last-run status chip styles. Mirrors backend E2E_STEP_STATUS_*.
type RunStatusFilterKey =
  | 'passed' | 'failed' | 'skipped' | 'timed_out' | 'broken' | 'never_run';

const RUN_STATUS_CONFIG: Record<RunStatusFilterKey, { label: string; color: string; description: string }> = {
  passed:    { label: 'Passed',    color: 'text-emerald-700 bg-emerald-50 border-emerald-300', description: 'Last run passed in CI' },
  failed:    { label: 'Failed',    color: 'text-red-700 bg-red-50 border-red-300',             description: 'Last run failed in CI' },
  skipped:   { label: 'Skipped',   color: 'text-slate-700 bg-slate-100 border-slate-300',     description: 'Last run was skipped' },
  timed_out: { label: 'Timed out', color: 'text-orange-700 bg-orange-50 border-orange-300',   description: 'Last run timed out' },
  broken:    { label: 'Broken',    color: 'text-rose-700 bg-rose-50 border-rose-300',         description: 'Last run errored before completing' },
  never_run: { label: 'Never run', color: 'text-slate-500 bg-white border-gray-300',         description: 'No CI run on record' },
};

// ── CSV Export ─────────────────────────────────────────────

function exportE2EScenarioCSV(scenarios: E2EScenario[], filename: string) {
  const header = [
    'Scenario ID', 'Title', 'Category', 'Priority', 'Rationale',
    'Preconditions', 'Step #', 'Action', 'Input Data', 'Expected Behavior',
    'Expected Outcome', 'Data Matrix', 'Workflow Steps', 'Risk Areas', 'Evidence',
  ];
  const rows: string[][] = [];

  for (const sc of scenarios) {
    const dataMatrixStr = sc.data_matrix
      .map(dm => Object.entries(dm).map(([k, v]) => `${k}=${v}`).join(', '))
      .join(' | ');
    const wfSteps = sc.workflow_steps_covered.join(', ');
    const risks = sc.risk_areas_addressed.join('; ');
    const evidence = sc.evidence_sources.map(e => e.text).join('; ');

    if (sc.steps.length === 0) {
      rows.push([
        sc.scenario_id, sc.title, sc.category, sc.priority, sc.rationale,
        sc.preconditions.join('; '), '', '', '', '',
        sc.expected_outcome, dataMatrixStr, wfSteps, risks, evidence,
      ]);
    } else {
      for (const s of sc.steps) {
        rows.push([
          sc.scenario_id, sc.title, sc.category, sc.priority, sc.rationale,
          sc.preconditions.join('; '),
          String(s.step_number), s.action, s.input_data || '', s.expected_behavior || '',
          sc.expected_outcome, dataMatrixStr, wfSteps, risks, evidence,
        ]);
      }
    }
  }

  const escape = (v: string) => `"${v.replace(/"/g, '""')}"`;
  const csv = [header.map(escape).join(','), ...rows.map(r => r.map(escape).join(','))].join('\n');
  const blob = new Blob([csv], { type: 'text/csv;charset=utf-8;' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  a.click();
  URL.revokeObjectURL(url);
}

// ── Modality Badge ────────────────────────────────────────

function ModalityBadge({ modality }: { modality: string }) {
  const cfg: Record<string, { icon: React.ReactNode; color: string }> = {
    visual:     { icon: <Eye className="h-2.5 w-2.5" />,  color: 'text-blue-400 bg-blue-500/20 border-blue-500/30' },
    transcript: { icon: <Mic className="h-2.5 w-2.5" />,  color: 'text-green-400 bg-green-500/20 border-green-500/30' },
    multimodal: { icon: <Activity className="h-2.5 w-2.5" />, color: 'text-purple-400 bg-purple-500/20 border-purple-500/30' },
  };
  const c = cfg[modality] ?? cfg.transcript;
  return (
    <span className={clsx('inline-flex items-center gap-0.5 rounded-full border px-1.5 py-0.5 text-[9px]', c.color)}>
      {c.icon} {modality}
    </span>
  );
}

// ── Step → Scene Heuristic Mapping ─────────────────────────
// Map a scenario step to the most likely scene in the visual graph.
// Strategy order:
//   0. Direct citation: step.evidence_scene_id (Heart visual_strict output).
//   1. If scenario.workflow_steps_covered[step_index] exists, use that
//      as a scene_index lookup (1-based → 0-based conversion).
//   2. Substring match: search scene OCR text for step.action / input_data.
//   3. Proportional fallback: step N of M → scene N*sceneCount/M.

function mapStepToScene(
  scenario: E2EScenario,
  step: TestStep,
  scenes: VisualScene[],
): VisualScene | null {
  if (scenes.length === 0) return null;

  // Strategy 0 (highest priority): direct citation from Heart in visual_strict mode
  if (step.evidence_scene_id) {
    const direct = scenes.find(s => s.scene_id === step.evidence_scene_id);
    if (direct) return direct;
  }

  const stepIdx = Math.max(0, step.step_number - 1);

  // Strategy 1: explicit workflow_steps_covered mapping
  const wfStep = scenario.workflow_steps_covered[stepIdx];
  if (wfStep != null) {
    const byWf = scenes.find(s => s.scene_index === wfStep - 1 || s.scene_index === wfStep);
    if (byWf) return byWf;
  }

  // Strategy 2: OCR / action substring match
  const haystack = `${step.action ?? ''} ${step.input_data ?? ''}`.toLowerCase();
  const tokens = haystack
    .split(/[^a-z0-9]+/)
    .filter(t => t.length >= 4);
  if (tokens.length > 0) {
    for (const scene of scenes) {
      const ocr = (scene.ocr_text ?? '').toLowerCase();
      if (!ocr) continue;
      if (tokens.some(t => ocr.includes(t))) return scene;
    }
  }

  // Strategy 3: proportional fallback
  const totalSteps = Math.max(1, scenario.steps.length);
  const proportional = Math.min(
    scenes.length - 1,
    Math.floor((stepIdx / totalSteps) * scenes.length),
  );
  return scenes[proportional];
}

// Resolve the directly-cited EvidenceControl for a step, if present.
function mapStepToControl(
  step: TestStep,
  controlsByScene: Record<string, EvidenceControl[]>,
): EvidenceControl | null {
  if (!step.evidence_control_id) return null;
  for (const controls of Object.values(controlsByScene)) {
    const found = controls.find(c => c.control_id === step.evidence_control_id);
    if (found) return found;
  }
  return null;
}

// Resolve the directly-cited VisualFlowEdge for a step, if present.
function mapStepToEdge(
  step: TestStep,
  edges: VisualFlowEdge[],
): VisualFlowEdge | null {
  if (!step.evidence_edge_id) return null;
  return edges.find(e => e.edge_id === step.evidence_edge_id) ?? null;
}

// ── Selector Stability Score ───────────────────────────────
// Derived from EvidenceControl.selector_confidence + selector_source.

function stabilityBand(confidence: number, source: string): {
  label: string;
  color: string;
  dotColor: string;
} {
  if (source === 'unknown' || confidence < 0.4) {
    return { label: 'unstable', color: 'text-red-400', dotColor: 'bg-red-400' };
  }
  if (confidence < 0.7 || source === 'vision') {
    return { label: 'degraded', color: 'text-amber-400', dotColor: 'bg-amber-400' };
  }
  return { label: 'strong', color: 'text-green-400', dotColor: 'bg-green-400' };
}

// ═══════════════════════════════════════════════════════════════
//  Main Component
// ═══════════════════════════════════════════════════════════════

export default function E2EArchitectWorkspacePage() {
  const { sessionId } = useParams<{ sessionId: string }>();
  const [searchParams] = useSearchParams();
  const navigate = useNavigate();
  const { token, user } = useAuth();

  const artifactId = searchParams.get('artifact_id') || '';
  const isReady = searchParams.get('ready') !== '0';
  // P1.B3: default to visual_strict — the product is visual-evidence-first.
  // Pass ?evidence_mode=multimodal explicitly to opt into the persona+transcript path.
  const evidenceMode = searchParams.get('evidence_mode') || 'visual_strict';
  const isVisualStrict = evidenceMode === 'visual_strict';

  const [state, setState] = useState<PageState>('loading');
  const [result, setResult] = useState<E2EArchitectResponse | null>(null);
  const [evidenceGraph, setEvidenceGraph] = useState<VisualEvidenceGraph | null>(null);
  const [evidenceGraphError, setEvidenceGraphError] = useState<string | null>(null);
  const [error, setError] = useState('');
  const [searchQuery, setSearchQuery] = useState('');
  const [activeFilters, setActiveFilters] = useState<Set<string>>(new Set());
  const [expandedScenarios, setExpandedScenarios] = useState<Set<string>>(new Set());
  const [showMax, setShowMax] = useState(20);
  const [playwrightExporting, setPlaywrightExporting] = useState(false);
  const [playwrightGateErrors, setPlaywrightGateErrors] = useState<string[]>([]);

  // ── Phase A state ───────────────────────────────────────
  const [viewMode, setViewMode] = useState<ViewMode>('engineer');
  const [selectedStep, setSelectedStep] = useState<StepSelection | null>(null);
  const [selectedSceneId, setSelectedSceneId] = useState<string | null>(null);
  // Phase 3 — user-clicked control via the SceneFrameWithOverlays bbox.
  // Overrides the "best automation-ready" pick in resolvedControl when set;
  // cleared automatically when the scene changes (see effect below).
  const [selectedControlId, setSelectedControlId] = useState<string | null>(null);

  // P4: Co-Architect dock open/closed
  const [coArchitectOpen, setCoArchitectOpen] = useState(false);

  // P7: Demo Diff panel open/closed
  const [demoDiffOpen, setDemoDiffOpen] = useState(false);

  // P5: Multi-format export handler. Handles blob-typed error bodies
  // (axios returns the 422 JSON body as a Blob when responseType='blob').
  type ExportFormatId = 'playwright' | 'cypress' | 'gherkin' | 'json';

  const EXPORT_FORMAT_META: Record<ExportFormatId, { label: string; filename: string }> = {
    playwright: { label: 'Playwright (.spec.ts)', filename: 'playwright' },
    cypress:    { label: 'Cypress (.cy.ts)',      filename: 'cypress' },
    gherkin:    { label: 'Gherkin (.feature)',    filename: 'gherkin' },
    json:       { label: 'JSON test plan',        filename: 'test-plan-json' },
  };

  async function handleExport(format: ExportFormatId) {
    if (!artifactId) return;
    setPlaywrightExporting(true);
    setPlaywrightGateErrors([]);
    try {
      // Gherkin and JSON describe scenarios faithfully even when they don't
      // pass the 5-gate automation check — don't enforce gates there.
      const enforceGates = format === 'playwright' || format === 'cypress';
      const blob = await api.exportTests(artifactId, format, { enforceGates });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `${EXPORT_FORMAT_META[format].filename}-${artifactId.slice(0, 8)}.zip`;
      a.click();
      URL.revokeObjectURL(url);
    } catch (err: unknown) {
      // Axios with responseType='blob' returns the 4xx body as a Blob.
      // Parse it before reading the detail.
      const errAny = err as { response?: { data?: unknown } };
      let detail: { unmet_gates?: string[]; message?: string } | undefined;
      const data = errAny?.response?.data;
      if (data instanceof Blob) {
        try {
          const text = await data.text();
          const parsed = JSON.parse(text) as { detail?: typeof detail };
          detail = parsed.detail;
        } catch {
          /* swallow — leave detail undefined */
        }
      } else if (data && typeof data === 'object') {
        detail = (data as { detail?: typeof detail }).detail;
      }

      if (detail?.unmet_gates && detail.unmet_gates.length > 0) {
        setPlaywrightGateErrors(detail.unmet_gates);
      } else if (detail?.message) {
        setPlaywrightGateErrors([detail.message]);
      } else {
        setPlaywrightGateErrors([err instanceof Error ? err.message : 'Export failed']);
      }
    } finally {
      setPlaywrightExporting(false);
    }
  }

  // ── Load architect ──────────────────────────────────────
  const loadArchitect = useCallback(async (forceRegenerate = false) => {
    if (!artifactId) {
      setError('No artifact_id in URL');
      setState('error');
      return;
    }
    setState(forceRegenerate ? 'generating' : 'loading');
    setError('');
    try {
      const data = await api.generateE2EArchitect(artifactId, sessionId, forceRegenerate, evidenceMode);
      setResult(data);
      setState('ready');
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : 'Unknown error';
      const errAny = err as { response?: { status?: number; data?: { detail?: string } } };
      const status = errAny?.response?.status;
      const axiosMsg = errAny?.response?.data?.detail;
      const finalMsg = axiosMsg || msg;
      setError(finalMsg);

      // The Platform API raises HTTP 422 for every prerequisite failure
      // (missing persona, no workflow steps, fallback-quality persona, etc.).
      // Route any 422 to the prerequisites screen — it has the "Generate
      // Persona" CTA wired up. Also catch the message text in case future
      // prereqs use a different status code.
      const looksLikePrereq =
        /persona|workflow\s+steps?|prerequisite|insufficient|fallback/i.test(finalMsg) ||
        /not\s+found|missing|required|generate\s+a/i.test(finalMsg);

      if (status === 422 || looksLikePrereq) {
        setState('prerequisites');
      } else {
        setState('error');
      }
    }
  }, [artifactId, sessionId, evidenceMode]);

  // ── Load visual evidence graph (Phase A — for inspector + filmstrip) ───
  const loadEvidenceGraph = useCallback(async () => {
    if (!artifactId) return;
    try {
      const graph = await api.getVisualEvidenceGraph(artifactId);
      setEvidenceGraph(graph);
      setEvidenceGraphError(null);
    } catch (err: unknown) {
      // Non-fatal — Architect still renders without it; inspector falls back to citations.
      const msg = err instanceof Error ? err.message : 'Failed to load visual evidence graph';
      setEvidenceGraphError(msg);
    }
  }, [artifactId]);

  useEffect(() => {
    if (token && artifactId) {
      if (isReady) {
        loadArchitect();
        loadEvidenceGraph();
      } else {
        setState('prerequisites');
      }
    }
  }, [token, artifactId, isReady, loadArchitect, loadEvidenceGraph]);

  // ── Derived data ──────────────────────────────────────
  const arch = result?.e2e_architect;
  const prov = result?.provenance;
  const cov = arch?.coverage_analysis;

  const scenarios = useMemo(() => arch?.critical_combinations ?? [], [arch]);
  const variables = useMemo(() => arch?.variables ?? [], [arch]);
  const decisionPoints = useMemo(() => arch?.decision_points ?? [], [arch]);

  const scenes = useMemo<VisualScene[]>(
    () => (evidenceGraph?.scenes ?? []).slice().sort((a, b) => a.scene_index - b.scene_index),
    [evidenceGraph],
  );
  const controlsByScene = evidenceGraph?.controls_by_scene ?? {};
  const edges = evidenceGraph?.edges ?? [];
  const flows = evidenceGraph?.flows ?? [];
  const appInstances = evidenceGraph?.app_instances ?? [];

  // ── Step → Scene resolution for the currently selected step ────────────
  const selectedScenarioObj = useMemo<E2EScenario | null>(
    () => (selectedStep ? scenarios.find(s => s.scenario_id === selectedStep.scenarioId) ?? null : null),
    [selectedStep, scenarios],
  );
  const selectedStepObj = useMemo<TestStep | null>(
    () => {
      if (!selectedScenarioObj || !selectedStep) return null;
      return selectedScenarioObj.steps.find(s => s.step_number === selectedStep.stepNumber) ?? null;
    },
    [selectedScenarioObj, selectedStep],
  );

  const resolvedScene = useMemo<VisualScene | null>(() => {
    if (selectedSceneId) {
      return scenes.find(s => s.scene_id === selectedSceneId) ?? null;
    }
    if (selectedScenarioObj && selectedStepObj) {
      return mapStepToScene(selectedScenarioObj, selectedStepObj, scenes);
    }
    return null;
  }, [selectedSceneId, selectedScenarioObj, selectedStepObj, scenes]);

  // Clear the user-clicked control whenever the active scene changes —
  // an overlay click is only meaningful in the context of its scene.
  useEffect(() => {
    setSelectedControlId(null);
  }, [resolvedScene?.scene_id]);

  // ── Control lookup: user click > direct citation > "best" in scene ────
  const resolvedControl = useMemo<EvidenceControl | null>(() => {
    // Strategy -1: user explicitly clicked an overlay box (Phase 3)
    if (selectedControlId && resolvedScene) {
      const all = controlsByScene[resolvedScene.scene_id] ?? [];
      const hit = all.find(c => c.control_id === selectedControlId);
      if (hit) return hit;
    }
    // Strategy 0: direct citation from Heart visual_strict output
    if (selectedStepObj) {
      const direct = mapStepToControl(selectedStepObj, controlsByScene);
      if (direct) return direct;
    }
    if (!resolvedScene) return null;
    const controls = controlsByScene[resolvedScene.scene_id] ?? [];
    if (controls.length === 0) return null;
    // Prefer automation_ready controls with highest selector_confidence
    const sorted = controls.slice().sort((a, b) => {
      if (a.automation_ready !== b.automation_ready) return a.automation_ready ? -1 : 1;
      return (b.selector_confidence ?? 0) - (a.selector_confidence ?? 0);
    });
    return sorted[0];
  }, [selectedControlId, selectedStepObj, resolvedScene, controlsByScene]);

  // ── Edge lookup: prefer direct citation, fall back to outgoing edge ──────
  const resolvedEdge = useMemo<VisualFlowEdge | null>(() => {
    // Strategy 0: direct citation
    if (selectedStepObj) {
      const direct = mapStepToEdge(selectedStepObj, edges);
      if (direct) return direct;
    }
    if (!resolvedScene) return null;
    return edges.find(e => e.from_scene_id === resolvedScene.scene_id) ?? null;
  }, [selectedStepObj, resolvedScene, edges]);

  // ── Coverage / risk per workflow step (for heatmap) ────────────────────
  const stepRiskMatrix = useMemo(() => {
    // In visual_strict mode, base the heatmap on scene_index from per-step
    // citations rather than persona workflow_steps_covered (which is empty).
    if (isVisualStrict) {
      const sceneCells = new Map<number, { count: number; maxPriority: number }>();
      for (const sc of scenarios) {
        const pri = PRIORITY_CONFIG[sc.priority]?.weight ?? 0;
        const sceneIdxs = new Set<number>();
        for (const step of sc.steps) {
          if (!step.evidence_scene_id) continue;
          const scene = scenes.find(s => s.scene_id === step.evidence_scene_id);
          if (scene) sceneIdxs.add(scene.scene_index);
        }
        for (const idx of sceneIdxs) {
          const cell = sceneCells.get(idx) ?? { count: 0, maxPriority: 0 };
          cell.count += 1;
          if (pri > cell.maxPriority) cell.maxPriority = pri;
          sceneCells.set(idx, cell);
        }
      }
      return Array.from(sceneCells.entries())
        .sort((a, b) => a[0] - b[0])
        .map(([idx, cell]) => ({ step: idx + 1, count: cell.count, maxPriority: cell.maxPriority }));
    }
    const allSteps = new Set<number>();
    for (const sc of scenarios) {
      for (const wf of sc.workflow_steps_covered) allSteps.add(wf);
    }
    const list = Array.from(allSteps).sort((a, b) => a - b);
    return list.map(stepNum => {
      let maxPriority = 0;
      let count = 0;
      for (const sc of scenarios) {
        if (!sc.workflow_steps_covered.includes(stepNum)) continue;
        count += 1;
        const pri = PRIORITY_CONFIG[sc.priority]?.weight ?? 0;
        if (pri > maxPriority) maxPriority = pri;
      }
      return { step: stepNum, count, maxPriority };
    });
  }, [scenarios, scenes, isVisualStrict]);

  // ── Visual coverage stats (visual_strict only) ──────────────────────────
  const visualCoverage = useMemo(() => {
    const totalScenes = scenes.length;
    const totalAutoControls = Object.values(controlsByScene)
      .flat()
      .filter(c => c.automation_ready).length;
    const totalConfirmedEdges = edges.filter(e => e.edge_type === 'action_confirmed_transition').length;

    const coveredScenes = new Set<string>();
    const coveredControls = new Set<string>();
    const coveredEdges = new Set<string>();
    let groundedSteps = 0;
    let totalSteps = 0;
    let totalProofConfidence = 0;

    for (const sc of scenarios) {
      for (const step of sc.steps) {
        totalSteps += 1;
        if (step.evidence_scene_id) coveredScenes.add(step.evidence_scene_id);
        if (step.evidence_control_id) coveredControls.add(step.evidence_control_id);
        if (step.evidence_edge_id) coveredEdges.add(step.evidence_edge_id);
        if (step.evidence_scene_id && step.evidence_control_id) {
          groundedSteps += 1;
          totalProofConfidence += step.proof_confidence ?? 0;
        }
      }
    }

    return {
      totalScenes,
      totalAutoControls,
      totalConfirmedEdges,
      coveredScenes: coveredScenes.size,
      coveredControls: coveredControls.size,
      coveredEdges: coveredEdges.size,
      sceneCoveragePct: totalScenes ? Math.round((coveredScenes.size / totalScenes) * 100) : 0,
      controlCoveragePct: totalAutoControls ? Math.round((coveredControls.size / totalAutoControls) * 100) : 0,
      edgeCoveragePct: totalConfirmedEdges ? Math.round((coveredEdges.size / totalConfirmedEdges) * 100) : 0,
      groundedSteps,
      totalSteps,
      groundedPct: totalSteps ? Math.round((groundedSteps / totalSteps) * 100) : 0,
      avgProofConfidence: groundedSteps > 0 ? totalProofConfidence / groundedSteps : 0,
    };
  }, [scenarios, scenes, controlsByScene, edges]);

  // ── Filtering ─────────────────────────────────────────
  const filteredScenarios = useMemo(() => {
    let list = scenarios;
    if (activeFilters.size > 0) {
      list = list.filter(sc => {
        const lastRunKey = sc.last_run?.last_run_status ?? 'never_run';
        const isFlakySelected = activeFilters.has('run:flaky');
        return (
          activeFilters.has(sc.category) ||
          activeFilters.has(sc.priority) ||
          (sc.strategy && activeFilters.has(`strategy:${sc.strategy}`)) ||
          (sc.state && activeFilters.has(`state:${sc.state}`)) ||
          activeFilters.has(`run:${lastRunKey}`) ||
          (isFlakySelected && !!sc.last_run?.is_flaky)
        );
      });
    }
    if (searchQuery.trim()) {
      const q = searchQuery.toLowerCase();
      list = list.filter(sc =>
        sc.title.toLowerCase().includes(q) ||
        sc.rationale.toLowerCase().includes(q) ||
        sc.scenario_id.toLowerCase().includes(q) ||
        sc.risk_areas_addressed.some(r => r.toLowerCase().includes(q))
      );
    }
    return list;
  }, [scenarios, activeFilters, searchQuery]);

  const toggleFilter = (key: string) => {
    setActiveFilters(prev => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  };

  const toggleScenario = (id: string) => {
    setExpandedScenarios(prev => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const handleSelectStep = useCallback((scenarioId: string, stepNumber: number) => {
    setSelectedStep({ scenarioId, stepNumber });
    setSelectedSceneId(null); // let step→scene mapping drive
  }, []);

  const handleSelectScene = useCallback((sceneId: string) => {
    setSelectedSceneId(sceneId);
    setSelectedStep(null);
  }, []);

  // ── P3: Lifecycle handlers ──────────────────────────────
  // Optimistic update on the local scenarios array, then reconcile against
  // the server response. On failure, revert and surface the error.
  const handleTransition = useCallback(async (
    scenarioId: string,
    newState: ScenarioLifecycleState,
    note: string = '',
  ) => {
    if (!result || !artifactId) return;
    // Take a snapshot for rollback
    const before = result;
    // Optimistic local mutation
    setResult(prev => {
      if (!prev) return prev;
      const updated = { ...prev };
      updated.e2e_architect = {
        ...prev.e2e_architect,
        critical_combinations: prev.e2e_architect.critical_combinations.map(sc =>
          sc.scenario_id === scenarioId
            ? { ...sc, state: newState }
            : sc,
        ),
      };
      return updated;
    });
    try {
      const resp = await api.transitionScenario(artifactId, scenarioId, newState, note);
      // Reconcile with server truth — pick up audit_log + state_changed_by
      setResult(prev => {
        if (!prev) return prev;
        return {
          ...prev,
          e2e_architect: {
            ...prev.e2e_architect,
            critical_combinations: prev.e2e_architect.critical_combinations.map(sc =>
              sc.scenario_id === scenarioId
                ? {
                    ...sc,
                    state: resp.state.state,
                    state_changed_at: resp.state.state_changed_at,
                    state_changed_by: resp.state.state_changed_by,
                    state_changed_by_email: resp.state.state_changed_by_email,
                    comments: resp.state.comments_json,
                    audit_log: resp.state.audit_log_json,
                  }
                : sc,
            ),
          },
        };
      });
    } catch (err: unknown) {
      // Rollback
      setResult(before);
      const detail = (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail;
      const msg = detail || (err instanceof Error ? err.message : 'Transition failed');
      // Surface to user — reuse the playwright gate error banner for now
      setPlaywrightGateErrors([`State transition failed: ${msg}`]);
    }
  }, [result, artifactId]);

  const handleAddComment = useCallback(async (
    scenarioId: string,
    body: string,
  ) => {
    if (!result || !artifactId) return;
    try {
      const resp = await api.commentOnScenario(artifactId, scenarioId, body);
      setResult(prev => {
        if (!prev) return prev;
        return {
          ...prev,
          e2e_architect: {
            ...prev.e2e_architect,
            critical_combinations: prev.e2e_architect.critical_combinations.map(sc =>
              sc.scenario_id === scenarioId
                ? {
                    ...sc,
                    state: resp.state.state,
                    comments: resp.state.comments_json,
                    audit_log: resp.state.audit_log_json,
                  }
                : sc,
            ),
          },
        };
      });
    } catch (err: unknown) {
      const detail = (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail;
      const msg = detail || (err instanceof Error ? err.message : 'Comment failed');
      setPlaywrightGateErrors([`Comment failed: ${msg}`]);
    }
  }, [result, artifactId]);

  // ── Loading / Error / Prerequisites states ─────────────
  if (state === 'prerequisites') {
    return (
      <div className="flex h-full items-center justify-center bg-[#f5f7fa] px-6">
        <div className="flex flex-col items-center gap-4 text-center max-w-lg">
          <div className="flex h-14 w-14 items-center justify-center rounded-full bg-amber-100">
            <Shield className="h-7 w-7 text-amber-500" />
          </div>
          <h2 className="text-xl font-semibold text-slate-800">Persona draft required</h2>
          <p className="text-sm text-slate-600 leading-relaxed">
            Test Studio synthesizes E2E scenarios from a <strong>Process Oracle persona</strong> — a
            structured outline of the workflow extracted from your demo. Generate the persona first,
            then return here to build tests grounded in its steps.
          </p>
          {error && (
            <div className="w-full rounded-lg border border-amber-300 bg-amber-50 px-4 py-2 text-left">
              <p className="text-[10px] font-semibold uppercase tracking-wider text-amber-700">Backend response</p>
              <p className="text-xs text-amber-700 font-mono mt-0.5">{error}</p>
            </div>
          )}
          <div className="flex gap-3 mt-1">
            <button
              onClick={() => navigate(-1)}
              className="rounded-lg border border-gray-300 bg-white px-4 py-2 text-sm text-slate-700 hover:bg-gray-50"
            >
              Go Back
            </button>
            <button
              onClick={() => navigate(`/sessions/${sessionId}/persona-workspace?artifact_id=${artifactId}`)}
              className="rounded-lg bg-nexus-600 px-5 py-2 text-sm font-medium text-white hover:bg-nexus-500 shadow-sm flex items-center gap-1.5"
            >
              <Sparkles className="h-4 w-4" /> Generate Persona
            </button>
            <button
              onClick={() => loadArchitect()}
              className="rounded-lg border border-gray-300 bg-white px-4 py-2 text-sm text-slate-700 hover:bg-gray-50"
            >
              Try Again
            </button>
          </div>
          <p className="text-[11px] text-slate-400 mt-2">
            Artifact <span className="font-mono">{artifactId.slice(0, 8)}</span> • Session <span className="font-mono">{sessionId?.slice(0, 8)}</span>
          </p>
        </div>
      </div>
    );
  }

  if (state === 'loading' || state === 'generating') {
    return (
      <div className="flex h-full items-center justify-center bg-[#f5f7fa]">
        <div className="flex flex-col items-center gap-4 text-center">
          <Loader2 className="h-10 w-10 animate-spin text-nexus-500" />
          <h2 className="text-lg font-semibold text-slate-700">
            {state === 'generating' ? 'Regenerating Test Studio...' : 'Loading Test Studio...'}
          </h2>
          <p className="text-sm text-slate-400 max-w-md">
            Two-pass LLM analysis: extracting testable variables, then generating critical E2E scenarios with pairwise data combinations.
          </p>
        </div>
      </div>
    );
  }

  if (state === 'error' || !result || !arch || !prov || !cov) {
    return (
      <div className="flex h-full items-center justify-center bg-[#f5f7fa]">
        <div className="flex flex-col items-center gap-4 text-center max-w-md">
          <AlertTriangle className="h-10 w-10 text-red-400" />
          <h2 className="text-lg font-semibold text-slate-700">Test Studio Error</h2>
          <p className="text-sm text-slate-8000">{error || 'Failed to load E2E Architect analysis.'}</p>
          <div className="flex gap-3">
            <button
              onClick={() => navigate(-1)}
              className="rounded-lg bg-gray-100 px-4 py-2 text-sm text-slate-600 hover:bg-gray-100"
            >
              Go Back
            </button>
            <button
              onClick={() => loadArchitect(true)}
              className="rounded-lg bg-nexus-600 px-4 py-2 text-sm text-white hover:bg-nexus-500"
            >
              Retry
            </button>
          </div>
        </div>
      </div>
    );
  }

  // ═══════════════════════════════════════════════════════
  //  Ready state — Test Studio layout
  // ═══════════════════════════════════════════════════════

  const isEngineer = viewMode === 'engineer';

  return (
    <div className="relative flex h-full flex-col overflow-hidden bg-[#f5f7fa]">

      {/* ── Top Bar ──────────────────────────────────────── */}
      <div className="shrink-0 border-b border-gray-200 bg-white px-6 py-3">
        <div className="flex items-center gap-4">
          <button
            onClick={() => navigate(-1)}
            className="flex items-center gap-1.5 text-sm text-slate-8000 hover:text-slate-700 transition-colors"
          >
            <ArrowLeft className="h-4 w-4" /> Back
          </button>
          <div className="h-5 w-px bg-gray-200" />
          <Shuffle className="h-5 w-5 text-nexus-400" />
          <div className="flex-1 min-w-0">
            <h1 className="text-sm font-semibold text-slate-700">
              Test Studio
            </h1>
            <p className="text-[11px] text-slate-400">
              {cov.total_scenarios} scenarios • {cov.variables_tested} variables • {cov.pairwise_combinations_generated} pairwise
              {scenes.length > 0 && ` • ${scenes.length} scenes`}
              {result.cached && ' • ⚡ cached'}
            </p>
          </div>

          {/* ── Mode toggle ─────────────────────────────── */}
          <div
            className="flex items-center rounded-lg border border-gray-200 bg-gray-50 p-0.5 text-xs"
            role="tablist"
            aria-label="View mode"
          >
            <button
              role="tab"
              aria-selected={isEngineer}
              onClick={() => setViewMode('engineer')}
              className={clsx(
                'flex items-center gap-1.5 rounded-md px-2.5 py-1 transition-colors',
                isEngineer
                  ? 'bg-white text-slate-700 shadow-sm font-medium'
                  : 'text-slate-500 hover:text-slate-700',
              )}
              title="Engineer mode — selectors, exports, dense view"
            >
              <Code2 className="h-3.5 w-3.5" /> Engineer
            </button>
            <button
              role="tab"
              aria-selected={!isEngineer}
              onClick={() => setViewMode('reviewer')}
              className={clsx(
                'flex items-center gap-1.5 rounded-md px-2.5 py-1 transition-colors',
                !isEngineer
                  ? 'bg-white text-slate-700 shadow-sm font-medium'
                  : 'text-slate-500 hover:text-slate-700',
              )}
              title="Reviewer mode — plain-English, evidence-first, sign-off"
            >
              <Users className="h-3.5 w-3.5" /> Reviewer
            </button>
            <button
              role="tab"
              aria-selected={viewMode === 'test-cases'}
              onClick={() => setViewMode('test-cases')}
              className={clsx(
                'flex items-center gap-1.5 rounded-md px-2.5 py-1 transition-colors',
                viewMode === 'test-cases'
                  ? 'bg-white text-emerald-700 shadow-sm font-medium'
                  : 'text-slate-500 hover:text-slate-700',
              )}
              title="Test Cases — grounded from Pages & Forms (demonstrated + combinations)"
            >
              <Sparkles className="h-3.5 w-3.5" /> Test Cases
            </button>
          </div>

          {/* P4: Co-Architect dock toggle */}
          <button
            onClick={() => setCoArchitectOpen(o => !o)}
            className={clsx(
              'rounded-lg px-3 py-1.5 text-xs font-medium transition-colors flex items-center gap-1.5',
              coArchitectOpen
                ? 'bg-violet-600 text-white hover:bg-violet-500'
                : 'bg-violet-100 text-violet-700 hover:bg-violet-200',
            )}
            title="Chat with the Co-Architect — visual-graph-grounded AI assistant"
          >
            <Bot className="h-3.5 w-3.5" /> Co-Architect
          </button>

          {/* P7: Demo Diff toggle */}
          <button
            onClick={() => setDemoDiffOpen(o => !o)}
            className={clsx(
              'rounded-lg px-3 py-1.5 text-xs font-medium transition-colors flex items-center gap-1.5',
              demoDiffOpen
                ? 'bg-orange-600 text-white hover:bg-orange-500'
                : 'bg-orange-100 text-orange-700 hover:bg-orange-200',
            )}
            title="Compare this artifact's visual graph against an earlier recording"
          >
            <GitCompare className="h-3.5 w-3.5" /> Demo Diff
          </button>

          <button
            onClick={() => loadArchitect(true)}
            className="rounded-lg bg-nexus-600/80 px-3 py-1.5 text-xs font-medium text-white hover:bg-nexus-500 transition-colors flex items-center gap-1.5"
          >
            <Sparkles className="h-3.5 w-3.5" /> Regenerate
          </button>

          {isEngineer && (
            <>
              <button
                onClick={() => exportE2EScenarioCSV(scenarios, `e2e-scenarios-${artifactId.slice(0, 8)}.csv`)}
                className="rounded-lg bg-gray-100 px-3 py-1.5 text-xs font-medium text-slate-600 hover:bg-gray-200 transition-colors flex items-center gap-1.5"
                title="Download all E2E scenarios as CSV"
              >
                <Download className="h-3.5 w-3.5" /> CSV
              </button>
              <ExportDropdown
                disabled={playwrightExporting}
                onExport={handleExport}
                exporting={playwrightExporting}
              />
            </>
          )}

          {!isEngineer && (
            <button
              disabled
              className="rounded-lg bg-emerald-100 px-3 py-1.5 text-xs font-medium text-emerald-700 transition-colors flex items-center gap-1.5 cursor-not-allowed opacity-70"
              title="Sign-off flow ships in Phase B"
            >
              <ThumbsUp className="h-3.5 w-3.5" /> Approve plan
            </button>
          )}

          <button
            onClick={() => navigate(`/sessions/${sessionId}/test-strategy?artifact_id=${artifactId}`)}
            className="rounded-lg bg-gray-100 px-3 py-1.5 text-xs font-medium text-slate-600 hover:bg-gray-200 transition-colors flex items-center gap-1.5"
          >
            <ArrowRight className="h-3.5 w-3.5" /> Strategy
          </button>
        </div>
      </div>

      {/* ── Playwright Gate Failure Banner ─────────────────── */}
      {playwrightGateErrors.length > 0 && (
        <div className="shrink-0 border-b border-red-500/20 bg-red-50 px-6 py-2">
          <div className="flex items-start gap-2">
            <AlertTriangle className="h-4 w-4 text-red-500 shrink-0 mt-0.5" />
            <div className="flex-1">
              <p className="text-[11px] font-semibold text-red-700">
                Playwright export blocked — {playwrightGateErrors.length} gate{playwrightGateErrors.length > 1 ? 's' : ''} not met:
              </p>
              <ul className="mt-1 space-y-0.5">
                {playwrightGateErrors.slice(0, 5).map((err, i) => (
                  <li key={i} className="text-[10px] text-red-600/80 font-mono">{err}</li>
                ))}
                {playwrightGateErrors.length > 5 && (
                  <li className="text-[10px] text-red-600/60">…and {playwrightGateErrors.length - 5} more</li>
                )}
              </ul>
            </div>
            <button onClick={() => setPlaywrightGateErrors([])} className="text-slate-500 hover:text-slate-700 text-[11px]">✕</button>
          </div>
        </div>
      )}

      {/* ── Visual Substrate Quality Banner ───────────────── */}
      {result.visual_substrate && result.visual_substrate.quality !== 'multimodal' && result.visual_substrate.quality !== 'deep' && (
        <div className="shrink-0 border-b border-amber-500/20 bg-amber-50 px-6 py-2 flex items-center gap-3">
          <AlertTriangle className="h-4 w-4 text-amber-500 shrink-0" />
          <p className="text-[11px] text-amber-700 flex-1">
            <span className="font-semibold">Visual substrate: {result.visual_substrate.quality}</span>
            {' '}&mdash; {result.visual_substrate.frame_count} frames{result.visual_substrate.has_ocr ? '' : ', OCR skipped'}.
            {result.visual_substrate.recommendation && (
              <span className="text-amber-600/80 ml-1">{result.visual_substrate.recommendation}</span>
            )}
          </p>
        </div>
      )}

      {/* ── Test Cases (Pages & Forms) — grounded test factory ─ */}
      {viewMode === 'test-cases' && (
        <div className="flex-1 min-h-0 overflow-y-auto p-6">
          <TestCasesPanel artifactId={artifactId} />
        </div>
      )}

      {/* ── 3-Panel Layout (left | center | right) ─────────── */}
      <div
        className="flex flex-1 min-h-0 overflow-hidden"
        style={{ display: viewMode === 'test-cases' ? 'none' : undefined }}
      >

        {/* ═══ LEFT PANEL: System Model ═══ */}
        <div className="w-80 shrink-0 border-r border-gray-200 overflow-y-auto p-4 space-y-4 bg-white/40">

          {/* Apps & Flows tree (G14: with per-scene quality summary) */}
          {(flows.length > 0 || appInstances.length > 0) && (
            <div className="rounded-xl border border-gray-200 bg-white/90 p-4 space-y-2">
              <h2 className="text-xs font-semibold uppercase tracking-wider text-slate-500 flex items-center gap-1.5">
                <Layers className="h-3.5 w-3.5" /> System Model
              </h2>
              <div className="space-y-1.5">
                {appInstances.map(app => {
                  const appFlows = flows.filter(f =>
                    f.first_scene_index >= app.first_scene_index &&
                    f.last_scene_index <= app.last_scene_index,
                  );
                  const appScenes = scenes.filter(s =>
                    s.scene_index >= app.first_scene_index &&
                    s.scene_index <= app.last_scene_index,
                  );
                  const qStrong = appScenes.filter(s => s.scene_quality === 'strong').length;
                  const qDegraded = appScenes.filter(s => s.scene_quality === 'degraded').length;
                  const qWeak = appScenes.filter(s => s.scene_quality === 'weak').length;
                  return (
                    <div key={app.instance_id} className="text-[11px]">
                      <div className="flex items-center gap-1.5 text-slate-700">
                        <span className="text-slate-400">●</span>
                        <span className="font-medium truncate">{app.app_name || app.app_type || 'app'}</span>
                        <span className="text-slate-400">({app.scene_count})</span>
                      </div>
                      {/* Scene quality summary (G14) */}
                      {(qStrong + qDegraded + qWeak) > 0 && (
                        <div className="ml-3 mt-0.5 flex items-center gap-1 text-[9px]">
                          {qStrong > 0 && (
                            <span className="inline-flex items-center gap-0.5 rounded px-1 py-px bg-emerald-50 border border-emerald-200 text-emerald-700" title="Strong scenes">
                              <span className="w-1.5 h-1.5 rounded-full bg-emerald-500" />{qStrong}
                            </span>
                          )}
                          {qDegraded > 0 && (
                            <span className="inline-flex items-center gap-0.5 rounded px-1 py-px bg-amber-50 border border-amber-200 text-amber-700" title="Degraded scenes">
                              <span className="w-1.5 h-1.5 rounded-full bg-amber-500" />{qDegraded}
                            </span>
                          )}
                          {qWeak > 0 && (
                            <span className="inline-flex items-center gap-0.5 rounded px-1 py-px bg-red-50 border border-red-200 text-red-700" title="Weak scenes">
                              <span className="w-1.5 h-1.5 rounded-full bg-red-500" />{qWeak}
                            </span>
                          )}
                          {app.segmentation_basis && app.segmentation_basis !== 'no_boundary' && (
                            <span className="text-slate-400 ml-auto" title={`Boundary: ${app.segmentation_basis} (confidence ${(app.segmentation_confidence ?? 0).toFixed(2)})`}>
                              {app.segmentation_basis.replace(/_/g, ' ')}
                            </span>
                          )}
                        </div>
                      )}
                      {appFlows.map(flow => {
                        const flowScenes = scenes.filter(s =>
                          s.scene_index >= flow.first_scene_index &&
                          s.scene_index <= flow.last_scene_index,
                        );
                        const fStrong = flowScenes.filter(s => s.scene_quality === 'strong').length;
                        const fDegraded = flowScenes.filter(s => s.scene_quality === 'degraded').length;
                        const fWeak = flowScenes.filter(s => s.scene_quality === 'weak').length;
                        return (
                          <div key={flow.flow_id} className="ml-3 mt-0.5 text-slate-500">
                            <div className="flex items-center gap-1.5">
                              <span>↳</span>
                              <span className="truncate">{flow.flow_label}</span>
                              {flow.is_noise && (
                                <span className="text-[9px] text-slate-400">noise</span>
                              )}
                              {flow.is_interleaved && (
                                <span className="text-[9px] text-amber-600" title={`Visited ${flow.visit_count ?? 2} times — interleaved flow`}>×{flow.visit_count ?? 2}</span>
                              )}
                            </div>
                            {(fStrong + fDegraded + fWeak) > 0 && (
                              <div className="ml-4 mt-0.5 flex items-center gap-1 text-[9px] text-slate-500">
                                <span>{flowScenes.length} scenes:</span>
                                {fStrong > 0 && <span className="text-emerald-600">{fStrong} strong</span>}
                                {fDegraded > 0 && <span className="text-amber-600">{fDegraded} degraded</span>}
                                {fWeak > 0 && <span className="text-red-600">{fWeak} weak</span>}
                              </div>
                            )}
                          </div>
                        );
                      })}
                    </div>
                  );
                })}
                {appInstances.length === 0 && flows.map(flow => (
                  <div key={flow.flow_id} className="text-[11px] text-slate-600 truncate">
                    ↳ {flow.flow_label}
                  </div>
                ))}
              </div>
            </div>
          )}

          {/* ── Risk × Coverage Heatmap ────────────────────── */}
          {stepRiskMatrix.length > 0 && (
            <div className="rounded-xl border border-gray-200 bg-white/90 p-4 space-y-2">
              <h2 className="text-xs font-semibold uppercase tracking-wider text-orange-500 flex items-center gap-1.5">
                <Target className="h-3.5 w-3.5" /> Risk × Coverage
              </h2>
              <p className="text-[10px] text-slate-400">Workflow step coverage shaded by highest scenario priority.</p>
              <div className="grid grid-cols-8 gap-1">
                {stepRiskMatrix.map(({ step, count, maxPriority }) => {
                  const intensity = Math.min(1, count / 3);
                  const colorByPriority = (() => {
                    if (maxPriority >= 4) return `rgba(239, 68, 68, ${0.25 + 0.55 * intensity})`;
                    if (maxPriority >= 3) return `rgba(249, 115, 22, ${0.25 + 0.55 * intensity})`;
                    if (maxPriority >= 2) return `rgba(234, 179, 8, ${0.25 + 0.55 * intensity})`;
                    if (maxPriority >= 1) return `rgba(59, 130, 246, ${0.25 + 0.55 * intensity})`;
                    return 'rgba(148, 163, 184, 0.15)';
                  })();
                  return (
                    <div
                      key={step}
                      className="aspect-square rounded flex items-center justify-center text-[10px] font-mono text-slate-700 border border-gray-200"
                      style={{ background: colorByPriority }}
                      title={`Step ${step} — ${count} scenario${count !== 1 ? 's' : ''}, top priority weight ${maxPriority}`}
                    >
                      {step}
                    </div>
                  );
                })}
              </div>
              <div className="flex items-center gap-2 text-[9px] text-slate-500 pt-1">
                <span className="inline-flex items-center gap-1"><span className="w-2 h-2 rounded bg-red-500/60" />P0</span>
                <span className="inline-flex items-center gap-1"><span className="w-2 h-2 rounded bg-orange-500/60" />P1</span>
                <span className="inline-flex items-center gap-1"><span className="w-2 h-2 rounded bg-yellow-500/60" />P2</span>
                <span className="inline-flex items-center gap-1"><span className="w-2 h-2 rounded bg-blue-500/60" />P3</span>
                <span className="ml-auto text-slate-400">opacity = #tests</span>
              </div>
            </div>
          )}

          {/* ── Visual Coverage (visual_strict mode) ─────────────────────── */}
          {isVisualStrict && (
            <div className="rounded-xl border border-emerald-200 bg-emerald-50/40 p-4 space-y-3">
              <h2 className="text-xs font-semibold uppercase tracking-wider text-emerald-700 flex items-center gap-1.5">
                <CheckCircle2 className="h-3.5 w-3.5" /> Visual Coverage
              </h2>
              <p className="text-[10px] text-emerald-700/80">
                % of the visual evidence graph that's exercised by approved scenarios.
              </p>

              {/* Grounded steps headline */}
              <div className="rounded-lg bg-white border border-emerald-200 px-3 py-2">
                <div className="flex items-baseline justify-between">
                  <span className="text-[10px] uppercase tracking-wider text-slate-500">Grounded steps</span>
                  <span className="text-lg font-bold text-emerald-700 font-mono">
                    {visualCoverage.groundedSteps}/{visualCoverage.totalSteps}
                  </span>
                </div>
                <div className="mt-1 h-1.5 rounded-full bg-emerald-100 overflow-hidden">
                  <div
                    className="h-full rounded-full bg-emerald-500 transition-all"
                    style={{ width: `${visualCoverage.groundedPct}%` }}
                  />
                </div>
                {visualCoverage.avgProofConfidence > 0 && (
                  <p className="text-[10px] text-slate-500 mt-1">
                    avg proof confidence {(visualCoverage.avgProofConfidence * 100).toFixed(0)}%
                  </p>
                )}
              </div>

              {/* Per-asset coverage bars */}
              {[
                { label: 'Scenes covered', covered: visualCoverage.coveredScenes, total: visualCoverage.totalScenes, pct: visualCoverage.sceneCoveragePct, color: 'bg-blue-500' },
                { label: 'Controls used', covered: visualCoverage.coveredControls, total: visualCoverage.totalAutoControls, pct: visualCoverage.controlCoveragePct, color: 'bg-nexus-500' },
                { label: 'Edges asserted', covered: visualCoverage.coveredEdges, total: visualCoverage.totalConfirmedEdges, pct: visualCoverage.edgeCoveragePct, color: 'bg-purple-500' },
              ].map(({ label, covered, total, pct, color }) => (
                <div key={label}>
                  <div className="flex items-center justify-between text-[11px] mb-1">
                    <span className="text-slate-600">{label}</span>
                    <span className="font-mono text-slate-700">{covered}/{total} <span className="text-slate-400">({pct}%)</span></span>
                  </div>
                  <div className="h-1.5 rounded-full bg-gray-200 overflow-hidden">
                    <div className={clsx('h-full rounded-full transition-all', color)} style={{ width: `${pct}%` }} />
                  </div>
                </div>
              ))}
            </div>
          )}

          {/* Variables (multimodal mode only — empty in visual_strict) */}
          {!isVisualStrict && (
          <div className="rounded-xl border border-gray-200 bg-white/90 p-4 space-y-3">
            <h2 className="text-xs font-semibold uppercase tracking-wider text-nexus-500 flex items-center gap-1.5">
              <Variable className="h-3.5 w-3.5" /> Variables ({variables.length})
            </h2>
            <div className="space-y-2">
              {variables.map((v, i) => (
                <div key={i} className="rounded-lg border border-gray-200 bg-gray-50 p-2.5 space-y-1">
                  <div className="flex items-center gap-2">
                    <span className="text-[11px] font-semibold text-slate-700">{v.name}</span>
                    {isEngineer && (
                      <span className="text-[9px] px-1.5 py-0.5 rounded-full bg-gray-100 text-slate-600 border border-gray-200">
                        {v.type}
                      </span>
                    )}
                  </div>
                  <div className="flex flex-wrap gap-1">
                    {v.observed_values.map((val, j) => (
                      <span key={`o-${j}`} className="text-[9px] px-1.5 py-0.5 rounded-full bg-green-500/15 text-green-700 border border-green-500/20">
                        {val}
                      </span>
                    ))}
                    {v.inferred_values.map((val, j) => (
                      <span key={`i-${j}`} className="text-[9px] px-1.5 py-0.5 rounded-full bg-amber-500/15 text-amber-700 border border-amber-500/20">
                        {val} ?
                      </span>
                    ))}
                  </div>
                  {v.impacts.length > 0 && (
                    <p className="text-[10px] text-slate-500">
                      Impacts: {v.impacts.join(', ')}
                    </p>
                  )}
                </div>
              ))}
              {variables.length === 0 && (
                <p className="text-[11px] text-slate-400 italic">No variables extracted</p>
              )}
            </div>
          </div>
          )}

          {/* Decision Points (multimodal mode only) */}
          {!isVisualStrict && (
          <div className="rounded-xl border border-gray-200 bg-white/90 p-4 space-y-3">
            <h2 className="text-xs font-semibold uppercase tracking-wider text-amber-500 flex items-center gap-1.5">
              <GitBranch className="h-3.5 w-3.5" /> Decision Points ({decisionPoints.length})
            </h2>
            <div className="space-y-2">
              {decisionPoints.map((dp, i) => (
                <div key={i} className="rounded-lg border border-gray-200 bg-gray-50 p-2.5 space-y-1">
                  <div className="flex items-center gap-2">
                    <span className="flex items-center justify-center h-5 w-5 rounded bg-gray-100 text-[10px] font-mono text-slate-600 shrink-0">
                      {dp.step_number}
                    </span>
                    <span className="text-[11px] text-slate-700">{dp.condition}</span>
                  </div>
                  <div className="grid grid-cols-2 gap-1.5 mt-1">
                    <div className="text-[10px]">
                      <span className="text-green-500">✓</span>
                      <span className="text-slate-600 ml-1">{dp.observed_path || '—'}</span>
                    </div>
                    <div className="text-[10px]">
                      <span className="text-amber-500">?</span>
                      <span className="text-slate-600 ml-1">{dp.alternative_path || '—'}</span>
                    </div>
                  </div>
                </div>
              ))}
              {decisionPoints.length === 0 && (
                <p className="text-[11px] text-slate-400 italic">No decision points found</p>
              )}
            </div>
          </div>
          )}

          {/* Coverage */}
          <div className="rounded-xl border border-gray-200 bg-white/90 p-4 space-y-3">
            <h2 className="text-xs font-semibold uppercase tracking-wider text-green-500 flex items-center gap-1.5">
              <BarChart3 className="h-3.5 w-3.5" /> Coverage
            </h2>

            <div>
              <div className="flex items-center justify-between text-[11px] mb-1">
                <span className="text-slate-600">Workflow</span>
                <span className={clsx('font-mono font-semibold',
                  cov.workflow_coverage_pct >= 80 ? 'text-green-500' :
                  cov.workflow_coverage_pct >= 50 ? 'text-yellow-500' :
                  'text-red-500'
                )}>
                  {cov.workflow_coverage_pct}%
                </span>
              </div>
              <div className="h-2 rounded-full bg-gray-200 overflow-hidden">
                <div
                  className={clsx('h-full rounded-full transition-all',
                    cov.workflow_coverage_pct >= 80 ? 'bg-green-500' :
                    cov.workflow_coverage_pct >= 50 ? 'bg-yellow-500' :
                    'bg-red-500'
                  )}
                  style={{ width: `${Math.min(cov.workflow_coverage_pct, 100)}%` }}
                />
              </div>
            </div>

            <div>
              <div className="flex items-center justify-between text-[11px] mb-1">
                <span className="text-slate-600">Pairwise</span>
                <span className="font-mono font-semibold text-nexus-500">
                  {cov.pairwise_coverage_pct}%
                </span>
              </div>
              <div className="h-2 rounded-full bg-gray-200 overflow-hidden">
                <div
                  className="h-full rounded-full bg-nexus-500 transition-all"
                  style={{ width: `${Math.min(cov.pairwise_coverage_pct, 100)}%` }}
                />
              </div>
            </div>

            <div className="grid grid-cols-2 gap-2">
              <div className="rounded-lg bg-white border border-gray-200 p-2 text-center">
                <p className="text-lg font-bold text-slate-700">{cov.total_scenarios}</p>
                <p className="text-[10px] text-slate-400">Scenarios</p>
              </div>
              <div className="rounded-lg bg-white border border-gray-200 p-2 text-center">
                <p className="text-lg font-bold text-slate-700">{cov.pairwise_combinations_generated}</p>
                <p className="text-[10px] text-slate-400">Pairwise</p>
              </div>
              <div className="rounded-lg bg-white border border-gray-200 p-2 text-center">
                <p className="text-lg font-bold text-slate-700">{cov.variables_tested}</p>
                <p className="text-[10px] text-slate-400">Variables</p>
              </div>
              <div className="rounded-lg bg-white border border-gray-200 p-2 text-center">
                <p className="text-lg font-bold text-slate-700">{cov.decision_points_found}</p>
                <p className="text-[10px] text-slate-400">Decisions</p>
              </div>
            </div>

            <div>
              <span className="text-[10px] text-slate-400 uppercase">By Category</span>
              <div className="mt-1 space-y-1">
                {Object.entries(cov.by_category).map(([key, count]) => {
                  const conf = CATEGORY_CONFIG[key];
                  if (!conf) return null;
                  return (
                    <div key={key} className="flex items-center gap-2 text-[11px]">
                      <span className="text-slate-500">{conf.icon}</span>
                      <span className="text-slate-600 flex-1">{conf.label}</span>
                      <span className="font-mono font-semibold text-slate-700">{count}</span>
                    </div>
                  );
                })}
              </div>
            </div>

            <div>
              <span className="text-[10px] text-slate-400 uppercase">By Priority</span>
              <div className="mt-1 space-y-1">
                {Object.entries(cov.by_priority).map(([key, count]) => {
                  const conf = PRIORITY_CONFIG[key];
                  if (!conf) return null;
                  return (
                    <div key={key} className="flex items-center gap-2 text-[11px]">
                      <span className={clsx('w-2 h-2 rounded-full', conf.bg.split(' ')[0])} />
                      <span className="text-slate-600 flex-1">{conf.label}</span>
                      <span className={clsx('font-mono font-semibold', conf.color)}>{count}</span>
                    </div>
                  );
                })}
              </div>
            </div>
          </div>

          {/* Provenance (Engineer only) */}
          {isEngineer && (
            <div className="rounded-xl border border-gray-200 bg-white/90 p-4 space-y-2">
              <h2 className="text-xs font-semibold uppercase tracking-wider text-slate-500 flex items-center gap-1.5">
                <Layers className="h-3.5 w-3.5" /> Provenance
              </h2>
              <div className="space-y-1 text-[11px]">
                <div className="flex justify-between"><span className="text-slate-400">Model</span><span className="text-slate-700 font-mono">{prov.model_used}</span></div>
                <div className="flex justify-between"><span className="text-slate-400">Steps</span><span className="text-slate-700">{prov.workflow_steps_analysed}</span></div>
                <div className="flex justify-between"><span className="text-slate-400">Risks</span><span className="text-slate-700">{prov.risks_considered}</span></div>
                <div className="flex justify-between"><span className="text-slate-400">LLM time</span><span className="text-slate-700">{(prov.generation_time_ms / 1000).toFixed(1)}s</span></div>
                {prov.generated_at && (
                  <div className="flex justify-between"><span className="text-slate-400">When</span><span className="text-slate-700">{new Date(prov.generated_at).toLocaleString()}</span></div>
                )}
              </div>
            </div>
          )}
        </div>

        {/* ═══ CENTER PANEL: Test Canvas ═══ */}
        <div className="flex-1 min-w-0 overflow-y-auto">

          {/* Visual-grounded banner (visual_strict mode) */}
          {isVisualStrict && scenarios.length > 0 && (
            <div className="border-b border-emerald-200 bg-emerald-50 px-6 py-2.5 flex items-center gap-3">
              <div className="flex h-7 w-7 items-center justify-center rounded-full bg-emerald-500 text-white shrink-0">
                <CheckCircle2 className="h-4 w-4" />
              </div>
              <div className="flex-1 min-w-0">
                <p className="text-[12px] font-semibold text-emerald-800">
                  {visualCoverage.groundedPct === 100
                    ? '100% visual-grounded'
                    : `${visualCoverage.groundedPct}% visual-grounded`}
                  <span className="ml-2 text-[11px] font-normal text-emerald-700">
                    {visualCoverage.groundedSteps} of {visualCoverage.totalSteps} steps cite a specific scene + control
                  </span>
                </p>
                <p className="text-[10px] text-emerald-700/80">
                  Every grounded step traces to a frame in the recorded demo. Click a step to see its visual proof.
                </p>
              </div>
              <div className="hidden sm:flex flex-col items-end gap-0.5 text-[10px] shrink-0">
                <span className="text-emerald-700">
                  <span className="font-mono font-semibold">{visualCoverage.coveredScenes}</span>/{visualCoverage.totalScenes} scenes
                </span>
                <span className="text-emerald-700">
                  <span className="font-mono font-semibold">{visualCoverage.coveredControls}</span>/{visualCoverage.totalAutoControls} controls
                </span>
                <span className="text-emerald-700">
                  <span className="font-mono font-semibold">{visualCoverage.coveredEdges}</span>/{visualCoverage.totalConfirmedEdges} edges
                </span>
              </div>
            </div>
          )}

          {/* Search + Filter bar */}
          <div className="sticky top-0 z-10 border-b border-gray-200 bg-[#f5f7fa]/95 backdrop-blur-sm px-6 py-3 space-y-2">
            <div className="flex items-center gap-3">
              <div className="relative flex-1">
                <Search className="absolute left-3 top-1/2 -translate-y-1/2 h-3.5 w-3.5 text-slate-400" />
                <input
                  type="text"
                  placeholder="Search scenarios..."
                  value={searchQuery}
                  onChange={e => setSearchQuery(e.target.value)}
                  className="w-full rounded-lg border border-gray-200 bg-white pl-9 pr-8 py-1.5 text-xs text-slate-700 placeholder-slate-400 focus:border-nexus-500/50 focus:outline-none"
                />
                {searchQuery && (
                  <button
                    onClick={() => setSearchQuery('')}
                    className="absolute right-2.5 top-1/2 -translate-y-1/2 text-slate-400 hover:text-slate-600"
                  >
                    <X className="h-3.5 w-3.5" />
                  </button>
                )}
              </div>
              <span className="text-[11px] text-slate-400">
                {filteredScenarios.length} of {scenarios.length}
              </span>
            </div>

            <div className="flex flex-wrap gap-1.5">
              {Object.entries(CATEGORY_CONFIG).map(([key, conf]) => (
                <button
                  key={key}
                  onClick={() => toggleFilter(key)}
                  className={clsx(
                    'flex items-center gap-1 rounded-full border px-2 py-0.5 text-[10px] transition-colors',
                    activeFilters.has(key)
                      ? conf.color
                      : 'border-gray-200 text-slate-500 hover:text-slate-700 hover:border-gray-300',
                  )}
                >
                  {conf.icon} {conf.label}
                </button>
              ))}
              <div className="h-4 w-px bg-gray-200 mx-1" />
              {Object.entries(PRIORITY_CONFIG).map(([key, conf]) => (
                <button
                  key={key}
                  onClick={() => toggleFilter(key)}
                  className={clsx(
                    'rounded-full border px-2 py-0.5 text-[10px] transition-colors',
                    activeFilters.has(key)
                      ? conf.bg + ' ' + conf.color
                      : 'border-gray-200 text-slate-500 hover:text-slate-700 hover:border-gray-300',
                  )}
                >
                  {conf.label}
                </button>
              ))}
              {/* P2: strategy filter chips (visual_strict only) */}
              {isVisualStrict && cov.by_strategy && Object.keys(cov.by_strategy).length > 0 && (
                <>
                  <div className="h-4 w-px bg-gray-200 mx-1" />
                  {Object.entries(STRATEGY_CONFIG).map(([key, conf]) => {
                    const count = cov.by_strategy?.[key] ?? 0;
                    if (count === 0) return null;
                    const filterKey = `strategy:${key}`;
                    return (
                      <button
                        key={key}
                        onClick={() => toggleFilter(filterKey)}
                        className={clsx(
                          'inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[10px] transition-colors',
                          activeFilters.has(filterKey)
                            ? conf.color
                            : 'border-gray-200 text-slate-500 hover:text-slate-700 hover:border-gray-300',
                        )}
                        title={`${count} scenario${count !== 1 ? 's' : ''} from ${conf.label} strategy`}
                      >
                        <Layers className="h-2.5 w-2.5" />
                        {conf.label}
                        <span className="font-mono text-[9px] opacity-70">{count}</span>
                      </button>
                    );
                  })}
                </>
              )}
              {/* P3: lifecycle state filter chips */}
              {cov.by_state && Object.keys(cov.by_state).length > 0 && (
                <>
                  <div className="h-4 w-px bg-gray-200 mx-1" />
                  {(Object.keys(LIFECYCLE_STATE_CONFIG) as LifecycleState[]).map(key => {
                    const count = cov.by_state?.[key] ?? 0;
                    if (count === 0) return null;
                    const conf = LIFECYCLE_STATE_CONFIG[key];
                    const filterKey = `state:${key}`;
                    return (
                      <button
                        key={key}
                        onClick={() => toggleFilter(filterKey)}
                        className={clsx(
                          'inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[10px] transition-colors',
                          activeFilters.has(filterKey)
                            ? conf.color
                            : 'border-gray-200 text-slate-500 hover:text-slate-700 hover:border-gray-300',
                        )}
                        title={conf.description}
                      >
                        {conf.label}
                        <span className="font-mono text-[9px] opacity-70">{count}</span>
                      </button>
                    );
                  })}
                </>
              )}
              {/* P6: last-run filter chips (only show buckets with content) */}
              {cov.by_last_run && Object.keys(cov.by_last_run).length > 0 && (
                <>
                  <div className="h-4 w-px bg-gray-200 mx-1" />
                  {(Object.keys(RUN_STATUS_CONFIG) as RunStatusFilterKey[]).map(key => {
                    const count = cov.by_last_run?.[key] ?? 0;
                    if (count === 0) return null;
                    const conf = RUN_STATUS_CONFIG[key];
                    const filterKey = `run:${key}`;
                    return (
                      <button
                        key={key}
                        onClick={() => toggleFilter(filterKey)}
                        className={clsx(
                          'inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[10px] transition-colors',
                          activeFilters.has(filterKey)
                            ? conf.color
                            : 'border-gray-200 text-slate-500 hover:text-slate-700 hover:border-gray-300',
                        )}
                        title={conf.description}
                      >
                        {conf.label}
                        <span className="font-mono text-[9px] opacity-70">{count}</span>
                      </button>
                    );
                  })}
                  {cov.flaky_scenarios != null && cov.flaky_scenarios > 0 && (
                    <button
                      onClick={() => toggleFilter('run:flaky')}
                      className={clsx(
                        'inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[10px] transition-colors',
                        activeFilters.has('run:flaky')
                          ? 'text-amber-700 bg-amber-50 border-amber-300'
                          : 'border-gray-200 text-slate-500 hover:text-slate-700 hover:border-gray-300',
                      )}
                      title="Flaky in the last 10 runs (pass-then-fail-then-pass pattern)"
                    >
                      Flaky
                      <span className="font-mono text-[9px] opacity-70">{cov.flaky_scenarios}</span>
                    </button>
                  )}
                </>
              )}
              {activeFilters.size > 0 && (
                <button
                  onClick={() => setActiveFilters(new Set())}
                  className="text-[10px] text-slate-400 hover:text-slate-700 flex items-center gap-1"
                >
                  <X className="h-3 w-3" /> Clear
                </button>
              )}
            </div>
          </div>

          {/* Scenario Cards */}
          <div className="p-6 space-y-4">
            {filteredScenarios.slice(0, showMax).map(sc => (
              <ScenarioCard
                key={sc.scenario_id}
                scenario={sc}
                expanded={expandedScenarios.has(sc.scenario_id)}
                onToggle={() => toggleScenario(sc.scenario_id)}
                viewMode={viewMode}
                selectedStep={selectedStep}
                onSelectStep={handleSelectStep}
                scenes={scenes}
                controlsByScene={controlsByScene}
                edges={edges}
                onTransition={handleTransition}
                onAddComment={handleAddComment}
                artifactId={artifactId}
                onHealingApplied={() => loadArchitect(true)}
              />
            ))}

            {filteredScenarios.length === 0 && (
              <div className="text-center py-12 text-slate-400">
                <Target className="h-8 w-8 mx-auto mb-3 opacity-50" />
                <p className="text-sm">No scenarios match your filters.</p>
              </div>
            )}

            {filteredScenarios.length > showMax && (
              <button
                onClick={() => setShowMax(prev => prev + 20)}
                className="w-full py-3 text-xs text-slate-600 hover:text-slate-800 border border-gray-200 rounded-lg hover:border-gray-300 transition-colors"
              >
                Show more ({filteredScenarios.length - showMax} remaining)
              </button>
            )}
          </div>
        </div>

        {/* ═══ RIGHT PANEL: Evidence Inspector ═══ */}
        <div className="w-80 shrink-0 border-l border-gray-200 overflow-y-auto p-4 space-y-4 bg-white/40">
          <EvidenceInspector
            viewMode={viewMode}
            scene={resolvedScene}
            control={resolvedControl}
            edge={resolvedEdge}
            step={selectedStepObj}
            scenario={selectedScenarioObj}
            graphError={evidenceGraphError}
            hasGraph={!!evidenceGraph}
            sceneControls={
              resolvedScene && evidenceGraph
                ? (evidenceGraph.controls_by_scene[resolvedScene.scene_id] ?? [])
                : []
            }
            onSelectControl={(ctrl) => setSelectedControlId(ctrl.control_id)}
          />
        </div>
      </div>

      {/* Phase 3 — App-switch timeline (Gantt-style).  Only renders
          when the artifact actually spans multiple apps; on a
          single-app demo the filmstrip below already conveys the
          full story and we don't want to waste vertical space. */}
      {scenes.length > 0 && evidenceGraph && evidenceGraph.app_instances.length > 1 && (
        <div className="shrink-0 border-t border-gray-200 bg-white px-4 py-2">
          <AppTimeline
            instances={evidenceGraph.app_instances}
            totalScenes={Math.max(
              evidenceGraph.summary.total_scenes,
              evidenceGraph.app_instances.reduce(
                (acc, i) => Math.max(acc, i.last_scene_index + 1),
                0,
              ),
            )}
            selectedInstanceId={
              resolvedScene?.app_instance_id ?? null
            }
            onSelectInstance={(inst) => {
              // Selecting an app instance scopes the inspector to its
              // first scene — the filmstrip then takes over for fine
              // navigation within that app's range.
              const firstSceneInInstance = scenes.find(
                s => s.app_instance_id === inst.instance_id,
              );
              if (firstSceneInInstance) {
                setSelectedSceneId(firstSceneInInstance.scene_id);
              }
            }}
          />
        </div>
      )}

      {/* ── BOTTOM: Timeline / Filmstrip ──────────────────── */}
      {scenes.length > 0 && (
        <div className="shrink-0 border-t border-gray-200 bg-white px-4 py-2">
          <div className="flex items-center gap-2 mb-1.5">
            <Film className="h-3.5 w-3.5 text-slate-500" />
            <span className="text-[10px] uppercase tracking-wider text-slate-500 font-semibold">
              Timeline · {scenes.length} scenes
            </span>
            {resolvedScene && (
              <span className="text-[10px] text-slate-500">
                · Scene #{resolvedScene.scene_index + 1}
                {resolvedScene.scene_state_summary?.screen_title && (
                  <> — {resolvedScene.scene_state_summary.screen_title}</>
                )}
              </span>
            )}
          </div>
          <div className="flex gap-1.5 overflow-x-auto pb-1">
            {scenes.map(scene => {
              const isActive = resolvedScene?.scene_id === scene.scene_id;
              const url = scene.representative_frame_asset_path
                ? api.getFrameImageUrl(scene.representative_frame_asset_path)
                : '';
              return (
                <button
                  key={scene.scene_id}
                  onClick={() => handleSelectScene(scene.scene_id)}
                  className={clsx(
                    'shrink-0 rounded-md overflow-hidden border-2 transition-all relative group',
                    isActive
                      ? 'border-nexus-500 shadow-md ring-2 ring-nexus-500/30'
                      : 'border-gray-200 hover:border-nexus-300',
                  )}
                  title={`Scene ${scene.scene_index + 1}${scene.scene_state_summary?.screen_title ? ` — ${scene.scene_state_summary.screen_title}` : ''}`}
                >
                  {url ? (
                    <img
                      src={url}
                      alt={`Scene ${scene.scene_index + 1}`}
                      className="h-16 w-28 object-cover bg-gray-100"
                      loading="lazy"
                    />
                  ) : (
                    <div className="h-16 w-28 bg-gray-100 flex items-center justify-center">
                      <ImageIcon className="h-4 w-4 text-slate-400" />
                    </div>
                  )}
                  <div className="absolute bottom-0 left-0 right-0 bg-gradient-to-t from-black/70 to-transparent px-1 py-0.5">
                    <span className="text-[9px] font-mono text-white">#{scene.scene_index + 1}</span>
                  </div>
                  {scene.scene_quality && scene.scene_quality !== 'strong' && (
                    <ConfidenceChip
                      level={scene.scene_quality}
                      compact
                      className="absolute top-0 right-0"
                    />
                  )}
                </button>
              );
            })}
          </div>
        </div>
      )}

      {/* Hint when graph is missing */}
      {scenes.length === 0 && evidenceGraphError && (
        <div className="shrink-0 border-t border-gray-200 bg-amber-50 px-4 py-2 text-[11px] text-amber-700 flex items-center gap-2">
          <Film className="h-3.5 w-3.5 text-amber-500" />
          Timeline unavailable: {evidenceGraphError}
        </div>
      )}

      {/* P4: Co-Architect chat dock (slide-in from the right) */}
      <CoArchitectDock
        artifactId={artifactId}
        sessionId={sessionId}
        open={coArchitectOpen}
        onClose={() => setCoArchitectOpen(false)}
        onProposalsCommitted={() => loadArchitect(true)}
      />

      {/* P7: Demo Diff panel */}
      {user?.tenant_id && (
        <DemoDiffPanel
          artifactId={artifactId}
          sessionId={sessionId}
          tenantId={user.tenant_id}
          open={demoDiffOpen}
          onClose={() => setDemoDiffOpen(false)}
        />
      )}
    </div>
  );
}

// ═══════════════════════════════════════════════════════════════
//  StepProofRow — Visual Proof Chain (G16)
//  Renders one test step with its full visual proof:
//    [thumb] action  [grounded ✓ 87%] [stability] [scene #N] [control] [transition]
//    Input: <observed value>
//    → expected behavior
//    sel: <playwright selector>            (Engineer mode)
//  Uses direct citations when present, falls back to heuristic mapping for
//  steps that lack a direct citation (multimodal mode).
// ═══════════════════════════════════════════════════════════════

function StepProofRow({
  step,
  scenario,
  isSelected,
  onSelect,
  viewMode,
  scenes,
  controlsByScene,
  edges,
}: {
  step: TestStep;
  scenario: E2EScenario;
  isSelected: boolean;
  onSelect: () => void;
  viewMode: ViewMode;
  scenes: VisualScene[];
  controlsByScene: Record<string, EvidenceControl[]>;
  edges: VisualFlowEdge[];
}) {
  const isEngineer = viewMode === 'engineer';

  // Direct citations preferred; heuristic fallback for multimodal mode
  const citedScene = step.evidence_scene_id
    ? scenes.find(s => s.scene_id === step.evidence_scene_id) ?? null
    : null;
  const matchedScene = citedScene ?? mapStepToScene(scenario, step, scenes);

  const citedControl = mapStepToControl(step, controlsByScene);
  const matchedControl = citedControl ?? (matchedScene
    ? (controlsByScene[matchedScene.scene_id] ?? [])
        .slice()
        .sort((a, b) =>
          (b.automation_ready ? 1 : 0) - (a.automation_ready ? 1 : 0) ||
          (b.selector_confidence ?? 0) - (a.selector_confidence ?? 0))[0]
    : null);

  const citedEdge = mapStepToEdge(step, edges);

  const stability = matchedControl
    ? stabilityBand(matchedControl.selector_confidence ?? 0, matchedControl.selector_source)
    : null;

  const sceneThumbUrl = matchedScene?.representative_frame_asset_path
    ? api.getFrameImageUrl(matchedScene.representative_frame_asset_path)
    : '';

  const isGrounded = !!(step.evidence_scene_id && step.evidence_control_id);
  const proofConfidence = step.proof_confidence ?? 0;

  return (
    <button
      onClick={onSelect}
      className={clsx(
        'w-full flex gap-2 text-left text-[11px] rounded-md px-2 py-1.5 transition-colors',
        isSelected ? 'bg-nexus-50 ring-1 ring-nexus-400/50' : 'hover:bg-gray-50',
      )}
    >
      <span className={clsx(
        'shrink-0 flex items-center justify-center h-5 w-5 rounded text-[10px] font-mono',
        isSelected ? 'bg-nexus-500 text-white' : 'bg-gray-100 text-slate-600',
      )}>
        {step.step_number}
      </span>

      {sceneThumbUrl && matchedScene && (
        <span
          className={clsx(
            'shrink-0 self-start overflow-hidden rounded border',
            isGrounded ? 'border-emerald-400' : 'border-gray-200',
          )}
          title={citedScene ? 'Directly cited scene' : 'Heuristically matched scene'}
        >
          <img
            src={sceneThumbUrl}
            alt={`Scene ${matchedScene.scene_index + 1}`}
            className="h-9 w-14 object-cover bg-gray-100"
            loading="lazy"
          />
        </span>
      )}

      <div className="flex-1 min-w-0">
        <div className="flex items-center gap-1.5 flex-wrap">
          <p className="text-slate-700">{step.action}</p>

          {isGrounded && (
            <span
              className="inline-flex items-center gap-1 rounded-full bg-emerald-50 border border-emerald-300 text-emerald-700 px-1.5 py-0 text-[9px] font-medium"
              title={`Visually grounded: scene + control + ${citedEdge ? 'edge' : 'no edge'}`}
            >
              <CheckCircle2 className="h-2.5 w-2.5" /> grounded
              {proofConfidence > 0 && (
                <span className="font-mono">{(proofConfidence * 100).toFixed(0)}%</span>
              )}
            </span>
          )}
          {!isGrounded && step.evidence_scene_id && (
            <span className="inline-flex items-center gap-1 rounded-full bg-amber-50 border border-amber-300 text-amber-700 px-1.5 py-0 text-[9px]" title="Scene cited, but no control bound">
              <AlertTriangle className="h-2.5 w-2.5" /> partial
            </span>
          )}

          {isEngineer && stability && matchedControl && (
            <span
              className={clsx('inline-flex items-center gap-1 rounded-full border px-1.5 py-0 text-[9px]', stability.color, 'border-current/30')}
              title={`Selector stability: ${stability.label} (confidence ${(matchedControl.selector_confidence ?? 0).toFixed(2)}, source: ${matchedControl.selector_source})`}
            >
              <span className={clsx('w-1.5 h-1.5 rounded-full', stability.dotColor)} />
              {stability.label}
            </span>
          )}

          {matchedScene && (
            <span className="text-[9px] text-slate-400 inline-flex items-center gap-0.5">
              <Link2 className="h-2.5 w-2.5" />
              scene #{matchedScene.scene_index + 1}
            </span>
          )}
          {citedControl && (
            <span className="text-[9px] text-nexus-600 inline-flex items-center gap-0.5" title={citedControl.playwright_selector ?? ''}>
              <Target className="h-2.5 w-2.5" />
              {citedControl.label_text || citedControl.element_type || 'control'}
            </span>
          )}
          {citedEdge && (
            <span className="text-[9px] text-purple-600 inline-flex items-center gap-0.5" title={citedEdge.primary_action_summary?.action_label ?? ''}>
              <ArrowRight className="h-2.5 w-2.5" /> transition
            </span>
          )}
        </div>
        {step.input_data && step.input_data !== 'N/A' && (
          <p className="text-slate-500">Input: <span className="text-nexus-500">{step.input_data}</span></p>
        )}
        {step.expected_behavior && (
          <p className="text-slate-500">→ {step.expected_behavior}</p>
        )}
        {isEngineer && citedControl?.playwright_selector && (
          <p className="text-[10px] text-indigo-600 font-mono truncate" title={citedControl.playwright_selector}>
            sel: {citedControl.playwright_selector}
          </p>
        )}
      </div>
    </button>
  );
}

// ═══════════════════════════════════════════════════════════════
//  Scenario Card
// ═══════════════════════════════════════════════════════════════

function ScenarioCard({
  scenario,
  expanded,
  onToggle,
  viewMode,
  selectedStep,
  onSelectStep,
  scenes,
  controlsByScene,
  edges,
  onTransition,
  onAddComment,
  artifactId,
  onHealingApplied,
}: {
  scenario: E2EScenario;
  expanded: boolean;
  onToggle: () => void;
  viewMode: ViewMode;
  selectedStep: StepSelection | null;
  onSelectStep: (scenarioId: string, stepNumber: number) => void;
  scenes: VisualScene[];
  controlsByScene: Record<string, EvidenceControl[]>;
  edges: VisualFlowEdge[];
  onTransition: (scenarioId: string, newState: ScenarioLifecycleState, note?: string) => Promise<void>;
  onAddComment: (scenarioId: string, body: string) => Promise<void>;
  artifactId: string;
  onHealingApplied: () => void;
}) {
  const catConf = CATEGORY_CONFIG[scenario.category] ?? CATEGORY_CONFIG.observed;
  const priConf = PRIORITY_CONFIG[scenario.priority] ?? PRIORITY_CONFIG.P1_high;
  const isEngineer = viewMode === 'engineer';

  // G15: per-scenario citation summary — unique scene/control/edge counts
  // derived from the step citations (no stubs).
  const citationCounts = useMemo(() => {
    const sceneIds = new Set<string>();
    const controlIds = new Set<string>();
    const edgeIds = new Set<string>();
    for (const step of scenario.steps) {
      if (step.evidence_scene_id) sceneIds.add(step.evidence_scene_id);
      if (step.evidence_control_id) controlIds.add(step.evidence_control_id);
      if (step.evidence_edge_id) edgeIds.add(step.evidence_edge_id);
    }
    return {
      scenes: sceneIds.size,
      controls: controlIds.size,
      edges: edgeIds.size,
    };
  }, [scenario.steps]);

  return (
    <div className="rounded-xl border border-gray-200 bg-white overflow-hidden shadow-sm">
      {/* Header */}
      <button
        onClick={onToggle}
        className="w-full flex items-start gap-3 px-5 py-4 text-left hover:bg-gray-50 transition-colors"
      >
        <div className="shrink-0 mt-0.5">
          {expanded ? (
            <ChevronDown className="h-4 w-4 text-slate-400" />
          ) : (
            <ChevronRight className="h-4 w-4 text-slate-400" />
          )}
        </div>
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 flex-wrap">
            {isEngineer && (
              <span className="text-[10px] font-mono text-slate-400">{scenario.scenario_id}</span>
            )}
            <span className={clsx('inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[9px]', catConf.color)}>
              {catConf.icon} {catConf.label}
            </span>
            <span className={clsx('rounded-full border px-2 py-0.5 text-[9px] font-medium', priConf.bg, priConf.color)}>
              {priConf.label}
            </span>
          </div>
          <h3 className="text-sm font-medium text-slate-700 mt-1">{scenario.title}</h3>
          {scenario.rationale && (
            <p className="text-[11px] text-slate-500 mt-0.5 line-clamp-2">{scenario.rationale}</p>
          )}
        </div>
        <div className="shrink-0 text-right space-y-0.5">
          <span className="text-[10px] text-slate-500">{scenario.steps.length} steps</span>
          {scenario.data_matrix.length > 0 && (
            <p className="text-[10px] text-nexus-500">{scenario.data_matrix.length} data set(s)</p>
          )}
          {/* G15: Citation summary — unique scenes/controls/transitions grounding this scenario */}
          {(citationCounts.scenes + citationCounts.controls + citationCounts.edges) > 0 && (
            <p className="text-[9px] text-slate-500" title="Distinct visual artifacts grounding this scenario">
              Grounded in <span className="font-mono font-semibold text-slate-700">{citationCounts.scenes}</span> scene{citationCounts.scenes !== 1 ? 's' : ''} ·
              {' '}<span className="font-mono font-semibold text-slate-700">{citationCounts.controls}</span> control{citationCounts.controls !== 1 ? 's' : ''} ·
              {' '}<span className="font-mono font-semibold text-slate-700">{citationCounts.edges}</span> transition{citationCounts.edges !== 1 ? 's' : ''}
            </p>
          )}
          {/* Visual grounding badge (visual_strict) */}
          {scenario.visual_total_steps != null && scenario.visual_total_steps > 0 && (
            <p
              className={clsx(
                'text-[10px] inline-flex items-center gap-1 rounded-full border px-1.5 py-0',
                (scenario.visual_proven_steps ?? 0) === scenario.visual_total_steps
                  ? 'bg-emerald-50 border-emerald-300 text-emerald-700'
                  : 'bg-amber-50 border-amber-300 text-amber-700',
              )}
              title={`${scenario.visual_proven_steps ?? 0} of ${scenario.visual_total_steps} steps grounded in scene + control`}
            >
              <CheckCircle2 className="h-2.5 w-2.5" />
              {scenario.visual_proven_steps ?? 0}/{scenario.visual_total_steps} grounded
            </p>
          )}
          {scenario.strategy && (
            <p className={clsx(
              'text-[9px] inline-flex items-center gap-0.5 rounded-full border px-1.5 py-0',
              STRATEGY_CONFIG[scenario.strategy]?.color ?? 'text-slate-500 border-gray-200 bg-gray-50',
            )}>
              <Layers className="h-2.5 w-2.5" />
              {STRATEGY_CONFIG[scenario.strategy]?.label ?? scenario.strategy.replace(/_/g, ' ')}
            </p>
          )}
          {/* P3: Lifecycle state badge */}
          <LifecycleStateBadge
            state={scenario.state ?? 'draft'}
            scenarioId={scenario.scenario_id}
            onTransition={onTransition}
          />
          {/* P6: Last-run badge — only render when there's a run on record */}
          {scenario.last_run && (
            <LastRunBadge summary={scenario.last_run} />
          )}
        </div>
      </button>

      {/* Expanded content */}
      {expanded && (
        <div className="border-t border-gray-200 px-5 py-4 space-y-4">

          {/* P3: Reviewer mode — prominent approve/reject row */}
          {!isEngineer && (
            <ReviewerActions
              scenarioId={scenario.scenario_id}
              state={scenario.state ?? 'draft'}
              onTransition={onTransition}
            />
          )}

          {/* P6: Last CI run summary + on-demand failing-step detail */}
          {scenario.last_run && (
            <RunSummaryPanel
              summary={scenario.last_run}
              artifactId={artifactId}
              scenarioId={scenario.scenario_id}
            />
          )}

          {scenario.preconditions.length > 0 && (
            <div>
              <span className="text-[10px] text-slate-400 uppercase">Preconditions</span>
              <ul className="mt-1 space-y-0.5">
                {scenario.preconditions.map((p, i) => (
                  <li key={i} className="text-[11px] text-slate-600">• {p}</li>
                ))}
              </ul>
            </div>
          )}

          {/* Test Steps */}
          {scenario.steps.length > 0 && (
            <div>
              <span className="text-[10px] text-slate-400 uppercase">Test Steps</span>
              <div className="mt-1 space-y-1">
                {scenario.steps.map(step => (
                  <StepProofRow
                    key={step.step_number}
                    step={step}
                    scenario={scenario}
                    isSelected={
                      selectedStep?.scenarioId === scenario.scenario_id &&
                      selectedStep?.stepNumber === step.step_number
                    }
                    onSelect={() => onSelectStep(scenario.scenario_id, step.step_number)}
                    viewMode={viewMode}
                    scenes={scenes}
                    controlsByScene={controlsByScene}
                    edges={edges}
                  />
                ))}
              </div>
            </div>
          )}

          {scenario.data_matrix.length > 0 && (
            <div>
              <span className="text-[10px] text-slate-400 uppercase flex items-center gap-1">
                <Shuffle className="h-3 w-3" /> Data Combinations
              </span>
              <div className="mt-1 space-y-1.5">
                {scenario.data_matrix.map((dm, idx) => (
                  <div key={idx} className="rounded-lg border border-nexus-500/20 bg-nexus-50 px-3 py-2">
                    <div className="flex flex-wrap gap-x-3 gap-y-1">
                      {Object.entries(dm).map(([k, v]) => (
                        <span key={k} className="text-[10px]">
                          <span className="text-slate-500">{k}:</span>
                          <span className="text-nexus-700 ml-1 font-medium">{v}</span>
                        </span>
                      ))}
                    </div>
                  </div>
                ))}
              </div>
            </div>
          )}

          {scenario.expected_outcome && (
            <div>
              <span className="text-[10px] text-slate-400 uppercase">Expected Outcome</span>
              <p className="text-[11px] text-slate-700 mt-0.5">{scenario.expected_outcome}</p>
            </div>
          )}

          {scenario.evidence_sources.length > 0 && (
            <div>
              <span className="text-[10px] text-slate-400 uppercase">Evidence</span>
              <div className="mt-1 space-y-1.5">
                {scenario.evidence_sources.map((ev, idx) => (
                  <div key={idx} className="rounded-lg border border-gray-200 bg-gray-50 px-3 py-2 flex items-start gap-2">
                    <ModalityBadge modality={ev.source_modality} />
                    <p className="text-[11px] text-slate-600 flex-1 italic">"{ev.text}"</p>
                    <span className={clsx(
                      'text-[9px] font-mono shrink-0',
                      ev.confidence >= 0.8 ? 'text-green-500' :
                      ev.confidence >= 0.5 ? 'text-yellow-500' :
                      'text-red-500'
                    )}>
                      {(ev.confidence * 100).toFixed(0)}%
                    </span>
                  </div>
                ))}
              </div>
            </div>
          )}

          {/* P3: Comments thread */}
          <div className="pt-2 border-t border-gray-200">
            <CommentsThread scenario={scenario} onAddComment={onAddComment} />
          </div>

          {/* P7: Self-healing — on-demand stale-selector scan + apply */}
          <div className="pt-2 border-t border-gray-200">
            <HealingSection
              artifactId={artifactId}
              scenario={scenario}
              onApplied={onHealingApplied}
            />
          </div>

          {/* P3: Audit log */}
          {(scenario.audit_log?.length ?? 0) > 0 && (
            <div>
              <AuditLogPanel scenario={scenario} />
            </div>
          )}

          <div className="flex flex-wrap gap-2 pt-2 border-t border-gray-200">
            {scenario.workflow_steps_covered.length > 0 && (
              <span className="text-[9px] text-slate-400">
                Steps: {scenario.workflow_steps_covered.join(', ')}
              </span>
            )}
            {scenario.risk_areas_addressed.length > 0 && (
              <span className="text-[9px] text-red-500/70">
                Risks: {scenario.risk_areas_addressed.join(', ')}
              </span>
            )}
            {scenario.state_changed_by_email && (
              <span className="text-[9px] text-slate-400">
                Last changed by {scenario.state_changed_by_email}
                {scenario.state_changed_at && (
                  <> · {new Date(scenario.state_changed_at).toLocaleString()}</>
                )}
              </span>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

// ═══════════════════════════════════════════════════════════════
//  Evidence Inspector
// ═══════════════════════════════════════════════════════════════

function EvidenceInspector({
  viewMode,
  scene,
  control,
  edge,
  step,
  scenario,
  graphError,
  hasGraph,
  sceneControls,
  onSelectControl,
}: {
  viewMode: ViewMode;
  scene: VisualScene | null;
  control: EvidenceControl | null;
  edge: VisualFlowEdge | null;
  step: TestStep | null;
  scenario: E2EScenario | null;
  graphError: string | null;
  hasGraph: boolean;
  /** All controls for the current scene — drives the overlay layer. */
  sceneControls: EvidenceControl[];
  /** Click handler for an overlay box. */
  onSelectControl: (control: EvidenceControl) => void;
}) {
  const isEngineer = viewMode === 'engineer';

  // Empty state — nothing selected
  if (!scene && !step) {
    return (
      <div className="rounded-xl border border-dashed border-gray-300 bg-white/60 p-5 space-y-3">
        <h2 className="text-xs font-semibold uppercase tracking-wider text-slate-500 flex items-center gap-1.5">
          <Eye className="h-3.5 w-3.5" /> Evidence Inspector
        </h2>
        <p className="text-[11px] text-slate-500 leading-relaxed">
          Click a test step or a scene in the timeline to see the visual evidence behind it.
        </p>
        {!hasGraph && graphError && (
          <p className="text-[10px] text-amber-600 flex items-start gap-1">
            <AlertTriangle className="h-3 w-3 mt-0.5 shrink-0" /> Visual graph not loaded: {graphError}
          </p>
        )}
        {hasGraph && (
          <div className="rounded-md bg-gray-50 border border-gray-200 px-3 py-2 text-[10px] text-slate-500 space-y-1">
            <p>Click any <span className="text-slate-700 font-medium">step</span> to:</p>
            <p>• See the matching frame</p>
            <p>• Inspect OCR + selector</p>
            <p>• Trace the flow edge</p>
          </div>
        )}
      </div>
    );
  }

  const frameUrl = scene?.representative_frame_asset_path
    ? api.getFrameImageUrl(scene.representative_frame_asset_path)
    : '';

  const stability = control
    ? stabilityBand(control.selector_confidence ?? 0, control.selector_source)
    : null;

  return (
    <>
      {/* Header — what's selected */}
      <div className="rounded-xl border border-gray-200 bg-white p-4 space-y-2">
        <h2 className="text-xs font-semibold uppercase tracking-wider text-nexus-500 flex items-center gap-1.5">
          <Eye className="h-3.5 w-3.5" /> Evidence Inspector
        </h2>
        {step && scenario && (
          <div className="text-[11px] text-slate-600 space-y-0.5">
            <p className="text-slate-400">Step {step.step_number} of</p>
            <p className="font-medium text-slate-700 line-clamp-2">{scenario.title}</p>
            <p className="text-slate-600 italic line-clamp-2">"{step.action}"</p>
          </div>
        )}
        {!step && scene && (
          <div className="text-[11px] text-slate-600">
            <p className="text-slate-400">Inspecting scene</p>
            <p className="font-medium text-slate-700">
              #{scene.scene_index + 1}
              {scene.scene_state_summary?.screen_title && (
                <span className="text-slate-500"> — {scene.scene_state_summary.screen_title}</span>
              )}
            </p>
          </div>
        )}
      </div>

      {/* Frame thumbnail with clickable control overlays.
          When the scene has any controls with real geometry, every
          control becomes a bounding-box on the image — colour-coded
          by selector_confidence band so weak selectors are immediately
          visible.  Falls back to a plain thumbnail when no geometry
          is available (mainframe / SAP / DB surfaces emit empty boxes). */}
      {scene && frameUrl && (
        <div className="space-y-1">
          <SceneFrameWithOverlays
            scene={scene}
            frameUrl={frameUrl}
            controls={sceneControls}
            selectedControlId={control?.control_id ?? null}
            onSelectControl={onSelectControl}
            maxHeight={260}
          />
          {(scene.scene_state_summary?.screen_type
            || scene.scene_state_summary?.application_label) && (
            <p className="text-[10px] text-slate-500 px-1">
              {scene.scene_state_summary?.screen_type && (
                <span className="capitalize">{scene.scene_state_summary.screen_type}</span>
              )}
              {scene.scene_state_summary?.application_label && (
                <> · {scene.scene_state_summary.application_label}</>
              )}
            </p>
          )}
        </div>
      )}

      {/* OCR / Screen content */}
      {scene && scene.ocr_text && (
        <div className="rounded-xl border border-gray-200 bg-white p-4 space-y-2">
          <h3 className="text-[10px] uppercase tracking-wider text-slate-500 font-semibold flex items-center gap-1.5">
            <Eye className="h-3 w-3" /> Screen text (OCR)
          </h3>
          <p className="text-[11px] text-slate-700 leading-relaxed line-clamp-6 whitespace-pre-wrap">
            {scene.ocr_text.slice(0, 400)}
            {scene.ocr_text.length > 400 && '…'}
          </p>
          {scene.detected_url && (
            <p className="text-[10px] text-slate-500 truncate">URL: <span className="font-mono">{scene.detected_url}</span></p>
          )}
        </div>
      )}

      {/* Control + Selector (Engineer mode primary; Reviewer sees only label) */}
      {control && (
        <div className="rounded-xl border border-gray-200 bg-white p-4 space-y-2">
          <h3 className="text-[10px] uppercase tracking-wider text-slate-500 font-semibold flex items-center gap-1.5">
            <Target className="h-3 w-3" /> Target control
          </h3>
          <div className="space-y-1 text-[11px]">
            <p className="text-slate-700 font-medium">
              {control.label_text || control.display_label || '(unlabeled)'}
            </p>
            <p className="text-slate-500">
              <span className="font-mono">{control.element_type || 'element'}</span>
              {control.action_kind && <> · {control.action_kind}</>}
            </p>
            {isEngineer && control.playwright_selector && (
              <div className="mt-2 space-y-1">
                <p className="text-[10px] uppercase tracking-wider text-slate-400">Playwright selector</p>
                <code className="block text-[10px] bg-gray-50 border border-gray-200 rounded px-2 py-1 font-mono text-indigo-700 break-all">
                  {control.playwright_selector}
                </code>
              </div>
            )}
            {isEngineer && stability && (
              <div className="flex items-center gap-2 pt-1">
                <span className={clsx('inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[10px]', stability.color, 'border-current/30')}>
                  <span className={clsx('w-1.5 h-1.5 rounded-full', stability.dotColor)} />
                  {stability.label}
                </span>
                <span className="text-[10px] text-slate-500">
                  conf {(control.selector_confidence ?? 0).toFixed(2)} · {control.selector_source}
                </span>
              </div>
            )}
            {control.automation_ready && (
              <p className="text-[10px] text-green-600 flex items-center gap-1">
                <CheckCircle2 className="h-3 w-3" /> Automation-ready
              </p>
            )}
            {!control.automation_ready && (
              <p className="text-[10px] text-amber-600 flex items-center gap-1">
                <AlertTriangle className="h-3 w-3" /> Not automation-ready (no OCR-grounded selector)
              </p>
            )}
          </div>
        </div>
      )}

      {/* Flow edge — what happens after this step */}
      {edge && (
        <div className="rounded-xl border border-gray-200 bg-white p-4 space-y-2">
          <h3 className="text-[10px] uppercase tracking-wider text-slate-500 font-semibold flex items-center gap-1.5">
            <ArrowRight className="h-3 w-3" /> Resulting transition
          </h3>
          <div className="text-[11px] text-slate-700 space-y-1">
            {edge.primary_action_summary?.action_label ? (
              <p>{edge.primary_action_summary.action_label}</p>
            ) : (
              <p className="text-slate-500 italic">
                {edge.edge_type === 'action_confirmed_transition' ? 'Action confirmed' : 'Observed transition'}
              </p>
            )}
            {edge.action_quality && (
              <ConfidenceChip
                level={edge.action_quality}
                score={edge.action_confidence ?? null}
              />
            )}
            {isEngineer && (
              <p className="text-[10px] text-slate-400 font-mono">{edge.edge_type}</p>
            )}
          </div>
        </div>
      )}

      {/* Scenario evidence — transcript / multimodal citations from the LLM grounding */}
      {scenario && scenario.evidence_sources.length > 0 && (
        <div className="rounded-xl border border-gray-200 bg-white p-4 space-y-2">
          <h3 className="text-[10px] uppercase tracking-wider text-slate-500 font-semibold flex items-center gap-1.5">
            <Mic className="h-3 w-3" /> Why this scenario exists
          </h3>
          <div className="space-y-1.5">
            {scenario.evidence_sources.slice(0, 3).map((ev, idx) => (
              <div key={idx} className="flex items-start gap-2">
                <ModalityBadge modality={ev.source_modality} />
                <p className="text-[11px] text-slate-600 flex-1 italic line-clamp-3">"{ev.text}"</p>
                <span className={clsx(
                  'text-[9px] font-mono shrink-0',
                  ev.confidence >= 0.8 ? 'text-green-500' :
                  ev.confidence >= 0.5 ? 'text-yellow-500' :
                  'text-red-500'
                )}>
                  {(ev.confidence * 100).toFixed(0)}%
                </span>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Reviewer mode — placeholder approve/reject controls (Phase B will wire these up) */}
      {!isEngineer && step && (
        <div className="rounded-xl border border-emerald-200 bg-emerald-50/40 p-4 space-y-2">
          <h3 className="text-[10px] uppercase tracking-wider text-emerald-700 font-semibold flex items-center gap-1.5">
            <ThumbsUp className="h-3 w-3" /> Reviewer actions
          </h3>
          <p className="text-[10px] text-emerald-600">
            Approve / request changes / comment flow ships in Phase B (lifecycle states).
          </p>
          <div className="flex gap-2 opacity-60">
            <button disabled className="flex-1 rounded-md bg-emerald-500/80 px-2 py-1 text-[11px] text-white cursor-not-allowed">
              Approve
            </button>
            <button disabled className="flex-1 rounded-md bg-white border border-gray-300 px-2 py-1 text-[11px] text-slate-600 cursor-not-allowed">
              Request changes
            </button>
          </div>
        </div>
      )}

      {/* Time range */}
      {scene && scene.start_ms != null && scene.end_ms != null && isEngineer && (
        <div className="rounded-xl border border-gray-200 bg-white p-3 text-[10px] text-slate-500 flex items-center gap-1.5">
          <Clock className="h-3 w-3" />
          {(scene.start_ms / 1000).toFixed(1)}s – {(scene.end_ms / 1000).toFixed(1)}s
          {scene.duration_ms != null && (
            <span className="text-slate-400">({(scene.duration_ms / 1000).toFixed(1)}s)</span>
          )}
        </div>
      )}

      {/* Hint when scene matched but lacks evidence */}
      {scene && !control && !edge && hasGraph && (
        <p className="text-[10px] text-slate-400 italic px-2">
          No control or transition data for this scene — visual graph may be in preview state.
        </p>
      )}
    </>
  );
}

// ═══════════════════════════════════════════════════════════════
//  P6 — Execution Feedback UI
// ═══════════════════════════════════════════════════════════════

function LastRunBadge({
  summary,
}: {
  summary: import('../types/canonical').ScenarioLastRunSummary;
}) {
  const conf = RUN_STATUS_CONFIG[summary.last_run_status as RunStatusFilterKey]
    ?? RUN_STATUS_CONFIG.broken;
  const durationS = (summary.last_duration_ms / 1000).toFixed(1);
  const tooltipParts: string[] = [
    `Last run: ${summary.last_run_status}`,
    `${durationS}s`,
    `${summary.runs_in_window} runs in window`,
  ];
  if (summary.flake_rate_pct > 0) {
    tooltipParts.push(`flake ${summary.flake_rate_pct.toFixed(0)}%`);
  }
  if (summary.is_flaky) tooltipParts.push('flaky pattern detected');
  if (summary.consecutive_failures > 1) {
    tooltipParts.push(`${summary.consecutive_failures} consecutive failures`);
  }
  if (summary.selector_drift_observed) tooltipParts.push('selector drift');
  return (
    <span
      className={clsx(
        'inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[10px] font-medium',
        conf.color,
      )}
      title={tooltipParts.join(' · ')}
    >
      <Activity className="h-2.5 w-2.5" />
      Last run: {conf.label}
      <span className="font-mono text-[9px] opacity-70">{durationS}s</span>
      {summary.is_flaky && (
        <span className="ml-1 inline-flex items-center gap-0.5 text-amber-700">
          <AlertTriangle className="h-2.5 w-2.5" />flaky
        </span>
      )}
    </span>
  );
}

function RunSummaryPanel({
  summary,
  artifactId,
  scenarioId,
}: {
  summary: import('../types/canonical').ScenarioLastRunSummary;
  artifactId: string;
  scenarioId: string;
}) {
  const conf = RUN_STATUS_CONFIG[summary.last_run_status as RunStatusFilterKey]
    ?? RUN_STATUS_CONFIG.broken;
  const isFailing = summary.last_run_status === 'failed'
    || summary.last_run_status === 'broken'
    || summary.last_run_status === 'timed_out';
  const [detail, setDetail] = useState<
    import('../types/canonical').ScenarioLastRunDetailResponse | null
  >(null);
  const [loadingDetail, setLoadingDetail] = useState(false);
  const [detailErr, setDetailErr] = useState<string | null>(null);

  const loadDetail = useCallback(async () => {
    if (detail || loadingDetail) return;
    setLoadingDetail(true);
    setDetailErr(null);
    try {
      const resp = await api.getScenarioLastRunDetail(artifactId, scenarioId);
      setDetail(resp);
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : 'Failed to load run detail';
      setDetailErr(msg);
    } finally {
      setLoadingDetail(false);
    }
  }, [artifactId, scenarioId, detail, loadingDetail]);

  const failingSteps = detail?.steps?.filter(s =>
    s.status === 'failed' || s.status === 'broken' || s.status === 'timed_out'
  ) ?? [];

  return (
    <div className="rounded-md border border-gray-200 bg-white p-3 space-y-2">
      <div className="flex items-center justify-between gap-2">
        <span className="text-[10px] uppercase tracking-wider text-slate-500 font-semibold flex items-center gap-1">
          <Activity className="h-3 w-3" /> Last CI run
        </span>
        <span className={clsx(
          'inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[10px] font-medium',
          conf.color,
        )}>
          {conf.label}
        </span>
      </div>
      <div className="grid grid-cols-2 gap-x-3 gap-y-0.5 text-[10px]">
        <div className="text-slate-500">Status</div>
        <div className="text-slate-700">{summary.last_run_status}</div>
        <div className="text-slate-500">Duration</div>
        <div className="font-mono text-slate-700">
          {(summary.last_duration_ms / 1000).toFixed(1)}s
        </div>
        <div className="text-slate-500">Runs in window</div>
        <div className="text-slate-700">{summary.runs_in_window}</div>
        <div className="text-slate-500">Flake rate</div>
        <div className={clsx(
          'font-mono',
          summary.is_flaky ? 'text-amber-700 font-semibold' : 'text-slate-700',
        )}>
          {summary.flake_rate_pct.toFixed(0)}%
          {summary.is_flaky ? ' (flaky)' : ''}
        </div>
        {summary.consecutive_failures > 0 && (
          <>
            <div className="text-slate-500">Consec. fails</div>
            <div className="font-mono text-red-700">{summary.consecutive_failures}</div>
          </>
        )}
        {summary.ci_commit_sha && (
          <>
            <div className="text-slate-500">Commit</div>
            <div className="font-mono text-slate-700 truncate">
              {summary.ci_commit_sha.slice(0, 12)}
            </div>
          </>
        )}
        {summary.selector_drift_observed && (
          <>
            <div className="text-slate-500">Selector</div>
            <div className="text-amber-700 inline-flex items-center gap-1">
              <AlertTriangle className="h-2.5 w-2.5" /> drift observed
            </div>
          </>
        )}
      </div>
      {summary.last_error_message && (
        <div className="rounded-md bg-red-50 border border-red-200 px-2 py-1 text-[10px] text-red-700 font-mono whitespace-pre-wrap line-clamp-3">
          {summary.last_error_message}
        </div>
      )}

      {/* Lazy-load failing-step detail on demand */}
      {isFailing && !detail && !loadingDetail && (
        <button
          onClick={() => void loadDetail()}
          className="text-[10px] text-red-700 hover:text-red-900 underline"
        >
          Show failing-step detail + root-cause hints
        </button>
      )}
      {loadingDetail && (
        <p className="text-[10px] text-slate-500 inline-flex items-center gap-1">
          <Loader2 className="h-3 w-3 animate-spin" /> Loading run detail…
        </p>
      )}
      {detailErr && (
        <p className="text-[10px] text-red-700">Failed: {detailErr}</p>
      )}
      {detail?.has_runs && failingSteps.length > 0 && (
        <div className="space-y-1.5 pt-1 border-t border-gray-100">
          {failingSteps.map(step => (
            <div key={step.step_run_id} className="rounded border border-red-200 bg-red-50/50 px-2 py-1.5 space-y-1">
              <div className="flex items-baseline justify-between gap-2">
                <span className="text-[10px] font-mono text-red-700">
                  Step {step.step_number} — {step.status}
                </span>
                <span className="text-[10px] text-slate-500 font-mono">
                  {(step.duration_ms / 1000).toFixed(1)}s
                </span>
              </div>
              {step.error_message && (
                <p className="text-[10px] text-red-800 font-mono whitespace-pre-wrap line-clamp-2">
                  {step.error_message}
                </p>
              )}
              {step.drift.selector_drifted && (
                <p className="text-[10px] text-amber-700 inline-flex items-start gap-1">
                  <AlertTriangle className="h-2.5 w-2.5 mt-0.5 shrink-0" />
                  Selector drift: {step.drift.selector_diff}
                </p>
              )}
              {step.drift.bbox_drifted && step.drift.bbox_pixel_distance != null && (
                <p className="text-[10px] text-amber-700 inline-flex items-start gap-1">
                  <AlertTriangle className="h-2.5 w-2.5 mt-0.5 shrink-0" />
                  Element moved {step.drift.bbox_pixel_distance.toFixed(0)}px from
                  its recorded position.
                </p>
              )}
              {step.root_cause_hints.length > 0 && (
                <ul className="space-y-0.5">
                  {step.root_cause_hints.map((hint, i) => (
                    <li key={i} className="text-[10px] text-slate-700 flex items-start gap-1">
                      <span className="text-slate-400">•</span>
                      <span>{hint}</span>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

// ═══════════════════════════════════════════════════════════════
//  P7 — Self-Healing UI
// ═══════════════════════════════════════════════════════════════

/** Inline "Suggest selector fixes" section per scenario. Loads heal
 *  suggestions on demand; lets the user review each swap and click Apply.
 *  When applied, triggers ``onApplied`` so the parent can refresh the
 *  architect response (the scenario steps now point at new control IDs). */
function HealingSection({
  artifactId,
  scenario,
  onApplied,
}: {
  artifactId: string;
  scenario: E2EScenario;
  onApplied: () => void;
}) {
  const [plan, setPlan] = useState<
    import('../types/canonical').HealPlan | null
  >(null);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [chosen, setChosen] = useState<Set<string>>(new Set()); // key = "scenario_id:step_number"
  const [applying, setApplying] = useState(false);
  const [appliedSummary, setAppliedSummary] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setErr(null);
    setAppliedSummary(null);
    try {
      const resp = await api.getHealSuggestions(artifactId, {
        scenarioIds: [scenario.scenario_id],
      });
      setPlan(resp.plan);
      // Pre-check high-confidence suggestions (>= 0.85)
      const preChecked = new Set<string>();
      for (const s of resp.plan.suggestions) {
        if (s.match_confidence >= 0.85) {
          preChecked.add(`${s.scenario_id}:${s.step_number}`);
        }
      }
      setChosen(preChecked);
    } catch (e: unknown) {
      const detail = (e as { response?: { data?: { detail?: unknown } } })
        ?.response?.data?.detail;
      let msg: string;
      if (typeof detail === 'string') msg = detail;
      else if (detail && typeof detail === 'object') {
        msg = (detail as { message?: string }).message ?? JSON.stringify(detail);
      } else if (e instanceof Error) msg = e.message;
      else msg = 'Heal-plan request failed';
      setErr(msg);
    } finally {
      setLoading(false);
    }
  }, [artifactId, scenario.scenario_id]);

  const toggleChosen = useCallback((key: string) => {
    setChosen(prev => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  }, []);

  const apply = useCallback(async () => {
    if (!plan || chosen.size === 0) return;
    setApplying(true);
    setErr(null);
    try {
      const subset = plan.suggestions.filter(
        s => chosen.has(`${s.scenario_id}:${s.step_number}`),
      );
      const resp = await api.applyHealSuggestions(artifactId, subset);
      setAppliedSummary(
        `Applied ${resp.result.applied_count}, rejected ${resp.result.rejected_count}.`,
      );
      setPlan(null);
      setChosen(new Set());
      onApplied();
    } catch (e: unknown) {
      const detail = (e as { response?: { data?: { detail?: unknown } } })
        ?.response?.data?.detail;
      let msg: string;
      if (typeof detail === 'string') msg = detail;
      else if (detail && typeof detail === 'object') {
        msg = (detail as { message?: string }).message ?? JSON.stringify(detail);
      } else if (e instanceof Error) msg = e.message;
      else msg = 'Apply failed';
      setErr(msg);
    } finally {
      setApplying(false);
    }
  }, [plan, chosen, artifactId, onApplied]);

  if (!plan && !loading && !err && !appliedSummary) {
    return (
      <button
        onClick={() => void load()}
        className="text-[10px] text-orange-700 hover:text-orange-900 underline inline-flex items-center gap-1"
        title="Scan this scenario for stale control_ids and propose live-graph replacements"
      >
        <Wrench className="h-3 w-3" />
        Suggest selector fixes
      </button>
    );
  }

  if (loading) {
    return (
      <p className="text-[10px] text-slate-500 inline-flex items-center gap-1">
        <Loader2 className="h-3 w-3 animate-spin" /> Scanning for stale selectors…
      </p>
    );
  }

  if (err) {
    return (
      <div className="rounded-md border border-red-300 bg-red-50 px-3 py-1.5 text-[10px] text-red-700">
        <AlertTriangle className="inline h-3 w-3" /> {err}
      </div>
    );
  }

  if (appliedSummary) {
    return (
      <p className="text-[10px] text-emerald-700 inline-flex items-center gap-1">
        <CheckCircle2 className="h-3 w-3" /> {appliedSummary}
      </p>
    );
  }

  if (!plan) return null;

  if (plan.suggestions.length === 0 && plan.unhealable_steps.length === 0) {
    return (
      <p className="text-[10px] text-emerald-700 inline-flex items-center gap-1">
        <CheckCircle2 className="h-3 w-3" /> All step selectors still resolve in the live graph. Nothing to fix.
      </p>
    );
  }

  return (
    <div className="rounded-md border border-orange-200 bg-orange-50/40 p-2 space-y-1.5">
      <div className="flex items-center justify-between">
        <span className="text-[10px] uppercase tracking-wider text-orange-700 font-semibold inline-flex items-center gap-1">
          <Wrench className="h-3 w-3" />
          Self-healing suggestions ({plan.suggestions.length})
        </span>
        <button
          onClick={() => void apply()}
          disabled={applying || chosen.size === 0}
          className="rounded-md bg-orange-500 text-white text-[10px] font-medium px-2.5 py-1 hover:bg-orange-400 disabled:opacity-50 disabled:cursor-not-allowed inline-flex items-center gap-1"
        >
          {applying ? <Loader2 className="h-3 w-3 animate-spin" /> : null}
          Apply {chosen.size > 0 ? `(${chosen.size})` : ''}
        </button>
      </div>

      {plan.suggestions.map(s => {
        const key = `${s.scenario_id}:${s.step_number}`;
        const isChosen = chosen.has(key);
        const confColor = s.match_confidence >= 0.85
          ? 'text-emerald-700'
          : s.match_confidence >= 0.6
          ? 'text-amber-700'
          : 'text-red-700';
        return (
          <label
            key={key}
            className="flex items-start gap-2 rounded border border-orange-200 bg-white p-1.5 cursor-pointer hover:bg-slate-50"
          >
            <input
              type="checkbox"
              checked={isChosen}
              onChange={() => toggleChosen(key)}
              className="mt-0.5"
            />
            <div className="flex-1 text-[10px] space-y-0.5">
              <div className="flex items-baseline justify-between gap-2">
                <span className="text-slate-700">
                  Step <span className="font-mono">{s.step_number}</span> ·{' '}
                  <span className="font-medium">{s.old_label || '(no label)'}</span>
                </span>
                <span className={clsx('font-mono', confColor)}>
                  {(s.match_confidence * 100).toFixed(0)}%
                </span>
              </div>
              <p className="text-slate-600 italic">{s.reason}</p>
              <p className="text-slate-500">
                Old: <span className="font-mono">{s.old_control_id.slice(0, 8)}</span>
                {' → '}
                New: <span className="font-mono">{s.new_control_id.slice(0, 8)}</span>
                {s.new_label && (
                  <> · <span className="text-slate-700">"{s.new_label}"</span></>
                )}
              </p>
              {s.match_kind === 'label_match_other_scene' && (
                <p className="text-amber-700 text-[9px]">
                  ⚠ Match found in a different scene — review carefully before applying.
                </p>
              )}
            </div>
          </label>
        );
      })}

      {plan.unhealable_steps.length > 0 && (
        <div className="rounded border border-red-200 bg-red-50/60 p-1.5 space-y-0.5">
          <p className="text-[10px] font-semibold text-red-700 inline-flex items-center gap-1">
            <AlertTriangle className="h-2.5 w-2.5" />
            {plan.unhealable_steps.length} step(s) cannot be healed automatically:
          </p>
          {plan.unhealable_steps.map((u, i) => (
            <p key={i} className="text-[10px] text-red-700">
              Step {u.step_number} — {u.reason}
            </p>
          ))}
        </div>
      )}
    </div>
  );
}

// ═══════════════════════════════════════════════════════════════
//  P3 — Lifecycle UI components
// ═══════════════════════════════════════════════════════════════

/** Clickable badge that opens a dropdown of allowed next states.
 *  Implemented as a div with role="button" because it's rendered inside a
 *  parent button (the scenario card toggle) — nested buttons are invalid HTML.
 */
function LifecycleStateBadge({
  state,
  scenarioId,
  onTransition,
}: {
  state: ScenarioLifecycleState;
  scenarioId: string;
  onTransition: (scenarioId: string, newState: ScenarioLifecycleState, note?: string) => Promise<void>;
}) {
  const [open, setOpen] = useState(false);
  const [pending, setPending] = useState(false);
  const conf = LIFECYCLE_STATE_CONFIG[state];
  const allowedNext = ALLOWED_NEXT_STATES[state] ?? [];

  // Close menu on outside click
  useEffect(() => {
    if (!open) return;
    const handler = () => setOpen(false);
    // Defer attachment so the click that opens the menu isn't caught
    const id = window.setTimeout(() => window.addEventListener('click', handler), 0);
    return () => {
      window.clearTimeout(id);
      window.removeEventListener('click', handler);
    };
  }, [open]);

  const handleClick = async (e: React.MouseEvent, nextState: ScenarioLifecycleState) => {
    e.stopPropagation();
    setOpen(false);
    setPending(true);
    try {
      await onTransition(scenarioId, nextState);
    } finally {
      setPending(false);
    }
  };

  return (
    <span className="relative inline-block">
      <span
        role="button"
        tabIndex={0}
        onClick={e => { e.stopPropagation(); setOpen(o => !o); }}
        onKeyDown={e => {
          if (e.key === 'Enter' || e.key === ' ') {
            e.preventDefault();
            e.stopPropagation();
            setOpen(o => !o);
          }
        }}
        className={clsx(
          'inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[10px] font-medium cursor-pointer transition-opacity',
          conf.color,
          pending && 'opacity-50',
        )}
        title={`${conf.description}${allowedNext.length ? ' — click to change' : ''}`}
      >
        {conf.label}
        {allowedNext.length > 0 && (
          <ChevronDown className="h-2.5 w-2.5 opacity-60" />
        )}
      </span>
      {open && allowedNext.length > 0 && (
        <span className="absolute right-0 top-full mt-1 z-30 min-w-[150px] rounded-md border border-gray-200 bg-white shadow-md py-1 flex flex-col text-left">
          <span className="px-2 py-1 text-[9px] uppercase tracking-wider text-slate-400 border-b border-gray-100">
            Move to…
          </span>
          {allowedNext.map(next => {
            const nextConf = LIFECYCLE_STATE_CONFIG[next];
            return (
              <span
                key={next}
                role="button"
                tabIndex={0}
                onClick={e => handleClick(e, next)}
                onKeyDown={e => {
                  if (e.key === 'Enter' || e.key === ' ') {
                    handleClick(e as unknown as React.MouseEvent, next);
                  }
                }}
                className="px-2 py-1 text-[11px] text-slate-700 hover:bg-slate-50 cursor-pointer flex items-center gap-2"
                title={nextConf.description}
              >
                <span className={clsx('w-2 h-2 rounded-full', nextConf.color.split(' ').find(c => c.startsWith('bg-')))} />
                {nextConf.label}
              </span>
            );
          })}
        </span>
      )}
    </span>
  );
}

/** Prominent Approve / Reject / Request-changes button row for Reviewer mode. */
function ReviewerActions({
  scenarioId,
  state,
  onTransition,
}: {
  scenarioId: string;
  state: ScenarioLifecycleState;
  onTransition: (scenarioId: string, newState: ScenarioLifecycleState, note?: string) => Promise<void>;
}) {
  const [pending, setPending] = useState<ScenarioLifecycleState | null>(null);

  const run = async (next: ScenarioLifecycleState) => {
    setPending(next);
    try {
      await onTransition(scenarioId, next);
    } finally {
      setPending(null);
    }
  };

  const canApprove = ALLOWED_NEXT_STATES[state]?.includes('approved') ?? false;
  const canReject = ALLOWED_NEXT_STATES[state]?.includes('rejected') ?? false;
  const canReview = ALLOWED_NEXT_STATES[state]?.includes('reviewed') ?? false;

  if (state === 'approved' || state === 'rejected') {
    return (
      <div className="rounded-md border border-slate-200 bg-slate-50 px-3 py-2 text-[11px] text-slate-600">
        Already {state}. Use the state dropdown above to change.
      </div>
    );
  }

  return (
    <div className="flex flex-wrap items-center gap-2 rounded-md border border-emerald-200 bg-emerald-50/40 px-3 py-2">
      <span className="text-[10px] uppercase tracking-wider text-emerald-700 font-semibold">
        Reviewer
      </span>
      {canApprove && (
        <button
          onClick={() => run('approved')}
          disabled={pending !== null}
          className="rounded-md bg-emerald-600 text-white text-[11px] font-medium px-3 py-1 hover:bg-emerald-500 disabled:opacity-50"
        >
          {pending === 'approved' ? 'Approving…' : 'Approve'}
        </button>
      )}
      {canReject && (
        <button
          onClick={() => run('rejected')}
          disabled={pending !== null}
          className="rounded-md bg-white border border-red-300 text-red-700 text-[11px] font-medium px-3 py-1 hover:bg-red-50 disabled:opacity-50"
        >
          {pending === 'rejected' ? 'Rejecting…' : 'Reject'}
        </button>
      )}
      {canReview && state !== 'reviewed' && (
        <button
          onClick={() => run('reviewed')}
          disabled={pending !== null}
          className="rounded-md bg-white border border-blue-300 text-blue-700 text-[11px] font-medium px-3 py-1 hover:bg-blue-50 disabled:opacity-50"
        >
          {pending === 'reviewed' ? 'Marking…' : 'Mark reviewed'}
        </button>
      )}
    </div>
  );
}

/** Comments thread — list existing + input to add. */
function CommentsThread({
  scenario,
  onAddComment,
}: {
  scenario: E2EScenario;
  onAddComment: (scenarioId: string, body: string) => Promise<void>;
}) {
  const [draft, setDraft] = useState('');
  const [pending, setPending] = useState(false);
  const comments = scenario.comments ?? [];

  const submit = async () => {
    const body = draft.trim();
    if (!body) return;
    setPending(true);
    try {
      await onAddComment(scenario.scenario_id, body);
      setDraft('');
    } finally {
      setPending(false);
    }
  };

  return (
    <div className="space-y-2">
      <span className="text-[10px] text-slate-400 uppercase flex items-center gap-1">
        <Mic className="h-3 w-3" /> Comments ({comments.length})
      </span>
      {comments.length === 0 && (
        <p className="text-[11px] text-slate-400 italic">No comments yet.</p>
      )}
      {comments.length > 0 && (
        <div className="space-y-1.5">
          {comments.map(c => (
            <div key={c.comment_id} className="rounded-md border border-gray-200 bg-gray-50 px-3 py-2">
              <div className="flex items-baseline justify-between gap-2">
                <span className="text-[11px] font-medium text-slate-700 truncate">
                  {c.email || c.user_id || 'unknown'}
                </span>
                <span className="text-[10px] text-slate-400 font-mono shrink-0">
                  {c.created_at ? new Date(c.created_at).toLocaleString() : ''}
                </span>
              </div>
              <p className="text-[11px] text-slate-700 mt-0.5 whitespace-pre-wrap">{c.body}</p>
            </div>
          ))}
        </div>
      )}
      <div className="flex items-start gap-2">
        <textarea
          value={draft}
          onChange={e => setDraft(e.target.value)}
          onKeyDown={e => {
            if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
              e.preventDefault();
              void submit();
            }
          }}
          placeholder="Add a comment… (Ctrl+Enter to submit)"
          rows={2}
          maxLength={5000}
          className="flex-1 rounded-md border border-gray-200 bg-white px-2 py-1.5 text-[11px] text-slate-700 placeholder-slate-400 focus:border-nexus-500/50 focus:outline-none resize-none"
        />
        <button
          onClick={() => void submit()}
          disabled={!draft.trim() || pending}
          className="shrink-0 rounded-md bg-nexus-600 text-white text-[11px] font-medium px-3 py-1.5 hover:bg-nexus-500 disabled:opacity-50"
        >
          {pending ? 'Posting…' : 'Post'}
        </button>
      </div>
    </div>
  );
}

/** Collapsible audit log panel listing state transitions. */
function AuditLogPanel({ scenario }: { scenario: E2EScenario }) {
  const [open, setOpen] = useState(false);
  const log = scenario.audit_log ?? [];
  if (log.length === 0) {
    return (
      <p className="text-[10px] text-slate-400 italic">No state changes yet.</p>
    );
  }
  return (
    <div>
      <button
        onClick={() => setOpen(o => !o)}
        className="text-[10px] text-slate-500 hover:text-slate-700 flex items-center gap-1"
      >
        {open ? <ChevronDown className="h-3 w-3" /> : <ChevronRight className="h-3 w-3" />}
        Audit log ({log.length} {log.length === 1 ? 'entry' : 'entries'})
      </button>
      {open && (
        <div className="mt-1 space-y-1 border-l-2 border-gray-200 pl-2">
          {log.map((entry, i) => {
            const fromConf = LIFECYCLE_STATE_CONFIG[entry.from_state] ?? LIFECYCLE_STATE_CONFIG.draft;
            const toConf = LIFECYCLE_STATE_CONFIG[entry.to_state] ?? LIFECYCLE_STATE_CONFIG.draft;
            return (
              <div key={i} className="text-[10px] text-slate-600">
                <div className="flex items-center gap-1.5">
                  <span className={clsx('inline-flex rounded-full border px-1.5 py-0', fromConf.color)}>
                    {fromConf.label}
                  </span>
                  <ArrowRight className="h-2.5 w-2.5 text-slate-400" />
                  <span className={clsx('inline-flex rounded-full border px-1.5 py-0', toConf.color)}>
                    {toConf.label}
                  </span>
                  <span className="text-slate-400 ml-1 truncate">
                    by {entry.email || entry.user_id || 'unknown'}
                  </span>
                  <span className="font-mono text-slate-400 ml-auto shrink-0">
                    {entry.at ? new Date(entry.at).toLocaleString() : ''}
                  </span>
                </div>
                {entry.note && (
                  <p className="text-slate-500 italic ml-1">"{entry.note}"</p>
                )}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

// ═══════════════════════════════════════════════════════════════
//  ExportDropdown — multi-format test export (P5)
// ═══════════════════════════════════════════════════════════════

type ExportFormat = 'playwright' | 'cypress' | 'gherkin' | 'json';

const EXPORT_FORMATS: Array<{
  id: ExportFormat;
  label: string;
  description: string;
  enforcesGates: boolean;
}> = [
  { id: 'playwright', label: 'Playwright (.spec.ts)', description: '5-gate automation check enforced',  enforcesGates: true },
  { id: 'cypress',    label: 'Cypress (.cy.ts)',      description: '5-gate automation check enforced',  enforcesGates: true },
  { id: 'gherkin',    label: 'Gherkin (.feature)',    description: 'BDD-style; gates not enforced',     enforcesGates: false },
  { id: 'json',       label: 'JSON test plan',        description: 'Machine-readable; gates not enforced', enforcesGates: false },
];

function ExportDropdown({
  disabled,
  exporting,
  onExport,
}: {
  disabled: boolean;
  exporting: boolean;
  onExport: (format: ExportFormat) => Promise<void>;
}) {
  const [open, setOpen] = useState(false);

  useEffect(() => {
    if (!open) return;
    const handler = () => setOpen(false);
    const id = window.setTimeout(() => window.addEventListener('click', handler), 0);
    return () => {
      window.clearTimeout(id);
      window.removeEventListener('click', handler);
    };
  }, [open]);

  const pick = async (e: React.MouseEvent, fmt: ExportFormat) => {
    e.stopPropagation();
    setOpen(false);
    await onExport(fmt);
  };

  return (
    <span className="relative inline-block">
      <button
        onClick={e => { e.stopPropagation(); setOpen(o => !o); }}
        disabled={disabled}
        className="rounded-lg bg-indigo-100 px-3 py-1.5 text-xs font-medium text-indigo-700 hover:bg-indigo-200 transition-colors flex items-center gap-1.5 disabled:opacity-50 disabled:cursor-not-allowed"
        title="Download evidence-grounded test files (only approved scenarios are included)"
      >
        {exporting ? (
          <><Loader2 className="h-3.5 w-3.5 animate-spin" /> Exporting…</>
        ) : (
          <><Download className="h-3.5 w-3.5" /> Export <ChevronDown className="h-3 w-3 opacity-70" /></>
        )}
      </button>
      {open && !exporting && (
        <span className="absolute right-0 top-full mt-1 z-30 min-w-[220px] rounded-md border border-gray-200 bg-white shadow-md py-1 flex flex-col text-left">
          <span className="px-3 py-1 text-[9px] uppercase tracking-wider text-slate-400 border-b border-gray-100">
            Approved scenarios only
          </span>
          {EXPORT_FORMATS.map(fmt => (
            <button
              key={fmt.id}
              onClick={e => void pick(e, fmt.id)}
              className="px-3 py-1.5 text-left hover:bg-slate-50 flex flex-col gap-0.5"
            >
              <span className="text-[11px] text-slate-700 font-medium">{fmt.label}</span>
              <span className="text-[10px] text-slate-400">{fmt.description}</span>
            </button>
          ))}
        </span>
      )}
    </span>
  );
}
