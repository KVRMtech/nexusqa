// ═══════════════════════════════════════════════════════════════
//  VisualFlowDiagramPage — v5  ·  3D JOURNEY VISUALIZATION
//
//  Spectacular 3D user journey visualization:
//    • CSS 3D perspective transforms with isometric card layout
//    • Animated floating particle field background
//    • Glassmorphism cards with depth and parallax hover
//    • Neon-glow connectors with animated energy flow
//    • Hero screenshots with cinematic lighting
//    • Interactive 3D detail panel with slide-up animation
//    • Vivid gradient color system — bright, no dark/black
// ═══════════════════════════════════════════════════════════════

import { useState, useEffect, useMemo, useCallback, useRef, Fragment } from 'react';
import { useParams, useSearchParams, Link, useNavigate } from 'react-router-dom';
import {
  ArrowLeft, MonitorPlay, Zap, Shield, CheckCircle,
  AlertTriangle, GitBranch, Globe, Clock, Layers,
  MousePointerClick, ExternalLink, Eye,
  Activity, Target, Box, Navigation, FormInput, MousePointer,
  Sparkles, ChevronRight, Info,
} from 'lucide-react';
import clsx from 'clsx';
import api from '../services/api';
import type {
  VisualEvidenceGraph,
  VisualScene,
  VisualFlow,
  AppInstance,
  EvidenceControl,
  VisualFlowEdge,
  PrimaryActionSummary,
  SceneStateSummary,
  EvidenceStep,
  CursorEvent,
  StoryboardPayload,
  SceneAction,
  PageVisit,
} from '../types/canonical';
import { EvidenceStepsPanel } from './components/EvidenceStepsPanel';
import { StoryboardView } from './components/StoryboardView';

/* ── Helpers ──────────────────────────────────────────────── */
function fmtMs(ms: number | null): string {
  if (ms == null) return '—';
  const s = Math.round(ms / 1000);
  const m = Math.floor(s / 60);
  const sec = s % 60;
  return `${m}:${sec.toString().padStart(2, '0')}`;
}
function fmtDuration(startMs: number | null, endMs: number | null): string {
  if (startMs == null || endMs == null) return '';
  const dur = Math.round((endMs - startMs) / 1000);
  if (dur < 1) return '<1s';
  if (dur < 60) return `${dur}s`;
  return `${Math.floor(dur / 60)}m ${dur % 60}s`;
}
function extractDomain(url: string | null | undefined): string {
  if (!url) return '';
  try { return new URL(url).hostname.replace(/^www\./, ''); }
  catch { return ''; }
}

/* ── Human-readable screen label from scene data ── */
function humanScreenName(scene: VisualScene): string {
  // Phase 8: prefer persisted scene_state_summary screen_title even when quality
  // is "weak" — a weak title is still strictly better than the legacy generic
  // fallback "web_ui screen with 3 buttons", and the detail panel renders a
  // quality badge so the user can see the confidence.
  const summary = scene.scene_state_summary;
  const candidate = (summary?.screen_title ?? '').trim();
  if (candidate.length > 2 && !/^Scene \d+$/i.test(candidate)) {
    return candidate;
  }
  const raw = (scene.screen_name ?? '').trim();
  // Strip generic fallback template names (e.g. "web_ui screen with 3 buttons")
  if (/^Scene \d+$/i.test(raw) || /^(\w+_\w+\s+)?screen (with|showing)/i.test(raw)) {
    const domain = extractDomain(scene.detected_url);
    if (domain) return domain;
    return raw || 'Screen';
  }
  return raw || 'Screen';
}

/* ── Duration display respecting duration_quality ── */
function fmtSceneDuration(scene: VisualScene): string {
  const quality = scene.duration_quality ?? 'unknown';
  if (quality === 'unknown') return '';
  const dms = scene.duration_ms ?? ((scene.end_ms ?? 0) - (scene.start_ms ?? 0));
  if (dms < 0) return '';
  const s = Math.round(dms / 1000);
  const label = s < 60 ? `${s}s` : `${Math.floor(s / 60)}m ${s % 60}s`;
  return quality === 'estimated' ? `~${label}` : label;
}

/* ── Action badge: renders quality-aware label from persisted summary ── */
function ActionBadge({ summary, fallback }: { summary?: PrimaryActionSummary | null; fallback: string }) {
  if (!summary) return <span className="text-slate-500 text-[10px]">{fallback}</span>;
  const { action_label, action_quality, fallback_used } = summary;
  if (action_quality === 'weak') {
    // Do not claim a specific action; show state-only text
    return (
      <span className="text-slate-400 text-[10px] italic">
        {fallback}
        <span className="ml-1 text-[8px] text-slate-400 opacity-60">(low confidence)</span>
      </span>
    );
  }
  return (
    <span className={clsx(
      'text-[10px] font-bold',
      action_quality === 'strong' ? 'text-slate-800' : 'text-slate-600',
    )}>
      {action_quality === 'degraded' ? `Likely: ${action_label}` : action_label}
      {fallback_used && (
        <span className="ml-1 text-[8px] text-amber-500 opacity-70">OCR-derived</span>
      )}
    </span>
  );
}

/* ── Narrative group classifier for the detail panel ────────────────────────
   Groups controls into: form inputs → primary CTA → back/cancel → chrome
   so the detail panel reads as a human story, not a random chip list.
── */
const _PRIMARY_CTA_LABELS = new Set([
  'continue','submit','next','save','done','confirm','get','apply',
  'proceed','start','finish','complete','agree','accept','sign','send',
]);
const _BACK_NAV_LABELS = new Set([
  'back','cancel','previous','prev','close','exit','return','skip','no','decline',
]);
/** 0=form-input  1=primary-cta  2=back-nav  3=chrome/utility */
function ctrlNarrativeGroup(c: EvidenceControl): 0 | 1 | 2 | 3 {
  const kind = c.action_kind ?? '';
  if (kind === 'enter_text' || kind === 'select_option' || kind === 'check') return 0;
  const label = (c.label_text ?? '').toLowerCase().trim();
  if (_PRIMARY_CTA_LABELS.has(label)) return 1;
  if (_BACK_NAV_LABELS.has(label)) return 2;
  return 3;
}

/* ── Smart action label — uses persisted summary as primary truth ── */
function deriveActionLabel(
  scene: VisualScene,
  controls: EvidenceControl[],
  prevScene: VisualScene | null,
  edgeType: string | undefined,
  incomingEdge: VisualFlowEdge | undefined,
): { action: string; icon: React.ElementType; quality: 'strong' | 'degraded' | 'weak' } {
  const screenName = humanScreenName(scene);

  // First scene — what did the user open?
  if (!prevScene) {
    const domain = extractDomain(scene.detected_url);
    if (domain) return { action: `Opens ${domain}`, icon: Globe, quality: 'strong' };
    return { action: 'Starts session', icon: Navigation, quality: 'degraded' };
  }

  // App-switch edge: user switched to a different application
  if (edgeType === 'app_switch') {
    return { action: `Switches to ${screenName}`, icon: Box, quality: 'degraded' };
  }

  // Phase 8: use persisted primary_action_summary as primary truth
  const summary = incomingEdge?.primary_action_summary;
  if (summary && summary.action_quality !== 'weak') {
    const kind = summary.action_kind;
    const icon =
      kind === 'select_option' ? FormInput :
      kind === 'enter_text' ? FormInput :
      kind === 'submit_form' ? CheckCircle :
      kind === 'click_cta' ? MousePointer :
      Navigation;
    const label = summary.action_quality === 'degraded'
      ? `Likely: ${summary.action_label}`
      : summary.action_label;
    return { action: label, icon, quality: summary.action_quality };
  }

  // Weak/missing persisted summary — fall back to heuristic (still useful for display)
  const buttons = controls.filter((c) => c.element_type === 'button' || c.element_type === 'link');
  const inputs = controls.filter((c) => c.element_type === 'input' || c.element_type === 'select' || c.element_type === 'dropdown' || c.element_type === 'textarea' || c.element_type === 'text_field');

  if (inputs.length >= 1) {
    const fieldNames = inputs
      .map((c) => { const l = c.label_text?.trim() ?? ''; return l.length > 0 && l.length <= 40 ? l : ''; })
      .filter(Boolean).slice(0, 3);
    if (fieldNames.length > 0) return { action: `Fills in: ${fieldNames.join(', ')}`, icon: FormInput, quality: 'weak' };
    return { action: `Fills form on ${screenName}`, icon: FormInput, quality: 'weak' };
  }

  const prevName = humanScreenName(prevScene);
  if (screenName !== prevName && screenName !== 'Screen') {
    return { action: `Navigates to ${screenName}`, icon: Navigation, quality: 'weak' };
  }

  if (buttons.length > 0) {
    const btnNames = buttons.map((c) => c.label_text?.trim())
      .filter((l) => l && l.length > 1 && l.length <= 25 && l.toLowerCase() !== 'search').slice(0, 2);
    if (btnNames.length > 0) return { action: `Clicks ${btnNames.join(' or ')}`, icon: MousePointer, quality: 'weak' };
  }

  // Phase D.3 — Confidence-first rendering: when no signal supports a
  // specific action label, surface that explicitly as "Needs review"
  // rather than fabricating a "Reviews <screen>" sentence the SME never
  // performed.  This is the difference between an honest weak signal and
  // an invented narrative.
  return { action: 'Action needs review', icon: AlertTriangle, quality: 'weak' };
}

/* ── Journey narrative — tells a human story of what happened ── */
function buildJourneyNarrative(
  scenes: VisualScene[],
  controlsByScene: Map<string, EvidenceControl[]>,
  flows: VisualFlow[],
  edges: VisualFlowEdge[],
): string {
  if (scenes.length === 0) return '';

  const totalDur = fmtDuration(scenes[0]?.start_ms, scenes[scenes.length - 1]?.end_ms);
  const totalControls = scenes.reduce((sum, s) => sum + (controlsByScene.get(s.scene_id)?.length ?? 0), 0);
  const confirmedEdges = edges.filter((e) => e.edge_type === 'action_confirmed_transition').length;
  const appSwitches = edges.filter((e) => e.edge_type === 'app_switch').length;

  // Collect meaningful screen names (not generic fallbacks)
  const screenNames: string[] = [];
  const seen = new Set<string>();
  for (const s of scenes) {
    const name = humanScreenName(s);
    const key = name.toLowerCase();
    if (name.length > 3 && !/^scene \d+$/i.test(name) && !seen.has(key)) {
      seen.add(key);
      screenNames.push(name);
    }
  }

  // Multi-app session: describe as workspace story
  if (appSwitches > 0 && flows.length > 1) {
    const appNames = flows
      .filter((f) => !f.is_noise)
      .map((f) => f.flow_label.replace(/ \(\d+ visits?\)$/, '').trim())
      .filter(Boolean)
      .slice(0, 4);
    const durStr = totalDur ? ` over ${totalDur}` : '';
    const visitStr = flows.some((f) => (f as any).visit_count > 1) ? ', with context switching between apps' : '';
    return `KT session spanning ${appNames.join(', ')}${durStr}${visitStr} · ${confirmedEdges} confirmed actions · ${totalControls} UI elements`;
  }

  // Single-app session: describe the journey
  const domain = extractDomain(scenes[0]?.detected_url) || flows[0]?.domain || 'the application';
  const journey = screenNames.slice(0, 4).join(' → ');
  const durStr = totalDur ? ` in ${totalDur}` : '';
  const actionStr = confirmedEdges > 0 ? ` · ${confirmedEdges} confirmed actions` : '';

  // Phase D.3 — surface in-page action evidence count when the step
  // extractor populated frame_actions on any scene.  Customers ask
  // "where are the typing/click steps inside each page" — show the count
  // so they know in-page granularity is being captured.
  const intraSceneSteps = scenes.reduce(
    (sum, s) => sum + (s.scene_state_summary?.frame_action_count ?? 0),
    0,
  );
  const intraStr = intraSceneSteps > 0 ? ` · ${intraSceneSteps} in-page actions` : '';
  return `${domain} session: ${journey}${durStr}${actionStr}${intraStr} · ${totalControls} UI elements`;
}

/* ── Between-flow app-switch banner ── */
function AppSwitchBanner({ toLabel }: { toLabel: string }) {
  return (
    <div className="flex items-center gap-3 px-5 py-1">
      <div className="flex-1 h-px" style={{ background: 'linear-gradient(90deg, rgba(245,158,11,0), rgba(245,158,11,0.4))' }} />
      <div
        className="flex items-center gap-2 px-3 py-1.5 rounded-full text-[10px] font-black uppercase tracking-widest shrink-0"
        style={{ background: 'rgba(245,158,11,0.1)', border: '1px solid rgba(245,158,11,0.35)', color: '#d97706' }}
      >
        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
          <path d="M5 12h14M12 5l7 7-7 7" />
        </svg>
        App Switch &#8594; {toLabel}
      </div>
      <div className="flex-1 h-px" style={{ background: 'linear-gradient(90deg, rgba(245,158,11,0.4), rgba(245,158,11,0))' }} />
    </div>
  );
}

/* ── Intra-flow interruption gap indicator ─────────────────── */
// Shown between two scenes that belong to the same flow but have a gap in
// scene_index, meaning other-app scenes occurred in between.
function InterruptionGap({ missedCount }: { missedCount: number }) {
  return (
    <div className="flex items-center shrink-0 self-center mx-1">
      <div className="flex flex-col items-center gap-0.5">
        <div className="w-px h-3" style={{ background: 'rgba(100,116,139,0.25)' }} />
        <div
          className="flex items-center gap-1 px-2 py-0.5 rounded-full text-[8px] font-bold whitespace-nowrap"
          style={{ background: 'rgba(100,116,139,0.08)', border: '1px dashed rgba(100,116,139,0.35)', color: '#64748b' }}
        >
          <svg width="8" height="8" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5">
            <circle cx="12" cy="12" r="1" fill="currentColor" />
            <circle cx="6" cy="12" r="1" fill="currentColor" />
            <circle cx="18" cy="12" r="1" fill="currentColor" />
          </svg>
          {missedCount} other screen{missedCount !== 1 ? 's' : ''} here
        </div>
        <div className="w-px h-3" style={{ background: 'rgba(100,116,139,0.25)' }} />
      </div>
    </div>
  );
}

/* ── Color system — vivid, saturated ── */
const FLOW_COLORS = [
  { from: '#6366f1', to: '#a78bfa', bg: 'rgba(99,102,241,0.08)',  ring: 'rgba(99,102,241,0.25)', text: '#4f46e5', glow: '99,102,241' },
  { from: '#10b981', to: '#34d399', bg: 'rgba(16,185,129,0.08)',  ring: 'rgba(16,185,129,0.25)', text: '#059669', glow: '16,185,129' },
  { from: '#8b5cf6', to: '#c084fc', bg: 'rgba(139,92,246,0.08)',  ring: 'rgba(139,92,246,0.25)', text: '#7c3aed', glow: '139,92,246' },
  { from: '#ec4899', to: '#f472b6', bg: 'rgba(236,72,153,0.08)',  ring: 'rgba(236,72,153,0.25)', text: '#db2777', glow: '236,72,153' },
  { from: '#f59e0b', to: '#fbbf24', bg: 'rgba(245,158,11,0.08)',  ring: 'rgba(245,158,11,0.25)', text: '#d97706', glow: '245,158,11' },
  { from: '#06b6d4', to: '#22d3ee', bg: 'rgba(6,182,212,0.08)',   ring: 'rgba(6,182,212,0.25)',  text: '#0891b2', glow: '6,182,212' },
];

/* ── Animated Background Particles ── */
function ParticleField() {
  return (
    <div className="fixed inset-0 pointer-events-none overflow-hidden" style={{ zIndex: 0 }}>
      {/* Large ambient gradient blobs */}
      <div className="absolute w-[900px] h-[900px] rounded-full v5-float-slow" style={{ background: 'radial-gradient(circle, rgba(38,112,163,0.1) 0%, transparent 70%)', top: '-15%', left: '5%' }} />
      <div className="absolute w-[700px] h-[700px] rounded-full v5-float-medium" style={{ background: 'radial-gradient(circle, rgba(38,112,163,0.1) 0%, transparent 70%)', bottom: '0%', right: '0%' }} />
      <div className="absolute w-[500px] h-[500px] rounded-full v5-float-fast" style={{ background: 'radial-gradient(circle, rgba(38,112,163,0.08) 0%, transparent 70%)', top: '30%', left: '50%' }} />
      <div className="absolute w-[400px] h-[400px] rounded-full v5-float-medium" style={{ background: 'radial-gradient(circle, rgba(236,72,153,0.06) 0%, transparent 70%)', top: '60%', left: '20%' }} />

      {/* Subtle grid for depth perception */}
      <svg className="absolute inset-0 w-full h-full opacity-[0.04]" xmlns="http://www.w3.org/2000/svg">
        <defs>
          <pattern id="v5grid" width="60" height="60" patternUnits="userSpaceOnUse">
            <path d="M 60 0 L 0 0 0 60" fill="none" stroke="#818cf8" strokeWidth="0.5"/>
          </pattern>
        </defs>
        <rect width="100%" height="100%" fill="url(#v5grid)" />
      </svg>

      {/* Floating particles */}
      {Array.from({ length: 25 }, (_, i) => (
        <div
          key={i}
          className="absolute rounded-full v5-particle"
          style={{
            width: `${2 + Math.random() * 3}px`,
            height: `${2 + Math.random() * 3}px`,
            background: ['#6366f1', '#8b5cf6', '#3b82f6', '#06b6d4', '#10b981', '#ec4899'][i % 6],
            left: `${Math.random() * 100}%`,
            top: `${Math.random() * 100}%`,
            animationDelay: `${Math.random() * 8}s`,
            animationDuration: `${6 + Math.random() * 8}s`,
            opacity: 0.4 + Math.random() * 0.3,
          }}
        />
      ))}
    </div>
  );
}

/* ── 3D Stat Card ── */
function StatCard3D({ icon: Icon, label, value, accent, delay }: {
  icon: React.ElementType; label: string; value: string | number; accent: string; delay: number;
}) {
  return (
    <div className="v5-card-enter" style={{ animationDelay: `${delay}ms` }}>
      <div
        className="flex items-center gap-2 rounded-xl px-3 py-1.5 border backdrop-blur-xl transition-all duration-500 hover:scale-105 group"
        style={{
          background: `linear-gradient(135deg, ${accent}18, ${accent}08, rgba(255,255,255,0.03))`,
          borderColor: `${accent}30`,
          boxShadow: `0 2px 12px ${accent}15, 0 0 0 1px ${accent}10`,
        }}
      >
        <div className="p-1 rounded-lg transition-transform duration-300 group-hover:scale-110" style={{ background: `${accent}20` }}>
          <Icon className="h-3 w-3" style={{ color: accent }} />
        </div>
        <div>
          <div className="text-sm font-black text-slate-900 leading-none">{value}</div>
          <div className="text-[8px] uppercase tracking-[0.15em] font-bold mt-0.5" style={{ color: `${accent}90` }}>{label}</div>
        </div>
      </div>
    </div>
  );
}

/* ── 3D Flow Connector with energy pulse ── */
function FlowConnector3D({
  isConfirmed,
  isAppSwitch,
  color,
  index,
}: {
  isConfirmed: boolean;
  isAppSwitch?: boolean;
  color: typeof FLOW_COLORS[0];
  index: number;
}) {
  // App-switch connectors: dashed amber arc indicating context switch between apps
  if (isAppSwitch) {
    return (
      <div className="flex flex-col items-center justify-center shrink-0 self-center" style={{ width: 56, marginTop: 24 }}>
        <div className="relative">
          <svg width="56" height="36" viewBox="0 0 100 60" className="overflow-visible">
            <path d="M 5 30 Q 50 5 95 30" fill="none"
              stroke="#f59e0b" strokeWidth="2.5" strokeDasharray="6 4"
              strokeLinecap="round" opacity="0.9" />
            <polygon points="90,24 98,30 90,36" fill="#f59e0b" opacity="0.9" />
          </svg>
        </div>
        <span className="text-[8px] font-black uppercase tracking-widest mt-0.5" style={{ color: '#d97706' }}>
          App Switch
        </span>
      </div>
    );
  }

  return (
    <div className="flex flex-col items-center justify-center shrink-0 self-center" style={{ width: 56, marginTop: 24 }}>
      <div className="relative">
        <svg width="56" height="30" viewBox="0 0 100 50" className="overflow-visible">
          <defs>
            <linearGradient id={`cg3d-${index}`} x1="0" y1="0" x2="1" y2="0">
              <stop offset="0%" stopColor={color.from} stopOpacity={isConfirmed ? 1 : 0.4} />
              <stop offset="50%" stopColor={color.to} stopOpacity={isConfirmed ? 1 : 0.3} />
              <stop offset="100%" stopColor={color.from} stopOpacity={isConfirmed ? 0.8 : 0.2} />
            </linearGradient>
            <filter id={`neon-${index}`}>
              <feGaussianBlur stdDeviation="3" result="blur" />
              <feComposite in="SourceGraphic" in2="blur" operator="over" />
            </filter>
            <filter id={`neon-outer-${index}`}>
              <feGaussianBlur stdDeviation="6" />
            </filter>
          </defs>

          {/* Outer neon glow */}
          {isConfirmed && (
            <line x1="0" y1="25" x2="80" y2="25"
              stroke={color.from} strokeWidth="12" opacity="0.15"
              strokeLinecap="round" filter={`url(#neon-outer-${index})`}
              className="v5-pulse-glow"
            />
          )}

          {/* Main connector */}
          <line x1="5" y1="25" x2="78" y2="25"
            stroke={`url(#cg3d-${index})`}
            strokeWidth={isConfirmed ? 3 : 1.5}
            strokeLinecap="round"
            strokeDasharray={isConfirmed ? 'none' : '4 6'}
            className={isConfirmed ? '' : 'flow-edge-animated'}
          />

          {/* Arrow head */}
          <polygon points="76,18 92,25 76,32"
            fill={isConfirmed ? color.to : `${color.from}50`}
            filter={isConfirmed ? `url(#neon-${index})` : undefined}
          />

          {/* Energy particles */}
          {isConfirmed && (
            <>
              <circle r="3" fill={color.to} opacity="0.9" filter={`url(#neon-${index})`}>
                <animateMotion dur="1.5s" repeatCount="indefinite" path="M 5,25 L 78,25" />
              </circle>
              <circle r="2" fill="#fff" opacity="0.6">
                <animateMotion dur="1.5s" repeatCount="indefinite" path="M 5,25 L 78,25" begin="0.5s" />
              </circle>
            </>
          )}
        </svg>
      </div>

      <span className={clsx(
        'text-[7px] font-black uppercase tracking-[0.15em] mt-0.5',
        isConfirmed ? 'opacity-70' : 'opacity-30',
      )} style={{ color: isConfirmed ? color.text : '#64748b' }}>
        {isConfirmed ? '\u26A1' : '\u25E6'}
      </span>
    </div>
  );
}

/* ── 3D Scene Card ── */
interface SceneCardProps {
  scene: VisualScene;
  controls: EvidenceControl[];
  isSelected: boolean;
  onClick: () => void;
  stepNumber: number;
  totalSteps: number;
  color: typeof FLOW_COLORS[0];
  appName: string;
  index: number;
  actionLabel: string;
  actionIcon: React.ElementType;
  /** Phase 1.7 — when provided, the card prefers this annotated PNG
   *  URL over the raw frame.  Falls back to the raw frame on load
   *  error so card never shows a broken image. */
  annotatedImgUrl?: string | null;
}

function SceneCard3D({
  scene, controls, isSelected, onClick, stepNumber, color, appName, index,
  actionLabel, actionIcon: ActionIcon, annotatedImgUrl,
}: SceneCardProps) {
  const autoReadyCount = controls.filter((c) => c.automation_ready).length;

  // Phase 1.7 — append JWT to annotated URL so the <img> tag can
  // auth.  Raw frame URLs already have the token appended by
  // api.getFrameImageUrl.
  const annotatedWithToken = useMemo(() => {
    if (!annotatedImgUrl) return null;
    const token = sessionStorage.getItem('nexus_token') || '';
    const sep = annotatedImgUrl.includes('?') ? '&' : '?';
    return token ? `${annotatedImgUrl}${sep}token=${encodeURIComponent(token)}` : annotatedImgUrl;
  }, [annotatedImgUrl]);

  const rawImgUrl = scene.representative_frame_asset_path
    ? api.getFrameImageUrl(scene.representative_frame_asset_path)
    : scene.representative_frame_id
      ? api.getFrameImageUrl(scene.representative_frame_id)
      : null;

  // Two-tier fallback: annotated first, raw second.  We track
  // ``annotatedFailed`` so a single 404 silently degrades to raw
  // without flashing a broken image.
  const [annotatedFailed, setAnnotatedFailed] = useState(false);
  const imgUrl = annotatedFailed
    ? rawImgUrl
    : (annotatedWithToken ?? rawImgUrl);
  const domain = extractDomain(scene.detected_url);
  const cardRef = useRef<HTMLDivElement>(null);

  const handleMouseMove = useCallback((e: React.MouseEvent<HTMLDivElement>) => {
    if (!cardRef.current) return;
    const rect = cardRef.current.getBoundingClientRect();
    const x = (e.clientX - rect.left) / rect.width - 0.5;
    const y = (e.clientY - rect.top) / rect.height - 0.5;
    cardRef.current.style.transform = `${isSelected ? 'scale(1.03) ' : 'scale(1.01) '}`;
  }, [isSelected]);

  const handleMouseLeave = useCallback(() => {
    if (!cardRef.current) return;
    cardRef.current.style.transform = isSelected ? 'scale(1.02) ' : 'scale(1) ';
  }, [isSelected]);

  // Scroll the card into view when it becomes selected. This is the key UX
  // for the reverse direction — clicking a row in the bottom EvidenceStepsPanel
  // sets selectedSceneId on the parent, which marks the matching card
  // isSelected. Without this effect the card highlights but stays off-screen
  // if the user has scrolled away in the flow rail. block:'nearest' is a
  // no-op when the card is already visible, so direct clicks don't jank.
  useEffect(() => {
    if (!isSelected || !cardRef.current) return;
    cardRef.current.scrollIntoView({
      behavior: 'smooth',
      block: 'nearest',
      inline: 'center',
    });
  }, [isSelected]);

  return (
    <div
      className="v5-card-enter shrink-0"
      style={{ animationDelay: `${index * 80}ms`, width: 200 }}
    >
      {/* Step badge + action */}
      <div className="flex items-center gap-2 mb-2 px-0.5">
        <div
          className="relative w-7 h-7 rounded-lg flex items-center justify-center text-[11px] font-black shrink-0"
          style={{
            background: `linear-gradient(135deg, ${color.from}, ${color.to})`,
            color: '#fff',
            boxShadow: `0 0 16px rgba(${color.glow},0.35), 0 2px 6px rgba(10,37,64,0.1)`,
          }}
        >
          {stepNumber}
          <div className="absolute inset-[-2px] rounded-lg border v5-ring-pulse" style={{ borderColor: `rgba(${color.glow},0.3)` }} />
        </div>
        <div className="min-w-0">
          <div className="flex items-center gap-1 text-[10px] font-bold leading-tight" style={{ color: color.text }}>
            <ActionIcon className="h-3 w-3 shrink-0" />
            <span className="truncate">{actionLabel}</span>
          </div>
          <div className="text-[9px] text-slate-500 flex items-center gap-1.5">
            <span>{fmtMs(scene.start_ms)}</span>
            {fmtSceneDuration(scene) && (
              <span className="text-slate-400">&middot; {fmtSceneDuration(scene)}</span>
            )}
            {(scene.scene_quality === 'weak' || !scene.scene_quality) && (
              <span title="Low confidence scene label" className="text-amber-400 opacity-60">
                <Info className="h-2.5 w-2.5" />
              </span>
            )}
          </div>
        </div>
      </div>

      {/* 3D Card */}
      <div
        ref={cardRef}
        onClick={onClick}
        onMouseMove={handleMouseMove}
        onMouseLeave={handleMouseLeave}
        className="group relative rounded-xl overflow-hidden cursor-pointer transition-all duration-500 will-change-transform"
        style={{
          border: `1.5px solid ${isSelected ? color.from : 'rgba(10,37,64,0.1)'}`,
          background: isSelected
            ? `linear-gradient(145deg, #ffffff, #f8fafc)`
            : 'linear-gradient(145deg, #ffffff, #f8fafc)',
          boxShadow: isSelected
            ? `0 0 40px rgba(${color.glow},0.25), 0 12px 30px rgba(10,37,64,0.1), 0 0 0 1px rgba(${color.glow},0.15)`
            : '0 6px 20px rgba(10,37,64,0.08), 0 0 0 1px rgba(10,37,64,0.04)',
          transform: isSelected ? 'scale(1.03) ' : 'scale(1)',
          transformStyle: 'preserve-3d',
        }}
      >
        {/* Screenshot area */}
        <div className="relative h-28 overflow-hidden" style={{ background: 'linear-gradient(135deg, #f8fafc, #eef2f7)' }}>
          {imgUrl ? (
            <img
              src={imgUrl}
              alt={humanScreenName(scene) || `Step ${stepNumber}`}
              className="h-full w-full object-cover transition-transform duration-500 group-hover:scale-108"
              loading="lazy"
              onError={(e) => {
                // Phase 1.7 — first failure: try the raw frame.
                // Second failure: hide the image so the placeholder
                // beneath shows.
                if (!annotatedFailed && annotatedWithToken && rawImgUrl) {
                  setAnnotatedFailed(true);
                  return;
                }
                (e.target as HTMLImageElement).style.display = 'none';
              }}
            />
          ) : (
            <div className="h-full flex items-center justify-center">
              <div className="text-center space-y-1">
                <div className="w-8 h-8 rounded-lg mx-auto flex items-center justify-center" style={{ background: 'rgba(38,112,163,0.08)', border: '1px solid rgba(38,112,163,0.15)' }}>
                  <MonitorPlay className="h-4 w-4 text-slate-600/60" />
                </div>
                <p className="text-[9px] text-slate-500 font-medium">Capture</p>
              </div>
            </div>
          )}

          {/* Cinematic gradient overlays */}
          <div className="absolute inset-0 bg-gradient-to-t from-white via-white/40 to-transparent" />
          <div className="absolute inset-0 bg-gradient-to-r from-white/50 to-transparent" />

          {/* Confidence badge */}
          <div className="absolute top-1.5 right-1.5">
            <div
              className="flex items-center gap-0.5 rounded-lg px-1.5 py-0.5 text-[9px] font-black backdrop-blur-xl"
              style={{
                background: scene.completeness_confidence >= 0.8 ? 'rgba(16,185,129,0.25)' : 'rgba(245,158,11,0.25)',
                color: scene.completeness_confidence >= 0.8 ? '#34d399' : '#fbbf24',
                border: `1px solid ${scene.completeness_confidence >= 0.8 ? 'rgba(16,185,129,0.4)' : 'rgba(245,158,11,0.4)'}`,
              }}
            >
              <Sparkles className="h-2.5 w-2.5" />
              {Math.round(scene.completeness_confidence * 100)}%
            </div>
          </div>

          {/* Screen name */}
          <div className="absolute bottom-0 inset-x-0 px-2.5 pb-2">
            <h3 className="text-[10px] font-black text-slate-900 leading-tight line-clamp-2" style={{ textShadow: '0 1px 6px rgba(10,37,64,0.15)' }}>
              {humanScreenName(scene) || `Screen ${scene.scene_index + 1}`}
            </h3>
          </div>
        </div>

        {/* Info strip */}
        <div className="px-2.5 py-2 space-y-1.5" style={{ background: 'linear-gradient(180deg, #ffffff, #f8fafc)' }}>
          <div className="flex items-center gap-1 flex-wrap">
            {domain && (
              <span className="inline-flex items-center gap-1 text-[8px] font-bold rounded-md px-1.5 py-0.5"
                style={{ background: 'rgba(38,112,163,0.1)', color: '#164465', border: '1px solid rgba(38,112,163,0.2)' }}>
                <Globe className="h-2.5 w-2.5" />
                {domain}
              </span>
            )}
            <span className="inline-flex items-center gap-0.5 text-[8px] font-bold rounded-md px-1.5 py-0.5"
              style={{ background: color.bg, color: color.text, border: `1px solid ${color.ring}` }}>
              {appName}
            </span>
          </div>

          <div className="flex items-center justify-between text-[9px] pt-1.5" style={{ borderTop: '1px solid rgba(38,112,163,0.08)' }}>
            <span className="flex items-center gap-1 text-slate-600">
              <Layers className="h-2.5 w-2.5 text-slate-400" />
              <span className="font-bold text-slate-700">{controls.length}</span>
            </span>
            {autoReadyCount > 0 && (
              <span className="flex items-center gap-1 font-bold" style={{ color: '#047857' }}>
                <Zap className="h-2.5 w-2.5" />
                {autoReadyCount}
              </span>
            )}
          </div>
        </div>

        {/* Selected glow */}
        {isSelected && (
          <div className="absolute inset-0 rounded-xl pointer-events-none v5-selected-glow"
            style={{ boxShadow: `inset 0 0 60px rgba(${color.glow},0.1), 0 0 40px rgba(${color.glow},0.15)` }} />
        )}
      </div>
    </div>
  );
}

/* ═══════════════════════════════════════════════════════════
   PHASE 2 — Pages & Form Values panel
   ───────────────────────────────────────────────────────────
   Renders the URL-keyed page_visits log, each page's extracted
   actions, and the final form snapshot (label → value).  This is
   the surface that survives the canonical pipeline merging several
   distinct URLs into one scene — every distinct page the user
   visited shows here with the data the user entered on it.
   ═══════════════════════════════════════════════════════════ */
const PAGE_VERB_STYLES: Record<string, { bg: string; fg: string }> = {
  click:    { bg: 'rgba(56,189,248,0.15)', fg: '#0369a1' },
  type:     { bg: 'rgba(168,85,247,0.15)', fg: '#7e22ce' },
  select:   { bg: 'rgba(16,185,129,0.15)', fg: '#047857' },
  submit:   { bg: 'rgba(239,68,68,0.15)',  fg: '#b91c1c' },
  navigate: { bg: 'rgba(245,158,11,0.15)', fg: '#b45309' },
  scroll:   { bg: 'rgba(148,163,184,0.18)',fg: '#475569' },
  hover:    { bg: 'rgba(148,163,184,0.18)',fg: '#475569' },
  none:     { bg: 'rgba(148,163,184,0.10)',fg: '#94a3b8' },
};

function PageVisitsPanel({ graph }: { graph: VisualEvidenceGraph }) {
  const visits: PageVisit[] = graph.page_visits ?? [];
  const pageActions = graph.page_actions ?? {};
  const formSnapshots = graph.form_snapshots ?? {};

  // Only render the section when the Phase 2 extractors have produced
  // data.  Older artifacts (pre-Phase-2) return empty arrays and the
  // panel stays hidden so the page looks unchanged for them.
  if (visits.length === 0) return null;

  const visitsWithForm = visits.filter(
    (v) => v.form_snapshot && Object.keys(v.form_snapshot).length > 0,
  ).length;
  const totalActions = Object.values(pageActions).reduce(
    (n, list) => n + (list?.length ?? 0), 0,
  );

  const displayLocation = (v: PageVisit): string => {
    if (v.location) return v.location;
    if (v.canonical_host) return `${v.canonical_host}${v.url_path || ''}`;
    return '(location not detected)';
  };

  return (
    <section className="relative z-10">
      <div
        className="rounded-2xl overflow-hidden"
        style={{
          background: 'linear-gradient(135deg, rgba(56,189,248,0.05), rgba(16,185,129,0.04))',
          border: '1px solid rgba(56,189,248,0.22)',
          boxShadow: '0 6px 24px rgba(38,112,163,0.08)',
        }}
      >
        {/* Header (static summary — the tab owns the scroll) */}
        <div
          className="w-full flex items-center gap-3 px-4 py-3"
          style={{ background: 'rgba(255,255,255,0.4)' }}
        >
          <div className="p-1.5 rounded-lg" style={{ background: 'rgba(56,189,248,0.15)' }}>
            <Globe className="h-4 w-4" style={{ color: '#0369a1' }} />
          </div>
          <div className="min-w-0">
            <div className="text-[12px] font-black text-slate-900 leading-tight">
              Pages &amp; Form Values
              <span className="ml-2 text-[10px] font-bold text-slate-400 uppercase tracking-wider">
                URL-keyed · independent of scene grouping
              </span>
            </div>
            <div className="text-[10px] text-slate-500 font-semibold">
              {visits.length} page{visits.length === 1 ? '' : 's'} visited
              {' · '}{totalActions} action{totalActions === 1 ? '' : 's'}
              {' · '}{visitsWithForm} with form data
            </div>
          </div>
        </div>

        <div className="px-3 pb-3 pt-1 space-y-2">
            {visits.map((v) => {
              const actions = pageActions[v.page_visit_id] ?? [];
              const snap = formSnapshots[v.page_visit_id];
              const fields = (snap?.fields ?? v.form_snapshot ?? {}) as Record<string, string>;
              const fieldEntries = Object.entries(fields);
              const isWeb = v.location?.startsWith('http');
              return (
                <div
                  key={v.page_visit_id}
                  className="rounded-xl px-3 py-2.5"
                  style={{
                    background: 'rgba(255,255,255,0.6)',
                    border: '1px solid rgba(56,189,248,0.15)',
                  }}
                >
                  {/* URL row */}
                  <div className="flex items-center gap-2 flex-wrap">
                    <span
                      className="inline-flex items-center justify-center min-w-[22px] h-[22px] px-1.5 rounded-full text-[10px] font-black"
                      style={{ background: 'rgba(38,112,163,0.12)', color: '#0369a1' }}
                    >
                      {v.sequence_index}
                    </span>
                    {isWeb ? (
                      <Globe className="h-3.5 w-3.5 shrink-0" style={{ color: '#0369a1' }} />
                    ) : (
                      <Layers className="h-3.5 w-3.5 shrink-0" style={{ color: '#7e22ce' }} />
                    )}
                    <span className="text-[12px] font-bold text-slate-900 break-all">
                      {displayLocation(v)}
                    </span>
                    <span className="flex items-center gap-1 text-[9px] text-slate-400 font-semibold ml-auto">
                      <Clock className="h-2.5 w-2.5" />
                      {fmtDuration(v.first_seen_ms, v.last_seen_ms)}
                    </span>
                    <span
                      className="inline-flex items-center px-1.5 py-0.5 rounded-md text-[8px] font-bold uppercase tracking-wide"
                      style={{ background: 'rgba(148,163,184,0.15)', color: '#64748b' }}
                      title={`source: ${v.source} · confidence ${Math.round((v.extraction_confidence ?? 0) * 100)}%`}
                    >
                      {v.source.replace(/_/g, ' ')}
                    </span>
                  </div>

                  {/* Actions */}
                  {actions.length > 0 && (
                    <div className="mt-2 flex flex-wrap gap-1.5">
                      {actions.map((a, i) => {
                        const vs = PAGE_VERB_STYLES[a.verb] ?? PAGE_VERB_STYLES.none;
                        if (a.verb === 'none' && !a.target_label) return null;
                        return (
                          <span
                            key={`${a.subaction_index ?? i}-${a.verb}`}
                            className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded-md text-[10px] font-semibold"
                            style={{ background: vs.bg, color: vs.fg }}
                          >
                            <span className="font-black uppercase">{a.verb}</span>
                            {a.target_label && <span className="text-slate-700">{a.target_label}</span>}
                            {a.value && (
                              <span className="font-black text-slate-900">= {a.value}</span>
                            )}
                          </span>
                        );
                      })}
                    </div>
                  )}

                  {/* Form snapshot */}
                  {fieldEntries.length > 0 && (
                    <div
                      className="mt-2 rounded-lg px-2.5 py-2"
                      style={{ background: 'rgba(16,185,129,0.06)', border: '1px solid rgba(16,185,129,0.18)' }}
                    >
                      <div className="flex items-center gap-1.5 mb-1.5">
                        <FormInput className="h-3 w-3" style={{ color: '#047857' }} />
                        <span className="text-[9px] font-black uppercase tracking-widest text-emerald-700">
                          Form values captured
                        </span>
                        {snap?.page_intent && (
                          <span className="text-[9px] text-slate-500 font-semibold italic">
                            · {snap.page_intent}
                          </span>
                        )}
                      </div>
                      <div className="grid grid-cols-1 sm:grid-cols-2 gap-x-4 gap-y-1">
                        {fieldEntries.map(([label, value]) => (
                          <div key={label} className="flex items-baseline gap-1.5 text-[11px] min-w-0">
                            <span className="text-slate-500 font-semibold truncate" title={label}>
                              {label}:
                            </span>
                            <span className="text-slate-900 font-black break-words">
                              {value || '—'}
                            </span>
                          </div>
                        ))}
                      </div>
                    </div>
                  )}
                </div>
              );
            })}
        </div>
      </div>
    </section>
  );
}

/* ═══════════════════════════════════════════════════════════
   MAIN PAGE
   ═══════════════════════════════════════════════════════════ */
export default function VisualFlowDiagramPage() {
  const { sessionId } = useParams<{ sessionId: string }>();
  const [searchParams] = useSearchParams();
  const navigate = useNavigate();
  const artifactId = searchParams.get('artifact_id') ?? '';

  const [graph, setGraph] = useState<VisualEvidenceGraph | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [selectedSceneId, setSelectedSceneId] = useState<string | null>(null);
  const [activeFlowId, setActiveFlowId] = useState<string | null>(null);
  const scrollRef = useRef<HTMLDivElement>(null);

  // ─── Storyboard tab (Phase 1) ────────────────────────────────
  // The storyboard view is the new picture-first default.  We fetch
  // it in parallel with the 3D journey graph so switching tabs is
  // instant.  When the storyboard endpoint is missing (older
  // deployment) the tab gracefully shows an empty state.
  const initialMode = (searchParams.get('view') || 'storyboard') as
    | 'storyboard'
    | 'journey'
    | 'pages';
  const [viewMode, setViewMode] = useState<'storyboard' | 'journey' | 'pages'>(initialMode);
  const [storyboard, setStoryboard] = useState<StoryboardPayload | null>(null);
  const [storyboardLoading, setStoryboardLoading] = useState(true);
  const [storyboardError, setStoryboardError] = useState<string | null>(null);

  /** Phase D.1 — triangulated steps + cursor events for the artifact.
   *  Loaded once per artifact load.  When the endpoints return empty (legacy
   *  artifact processed before persist-action-evidence stage existed), the
   *  EvidenceStepsPanel renders an explicit "no steps yet" placeholder
   *  instead of fabricating labels. */
  const [evidenceSteps, setEvidenceSteps] = useState<EvidenceStep[]>([]);
  const [cursorEvents, setCursorEvents] = useState<CursorEvent[]>([]);
  const [allFrames, setAllFrames] = useState<Array<{
    frame_id: string;
    frame_index: number;
    frame_asset_path: string;
    timestamp_seconds: number;
    is_keyframe: boolean;
    // Phase 1.7 — per-frame evidence fields used by the new scene
    // detail panel.  Always returned by the backend; optional in the
    // type so we degrade gracefully if a row is missing one.
    extracted_text?: string;
    url_or_path?: string;
    ui_elements_json?: Array<Record<string, unknown>>;
    scene_id?: string | null;
    ocr_confidence?: number;
  }>>([]);
  const [selectedStepId, setSelectedStepId] = useState<string | null>(null);
  /** Phase D.2 — overlay coordinates that the step-replay layer renders
   *  on top of the selected step's frame thumbnail. */
  const [activeCursorMark, setActiveCursorMark] = useState<{
    x: number;
    y: number;
    frameId: string | null;
    timestampMs: number;
  } | null>(null);

  useEffect(() => {
    if (!artifactId) return;
    setLoading(true);
    // Phase 1.7 — request storyboard overlays so the journey rail
    // can use dedup'd app names, LLM captions, annotated thumbnails,
    // and noise filtering.  Backwards-compat: backend ignores the
    // flag when storyboard derivations haven't run yet (empty
    // overlay fields, journey renders with the original heuristics).
    api.getVisualEvidenceGraph(artifactId, { includeStoryboardOverlays: true })
      .then((g) => { setGraph(g); setLoading(false); })
      .catch((e) => { setError(String(e)); setLoading(false); });
  }, [artifactId]);

  // Fetch the storyboard payload alongside the visual graph.  This
  // triggers lazy derivation on the backend so the first viewer of
  // an artifact pays the LLM cost; subsequent loads are cached.
  useEffect(() => {
    if (!artifactId) return;
    let cancelled = false;
    setStoryboardLoading(true);
    setStoryboardError(null);
    api.getStoryboard(artifactId)
      .then((payload) => {
        if (cancelled) return;
        setStoryboard(payload);
        setStoryboardLoading(false);
      })
      .catch((e) => {
        if (cancelled) return;
        const message =
          (e?.response?.data?.detail as string | undefined) ||
          (e?.message as string | undefined) ||
          'Failed to load storyboard';
        setStoryboardError(message);
        setStoryboardLoading(false);
      });
    return () => { cancelled = true; };
  }, [artifactId]);

  // Load triangulated steps + cursor events + per-frame thumbnails
  // independently of the visual graph so the slower per-frame queries do
  // not delay the main flow page.  All three are best-effort: failures
  // degrade the detail panel rather than blocking the page.
  useEffect(() => {
    if (!artifactId) return;
    let cancelled = false;
    api.listArtifactEvidenceSteps(artifactId)
      .then((res) => { if (!cancelled) setEvidenceSteps(res.steps || []); })
      .catch(() => { if (!cancelled) setEvidenceSteps([]); });
    api.listArtifactCursorEvents(artifactId)
      .then((res) => { if (!cancelled) setCursorEvents(res.events || []); })
      .catch(() => { if (!cancelled) setCursorEvents([]); });
    api.listArtifactFramesAll(artifactId)
      .then((frames) => { if (!cancelled) setAllFrames(frames); })
      .catch(() => { if (!cancelled) setAllFrames([]); });
    return () => { cancelled = true; };
  }, [artifactId]);

  /* ── Derived data ── */
  // Phase 1.7 \u2014 prefer the storyboard composer's dedup'd app names
  // (e.g. "Guardian Life Insurance" instead of "Wivwquardianlife.com")
  // when the backend supplied them via app_groupings.  Falls back to
  // the raw app_instance.app_name for legacy artifacts.
  const appNameMap = useMemo(() => {
    const m = new Map<string, string>();
    if (!graph) return m;

    graph.app_instances.forEach((inst) => {
      let name = '';
      // Look up the dedup'd grouping that contains this instance.
      for (const g of graph.app_groupings ?? []) {
        const members = (g as unknown as { member_instance_ids?: string[] })
          .member_instance_ids ?? [];
        if (members.includes(inst.instance_id)) {
          name = g.display_label || g.canonical_name || g.canonical_domain || '';
          break;
        }
      }
      // Fall back to raw app_instance name when no grouping match
      // (legacy artifact or storyboard derivations not run yet).
      if (!name) {
        name = inst.app_name ?? inst.app_type ?? 'Application';
      }
      if (name.length > 24) name = name.slice(0, 21) + '\u2026';
      m.set(inst.instance_id, name);
    });
    return m;
  }, [graph]);

  // Phase 1.7 \u2014 quick lookup for the LLM short caption per scene.
  // When present, the card's actionLabel uses this string verbatim
  // (it's already a 5-8 word imperative caption from llama3.2:3b).
  // When missing, deriveActionLabel() falls through to the existing
  // heuristic so legacy artifacts still render captions.
  const captionByScene = useMemo(() => {
    const m = new Map<string, string>();
    if (!graph?.scene_captions) return m;
    for (const [sceneId, c] of Object.entries(graph.scene_captions)) {
      if (c?.short) m.set(sceneId, c.short);
    }
    return m;
  }, [graph]);

  // Phase 1.7 defect-fix \u2014 clean flow tab labels.
  //
  // The visual_flows table stores raw flow_label values like
  // "Wivwquardianlife.com - Ivwquardianlife" (OCR-corrupted).  We
  // override that with the canonical app grouping's display_label
  // when the storyboard composer has dedup'd the flow's scenes into
  // a known grouping.
  //
  // Logic: for each flow, find the scenes that belong to it, look up
  // their app_grouping_id via scene_to_app_grouping, and use the
  // dominant grouping's display_label.  Falls back to flow.flow_label
  // for legacy artifacts or flows with no scene mapping.
  const cleanFlowLabel = useCallback(
    (flow: VisualFlow): string => {
      if (!graph) return flow.flow_label || '';
      const groupingCount = new Map<string, number>();
      const flowScenes = graph.scenes.filter((s) => s.flow_id === flow.flow_id);
      const s2g = graph.scene_to_app_grouping || {};
      for (const scene of flowScenes) {
        const gid = s2g[scene.scene_id];
        if (gid) groupingCount.set(gid, (groupingCount.get(gid) || 0) + 1);
      }
      if (groupingCount.size === 0) return flow.flow_label || '';
      // Pick the grouping with the most scenes in this flow
      let dominantId = '';
      let dominantCount = 0;
      for (const [gid, cnt] of groupingCount) {
        if (cnt > dominantCount) {
          dominantCount = cnt;
          dominantId = gid;
        }
      }
      const grouping = (graph.app_groupings || []).find(
        (g) => g.grouping_id === dominantId,
      );
      if (!grouping) return flow.flow_label || '';
      return (
        grouping.display_label
        || grouping.canonical_name
        || grouping.canonical_domain
        || flow.flow_label
        || ''
      );
    },
    [graph],
  );

  const controlsByScene = useMemo(() => {
    if (!graph) return new Map<string, EvidenceControl[]>();
    const m = new Map<string, EvidenceControl[]>();
    for (const [sid, ctrls] of Object.entries(graph.controls_by_scene)) m.set(sid, ctrls);
    return m;
  }, [graph]);

  const edgeFromMap = useMemo(() => {
    if (!graph) return new Map<string, VisualFlowEdge>();
    const m = new Map<string, VisualFlowEdge>();
    for (const e of graph.edges) m.set(e.from_scene_id, e);
    return m;
  }, [graph]);

  const edgeToMap = useMemo(() => {
    if (!graph) return new Map<string, VisualFlowEdge>();
    const m = new Map<string, VisualFlowEdge>();
    for (const e of graph.edges) m.set(e.to_scene_id, e);
    return m;
  }, [graph]);

  // Phase 1.7 — noise filter.  Set of scene_ids the storyboard
  // composer flagged as noise (e.g. "New Tab" cards).  Filtered out
  // of flowGroups by default; a toggle in the header lets the user
  // reveal them when investigating gaps.
  const noiseSceneIds = useMemo(() => {
    return new Set<string>(graph?.noise_scene_ids ?? []);
  }, [graph]);
  const [showNoiseScenes, setShowNoiseScenes] = useState(false);

  // Phase 1.7 defect-fix — consolidate spurious flow tabs.
  //
  // graph.flows comes from the canonical flow_segmenter, which opens a
  // new flow every time the per-scene boundary score exceeds 0.5.
  // Domain change alone weighs 0.4, so a single frame of OCR misreading
  // the URL (e.g. real usaa.com → "msdd.com" for one frame) is enough
  // to start a brand-new flow.  Each OCR slip → a separate tab in the
  // header, even though the user only visited one site.
  //
  // The storyboard app_deduper already merges OCR-corrupted
  // app_instances into canonical app_groupings.  We piggy-back on that
  // here: every flow is mapped to its dominant app_grouping (the same
  // mechanism cleanFlowLabel uses), then flows sharing a grouping are
  // folded into a single tab with summed visit_count + scene_count.
  //
  // Three filter rules, all data-derived (no hardcoded domains):
  //   1. Drop a flow when every one of its scenes is in noise_scene_ids
  //      — the composer already classified those scenes as noise.
  //   2. Merge flows sharing the same dominant app_grouping_id — one
  //      tab per real application, not one per OCR boundary.
  //   3. Drop a flow with no grouping mapping AND no real time spent
  //      (visit_count == non_noise_scene_count, scene_count ≤ 2) —
  //      the dominant signature of a single-frame OCR-only flow.
  //
  // When `showNoiseScenes` is on the consolidation is bypassed so the
  // raw shape is visible for debugging.
  type ConsolidatedFlow = {
    flow_id: string;
    member_flow_ids: string[];
    label: string;
    visit_count: number;
    is_interleaved: boolean;
    scene_count: number;
    is_noise: boolean;
    grouping_id: string | null;
  };

  const consolidatedFlows = useMemo<ConsolidatedFlow[]>(() => {
    if (!graph) return [];
    const flows = graph.flows ?? [];
    if (showNoiseScenes) {
      return flows.map((f) => ({
        flow_id: f.flow_id,
        member_flow_ids: [f.flow_id],
        label: cleanFlowLabel(f),
        visit_count: f.visit_count ?? 1,
        is_interleaved: !!f.is_interleaved,
        scene_count: f.scene_count,
        is_noise: !!f.is_noise,
        grouping_id: null,
      }));
    }

    const s2g = graph.scene_to_app_grouping || {};
    const scenesByFlow = new Map<string, VisualScene[]>();
    for (const s of graph.scenes) {
      if (!s.flow_id) continue;
      const list = scenesByFlow.get(s.flow_id) ?? [];
      list.push(s);
      scenesByFlow.set(s.flow_id, list);
    }

    const result: ConsolidatedFlow[] = [];
    const byGrouping = new Map<string, ConsolidatedFlow>();

    for (const flow of flows) {
      const flowScenes = scenesByFlow.get(flow.flow_id) ?? [];
      const nonNoise = flowScenes.filter(
        (s) => !noiseSceneIds.has(s.scene_id),
      );
      if (nonNoise.length === 0) continue;

      const counts = new Map<string, number>();
      for (const s of flowScenes) {
        const gid = s2g[s.scene_id];
        if (gid) counts.set(gid, (counts.get(gid) ?? 0) + 1);
      }
      let dominantId: string | null = null;
      let dominantCount = 0;
      for (const [gid, cnt] of counts) {
        if (cnt > dominantCount) {
          dominantCount = cnt;
          dominantId = gid;
        }
      }

      const incomingVisits = flow.visit_count ?? 1;

      if (
        dominantId === null
        && incomingVisits === nonNoise.length
        && nonNoise.length <= 2
      ) {
        continue;
      }

      const label = cleanFlowLabel(flow);

      if (dominantId !== null) {
        const existing = byGrouping.get(dominantId);
        if (existing) {
          existing.member_flow_ids.push(flow.flow_id);
          existing.visit_count += incomingVisits;
          existing.is_interleaved = true;
          existing.scene_count += nonNoise.length;
          existing.is_noise = existing.is_noise && !!flow.is_noise;
        } else {
          const entry: ConsolidatedFlow = {
            flow_id: flow.flow_id,
            member_flow_ids: [flow.flow_id],
            label,
            visit_count: incomingVisits,
            is_interleaved: !!flow.is_interleaved,
            scene_count: nonNoise.length,
            is_noise: !!flow.is_noise,
            grouping_id: dominantId,
          };
          byGrouping.set(dominantId, entry);
          result.push(entry);
        }
      } else {
        result.push({
          flow_id: flow.flow_id,
          member_flow_ids: [flow.flow_id],
          label,
          visit_count: incomingVisits,
          is_interleaved: !!flow.is_interleaved,
          scene_count: nonNoise.length,
          is_noise: !!flow.is_noise,
          grouping_id: null,
        });
      }
    }
    return result;
  }, [graph, noiseSceneIds, showNoiseScenes, cleanFlowLabel]);

  // When a consolidated tab is active, flowGroups must include scenes
  // from every member flow_id, not just the anchor.  Empty set when no
  // tab is selected (all flows visible).
  const activeMemberFlowIds = useMemo<Set<string> | null>(() => {
    if (!activeFlowId) return null;
    const entry = consolidatedFlows.find((cf) =>
      cf.member_flow_ids.includes(activeFlowId),
    );
    return entry ? new Set(entry.member_flow_ids) : new Set([activeFlowId]);
  }, [activeFlowId, consolidatedFlows]);

  // Phase 1.7 Day 4 — Session at a Glance is collapsible.  Auto-
  // collapses when the user selects a scene so the bottom detail
  // panel has room (without this, the page was squeezed and the
  // user couldn't scroll back up).  Manual toggle lets the user
  // expand it again at any time.
  const [glanceCollapsed, setGlanceCollapsed] = useState(false);
  useEffect(() => {
    if (selectedSceneId) setGlanceCollapsed(true);
  }, [selectedSceneId]);

  // Phase 1.7 Day 4 — Session at a Glance.
  //
  // Pre-computes the header card's data: hero scene (the last non-
  // noise scene — usually the WOW moment, e.g. quote result page),
  // visible app groupings (any grouping whose scenes are not 100%
  // noise), total non-noise duration, and a per-quality breakdown
  // counted from the underlying VisualSceneRow.scene_quality field.
  //
  // Returns null when the artifact has no non-noise scenes — the
  // header simply doesn't render in that case (the user is staring
  // at a pure-noise session and the journey rail's empty state
  // explains why).
  const sessionGlance = useMemo(() => {
    if (!graph) return null;
    const nonNoiseScenes = graph.scenes.filter(
      (s) => !noiseSceneIds.has(s.scene_id),
    );
    if (nonNoiseScenes.length === 0) return null;

    // Hero = last non-noise scene chronologically (the "WOW moment").
    // For Guardian Life this is the Quote Result page; for other
    // demos it's whatever the user ended on.
    const sortedByTime = [...nonNoiseScenes].sort(
      (a, b) => (a.scene_index ?? 0) - (b.scene_index ?? 0),
    );
    const heroScene = sortedByTime[sortedByTime.length - 1];
    const heroAnnotatedRaw = graph.annotated_frame_urls?.[heroScene.scene_id];
    const heroCaption = graph.scene_captions?.[heroScene.scene_id]?.short || '';

    // Append JWT to the annotated URL so an unauthenticated <img>
    // tag can load it.
    let heroAnnotatedUrl: string | null = null;
    if (heroAnnotatedRaw) {
      const token = sessionStorage.getItem('nexus_token') || '';
      const sep = heroAnnotatedRaw.includes('?') ? '&' : '?';
      heroAnnotatedUrl = token
        ? `${heroAnnotatedRaw}${sep}token=${encodeURIComponent(token)}`
        : heroAnnotatedRaw;
    }

    // Total non-noise duration: min start_ms to max end_ms.
    const startMs = Math.min(
      ...nonNoiseScenes.map((s) => s.start_ms ?? 0),
    );
    const endMs = Math.max(
      ...nonNoiseScenes.map((s) => s.end_ms ?? 0),
    );
    const totalDurationMs = Math.max(0, endMs - startMs);

    // Visible app groupings — keep any grouping that has at least one
    // non-noise scene mapped to it.  This silently hides the "Web
    // Application — New Tab" grouping when all its scenes are noise.
    const visibleApps = (graph.app_groupings ?? []).filter((g) => {
      if (!graph.scene_to_app_grouping) return true;
      for (const [sid, gid] of Object.entries(graph.scene_to_app_grouping)) {
        if (gid === g.grouping_id && !noiseSceneIds.has(sid)) return true;
      }
      return false;
    });

    // Scene quality counts (non-noise only).
    let strong = 0;
    let degraded = 0;
    let weak = 0;
    for (const s of nonNoiseScenes) {
      const q = (s.scene_quality ?? 'weak').toLowerCase();
      if (q === 'strong') strong += 1;
      else if (q === 'degraded') degraded += 1;
      else weak += 1;
    }

    return {
      heroScene,
      heroAnnotatedUrl,
      heroCaption,
      totalDurationMs,
      visibleApps,
      strong,
      degraded,
      weak,
      noiseCount: noiseSceneIds.size,
      nonNoiseSceneCount: nonNoiseScenes.length,
    };
  }, [graph, noiseSceneIds]);

  // Set of flow_ids surfaced by the consolidation pass — used to drop
  // dropped/blip flows from the rail too, so the body and the tab list
  // agree on which flows are real.
  const consolidatedFlowIdSet = useMemo<Set<string>>(() => {
    const s = new Set<string>();
    for (const cf of consolidatedFlows) {
      for (const fid of cf.member_flow_ids) s.add(fid);
    }
    return s;
  }, [consolidatedFlows]);

  const flowGroups = useMemo(() => {
    if (!graph) return [];
    const flows = graph.flows ?? [];
    const groups: Array<{ flow: VisualFlow; scenes: VisualScene[]; edges: VisualFlowEdge[] }> = [];
    const sceneIsVisible = (s: VisualScene) =>
      showNoiseScenes || !noiseSceneIds.has(s.scene_id);
    for (const flow of flows) {
      if (activeMemberFlowIds && !activeMemberFlowIds.has(flow.flow_id)) continue;
      // When no specific tab is active, restrict to flows the
      // consolidation kept — keeps the rail and the tab bar in sync.
      if (!activeMemberFlowIds && !consolidatedFlowIdSet.has(flow.flow_id)) continue;
      const scenes = graph.scenes
        .filter((s) => s.flow_id === flow.flow_id && sceneIsVisible(s))
        .sort((a, b) => a.scene_index - b.scene_index);
      const sceneIds = new Set(scenes.map((s) => s.scene_id));
      const edges = graph.edges.filter((e) => sceneIds.has(e.from_scene_id) && sceneIds.has(e.to_scene_id));
      if (scenes.length > 0) groups.push({ flow, scenes, edges });
    }
    if (!activeFlowId) {
      const flowSceneIds = new Set(flows.flatMap((f) => graph.scenes.filter((s) => s.flow_id === f.flow_id).map((s) => s.scene_id)));
      const unassigned = graph.scenes
        .filter((s) => !flowSceneIds.has(s.scene_id) && sceneIsVisible(s));
      if (unassigned.length > 0) {
        groups.push({
          flow: { flow_id: '__unassigned__', flow_label: 'Other Screens', scene_count: unassigned.length, is_noise: true } as VisualFlow,
          scenes: unassigned.sort((a, b) => a.scene_index - b.scene_index),
          edges: [],
        });
      }
    }
    return groups;
  }, [graph, activeFlowId, activeMemberFlowIds, consolidatedFlowIdSet, noiseSceneIds, showNoiseScenes]);

  const gateResult = useMemo(() => {
    if (!graph) return { can_generate: false, reasons: [] as string[], action_confirmed_count: 0, automation_ready_count: 0 };
    const ac = graph.summary.action_confirmed_edges;
    const ar = graph.summary.automation_ready_controls;
    const reasons: string[] = [];
    if (ac === 0) reasons.push('No action-confirmed transitions');
    if (ar === 0) reasons.push('No automation-ready controls');
    return { can_generate: ac > 0 && ar > 0, reasons, action_confirmed_count: ac, automation_ready_count: ar };
  }, [graph]);

  const selectedNextScene = useMemo(() => {
    if (!selectedSceneId || !graph) return null;
    const edge = edgeFromMap.get(selectedSceneId);
    if (!edge) return null;
    return graph.scenes.find((s) => s.scene_id === edge.to_scene_id) ?? null;
  }, [selectedSceneId, edgeFromMap, graph]);

  // Note: these selected-scene hooks must live above the early returns
  // (loading / error / empty) so the hook order stays stable across renders.
  const selectedScene = useMemo<VisualScene | null>(() => {
    if (!selectedSceneId || !graph) return null;
    return graph.scenes.find((s) => s.scene_id === selectedSceneId) ?? null;
  }, [selectedSceneId, graph]);

  const selectedControls = useMemo<EvidenceControl[]>(() => {
    if (!selectedSceneId) return [];
    return controlsByScene.get(selectedSceneId) ?? [];
  }, [selectedSceneId, controlsByScene]);

  const selectedIncomingEdge = useMemo<VisualFlowEdge | undefined>(() => {
    if (!selectedSceneId) return undefined;
    return edgeToMap.get(selectedSceneId);
  }, [selectedSceneId, edgeToMap]);

  const selectedPrevScene = useMemo<VisualScene | null>(() => {
    if (!selectedScene || !graph) return null;
    const idx = (selectedScene.scene_index ?? -1);
    if (idx <= 0) return null;
    return graph.scenes.find((s) => s.scene_index === idx - 1) ?? null;
  }, [selectedScene, graph]);

  const selectedActionLabel = useMemo(() => {
    if (!selectedScene) return null;
    // Phase 1.7 — prefer the LLM short caption when available, same
    // policy used by the flow-rail cards.  Falls back to the
    // heuristic for legacy artifacts.
    const llmCaption = captionByScene.get(selectedScene.scene_id);
    const heuristic = deriveActionLabel(
      selectedScene,
      selectedControls,
      selectedPrevScene,
      selectedIncomingEdge?.edge_type,
      selectedIncomingEdge,
    );
    return llmCaption
      ? { action: llmCaption, icon: heuristic.icon, quality: 'strong' as const }
      : heuristic;
  }, [selectedScene, selectedControls, selectedPrevScene, selectedIncomingEdge, captionByScene]);

  // Phase 1.7 defect-fix — unified scene actions derivation.
  //
  // The frozen pipeline writes action data into THREE different
  // tables/columns; the original "Page actions & form fields"
  // section only read from `evidence_controls`, missing 80% of the
  // action signal.  This memo combines all three:
  //
  //   1. scene_state_summary.frame_actions  — per-frame action
  //      timeline with action_kind + target_label + timestamps.
  //      Pipeline detects: open_overlay, state_change, click,
  //      enter_text, select_option, navigate, etc.
  //   2. evidence_controls                  — detected UI elements
  //      (buttons, dropdowns, text fields) with playwright_selector
  //      + automation_ready flag.
  //   3. visual_flow_edges.primary_action_summary
  //                                          — the dominant action
  //      that led TO this scene (action_label + action_value).
  //
  // The result is rendered as a chronological timeline of user
  // actions PLUS a separate list of detected form controls.  Low-
  // confidence noise (empty target_label + confidence < 0.5) is
  // filtered out so the timeline stays readable.
  type SceneActionRow = {
    kind: 'frame_action' | 'control';
    timestamp_s: number | null;
    verb: string;
    target: string;
    type_label: string;
    confidence: number;
    automation_ready?: boolean;
    evidence_signals?: string[];
    value?: string;
    control_id?: string;
  };

  const sceneActions = useMemo(() => {
    if (!selectedScene) {
      return {
        userActions: [] as SceneActionRow[],
        weakActions: [] as SceneActionRow[],
        controls: [] as SceneActionRow[],
        incomingEdgeLabel: '',
        incomingEdgeValue: '',
      };
    }

    const verbMap: Record<string, string> = {
      open_overlay: 'Opened',
      close_overlay: 'Closed',
      state_change: 'Changed',
      click: 'Clicked',
      click_cta: 'Clicked',
      enter_text: 'Entered',
      select_option: 'Selected',
      submit_form: 'Submitted',
      navigate: 'Navigated to',
      review: 'Reviewed',
      scroll: 'Scrolled',
      scroll_or_repaint: 'Page updated',
      user_interaction: 'User interaction',
    };

    // Loose OCR-garbage filter.  Only drops the most egregious cases
    // so the user sees the full action stream:
    //   - 50+ char unbroken alphanumeric strings (the
    //     "suymiuyqyuymnntbztlci..." 78-char monsters)
    //   - targets with no letters at all
    // EVERYTHING ELSE is kept — even short OCR-corrupted words like
    // "informatlon" or "ivuquardianlife" — because the user evaluates
    // them in context and a partially-corrupted label is still
    // useful evidence for building a test case.
    const isLikelyGarbage = (text: string): boolean => {
      if (!text) return true;
      const trimmed = text.trim();
      if (!trimmed) return true;
      // Pure-symbol target with no letters.
      if (!/[a-zA-Z]/.test(trimmed)) return true;
      // Catastrophically long string (50+ chars unbroken alphanumeric).
      // This catches the 78-char OCR-monsters while leaving
      // legitimate domain-looking targets ("annualincome",
      // "screenwhereyoucan") alone.
      if (/[a-z0-9]{50,}/i.test(trimmed)) return true;
      return false;
    };

    // Phase 1.7 iteration 5 — single bucket, no confidence
    // threshold gate.  The user wants to SEE every action the
    // pipeline detected (selecting, dropdown, click, etc.) and
    // judge quality themselves via the inline "weak" badge.
    // Hiding low-confidence actions removed too much useful signal.
    const userActions: SceneActionRow[] = [];
    const weakActions: SceneActionRow[] = []; // kept for type compat, unused
    const frameActions = selectedScene.scene_state_summary?.frame_actions || [];
    for (const fa of frameActions) {
      const hasTarget = (fa.target_label || '').trim().length > 0;
      const hasDelta = (fa.delta_text || '').trim().length > 0;

      // Drop ONLY when there is literally no signal — empty target,
      // empty delta, AND very low confidence.  Otherwise show it.
      if (!hasTarget && !hasDelta && fa.confidence < 0.3) continue;

      const rawTarget = fa.target_label || (fa.delta_text || '').split(/\s+/).slice(0, 5).join(' ');
      if (isLikelyGarbage(rawTarget)) continue;

      const target = rawTarget.length > 40 ? rawTarget.slice(0, 40) + '…' : rawTarget;

      const verb = verbMap[fa.action_kind] || fa.action_kind;
      userActions.push({
        kind: 'frame_action',
        timestamp_s: fa.from_timestamp_s,
        verb,
        target,
        type_label: fa.action_kind.replace(/_/g, ' '),
        confidence: fa.confidence,
        evidence_signals: fa.evidence_signals,
      });
    }
    userActions.sort((a, b) => (a.timestamp_s ?? 0) - (b.timestamp_s ?? 0));

    // Evidence controls — detected UI elements (separate from
    // user_actions because controls are static "what's on the page"
    // signals, not "what the user did").
    const controls: SceneActionRow[] = [];
    const order = (c: EvidenceControl): number => {
      const t = (c.element_type || '').toLowerCase();
      const k = (c.action_kind || '').toLowerCase();
      if (k === 'enter_text' || t === 'input' || t === 'textarea' || t === 'text_field') return 0;
      if (t === 'select' || t === 'dropdown' || k === 'select_option') return 1;
      if (t === 'radio') return 2;
      if (t === 'checkbox' || k === 'check' || k === 'toggle') return 3;
      if (t === 'button' || k === 'click_cta' || k === 'submit_form') return 4;
      if (t === 'link') return 5;
      return 6;
    };
    const sortedControls = [...selectedControls].sort((a, b) => order(a) - order(b));
    for (const c of sortedControls) {
      const t = (c.element_type || '').toLowerCase();
      const k = (c.action_kind || '').toLowerCase();
      const typeLabel =
        k === 'enter_text' ? 'text field'
        : (t === 'select' || t === 'dropdown') ? 'dropdown'
        : t === 'radio' ? 'radio'
        : t === 'checkbox' ? 'checkbox'
        : t === 'button' ? 'button'
        : t === 'link' ? 'link'
        : t || 'control';
      const verb =
        k === 'enter_text' ? 'Entered'
        : k === 'select_option' ? 'Selected'
        : k === 'toggle' || k === 'check' ? 'Toggled'
        : k === 'submit_form' ? 'Submitted'
        : k === 'click_cta' ? 'Clicked'
        : '';
      const value =
        (c as unknown as { observed_value?: string }).observed_value
        || (c as unknown as { default_value?: string }).default_value
        || (c as unknown as { placeholder_text?: string }).placeholder_text
        || '';
      controls.push({
        kind: 'control',
        timestamp_s: null,
        verb,
        target: (c.display_label || c.label_text || '').trim() || '(unlabeled)',
        type_label: typeLabel,
        confidence: c.selector_confidence || 0,
        automation_ready: c.automation_ready,
        value: value || undefined,
        control_id: c.control_id,
      });
    }

    // Incoming-edge dominant action — what brought the user TO this
    // scene.  Surfaced as a header line above the timeline.
    const edgeSummary = selectedIncomingEdge?.primary_action_summary;
    const incomingEdgeLabel = (edgeSummary?.action_label || '').trim();
    const incomingEdgeValue = ((edgeSummary as unknown as { action_value?: string })?.action_value || '').trim();

    return {
      userActions,
      weakActions,
      controls,
      incomingEdgeLabel,
      incomingEdgeValue,
    };
  }, [selectedScene, selectedControls, selectedIncomingEdge]);

  // Phase 1.7 — toggle for weak-confidence frame_actions.  Default
  // hidden so the timeline shows only high-signal actions.
  const [showWeakActions, setShowWeakActions] = useState(false);

  // Phase 1.7 iteration 6 — per-frame evidence for the selected scene.
  //
  // Audit found scene_state_summary.frame_actions is largely OCR-noise
  // (target_label values like "ivuquardianlife", "screenwhereyoucan"
  // are mangled OCR strings, not real field names).  The real signal
  // lives in visual_frames.ui_elements_json: an LLM-derived list of
  // detected buttons / text_fields / dropdowns per frame, each with a
  // confidence score, an action_kind ("click" / "enter_text" /
  // "select_option") and an observed_value.  This memo:
  //
  //   1. Picks all frames belonging to the selected scene (filtered
  //      from allFrames by scene_id).
  //   2. Flattens ui_elements_json across those frames, dedupes by
  //      element text + element_type, and keeps the highest-confidence
  //      occurrence — so the user sees "Sign in (button)" once even
  //      when it appears on 9 consecutive frames.
  //   3. Detects URL deltas (when url_or_path changes between frames)
  //      so even scenes with no UI elements show navigation evidence.
  //   4. Computes a `data_quality` verdict the banner reads to decide
  //      whether to warn the user about pipeline gaps.
  type DetectedElement = {
    element_type: string;
    text: string;
    confidence: number;
    action_kind: string;
    observed_value: string;
    frame_index: number;
    persistence_count: number;
  };
  type FrameOcrRow = {
    frame_index: number;
    timestamp_s: number;
    is_keyframe: boolean;
    ocr_preview: string;
    url: string;
  };
  type UrlChange = {
    frame_index: number;
    from_url: string;
    to_url: string;
    timestamp_s: number;
  };

  const sceneFrameEvidence = useMemo(() => {
    if (!selectedScene || allFrames.length === 0) {
      return {
        elements: [] as DetectedElement[],
        urlChanges: [] as UrlChange[],
        frameRows: [] as FrameOcrRow[],
        dataQuality: 'no_scene' as const,
      };
    }

    const sceneFrames = allFrames
      .filter((f) => f.scene_id === selectedScene.scene_id)
      .sort((a, b) => a.frame_index - b.frame_index);

    if (sceneFrames.length === 0) {
      return {
        elements: [] as DetectedElement[],
        urlChanges: [] as UrlChange[],
        frameRows: [] as FrameOcrRow[],
        dataQuality: 'no_frames' as const,
      };
    }

    // Aggregate ui_elements_json across all frames in the scene.  Dedupe
    // by (element_type, normalized text) keeping the highest-confidence
    // occurrence so the user sees "Sign in" once, not 9 times.
    const elemKey = (et: string, text: string) =>
      `${et}::${text.trim().toLowerCase()}`;
    const elementMap = new Map<string, DetectedElement>();

    for (const frame of sceneFrames) {
      const uiList = frame.ui_elements_json || [];
      if (!Array.isArray(uiList)) continue;
      for (const elem of uiList) {
        const e = elem as Record<string, unknown>;
        const et = (e.element_type as string | undefined) || '';
        const text = ((e.text as string | undefined) || '').trim();
        if (!et || !text) continue;
        const conf = (e.confidence as number | undefined) ?? 0;
        const props = (e.properties as Record<string, unknown> | undefined) || {};
        const actionKind = (props.action_kind as string | undefined) || '';
        const observedValue = (props.observed_value as string | undefined) || '';
        const persistence = (props.persistence_count as number | undefined) ?? 1;

        const k = elemKey(et, text);
        const existing = elementMap.get(k);
        if (!existing || conf > existing.confidence) {
          elementMap.set(k, {
            element_type: et,
            text,
            confidence: conf,
            action_kind: actionKind,
            observed_value: observedValue,
            frame_index: frame.frame_index,
            persistence_count: persistence,
          });
        }
      }
    }
    const elements = Array.from(elementMap.values()).sort((a, b) => {
      // Order: text_field → dropdown → radio → checkbox → button → link
      const rank = (et: string) => {
        const t = et.toLowerCase();
        if (t === 'text_field' || t === 'input' || t === 'textarea') return 0;
        if (t === 'select' || t === 'dropdown') return 1;
        if (t === 'radio') return 2;
        if (t === 'checkbox') return 3;
        if (t === 'button') return 4;
        if (t === 'link') return 5;
        return 6;
      };
      const r = rank(a.element_type) - rank(b.element_type);
      if (r !== 0) return r;
      return b.confidence - a.confidence;
    });

    // URL deltas — surface every change in url_or_path between adjacent
    // frames.  This is the cleanest navigation signal we have on
    // scenes where ui_elements_json is empty (the form-fill scenes 8-11
    // in the audited artifact).
    const urlChanges: UrlChange[] = [];
    let prevUrl = '';
    for (const frame of sceneFrames) {
      const u = (frame.url_or_path || '').trim();
      if (!u) continue;
      if (u !== prevUrl && prevUrl) {
        urlChanges.push({
          frame_index: frame.frame_index,
          from_url: prevUrl,
          to_url: u,
          timestamp_s: frame.timestamp_seconds,
        });
      }
      if (u) prevUrl = u;
    }

    // Per-frame OCR rows for the filmstrip.  We show one row per
    // frame in the scene with: timestamp, keyframe flag, first 120
    // chars of extracted_text, and the url_or_path.  This is the
    // last-resort signal when no structured UI/control data exists.
    const frameRows: FrameOcrRow[] = sceneFrames.map((f) => ({
      frame_index: f.frame_index,
      timestamp_s: f.timestamp_seconds,
      is_keyframe: f.is_keyframe,
      ocr_preview: ((f.extracted_text || '').trim()).slice(0, 200),
      url: (f.url_or_path || '').trim(),
    }));

    // Data quality verdict — drives the banner.
    //   strong   : ui_elements present, evidence_controls present
    //   degraded : url changes OR a few elements, but no controls
    //   weak     : nothing structured, only OCR text
    let dataQuality: 'strong' | 'degraded' | 'weak' | 'no_scene' | 'no_frames';
    if (elements.length >= 3 && selectedControls.length >= 2) {
      dataQuality = 'strong';
    } else if (elements.length > 0 || urlChanges.length > 0 || selectedControls.length > 0) {
      dataQuality = 'degraded';
    } else {
      dataQuality = 'weak';
    }

    return { elements, urlChanges, frameRows, dataQuality };
  }, [selectedScene, allFrames, selectedControls]);

  /** Phase D.1 — triangulated steps filtered to the selected scene.
   *  When no scene is selected, the panel shows all steps in chronological
   *  order so reviewers can scan the whole artifact at once. */
  const stepsForPanel = useMemo<EvidenceStep[]>(() => {
    if (!selectedSceneId) return evidenceSteps;
    return evidenceSteps.filter((s) => s.scene_id === selectedSceneId);
  }, [evidenceSteps, selectedSceneId]);

  /** Frame_id → asset URL helper used by the EvidenceStepsPanel to render
   *  before/after thumbnails per step.  Combines the representative-frame
   *  paths from the graph (immediately available) with the full per-frame
   *  list from /api/v1/artifacts/{id}/frames (loaded asynchronously) so
   *  every before_frame_id / after_frame_id resolves to a real image once
   *  the second request completes. */
  const frameAssetById = useMemo(() => {
    const m = new Map<string, string | null>();
    if (graph) {
      for (const s of graph.scenes || []) {
        if (s.representative_frame_id && s.representative_frame_asset_path) {
          m.set(s.representative_frame_id, s.representative_frame_asset_path);
        }
      }
    }
    for (const f of allFrames) {
      if (f.frame_id && f.frame_asset_path) {
        m.set(f.frame_id, f.frame_asset_path);
      }
    }
    return m;
  }, [graph, allFrames]);

  const thumbnailFor = useCallback(
    (frameId: string | null): string | null => {
      if (!frameId) return null;
      const asset = frameAssetById.get(frameId);
      if (!asset) return null;
      return api.getFrameImageUrl ? api.getFrameImageUrl(asset) : null;
    },
    [frameAssetById],
  );

  /** Phase D.2 — cursor_event lookup keyed by frame_id, used to draw the
   *  click marker on the before/after thumbnail of the selected step. */
  const cursorByFrameId = useMemo(() => {
    const m = new Map<string, CursorEvent>();
    for (const e of cursorEvents) {
      if (e.frame_id) m.set(e.frame_id, e);
    }
    return m;
  }, [cursorEvents]);

  /** Phase 1.7 iter 6 — synthesized cursor_events for the selected scene.
   *
   * Many SME recordings don't show the mouse cursor, so the motion-based
   * CursorTracker produces zero real events.  The spine engine's
   * /synthesize-clicks endpoint runs an additive synthesizer that emits
   * events with detection_method ∈ { url_delta, ocr_new_block,
   * ocr_removed_block, ui_element_change, control_value,
   * frame_action_promotion }.
   *
   * This memo:
   *   1. Builds a frame_id → scene_id lookup from allFrames
   *   2. Filters cursor_events to those belonging to the selected scene
   *      AND whose detection_method != 'motion' (synthesized only)
   *   3. Sorts chronologically + dedupes per frame
   *
   * Output rows feed the new "Inferred user actions" detail-panel section.
   */
  const sceneSyntheticClicks = useMemo(() => {
    if (!selectedScene || cursorEvents.length === 0) return [];
    const frameToScene = new Map<string, string | null>();
    for (const f of allFrames) {
      if (f.frame_id) frameToScene.set(f.frame_id, f.scene_id ?? null);
    }
    const seenFrameIdx = new Set<number>();
    const filtered = cursorEvents
      .filter((e) =>
        e.detection_method !== 'motion'
        && e.frame_id
        && frameToScene.get(e.frame_id) === selectedScene.scene_id,
      )
      .sort((a, b) => a.timestamp_ms - b.timestamp_ms);

    const out: Array<{
      method: string;
      methodLabel: string;
      methodColor: string;
      target: string;
      observedValue: string;
      actionVerb: string;
      timestamp_s: number;
      confidence: number;
      corroborating: number;
      isClick: boolean;
    }> = [];
    // Allow multiple events per frame when target_labels differ
    // (e.g. Gender=Male AND Smoker=Yes captured on the same frame).
    // Dedup key is (frame_index, lowercased target_label).
    const seenKey = new Set<string>();
    for (const e of filtered) {
      const meta = e.metadata_json || {};
      const target = String(
        (meta.target_label as string | undefined)
          || (meta.element_text as string | undefined)
          || (meta.control_label as string | undefined)
          || (meta.delta_text as string | undefined)
          || (meta.to_url as string | undefined)
          || 'user interaction'
      );
      const dedupKey = `${e.frame_index}::${target.toLowerCase()}`;
      if (seenKey.has(dedupKey)) continue;
      seenKey.add(dedupKey);
      const observedValue = String(
        (meta.observed_value as string | undefined)
          || (meta.control_value as string | undefined)
          || ''
      );
      const actionKind = String((meta.action_kind as string | undefined) || '');
      const actionVerb =
        actionKind === 'enter_text' ? 'Entered'
        : actionKind === 'select_option' ? 'Selected'
        : actionKind === 'check' || actionKind === 'toggle' ? 'Toggled'
        : actionKind === 'submit_form' ? 'Submitted'
        : e.is_click ? 'Clicked'
        : 'Interacted';
      const corr = Array.isArray(meta.corroborating_methods)
        ? (meta.corroborating_methods as string[]).length
        : 0;
      const methodMap: Record<string, { label: string; color: string }> = {
        url_delta: { label: 'URL navigation', color: '#6366f1' },
        ocr_new_block: { label: 'New UI text', color: '#10b981' },
        ocr_removed_block: { label: 'UI dismissed', color: '#f59e0b' },
        ui_element_change: { label: 'Button gone', color: '#0369a1' },
        control_value: { label: 'Form value', color: '#047857' },
        frame_action_promotion: { label: 'Click detected', color: '#7c3aed' },
        form_value_inferred: { label: 'Form field entered', color: '#047857' },
        // Strategy 7 Case A: domain-free placeholder→value first fill.
        placeholder_transition: { label: 'Field filled', color: '#0f766e' },
        // Strategy 7 Case B: re-edit (typed John then changed to Mary).
        field_re_edit: { label: 'Field re-edited', color: '#be185d' },
        // Strategy 7 Case C / Strategy 8: punctuation-only label.
        labelled_value_change: { label: 'Field value', color: '#15803d' },
        // Strategy 11: captcha / masked / blurred — value unreadable.
        field_interaction_unreadable: { label: 'Field touched (value hidden)', color: '#dc2626' },
        // Strategy 9: SME narrated this action; OCR didn't catch it.
        audio_intent: { label: 'Narrated action', color: '#0891b2' },
        // Strategy 10: scroll / viewport change.
        viewport_shift: { label: 'Scrolled', color: '#737373' },
      };
      const m = methodMap[e.detection_method as string] || { label: e.detection_method as string, color: '#64748b' };
      out.push({
        method: e.detection_method as string,
        methodLabel: m.label,
        methodColor: m.color,
        target: target.length > 60 ? target.slice(0, 60) + '…' : target,
        observedValue: observedValue.length > 80 ? observedValue.slice(0, 80) + '…' : observedValue,
        actionVerb,
        timestamp_s: e.timestamp_ms / 1000,
        confidence: e.confidence,
        corroborating: corr,
        isClick: e.is_click,
      });
    }
    return out;
  }, [selectedScene, cursorEvents, allFrames]);

  /** Phase D.2 — step replay handler.  When a step is selected:
   *    1. update selectedStepId so the panel highlights the row
   *    2. set selectedSceneId so the flow card scrolls into view
   *    3. set activeCursorMark to the cursor coordinates for the after-frame
   *       so the overlay can draw a click marker on the thumbnail
   */
  const handleStepSelect = useCallback((step: EvidenceStep) => {
    setSelectedStepId(step.step_id);
    setSelectedSceneId(step.scene_id);
    const targetFrameId = step.after_frame_id || step.before_frame_id;
    const cursorEvent = targetFrameId ? cursorByFrameId.get(targetFrameId) : null;
    if (cursorEvent) {
      setActiveCursorMark({
        x: cursorEvent.cursor_x,
        y: cursorEvent.cursor_y,
        frameId: targetFrameId,
        timestampMs: cursorEvent.timestamp_ms,
      });
    } else if (step.cursor_x != null && step.cursor_y != null) {
      // Step persisted cursor coords but the standalone event row is
      // unavailable (e.g. retention pruned cursor_events).  Still useful
      // for the overlay marker.
      setActiveCursorMark({
        x: step.cursor_x,
        y: step.cursor_y,
        frameId: targetFrameId,
        timestampMs: step.start_ms,
      });
    } else {
      setActiveCursorMark(null);
    }
  }, [cursorByFrameId]);

  const handleGenerate = useCallback(() => {
    if (!gateResult.can_generate || !sessionId) return;
    navigate(`/sessions/${sessionId}/e2e-architect?artifact_id=${artifactId}&evidence_mode=visual_strict`);
  }, [gateResult, sessionId, artifactId, navigate]);

  /* ── Loading ── */
  if (loading) {
    return (
      <div className="flex h-screen items-center justify-center" style={{ background: 'linear-gradient(180deg, #f5f7fa 0%, #eef2f7 100%)' }}>
        <div className="flex flex-col items-center gap-6 relative z-10">
          <div className="relative w-20 h-20" >
            <div className="absolute inset-0 rounded-full border-2 border-indigo-500/30 v5-spin-3d" />
            <div className="absolute inset-2 rounded-full border-2 border-purple-400/40 v5-spin-3d-reverse" />
            <div className="absolute inset-4 rounded-full border-2 border-blue-400/30 v5-spin-3d" style={{ animationDuration: '2s' }} />
            <div className="absolute inset-0 flex items-center justify-center">
              <Sparkles className="h-6 w-6 text-slate-600 v5-pulse-glow" />
            </div>
          </div>
          <div className="text-center space-y-2">
            <p className="text-base text-slate-900 font-bold tracking-wide">Analyzing User Journey</p>
            <p className="text-xs text-slate-500">Building 3D flow visualization\u2026</p>
          </div>
        </div>
      </div>
    );
  }

  if (error || !graph) {
    return (
      <div className="flex h-screen flex-col items-center justify-center gap-5" style={{ background: 'linear-gradient(180deg, #f5f7fa, #eef2f7)' }}>
        <div className="relative z-10 text-center space-y-4">
          <div className="w-16 h-16 rounded-2xl mx-auto flex items-center justify-center" style={{ background: 'rgba(239,68,68,0.15)', border: '1px solid rgba(239,68,68,0.3)' }}>
            <AlertTriangle className="h-8 w-8 text-red-400" />
          </div>
          <p className="text-sm font-medium text-slate-700">{error ?? 'Failed to load visual flow graph'}</p>
          <Link to={`/sessions/${sessionId}`} className="inline-flex items-center gap-2 text-xs text-slate-600 hover:text-indigo-300 transition-colors bg-indigo-500/10 px-4 py-2 rounded-xl border border-indigo-500/20">
            <ArrowLeft className="h-3.5 w-3.5" /> Back to Session
          </Link>
        </div>
      </div>
    );
  }

  // ── Empty state: API succeeded but pipeline produced no visual scenes ──
  // This happens when the Eyes+Spine pipeline skipped or hasn't run yet for
  // this artifact (e.g. RLS bug, no video uploaded, or processing in progress).
  if (graph.scenes.length === 0) {
    return (
      <div className="flex h-screen flex-col items-center justify-center gap-5" style={{ background: 'linear-gradient(180deg, #f5f7fa, #eef2f7)' }}>
        <div className="relative z-10 text-center space-y-5 max-w-sm px-4">
          <div className="w-16 h-16 rounded-2xl mx-auto flex items-center justify-center" style={{ background: 'rgba(38,112,163,0.12)', border: '1px solid rgba(38,112,163,0.25)' }}>
            <MonitorPlay className="h-8 w-8" style={{ color: '#2670a3' }} />
          </div>
          <div className="space-y-2">
            <h2 className="text-base font-black text-slate-900">No Visual Scenes Found</h2>
            <p className="text-xs text-slate-500 leading-relaxed">
              The visual evidence pipeline did not produce any screen-flow data for
              this artifact. This can happen when the video was uploaded but visual
              processing was skipped or is still in progress.
            </p>
          </div>
          <div className="rounded-xl p-3 text-left space-y-1.5" style={{ background: 'rgba(38,112,163,0.06)', border: '1px solid rgba(38,112,163,0.14)' }}>
            <p className="text-[10px] font-black uppercase tracking-widest" style={{ color: '#2670a3' }}>What to try</p>
            <ul className="text-[11px] text-slate-600 space-y-0.5 list-disc list-inside">
              <li>Re-upload the video to re-trigger processing</li>
              <li>Ensure the video file contains screen recordings</li>
              <li>Check that all services (Eyes, Spine) are healthy</li>
            </ul>
          </div>
          <Link to={`/sessions/${sessionId}`} className="inline-flex items-center gap-2 text-xs font-bold transition-all hover:scale-105" style={{ color: '#2670a3' }}>
            <ArrowLeft className="h-3.5 w-3.5" /> Back to Session
          </Link>
        </div>
      </div>
    );
  }

  const avgConfidence = graph.scenes.length > 0
    ? Math.round(graph.scenes.reduce((sum, s) => sum + s.completeness_confidence, 0) / graph.scenes.length * 100)
    : 0;
  // For multi-flow sessions use all scenes so duration and totals span the whole session
  const primaryScenes = flowGroups.length > 1 ? graph.scenes : (flowGroups.length > 0 ? flowGroups[0].scenes : graph.scenes);
  const journeyNarrative = buildJourneyNarrative(primaryScenes, controlsByScene, graph.flows ?? [], graph.edges);

  return (
    <div className="flex h-screen flex-col overflow-hidden" style={{ background: 'linear-gradient(180deg, #f5f7fa 0%, #eef2f7 100%)' }}>

      {/* ─────────────────── HEADER ─────────────────── */}
      <header className="shrink-0 relative z-10" style={{
        borderBottom: '1px solid rgba(38,112,163,0.15)',
        background: 'linear-gradient(180deg, #ffffff 0%, #f8fafc 100%)',
        backdropFilter: 'blur(20px)',
      }}>
        <div className="flex items-center gap-3 px-4 py-2.5">
          <Link to={`/sessions/${sessionId}`}
            className="p-2 rounded-lg transition-all text-slate-600 hover:text-slate-900 hover:scale-105"
            style={{ background: 'rgba(10,37,64,0.04)', border: '1px solid rgba(38,112,163,0.12)' }}>
            <ArrowLeft className="h-3.5 w-3.5" />
          </Link>

          <div className="flex-1 min-w-0">
            <h1 className="text-sm font-black text-slate-900 flex items-center gap-2 tracking-tight">
              <div className="w-7 h-7 rounded-lg flex items-center justify-center relative"
                style={{ background: 'linear-gradient(135deg, #0a2540, #2670a3)', boxShadow: '0 0 20px rgba(38,112,163,0.25)' }}>
                <GitBranch className="h-3.5 w-3.5 text-white" />
              </div>
              <span style={{ background: 'linear-gradient(135deg, #0a2540, #2670a3)', WebkitBackgroundClip: 'text', WebkitTextFillColor: 'transparent' }}>
                3D Journey Map
              </span>
              <span className="text-[8px] font-bold px-1.5 py-0.5 rounded ml-0.5"
                style={{ background: 'linear-gradient(135deg, rgba(38,112,163,0.15), rgba(38,112,163,0.1))', color: '#2670a3', border: '1px solid rgba(38,112,163,0.2)' }}>
                LIVE
              </span>
            </h1>
          </div>

          <button
            disabled={!gateResult.can_generate}
            onClick={handleGenerate}
            className={clsx(
              'flex items-center gap-2 rounded-lg px-4 py-2 text-xs font-black transition-all duration-300',
              gateResult.can_generate ? 'text-white hover:scale-105 active:scale-95' : 'text-slate-500 cursor-not-allowed',
            )}
            style={gateResult.can_generate ? {
              background: 'linear-gradient(135deg, #0a2540, #2670a3)',
              boxShadow: '0 8px 32px rgba(38,112,163,0.3), 0 0 0 1px rgba(38,112,163,0.25), inset 0 1px 0 rgba(255,255,255,0.1)',
            } : {
              background: 'rgba(10,37,64,0.04)',
              border: '1px solid rgba(10,37,64,0.08)',
            }}
            title={gateResult.reasons.join('; ')}
          >
            <Shield className="h-3.5 w-3.5" /> Generate E2E
          </button>
        </div>

        {/* Stats row */}
        <div className="px-4 pb-2 space-y-2">
          <div className="flex items-center gap-2 overflow-x-auto pb-0.5">
            <StatCard3D icon={MonitorPlay} label="Screens" value={graph.summary.total_scenes} accent="#818cf8" delay={0} />
            <StatCard3D icon={Activity} label="Transitions" value={graph.summary.total_edges} accent="#38bdf8" delay={80} />
            <StatCard3D icon={CheckCircle} label="Confirmed" value={gateResult.action_confirmed_count} accent="#34d399" delay={160} />
            <StatCard3D icon={Zap} label="Selectors" value={gateResult.automation_ready_count} accent="#fbbf24" delay={240} />
            <StatCard3D icon={Target} label="Quality" value={`${avgConfidence}%`} accent="#f472b6" delay={320} />
            <StatCard3D icon={Box} label="Apps" value={graph.app_instances.length} accent="#22d3ee" delay={400} />
          </div>

          {journeyNarrative && (
            <div className="rounded-lg px-3 py-1.5 text-[10px] text-slate-700 leading-snug v5-card-enter"
              style={{
                animationDelay: '500ms',
                background: 'linear-gradient(90deg, rgba(10,37,64,0.04), rgba(38,112,163,0.1), rgba(38,112,163,0.08), rgba(10,37,64,0.04))',
                border: '1px solid rgba(38,112,163,0.12)',
              }}>
              <span className="text-slate-600 font-black mr-1">{'\u2726'}</span>
              {journeyNarrative}
            </div>
          )}
        </div>
      </header>

      {/* ─────────── VIEW MODE TABS (Storyboard / 3D Journey / Pages) ─────── */}
      <div
        className="flex items-center gap-2 px-6 py-2 shrink-0 relative z-10"
        style={{
          borderBottom: '1px solid rgba(38,112,163,0.1)',
          background: 'linear-gradient(180deg, #ffffff, #f8fafc)',
        }}
      >
        <button
          onClick={() => setViewMode('storyboard')}
          className={clsx(
            'flex items-center gap-1.5 px-4 py-1.5 text-xs font-bold rounded-lg transition-all',
            viewMode === 'storyboard'
              ? 'text-white shadow'
              : 'text-slate-500 hover:text-slate-900',
          )}
          style={
            viewMode === 'storyboard'
              ? { background: 'linear-gradient(135deg, #0a2540, #2670a3)' }
              : { background: 'rgba(10,37,64,0.04)' }
          }
          aria-pressed={viewMode === 'storyboard'}
        >
          <Sparkles className="h-3.5 w-3.5" /> Storyboard
        </button>
        <button
          onClick={() => setViewMode('journey')}
          className={clsx(
            'flex items-center gap-1.5 px-4 py-1.5 text-xs font-bold rounded-lg transition-all',
            viewMode === 'journey'
              ? 'text-white shadow'
              : 'text-slate-500 hover:text-slate-900',
          )}
          style={
            viewMode === 'journey'
              ? { background: 'linear-gradient(135deg, #0a2540, #2670a3)' }
              : { background: 'rgba(10,37,64,0.04)' }
          }
          aria-pressed={viewMode === 'journey'}
        >
          <GitBranch className="h-3.5 w-3.5" /> 3D Journey
        </button>
        {/* Phase 2 — only show the Pages tab when URL-keyed data exists. */}
        {(graph.page_visits?.length ?? 0) > 0 && (
          <button
            onClick={() => setViewMode('pages')}
            className={clsx(
              'flex items-center gap-1.5 px-4 py-1.5 text-xs font-bold rounded-lg transition-all',
              viewMode === 'pages'
                ? 'text-white shadow'
                : 'text-slate-500 hover:text-slate-900',
            )}
            style={
              viewMode === 'pages'
                ? { background: 'linear-gradient(135deg, #0a2540, #2670a3)' }
                : { background: 'rgba(10,37,64,0.04)' }
            }
            aria-pressed={viewMode === 'pages'}
          >
            <Globe className="h-3.5 w-3.5" /> Pages &amp; Forms
          </button>
        )}
        <div className="flex-1" />
        {storyboard && viewMode === 'storyboard' ? (
          <span className="text-[10px] text-slate-500">
            {storyboard.summary?.non_noise_panel_count ?? storyboard.panels.length}{' '}
            panels &middot; {storyboard.summary?.app_count ?? storyboard.apps.length} apps
          </span>
        ) : null}
      </div>

      {viewMode === 'storyboard' && (
        <div className="flex-1 overflow-auto relative z-10">
          <div className="max-w-5xl mx-auto px-6 py-6">
            <StoryboardView
              payload={storyboard}
              loading={storyboardLoading}
              error={storyboardError}
              artifactId={artifactId ?? ''}
              layout="vertical"
              onPanelClick={(panel) => {
                // Switch to journey mode and focus the underlying scene
                if (panel.representative_frame_id) {
                  setSelectedSceneId(null);
                }
                setViewMode('journey');
              }}
            />
          </div>
        </div>
      )}

      {/* Phase 2 — Pages & Forms tab.  Owns the full content area (its
          own scroll) so it never compresses the 3D journey — this is
          the fix for the "click expands panel → journey squeezes"
          defect from the earlier inline placement. */}
      {viewMode === 'pages' && (
        <div className="flex-1 overflow-auto relative z-10">
          <div className="max-w-5xl mx-auto px-6 py-6">
            <PageVisitsPanel graph={graph} />
          </div>
        </div>
      )}

      {viewMode === 'journey' && (
        <>
      {/* ─────────── SESSION AT A GLANCE (Phase 1.7 Day 4) ─────────
          Hero + apps + quality breakdown at the top of the journey
          tab.  Gives the user a one-screen overview of a long demo
          before they scroll into the flow rail.
          Collapsible: auto-collapses when a scene is selected so the
          bottom detail panel has vertical room.  A click target on
          the header bar re-expands it.  This fixes the "screen
          squeezed" defect that happened when both the hero and the
          detail panel were vying for the same screen real estate. */}
      {sessionGlance && (
        <div
          className="px-6 py-3 shrink-0 relative z-10"
          style={{
            borderBottom: '1px solid rgba(38,112,163,0.08)',
            background: 'linear-gradient(180deg, #ffffff, #f8fafc)',
          }}
        >
          {/* Compact header bar — always visible.  Click to toggle. */}
          <button
            type="button"
            onClick={() => setGlanceCollapsed((v) => !v)}
            className="w-full flex items-center gap-3 text-left group"
            aria-expanded={!glanceCollapsed}
          >
            <ChevronRight
              className="h-4 w-4 text-slate-500 shrink-0 transition-transform"
              style={{
                transform: glanceCollapsed ? 'rotate(0deg)' : 'rotate(90deg)',
              }}
            />
            <div className="font-black text-slate-900 text-sm">
              Session at a Glance
            </div>
            <div className="text-[11px] text-slate-500 flex items-center gap-1">
              <span>{fmtDuration(0, sessionGlance.totalDurationMs)}</span>
              <span>·</span>
              <span>
                {sessionGlance.visibleApps.length} app
                {sessionGlance.visibleApps.length === 1 ? '' : 's'}
              </span>
              <span>·</span>
              <span>
                {sessionGlance.nonNoiseSceneCount} scene
                {sessionGlance.nonNoiseSceneCount === 1 ? '' : 's'}
              </span>
            </div>

            {/* Inline quality + app pills when collapsed — keeps the
                glance information density visible at all times. */}
            {glanceCollapsed && (
              <div className="ml-auto flex items-center gap-1.5 text-[10px] font-bold">
                <span className="text-emerald-700 bg-emerald-50 px-1.5 py-0.5 rounded border border-emerald-200">
                  {sessionGlance.strong} STRONG
                </span>
                {sessionGlance.degraded > 0 && (
                  <span className="text-amber-700 bg-amber-50 px-1.5 py-0.5 rounded border border-amber-200">
                    {sessionGlance.degraded} DEG
                  </span>
                )}
                {sessionGlance.weak > 0 && (
                  <span className="text-red-700 bg-red-50 px-1.5 py-0.5 rounded border border-red-200">
                    {sessionGlance.weak} WEAK
                  </span>
                )}
                {sessionGlance.visibleApps.slice(0, 3).map((app, i) => {
                  const color = FLOW_COLORS[i % FLOW_COLORS.length];
                  return (
                    <span
                      key={app.grouping_id}
                      className="flex items-center gap-1 text-slate-700 bg-white px-1.5 py-0.5 rounded border border-slate-200 max-w-[160px] truncate"
                    >
                      <span
                        className="w-1.5 h-1.5 rounded-full shrink-0"
                        style={{ background: color.from }}
                      />
                      <span className="truncate">{app.display_label}</span>
                    </span>
                  );
                })}
              </div>
            )}
          </button>

          {/* Expanded body — hero + apps + quality (only when not collapsed) */}
          {!glanceCollapsed && (
          <div className="mt-3 space-y-3">

          <div className="grid grid-cols-12 gap-4">
            {/* Hero — last non-noise frame + caption */}
            <div className="col-span-12 md:col-span-7 rounded-xl overflow-hidden relative bg-slate-100"
              style={{ border: '1px solid rgba(38,112,163,0.12)', minHeight: 200 }}
            >
              {sessionGlance.heroAnnotatedUrl ? (
                <img
                  src={sessionGlance.heroAnnotatedUrl}
                  alt={sessionGlance.heroCaption || 'Hero frame'}
                  className="w-full h-full object-cover"
                  style={{ minHeight: 200, maxHeight: 280 }}
                  loading="lazy"
                  onError={(e) => {
                    (e.target as HTMLImageElement).style.display = 'none';
                  }}
                />
              ) : (
                <div className="h-full min-h-[200px] flex items-center justify-center text-slate-400 text-xs">
                  <MonitorPlay className="h-6 w-6 opacity-40 mr-2" />
                  Hero frame pending annotation
                </div>
              )}
              {sessionGlance.heroCaption && (
                <div
                  className="absolute bottom-0 left-0 right-0 px-4 py-2 text-white"
                  style={{
                    background:
                      'linear-gradient(180deg, rgba(10,37,64,0) 0%, rgba(10,37,64,0.85) 100%)',
                  }}
                >
                  <div className="text-sm font-bold tracking-tight">
                    {sessionGlance.heroCaption}
                  </div>
                </div>
              )}
            </div>

            {/* Apps panel — one row per dedup'd grouping */}
            <div className="col-span-12 md:col-span-5 rounded-xl overflow-hidden"
              style={{
                background: 'linear-gradient(180deg, #ffffff, #fafbfc)',
                border: '1px solid rgba(38,112,163,0.12)',
              }}
            >
              <div className="px-3 py-2 text-[10px] font-black uppercase tracking-wider text-slate-500 border-b border-slate-100">
                Apps in session
              </div>
              <div className="divide-y divide-slate-100 max-h-[244px] overflow-y-auto">
                {sessionGlance.visibleApps.map((app, idx) => {
                  const color = FLOW_COLORS[idx % FLOW_COLORS.length];
                  return (
                    <div key={app.grouping_id} className="flex items-center gap-2.5 px-3 py-2">
                      <div
                        className="w-2.5 h-2.5 rounded-full shrink-0"
                        style={{ background: color.from }}
                      />
                      <div className="min-w-0 flex-1">
                        <div className="text-[12px] font-bold text-slate-900 truncate">
                          {app.display_label}
                        </div>
                        <div className="text-[10px] text-slate-500">
                          {app.scene_count} scene{app.scene_count === 1 ? '' : 's'}
                          {' · '}
                          {app.dedup_basis === 'exact_domain'
                            ? 'verified'
                            : app.dedup_basis === 'fuzzy_domain'
                            ? 'OCR-corrected'
                            : app.dedup_basis === 'shared_suffix'
                            ? 'OCR-corrected'
                            : app.dedup_basis === 'window_title_normalized'
                            ? 'desktop app'
                            : 'detected'}
                        </div>
                      </div>
                      <div className="text-[10px] text-slate-400 shrink-0">
                        {Math.round((app.confidence || 0) * 100)}%
                      </div>
                    </div>
                  );
                })}
                {sessionGlance.visibleApps.length === 0 && (
                  <div className="px-3 py-4 text-[11px] text-slate-400 text-center">
                    No apps detected
                  </div>
                )}
              </div>
            </div>
          </div>

          {/* Quality breakdown chips */}
          <div className="flex items-center gap-3 text-[10px] font-bold pt-1">
            <span className="text-emerald-700 bg-emerald-50 px-2 py-0.5 rounded border border-emerald-200">
              {sessionGlance.strong} STRONG
            </span>
            <span className="text-amber-700 bg-amber-50 px-2 py-0.5 rounded border border-amber-200">
              {sessionGlance.degraded} DEGRADED
            </span>
            <span className="text-red-700 bg-red-50 px-2 py-0.5 rounded border border-red-200">
              {sessionGlance.weak} WEAK
            </span>
            {sessionGlance.noiseCount > 0 && (
              <span className="text-slate-600 bg-slate-100 px-2 py-0.5 rounded border border-slate-200">
                {sessionGlance.noiseCount} NOISE (hidden)
              </span>
            )}
          </div>
          </div>
          )}
        </div>
      )}

      {/* ─────────── NOISE FILTER TOGGLE (Phase 1.7) ────────────── */}
      {noiseSceneIds.size > 0 && (
        <div
          className="flex items-center gap-2 px-6 py-2 shrink-0 relative z-10 text-[11px]"
          style={{
            borderBottom: '1px solid rgba(38,112,163,0.08)',
            background: 'linear-gradient(180deg, #ffffff, #fafbfc)',
          }}
        >
          <span className="text-slate-500">
            {showNoiseScenes
              ? `Showing all ${graph.scenes.length} scenes (including ${noiseSceneIds.size} noise)`
              : `${noiseSceneIds.size} noise scene${noiseSceneIds.size === 1 ? '' : 's'} hidden`}
          </span>
          <button
            onClick={() => setShowNoiseScenes((v) => !v)}
            className="ml-auto px-2 py-0.5 rounded text-[10px] font-bold transition-all"
            style={{
              background: showNoiseScenes ? 'rgba(38,112,163,0.15)' : 'rgba(10,37,64,0.05)',
              color: showNoiseScenes ? '#2670a3' : '#475569',
              border: `1px solid ${showNoiseScenes ? 'rgba(38,112,163,0.25)' : 'rgba(10,37,64,0.1)'}`,
            }}
          >
            {showNoiseScenes ? 'Hide noise' : 'Show noise'}
          </button>
        </div>
      )}

      {/* ─────────── FLOW TABS ────────────── */}
      {consolidatedFlows.length > 1 && (
        <div className="flex items-center gap-2.5 px-6 py-3.5 overflow-x-auto shrink-0 relative z-10"
          style={{ borderBottom: '1px solid rgba(38,112,163,0.1)', background: 'linear-gradient(180deg, #ffffff, #f8fafc)', backdropFilter: 'blur(12px)' }}>
          <button
            onClick={() => setActiveFlowId(null)}
            className={clsx(
              'flex items-center gap-2 rounded-xl px-5 py-2.5 text-xs font-bold transition-all whitespace-nowrap',
              !activeFlowId ? 'text-slate-900' : 'text-slate-500 hover:text-slate-800',
            )}
            style={!activeFlowId ? {
              background: 'linear-gradient(135deg, rgba(38,112,163,0.15), rgba(38,112,163,0.1))',
              border: '1px solid rgba(38,112,163,0.25)',
              boxShadow: '0 4px 16px rgba(38,112,163,0.12)',
            } : { border: '1px solid rgba(10,37,64,0.06)' }}
          >
            <Eye className="h-3.5 w-3.5" />
            All Flows
            <span className="text-[10px] opacity-50">({consolidatedFlows.reduce((sum, cf) => sum + cf.scene_count, 0)})</span>
          </button>
          {consolidatedFlows.map((cf, fi) => {
            const fc = FLOW_COLORS[fi % FLOW_COLORS.length];
            const isActive = activeMemberFlowIds?.has(cf.flow_id) ?? false;
            return (
              <button
                key={cf.flow_id}
                onClick={() => setActiveFlowId(cf.flow_id)}
                className={clsx(
                  'flex items-center gap-2.5 rounded-xl px-5 py-2.5 text-xs font-bold transition-all whitespace-nowrap',
                  isActive ? 'text-slate-900' : 'text-slate-500 hover:text-slate-800',
                  cf.is_noise && 'opacity-40',
                )}
                style={isActive ? {
                  background: `linear-gradient(135deg, ${fc.bg}, ${fc.bg.replace('0.15', '0.06')})`,
                  border: `1px solid ${fc.ring}`,
                  boxShadow: `0 4px 20px ${fc.bg}`,
                } : { border: '1px solid rgba(10,37,64,0.06)' }}
              >
                <span className="w-3.5 h-3.5 rounded-full shrink-0" style={{ background: `linear-gradient(135deg, ${fc.from}, ${fc.to})`, boxShadow: `0 0 8px rgba(${fc.glow},0.4)` }} />
                {cf.label}
                {cf.is_interleaved && cf.visit_count > 1 && (
                  <span className="text-[8px] font-black px-1.5 py-0.5 rounded-full" style={{ background: 'rgba(245,158,11,0.15)', color: '#d97706', border: '1px solid rgba(245,158,11,0.3)' }}>
                    {cf.visit_count}× visits
                  </span>
                )}
                <span className="text-[10px] opacity-50">({cf.scene_count})</span>
              </button>
            );
          })}
        </div>
      )}

      {/* ─────────────── 3D FLOW CANVAS ─────────────── */}
      <div ref={scrollRef} className="flex-1 overflow-auto relative z-10">
        <div className="py-5 space-y-8">
          {flowGroups.map(({ flow, scenes }, groupIdx) => {
            const flowColorIdx = graph.flows?.findIndex((f) => f.flow_id === flow.flow_id) ?? 0;
            const fc = FLOW_COLORS[(flowColorIdx >= 0 ? flowColorIdx : groupIdx) % FLOW_COLORS.length];

            // Check for a cross-flow app_switch edge leaving the last scene of this group.
            // Fix #2: resolve destination label from the actual target flow, not the next
            // rendered group — those can diverge when groups are filtered or reordered.
            const lastSceneId = scenes[scenes.length - 1]?.scene_id;
            const switchEdge = lastSceneId
              ? graph.edges.find((e) => e.edge_type === 'app_switch' && e.from_scene_id === lastSceneId)
              : undefined;
            const switchTargetLabel = (() => {
              if (!switchEdge) return undefined;
              const targetScene = graph.scenes.find((s) => s.scene_id === switchEdge.to_scene_id);
              if (targetScene?.flow_id) {
                const targetFlow = graph.flows?.find((f) => f.flow_id === targetScene.flow_id);
                if (targetFlow) return targetFlow.flow_label;
              }
              // Fallback: next rendered group (original behavior)
              return groupIdx < flowGroups.length - 1 ? flowGroups[groupIdx + 1].flow.flow_label : undefined;
            })();

            return (
              <Fragment key={flow.flow_id}>
              <div className="space-y-3">
                {(flowGroups.length > 1 || consolidatedFlows.length > 1) && (
                  <div className="px-5 flex items-center gap-2.5 v5-card-enter">
                    <div className="h-8 w-1.5 rounded-full" style={{ background: `linear-gradient(180deg, ${fc.from}, ${fc.to})`, boxShadow: `0 0 12px rgba(${fc.glow},0.3)` }} />
                    <div>
                      <h2 className="text-sm font-black text-slate-900 flex items-center gap-1.5">
                        {cleanFlowLabel(flow)}
                        <ChevronRight className="h-3 w-3 text-slate-600" />
                      </h2>
                      <p className="text-[9px] text-slate-500 font-medium">
                        {scenes.length} screen{scenes.length !== 1 ? 's' : ''}
                        {flow.is_noise && <span className="text-yellow-500/60 ml-1">{'\u00B7'} noise</span>}
                      </p>
                    </div>
                  </div>
                )}

                {/* Horizontal 3D card flow */}
                <div className="overflow-x-auto px-5">
                  <div className="flex items-start pb-4" >
                    {scenes.map((scene, i) => {
                      const appId = scene.app_instance_id ?? '';
                      const appName = appNameMap.get(appId) ?? 'Application';
                      const edge = edgeFromMap.get(scene.scene_id);
                      const incomingEdge = edgeToMap.get(scene.scene_id);
                      const isConfirmed = edge?.edge_type === 'action_confirmed_transition';
                      const prevScene = i > 0 ? scenes[i - 1] : null;
                      const ctrls = controlsByScene.get(scene.scene_id) ?? [];
                      // Phase 1.7 — prefer the LLM-generated short
                      // caption from the storyboard composer when
                      // available.  It's a 5-8 word imperative
                      // ("Filled email field") instead of the
                      // heuristic's "Fills form" / "Action needs
                      // review" verbiage.  Fall through to the
                      // existing heuristic for legacy artifacts.
                      const llmCaption = captionByScene.get(scene.scene_id);
                      const heuristic = deriveActionLabel(scene, ctrls, prevScene, incomingEdge?.edge_type, incomingEdge);
                      const action = llmCaption || heuristic.action;
                      const icon = heuristic.icon;
                      const quality = llmCaption ? 'strong' : heuristic.quality;

                      // Fix #1: detect chronological gap — other-app scenes occurred between
                      // this scene and the previous one in this (interleaved) flow.
                      const prevInFlow = i > 0 ? scenes[i - 1] : null;
                      const idxGap = prevInFlow != null
                        ? scene.scene_index - prevInFlow.scene_index - 1
                        : 0;

                      return (
                        <div key={scene.scene_id} className="flex items-start shrink-0">
                          {idxGap > 0 && <InterruptionGap missedCount={idxGap} />}
                          <SceneCard3D
                            scene={scene}
                            controls={ctrls}
                            isSelected={selectedSceneId === scene.scene_id}
                            onClick={() => setSelectedSceneId((prev) => prev === scene.scene_id ? null : scene.scene_id)}
                            stepNumber={i + 1}
                            totalSteps={scenes.length}
                            color={fc}
                            appName={appName}
                            index={i}
                            actionLabel={action}
                            actionIcon={icon}
                            annotatedImgUrl={graph.annotated_frame_urls?.[scene.scene_id]}
                          />
                          {i < scenes.length - 1 && (
                            <FlowConnector3D
                              isConfirmed={isConfirmed}
                              isAppSwitch={edge?.edge_type === 'app_switch'}
                              color={fc}
                              index={groupIdx * 100 + i}
                            />
                          )}
                        </div>
                      );
                    })}
                  </div>
                </div>
              </div>
              {switchEdge && switchTargetLabel && (
                <AppSwitchBanner toLabel={switchTargetLabel} />
              )}
              </Fragment>
            );
          })}
        </div>
      </div>

      {/* ─────────────── DETAIL PANEL ─────────────── */}
      {/* Phase 1.7 layout-squeeze fix: capped at 55vh (was 70vh).
          Iteration 2: thicker top border + box-shadow above so there's
          a visible separation from the flow rail (user reported "no
          space between bottom and above panel"). */}
      <div
        className={clsx(
          'shrink-0 transition-all duration-500 ease-out relative z-10',
          selectedScene ? 'max-h-[55vh] opacity-100 translate-y-0 overflow-y-auto' : 'max-h-0 opacity-0 translate-y-4 overflow-hidden',
        )}
        style={{
          borderTop: selectedScene ? '3px solid #2670a3' : 'none',
          background: selectedScene ? 'linear-gradient(180deg, #ffffff, #f8fafc)' : 'transparent',
          backdropFilter: selectedScene ? 'blur(20px)' : 'none',
          boxShadow: selectedScene
            ? '0 -8px 24px -8px rgba(10,37,64,0.18), 0 -2px 0 rgba(38,112,163,0.06)'
            : 'none',
        }}
      >
        {selectedScene && (
          <div className="px-5 py-4 v5-card-enter">
            <div className="flex items-start gap-4">
              {/* Thumbnail \u2014 Phase 1.7: prefer annotated PNG with raw
                  fallback so the detail panel shows the same overlay
                  the flow rail card does. */}
              <div
                className="w-56 h-32 rounded-xl overflow-hidden shrink-0 relative group"
                style={{
                  background: 'linear-gradient(135deg, #f8fafc, #eef2f7)',
                  border: '2px solid rgba(38,112,163,0.2)',
                  boxShadow: '0 12px 40px rgba(10,37,64,0.12), 0 0 30px rgba(10,37,64,0.04)',
                }}
              >
                {(() => {
                  const annotatedRaw = graph?.annotated_frame_urls?.[selectedScene.scene_id];
                  const annotatedUrl = annotatedRaw
                    ? (() => {
                        const token = sessionStorage.getItem('nexus_token') || '';
                        const sep = annotatedRaw.includes('?') ? '&' : '?';
                        return token
                          ? `${annotatedRaw}${sep}token=${encodeURIComponent(token)}`
                          : annotatedRaw;
                      })()
                    : null;
                  const rawUrl = selectedScene.representative_frame_asset_path
                    ? api.getFrameImageUrl(selectedScene.representative_frame_asset_path)
                    : selectedScene.representative_frame_id
                      ? api.getFrameImageUrl(selectedScene.representative_frame_id)
                      : null;
                  const url = annotatedUrl ?? rawUrl;
                  return url ? (
                    <img
                      src={url}
                      alt=""
                      className="w-full h-full object-cover group-hover:scale-105 transition-transform duration-500"
                      onError={(e) => {
                        // Fall back to raw if annotated 404s.
                        const img = e.target as HTMLImageElement;
                        if (annotatedUrl && img.src.includes(annotatedRaw || '') && rawUrl) {
                          img.src = rawUrl;
                          return;
                        }
                        img.style.display = 'none';
                      }}
                    />
                  ) : (
                    <div className="w-full h-full flex items-center justify-center"><MonitorPlay className="h-8 w-8 text-slate-600/40" /></div>
                  );
                })()}
              </div>

              {/* Info */}
              <div className="flex-1 min-w-0 space-y-3">
                <div>
                  {/* LLM caption headline \u2014 picture-first design.
                      Falls back to humanScreenName when no caption. */}
                  <h3 className="text-base font-black text-slate-900 leading-tight">
                    {captionByScene.get(selectedScene.scene_id) || humanScreenName(selectedScene)}
                  </h3>
                  {/* Screen title \u2014 subtitle when caption is the headline */}
                  {captionByScene.get(selectedScene.scene_id) && humanScreenName(selectedScene) && (
                    <p className="text-[11px] text-slate-500 mt-0.5">
                      Screen: {humanScreenName(selectedScene)}
                    </p>
                  )}
                  <div className="flex items-center gap-4 mt-2 text-xs text-slate-600 flex-wrap">
                    <span className="flex items-center gap-1.5">
                      <Clock className="h-3.5 w-3.5 text-slate-600/60" />
                      {fmtMs(selectedScene.start_ms)} {'\u2192'} {fmtMs(selectedScene.end_ms)}
                      {' \u00b7 '}
                      <span className="text-slate-500">{fmtSceneDuration(selectedScene)}</span>
                    </span>
                    {/* Canonical app badge \u2014 Phase 1.7: prefer
                        dedup'd grouping label. */}
                    {(() => {
                      const groupingId =
                        graph?.scene_to_app_grouping?.[selectedScene.scene_id];
                      const grouping =
                        groupingId &&
                        graph?.app_groupings?.find((g) => g.grouping_id === groupingId);
                      const appLabel = grouping
                        ? grouping.display_label
                        : appNameMap.get(selectedScene.app_instance_id ?? '') ?? '';
                      return appLabel ? (
                        <span className="flex items-center gap-1.5 text-indigo-600 font-bold">
                          <Box className="h-3.5 w-3.5" />
                          {appLabel}
                        </span>
                      ) : null;
                    })()}
                    {(selectedScene.canonical_url || selectedScene.detected_url) && (
                      <span className="flex items-center gap-1.5 text-blue-500/80">
                        <ExternalLink className="h-3.5 w-3.5" />
                        {extractDomain(selectedScene.canonical_url || selectedScene.detected_url)}
                      </span>
                    )}
                  </div>
                  {(selectedScene.canonical_url || selectedScene.detected_url) && (
                    <p className="text-[10px] text-slate-500 mt-1.5 font-mono truncate">{selectedScene.canonical_url || selectedScene.detected_url}</p>
                  )}
                </div>

                <div className="flex items-center gap-4 py-2 px-3 rounded-lg text-[11px]"
                  style={{ background: 'rgba(10,37,64,0.04)', border: '1px solid rgba(38,112,163,0.1)' }}>
                  <div className="flex items-center gap-1.5">
                    <Layers className="h-3 w-3 text-slate-600/60" />
                    <span className="text-slate-900 font-bold">{selectedControls.length}</span>
                    <span className="text-slate-500">ctrls</span>
                  </div>
                  <div className="w-px h-4" style={{ background: 'rgba(38,112,163,0.12)' }} />
                  <div className="flex items-center gap-1.5">
                    <Zap className="h-3 w-3" style={{ color: '#047857' }} />
                    <span style={{ color: '#047857' }} className="font-bold">{selectedControls.filter((c) => c.automation_ready).length}</span>
                    <span className="text-slate-500">auto</span>
                  </div>
                  <div className="w-px h-4" style={{ background: 'rgba(38,112,163,0.12)' }} />
                  <div className="flex items-center gap-1.5">
                    <MousePointerClick className="h-3 w-3 text-slate-600/60" />
                    <span className="text-slate-900 font-bold">{selectedControls.filter((c) => c.element_type === 'button' || c.element_type === 'link').length}</span>
                    <span className="text-slate-500">interactive</span>
                  </div>
                </div>

                {/* ── Phase 1.6 scene_actions — multimodal LLM extraction.
                       Now renders a LIST of actions per scene (long form-fill
                       scenes can have 5+ actions in chronological order).
                       Each entry shows verb chip + target + value + confidence
                       + reasoning.  Single-action scenes render exactly one
                       card; multi-action scenes stack cards with a small
                       index badge so the chronological order is obvious. */}
                {(() => {
                  const raw = graph.scene_actions?.[selectedScene.scene_id];
                  // Accept both the new list shape (Phase 1.6+) and the
                  // legacy single-object shape (Phase 1 - 1.5) so the
                  // panel keeps working across mixed-version artifacts.
                  const actions: SceneAction[] = Array.isArray(raw)
                    ? raw
                    : raw
                    ? [raw as unknown as SceneAction]
                    : [];
                  if (actions.length === 0) return null;
                  const verbStyles: Record<string, { bg: string; fg: string }> = {
                    click:    { bg: 'rgba(56,189,248,0.15)', fg: '#0369a1' },
                    type:     { bg: 'rgba(168,85,247,0.15)', fg: '#7e22ce' },
                    select:   { bg: 'rgba(16,185,129,0.15)', fg: '#047857' },
                    submit:   { bg: 'rgba(239,68,68,0.15)',  fg: '#b91c1c' },
                    navigate: { bg: 'rgba(245,158,11,0.15)', fg: '#b45309' },
                    scroll:   { bg: 'rgba(148,163,184,0.18)',fg: '#475569' },
                    hover:    { bg: 'rgba(148,163,184,0.18)',fg: '#475569' },
                    none:     { bg: 'rgba(148,163,184,0.10)',fg: '#94a3b8' },
                  };
                  return (
                    <div className="rounded-xl px-3 py-3 space-y-2.5"
                      style={{
                        background: 'linear-gradient(135deg, rgba(16,185,129,0.06), rgba(56,189,248,0.04))',
                        border: '1px solid rgba(16,185,129,0.25)',
                        boxShadow: '0 4px 16px rgba(16,185,129,0.08)',
                      }}>
                      <div className="flex items-center justify-between gap-2">
                        <p className="text-[9px] font-black uppercase tracking-widest text-slate-500">
                          User actions (vision-extracted)
                          {actions.length > 1 && (
                            <span className="ml-1.5 text-slate-400 font-bold">· {actions.length}</span>
                          )}
                        </p>
                      </div>
                      {actions.map((sa, i) => {
                        const vs = verbStyles[sa.verb] ?? verbStyles.none;
                        const confColor =
                          sa.confidence >= 0.85 ? '#047857'
                          : sa.confidence >= 0.65 ? '#a16207'
                          : '#b91c1c';
                        return (
                          <div
                            key={`${sa.subaction_index ?? i}-${sa.verb}-${sa.target_label}`}
                            className="rounded-lg px-2.5 py-2 space-y-1.5"
                            style={{
                              background: 'rgba(255,255,255,0.55)',
                              border: '1px solid rgba(16,185,129,0.15)',
                            }}
                          >
                            <div className="flex items-center gap-2 flex-wrap">
                              {actions.length > 1 && (
                                <span
                                  className="inline-flex items-center justify-center w-5 h-5 rounded-full text-[9px] font-black"
                                  style={{ background: 'rgba(56,189,248,0.15)', color: '#0369a1' }}
                                >
                                  {(sa.subaction_index ?? i) + 1}
                                </span>
                              )}
                              <span
                                className="inline-flex items-center px-2 py-0.5 rounded-md text-[10px] font-black uppercase tracking-wider"
                                style={{ background: vs.bg, color: vs.fg }}
                              >
                                {sa.verb}
                              </span>
                              {sa.target_label && (
                                <span className="text-[12px] font-bold text-slate-900 break-words">
                                  {sa.target_label}
                                </span>
                              )}
                              <span
                                className="text-[10px] font-bold ml-auto"
                                style={{ color: confColor }}
                              >
                                {Math.round(sa.confidence * 100)}%
                              </span>
                              {sa.automation_ready && (
                                <span
                                  className="inline-flex items-center px-1.5 py-0.5 rounded-md text-[9px] font-bold uppercase"
                                  style={{ background: 'rgba(16,185,129,0.15)', color: '#047857' }}
                                >
                                  auto
                                </span>
                              )}
                            </div>
                            {sa.value !== null && sa.value !== undefined && sa.value !== '' && (
                              <div className="flex items-baseline gap-2">
                                <span className="text-[9px] font-black uppercase tracking-widest text-slate-500">
                                  value
                                </span>
                                <span
                                  className="px-2 py-0.5 rounded-md text-[12px] font-mono font-bold text-slate-900"
                                  style={{ background: 'rgba(255,255,255,0.75)', border: '1px solid rgba(16,185,129,0.25)' }}
                                >
                                  {sa.value}
                                </span>
                              </div>
                            )}
                            {sa.reasoning && (
                              <p className="text-[10px] italic text-slate-600 leading-snug">
                                {sa.reasoning}
                              </p>
                            )}
                          </div>
                        );
                      })}
                    </div>
                  );
                })()}

                {/* ── Step Narrative — ALWAYS rendered so the bottom panel
                       never looks empty even when controls are sparse.  Surfaces
                       the most authoritative source available, in order:
                         1. action verb from incoming edge primary_action_summary
                         2. scene_state_summary.step_label (if persisted)
                         3. LLaVA description on the scene
                         4. OCR text snippet
                       Each section renders its own quality badge so the user can
                       see whether the data is vision-grounded or heuristic. */}
                {(() => {
                  const summary = selectedScene.scene_state_summary;
                  const edgeSummary = selectedIncomingEdge?.primary_action_summary;
                  const actionLabel = selectedActionLabel?.action ?? '';
                  const actionQuality = selectedActionLabel?.quality ?? 'weak';
                  const ActionIcon = selectedActionLabel?.icon ?? Eye;
                  const stepLabel = summary?.step_label ?? '';
                  const screenTitle = summary?.screen_title ?? '';
                  const sceneDesc = (selectedScene as VisualScene & { description?: string }).description ?? '';
                  const ocrSnippet = (selectedScene.ocr_text ?? '').trim().slice(0, 320);

                  const qualityBadge = (q: string | undefined) => {
                    const map: Record<string, { bg: string; fg: string; label: string }> = {
                      strong:    { bg: 'rgba(16,185,129,0.12)', fg: '#047857', label: 'vision-grounded' },
                      degraded:  { bg: 'rgba(234,179,8,0.12)',  fg: '#a16207', label: 'partial signal' },
                      weak:      { bg: 'rgba(148,163,184,0.18)',fg: '#475569', label: 'heuristic' },
                    };
                    const k = (q ?? 'weak').toLowerCase();
                    const m = map[k] ?? map.weak;
                    return (
                      <span
                        className="inline-flex items-center px-1.5 py-0.5 rounded-md text-[9px] font-bold uppercase tracking-wider"
                        style={{ background: m.bg, color: m.fg }}
                      >
                        {m.label}
                      </span>
                    );
                  };

                  return (
                    <div className="rounded-xl px-3 py-2.5 space-y-2.5"
                      style={{
                        background: 'linear-gradient(135deg, rgba(56,189,248,0.06), rgba(99,102,241,0.04))',
                        border: '1px solid rgba(56,189,248,0.15)',
                      }}>
                      {/* Action verb */}
                      <div className="flex items-start gap-2">
                        <ActionIcon className="h-4 w-4 mt-0.5 shrink-0" style={{ color: '#0369a1' }} />
                        <div className="flex-1 min-w-0">
                          <p className="text-[9px] font-black uppercase tracking-widest text-slate-500 mb-0.5">
                            What the user did
                          </p>
                          <div className="flex items-center gap-2 flex-wrap">
                            <p className="text-[12px] font-bold text-slate-900 break-words">
                              {actionLabel || 'Reviewed this screen'}
                            </p>
                            {qualityBadge(actionQuality)}
                          </div>
                        </div>
                      </div>

                      {/* Step label from persisted scene_state_summary */}
                      {(stepLabel || screenTitle) && (
                        <div>
                          <p className="text-[9px] font-black uppercase tracking-widest text-slate-500 mb-0.5">
                            Step
                          </p>
                          <div className="flex items-center gap-2 flex-wrap">
                            <p className="text-[11px] font-semibold text-slate-800 break-words">
                              {stepLabel || screenTitle}
                            </p>
                            {qualityBadge(summary?.state_quality ?? selectedScene.scene_quality)}
                          </div>
                        </div>
                      )}

                      {/* Edge primary action label (richer than card-strip verb) */}
                      {edgeSummary?.action_label && edgeSummary.action_label !== actionLabel && (
                        <div>
                          <p className="text-[9px] font-black uppercase tracking-widest text-slate-500 mb-0.5">
                            Transition into this step
                          </p>
                          <div className="flex items-center gap-2 flex-wrap">
                            <p className="text-[11px] text-slate-700 break-words">
                              {edgeSummary.action_label}
                            </p>
                            {qualityBadge(edgeSummary.action_quality)}
                          </div>
                        </div>
                      )}

                      {/* LLaVA description, when present */}
                      {sceneDesc && (
                        <div>
                          <p className="text-[9px] font-black uppercase tracking-widest text-slate-500 mb-0.5">
                            Description
                          </p>
                          <p className="text-[11px] text-slate-700 leading-relaxed break-words line-clamp-3">
                            {sceneDesc}
                          </p>
                        </div>
                      )}

                      {/* OCR snippet — truthful evidence even when LLaVA was skipped */}
                      {ocrSnippet && (
                        <div>
                          <p className="text-[9px] font-black uppercase tracking-widest text-slate-500 mb-0.5">
                            On-screen text (OCR)
                          </p>
                          <p className="text-[10px] font-mono text-slate-600 leading-relaxed break-words line-clamp-3"
                            style={{ background: 'rgba(15,23,42,0.04)', padding: '6px 8px', borderRadius: 6 }}>
                            {ocrSnippet}{(selectedScene.ocr_text?.length ?? 0) > 320 ? '…' : ''}
                          </p>
                        </div>
                      )}
                    </div>
                  );
                })()}

                {selectedControls.length > 0 && (() => {
                  // ── Narrative grouping: form → primary CTA → back-nav → chrome ──
                  const g: [EvidenceControl[], EvidenceControl[], EvidenceControl[], EvidenceControl[]] = [[], [], [], []];
                  for (const c of selectedControls) g[ctrlNarrativeGroup(c)].push(c);

                  const GROUP_META = [
                    { items: g[0], label: 'User fills in',   color: '#0369a1' },
                    { items: g[1], label: 'Then clicks',     color: '#047857' },
                    { items: g[2], label: 'Or goes back',    color: '#9333ea' },
                    { items: g[3], label: 'Page elements',   color: '#94a3b8' },
                  ].filter(({ items }) => items.length > 0);

                  const renderChip = (c: EvidenceControl) => {
                    let displayText: string;
                    if (c.display_label && c.display_label.length > 0) {
                      displayText = c.display_label;
                    } else {
                      const rawLabel = c.label_text ?? '';
                      const cleanLabel = rawLabel.length > 0 ? rawLabel.slice(0, 60) : '';
                      const isButton = c.element_type === 'button' || c.element_type === 'link';
                      const isInput = c.element_type === 'text_field' || c.element_type === 'input' ||
                        c.element_type === 'dropdown' || c.element_type === 'textarea' ||
                        c.element_type === 'select' || c.element_type === 'checkbox' || c.element_type === 'radio';
                      const actionVerb = isButton ? 'Click' : isInput ? 'Fill' : c.element_type ?? '';
                      displayText = cleanLabel ? `${actionVerb}: ${cleanLabel}` : (cleanLabel || c.element_type) ?? '?';
                    }
                    const observedVal = (c.observed_value && c.observed_value.length > 0)
                      ? c.observed_value
                      : (c.value_text && c.value_text.length > 0 ? c.value_text : null);
                    const showValueChip = observedVal !== null && !(c.display_label && c.display_label.includes(' = '));
                    return (
                      <span
                        key={c.control_id}
                        className="inline-flex items-center gap-1.5 rounded-xl px-3 py-1.5 text-[10px] transition-all duration-200 hover:scale-105"
                        style={{
                          background: c.automation_ready ? 'rgba(16,185,129,0.1)' : 'rgba(10,37,64,0.04)',
                          border: `1px solid ${c.automation_ready ? 'rgba(16,185,129,0.25)' : 'rgba(38,112,163,0.1)'}`,
                          color: c.automation_ready ? '#059669' : '#94a3b8',
                          boxShadow: c.automation_ready ? '0 2px 8px rgba(16,185,129,0.08)' : 'none',
                        }}
                        title={c.display_label ? `${c.display_label}${c.playwright_selector ? `\nSelector: ${c.playwright_selector}` : ''}` : (c.playwright_selector ? `Selector: ${c.playwright_selector}` : undefined)}
                      >
                        <span className="font-bold truncate max-w-[280px]">{displayText}</span>
                        {showValueChip && <span className="opacity-60 truncate max-w-[80px] font-medium">= {observedVal}</span>}
                        {c.automation_ready && <Zap className="h-2.5 w-2.5 shrink-0" style={{ color: '#059669' }} />}
                      </span>
                    );
                  };

                  return (
                    <div className="space-y-2 max-h-[160px] overflow-y-auto pr-1">
                      {GROUP_META.map(({ items, label, color }) => (
                        <div key={label}>
                          <p className="text-[9px] font-black uppercase tracking-widest mb-1.5" style={{ color }}>{label}</p>
                          <div className="flex flex-wrap gap-1.5">{items.map(renderChip)}</div>
                        </div>
                      ))}
                      {selectedNextScene && (
                        <div className="flex items-center gap-2 pt-2 mt-1" style={{ borderTop: '1px solid rgba(38,112,163,0.12)' }}>
                          <ChevronRight className="h-3.5 w-3.5 shrink-0" style={{ color: '#0369a1' }} />
                          <span className="text-[10px] text-slate-500 font-medium">Leads to:</span>
                          <span className="text-[10px] font-black truncate" style={{ color: '#0369a1' }}>
                            {humanScreenName(selectedNextScene) || `Screen ${(selectedNextScene.scene_index ?? 0) + 1}`}
                          </span>
                        </div>
                      )}
                    </div>
                  );
                })()}

                {/* Phase 1.7 iteration 6 — data-quality banner.
                    Audit revealed the pipeline did not capture per-field
                    form actions for the form-fill scenes (0 evidence
                    controls + 0 ui_elements_json on scenes 8-11 of the
                    test artifact).  Telling the user honestly what
                    signals are available is better than padding the
                    panel with OCR noise that looks rich but isn't. */}
                {sceneFrameEvidence.dataQuality !== 'no_scene' && (
                  <div
                    className="rounded-xl px-3 py-2 text-[11px]"
                    style={{
                      background:
                        sceneFrameEvidence.dataQuality === 'strong' ? 'rgba(16,185,129,0.08)'
                        : sceneFrameEvidence.dataQuality === 'degraded' ? 'rgba(234,179,8,0.08)'
                        : 'rgba(239,68,68,0.07)',
                      border:
                        sceneFrameEvidence.dataQuality === 'strong' ? '1px solid rgba(16,185,129,0.25)'
                        : sceneFrameEvidence.dataQuality === 'degraded' ? '1px solid rgba(234,179,8,0.3)'
                        : '1px solid rgba(239,68,68,0.25)',
                    }}
                  >
                    <div className="flex items-center gap-2 mb-1">
                      <span className="text-[9px] font-black uppercase tracking-widest text-slate-500">
                        Data quality
                      </span>
                      <span
                        className="text-[10px] font-black px-2 py-0.5 rounded-full uppercase tracking-wider"
                        style={{
                          background:
                            sceneFrameEvidence.dataQuality === 'strong' ? 'rgba(16,185,129,0.18)'
                            : sceneFrameEvidence.dataQuality === 'degraded' ? 'rgba(234,179,8,0.18)'
                            : 'rgba(239,68,68,0.18)',
                          color:
                            sceneFrameEvidence.dataQuality === 'strong' ? '#047857'
                            : sceneFrameEvidence.dataQuality === 'degraded' ? '#a16207'
                            : '#b91c1c',
                        }}
                      >
                        {sceneFrameEvidence.dataQuality === 'strong' ? '✓ rich'
                          : sceneFrameEvidence.dataQuality === 'degraded' ? '⚠ partial'
                          : '⚠ page-level only'}
                      </span>
                      <span className="text-[10px] text-slate-600 ml-auto">
                        {sceneFrameEvidence.elements.length} UI elements ·{' '}
                        {selectedControls.length} controls ·{' '}
                        {sceneFrameEvidence.urlChanges.length} URL changes ·{' '}
                        {sceneFrameEvidence.frameRows.length} frames
                      </span>
                    </div>
                    <p className="text-[10px] text-slate-600 leading-snug">
                      {sceneFrameEvidence.dataQuality === 'strong' && (
                        <>This scene has structured UI affordances and form controls — every action below is grounded in detected elements.</>
                      )}
                      {sceneFrameEvidence.dataQuality === 'degraded' && (
                        <>Partial structured signal. Some buttons/fields detected; per-field values may be missing. Page-level navigation evidence is reliable.</>
                      )}
                      {sceneFrameEvidence.dataQuality === 'weak' && (
                        <>This scene was processed without per-field detection. Only page-level OCR and URL transitions are available — no granular form actions exist for this video. Re-record with cursor tracking enabled for richer detail.</>
                      )}
                    </p>
                  </div>
                )}

                {/* Phase 1.7 iter 6 — INFERRED user actions (synthesized
                    cursor_events from /synthesize-clicks).
                    These are the additive-fallback events for recordings
                    that don't show the mouse cursor.  Each row carries:
                      • the detection method (URL nav / new UI text /
                        button gone / form value / click detected)
                      • a confidence badge
                      • the inferred target label (button text, URL,
                        control name, OCR delta)
                      • a "corroborated by N" chip when multiple
                        synthesis strategies agreed on the same frame.
                    Real motion-detected events (detection_method='motion')
                    are NOT shown here — those flow through the existing
                    EvidenceStepsPanel below. */}
                {sceneSyntheticClicks.length > 0 && (
                  <div
                    className="rounded-xl px-3 py-2.5 space-y-2"
                    style={{
                      background: 'rgba(124,58,237,0.05)',
                      border: '1px solid rgba(124,58,237,0.22)',
                    }}
                  >
                    <div className="flex items-center gap-2">
                      <p className="text-[10px] font-black uppercase tracking-widest text-violet-700">
                        Inferred user actions on this page
                      </p>
                      <span className="text-[10px] text-slate-500">
                        ({sceneSyntheticClicks.length})
                      </span>
                      <span
                        className="text-[9px] text-violet-700 ml-auto italic px-1.5 py-0.5 rounded"
                        style={{ background: 'rgba(124,58,237,0.12)' }}
                        title="No mouse cursor visible in this recording — these clicks were inferred from URL changes, OCR deltas, UI element disappearance, form values, and pipeline frame_actions"
                      >
                        synthesized (no cursor in recording)
                      </span>
                    </div>
                    <ul className="space-y-1 overflow-y-auto pr-1" style={{ maxHeight: '220px' }}>
                      {sceneSyntheticClicks.map((c, idx) => (
                        <li
                          key={`syn-${idx}`}
                          className="flex items-start gap-2 text-[11px] leading-snug py-1 px-1.5 rounded"
                          style={{ background: 'rgba(255,255,255,0.75)' }}
                        >
                          <span className="text-[10px] font-mono text-slate-500 shrink-0 mt-0.5">
                            {`${Math.floor(c.timestamp_s / 60)}:${String(Math.floor(c.timestamp_s % 60)).padStart(2, '0')}`}
                          </span>
                          <span className="flex-1 min-w-0">
                            <div className="flex items-baseline gap-1.5 flex-wrap">
                              <span className="font-bold text-slate-900">{c.actionVerb}</span>
                              <span className="text-slate-800 break-words">{c.target}</span>
                              {c.observedValue && (
                                <span
                                  className="font-mono text-[11px] font-bold px-1.5 py-px rounded whitespace-nowrap"
                                  style={{
                                    background: 'rgba(16,185,129,0.18)',
                                    color: '#065f46',
                                    border: '1px solid rgba(16,185,129,0.35)',
                                  }}
                                >
                                  = {c.observedValue}
                                </span>
                              )}
                              <span
                                className="px-1 py-px rounded text-[8px] uppercase font-black tracking-wider whitespace-nowrap"
                                style={{
                                  background: 'rgba(124,58,237,0.1)',
                                  color: c.methodColor,
                                }}
                              >
                                {c.methodLabel}
                              </span>
                              {c.corroborating > 0 && (
                                <span
                                  className="px-1 py-px rounded text-[8px] uppercase font-black tracking-wider whitespace-nowrap"
                                  style={{
                                    background: 'rgba(16,185,129,0.12)',
                                    color: '#047857',
                                  }}
                                  title={`Corroborated by ${c.corroborating} additional synthesis strategy(s)`}
                                >
                                  +{c.corroborating} agree
                                </span>
                              )}
                              {c.confidence < 0.5 && (
                                <span
                                  className="px-1 py-px rounded text-[8px] uppercase font-black tracking-wider whitespace-nowrap"
                                  style={{
                                    background: 'rgba(234,179,8,0.12)',
                                    color: '#a16207',
                                  }}
                                  title={`Confidence ${(c.confidence * 100).toFixed(0)}%`}
                                >
                                  ⚠ weak
                                </span>
                              )}
                            </div>
                          </span>
                        </li>
                      ))}
                    </ul>
                  </div>
                )}

                {/* Phase 1.7 iteration 6 — detected UI elements from
                    visual_frames.ui_elements_json.  This is the REAL
                    structured action signal: LLM-derived buttons,
                    text_fields, dropdowns with action_kind and
                    observed_value.  Renders only when the pipeline
                    actually detected elements; otherwise the panel
                    falls back to URL changes + frame filmstrip. */}
                {sceneFrameEvidence.elements.length > 0 && (
                  <div
                    className="rounded-xl px-3 py-2.5 space-y-2"
                    style={{
                      background: 'rgba(56,189,248,0.06)',
                      border: '1px solid rgba(56,189,248,0.25)',
                    }}
                  >
                    <div className="flex items-center gap-2">
                      <p className="text-[10px] font-black uppercase tracking-widest text-sky-700">
                        Detected on-page UI elements
                      </p>
                      <span className="text-[10px] text-slate-500">
                        ({sceneFrameEvidence.elements.length})
                      </span>
                      <span className="text-[9px] text-slate-500 ml-auto italic">
                        from per-frame UI detection
                      </span>
                    </div>
                    <ul className="space-y-1 overflow-y-auto pr-1" style={{ maxHeight: '180px' }}>
                      {sceneFrameEvidence.elements.map((e, idx) => {
                        const verb =
                          e.action_kind === 'enter_text' ? 'Enter text'
                          : e.action_kind === 'select_option' ? 'Select'
                          : e.action_kind === 'click' ? 'Click'
                          : 'Interact';
                        const typeColor =
                          e.element_type === 'button' ? '#0369a1'
                          : e.element_type === 'text_field' ? '#047857'
                          : e.element_type === 'select' || e.element_type === 'dropdown' ? '#7c3aed'
                          : '#475569';
                        return (
                          <li
                            key={`uie-${idx}`}
                            className="flex items-start gap-2 text-[11px] leading-snug py-1 px-1.5 rounded"
                            style={{ background: 'rgba(255,255,255,0.7)' }}
                          >
                            <span className="flex-1 min-w-0">
                              <div className="flex items-baseline gap-1.5 flex-wrap">
                                <span className="font-bold text-slate-900">{verb}</span>
                                <span className="text-slate-800 break-words">{e.text}</span>
                                <span
                                  className="px-1 py-px rounded text-[8px] uppercase font-black tracking-wider"
                                  style={{ background: 'rgba(56,189,248,0.12)', color: typeColor }}
                                >
                                  {e.element_type.replace('_', ' ')}
                                </span>
                                {e.confidence < 0.5 && (
                                  <span
                                    className="px-1 py-px rounded text-[8px] uppercase font-black tracking-wider"
                                    style={{ background: 'rgba(234,179,8,0.12)', color: '#a16207' }}
                                    title={`Confidence ${(e.confidence * 100).toFixed(0)}%`}
                                  >
                                    ⚠ weak
                                  </span>
                                )}
                              </div>
                              {e.observed_value && (
                                <div
                                  className="text-[10px] text-slate-700 font-mono mt-0.5 px-2 py-0.5 rounded inline-block max-w-full break-words"
                                  style={{
                                    background: 'rgba(10,37,64,0.04)',
                                    border: '1px solid rgba(10,37,64,0.08)',
                                  }}
                                >
                                  = {e.observed_value.length > 80 ? e.observed_value.slice(0, 80) + '…' : e.observed_value}
                                </div>
                              )}
                            </span>
                          </li>
                        );
                      })}
                    </ul>
                  </div>
                )}

                {/* Phase 1.7 iteration 6 — URL deltas inside the scene.
                    For form-fill scenes where no UI elements were
                    detected, the URL change is often the only reliable
                    evidence the user navigated to a different page
                    state (e.g. "?quote-tool-tab-Disability-1"). */}
                {sceneFrameEvidence.urlChanges.length > 0 && (
                  <div
                    className="rounded-xl px-3 py-2 space-y-1.5"
                    style={{
                      background: 'rgba(99,102,241,0.05)',
                      border: '1px solid rgba(99,102,241,0.2)',
                    }}
                  >
                    <div className="flex items-center gap-2">
                      <p className="text-[10px] font-black uppercase tracking-widest text-indigo-700">
                        URL transitions in this scene
                      </p>
                      <span className="text-[10px] text-slate-500">
                        ({sceneFrameEvidence.urlChanges.length})
                      </span>
                    </div>
                    <ul className="space-y-1">
                      {sceneFrameEvidence.urlChanges.map((u, idx) => (
                        <li
                          key={`url-${idx}`}
                          className="text-[10px] font-mono leading-snug rounded px-2 py-1"
                          style={{ background: 'rgba(255,255,255,0.7)' }}
                        >
                          <div className="text-slate-500">
                            @ {Math.floor(u.timestamp_s / 60)}:{String(Math.floor(u.timestamp_s % 60)).padStart(2, '0')} · frame #{u.frame_index}
                          </div>
                          <div className="text-slate-700 break-words">
                            <span className="text-slate-400">from</span> {u.from_url.slice(0, 80)}{u.from_url.length > 80 ? '…' : ''}
                          </div>
                          <div className="text-indigo-700 break-words font-bold">
                            <span className="text-slate-400 font-normal">to</span> {u.to_url.slice(0, 80)}{u.to_url.length > 80 ? '…' : ''}
                          </div>
                        </li>
                      ))}
                    </ul>
                  </div>
                )}

                {/* Phase 1.7 iteration 6 — per-frame OCR filmstrip.
                    The "last-resort" evidence layer: every frame in the
                    scene with its OCR text and URL.  When data quality
                    is "weak" this is what the user has to work with
                    for building a test case.  Collapsed by default
                    (max-h 140px) and scrollable. */}
                {sceneFrameEvidence.frameRows.length > 0 && (
                  <details
                    className="rounded-xl px-3 py-2"
                    style={{
                      background: 'rgba(15,23,42,0.03)',
                      border: '1px solid rgba(15,23,42,0.1)',
                    }}
                  >
                    <summary className="cursor-pointer flex items-center gap-2 list-none">
                      <p className="text-[10px] font-black uppercase tracking-widest text-slate-700">
                        Per-frame evidence (OCR + URL)
                      </p>
                      <span className="text-[10px] text-slate-500">
                        ({sceneFrameEvidence.frameRows.length} frames)
                      </span>
                      <span className="text-[9px] text-slate-400 ml-auto">click to expand</span>
                    </summary>
                    <ul
                      className="space-y-1 overflow-y-auto pr-1 mt-2"
                      style={{ maxHeight: '180px' }}
                    >
                      {sceneFrameEvidence.frameRows.map((f) => (
                        <li
                          key={`fr-${f.frame_index}`}
                          className="text-[10px] leading-snug rounded px-2 py-1"
                          style={{ background: 'rgba(255,255,255,0.7)' }}
                        >
                          <div className="flex items-center gap-1.5 mb-0.5">
                            <span className="font-mono text-slate-500">
                              #{f.frame_index} @ {Math.floor(f.timestamp_s / 60)}:{String(Math.floor(f.timestamp_s % 60)).padStart(2, '0')}
                            </span>
                            {f.is_keyframe && (
                              <span
                                className="px-1 py-px rounded text-[8px] uppercase font-black tracking-wider"
                                style={{ background: 'rgba(16,185,129,0.15)', color: '#047857' }}
                              >
                                key
                              </span>
                            )}
                            {f.url && (
                              <span className="font-mono text-indigo-600 truncate" title={f.url}>
                                {f.url.slice(0, 60)}{f.url.length > 60 ? '…' : ''}
                              </span>
                            )}
                          </div>
                          {f.ocr_preview && (
                            <div className="text-slate-700 break-words">
                              {f.ocr_preview}{f.ocr_preview.length >= 200 ? '…' : ''}
                            </div>
                          )}
                        </li>
                      ))}
                    </ul>
                  </details>
                )}

                {/* Phase 1.7 iteration 6 — legacy OCR-derived signals.
                    These are heuristic state_change / open_overlay /
                    user_interaction rows extracted from OCR text deltas.
                    They can be noisy (target_label often contains
                    mangled OCR like "ivuquardianlife") so they're now
                    rendered as a collapsed "Additional OCR signals"
                    section AFTER the real ui_elements_json data above.
                    Kept because some scenes have legitimate signal
                    here (e.g. "Opened indemnity" on scene 8). */}
                {(sceneActions.userActions.length > 0
                  || sceneActions.controls.length > 0
                  || sceneActions.incomingEdgeLabel) && (
                  <details
                    className="rounded-xl px-3 py-2.5 space-y-3"
                    style={{
                      background: 'linear-gradient(135deg, rgba(16,185,129,0.04), rgba(56,189,248,0.03))',
                      border: '1px solid rgba(16,185,129,0.15)',
                    }}
                  >
                    <summary className="cursor-pointer flex items-center justify-between list-none mb-2">
                      <p className="text-[9px] font-black uppercase tracking-widest text-slate-500">
                        Additional OCR-derived signals (legacy)
                      </p>
                      <span
                        className="text-[10px] font-black px-2 py-0.5 rounded-full"
                        style={{
                          background: 'rgba(16,185,129,0.12)',
                          color: '#047857',
                          border: '1px solid rgba(16,185,129,0.25)',
                        }}
                      >
                        {sceneActions.userActions.length + sceneActions.controls.length} signals · click to expand
                      </span>
                    </summary>

                    {/* Dominant navigation action — what got the user
                        TO this scene.  Shown as a header line so the
                        E2E test author sees the entry point first. */}
                    {sceneActions.incomingEdgeLabel && (
                      <div className="text-[11px] leading-snug rounded px-2 py-1.5"
                        style={{
                          background: 'rgba(99,102,241,0.06)',
                          border: '1px solid rgba(99,102,241,0.2)',
                        }}
                      >
                        <span className="text-[9px] font-black uppercase tracking-widest text-slate-500 mr-1.5">Entry:</span>
                        <span className="font-bold text-indigo-700">
                          {sceneActions.incomingEdgeLabel}
                        </span>
                        {sceneActions.incomingEdgeValue && (
                          <div className="text-[10px] text-slate-600 mt-0.5 font-mono break-words">
                            {sceneActions.incomingEdgeValue.length > 160
                              ? sceneActions.incomingEdgeValue.slice(0, 160) + '…'
                              : sceneActions.incomingEdgeValue}
                          </div>
                        )}
                      </div>
                    )}

                    {/* Subsection 1: User actions on this page
                        (chronological timeline from frame_actions).
                        Phase 1.7 iteration 5 — single bucket: every
                        action is shown by default with a "weak" badge
                        on low-confidence rows.  User judges quality
                        in context; hiding low-confidence rows removed
                        too much signal for E2E test-case building. */}
                    {sceneActions.userActions.length > 0 && (
                      <div className="space-y-1.5">
                        <div className="flex items-center gap-2">
                          <p className="text-[10px] font-black uppercase tracking-widest text-emerald-700">
                            User actions on this page
                          </p>
                          <span className="text-[10px] text-slate-500">
                            ({sceneActions.userActions.length})
                          </span>
                          <span className="text-[9px] text-slate-400 ml-auto italic">
                            yellow ⚠ = low-confidence
                          </span>
                        </div>
                        <ul
                          className="space-y-1 overflow-y-auto pr-1"
                          style={{ maxHeight: '240px' }}
                        >
                          {sceneActions.userActions.map((a, idx) => (
                            <li
                              key={`a-${idx}`}
                              className="flex items-start gap-2 text-[11px] leading-snug py-1 px-1.5 rounded"
                              style={{
                                background: a.confidence >= 0.6
                                  ? 'rgba(255,255,255,0.85)'
                                  : 'rgba(255,255,255,0.5)',
                              }}
                            >
                              <span className="text-[10px] font-mono text-slate-500 shrink-0 mt-0.5">
                                {a.timestamp_s != null
                                  ? `${Math.floor(a.timestamp_s / 60)}:${String(Math.floor(a.timestamp_s % 60)).padStart(2, '0')}`
                                  : ''}
                              </span>
                              <span className="flex-1 min-w-0 flex items-baseline gap-1.5 flex-wrap">
                                <span className="font-bold text-slate-900">{a.verb}</span>
                                <span className="text-slate-800">{a.target}</span>
                                <span
                                  className="px-1 py-px rounded text-[8px] uppercase font-black tracking-wider whitespace-nowrap"
                                  style={{
                                    background: 'rgba(16,185,129,0.1)',
                                    color: '#047857',
                                  }}
                                >
                                  {a.type_label}
                                </span>
                                {a.confidence < 0.5 && (
                                  <span
                                    className="px-1 py-px rounded text-[8px] uppercase font-black tracking-wider whitespace-nowrap"
                                    style={{
                                      background: 'rgba(234,179,8,0.12)',
                                      color: '#a16207',
                                    }}
                                    title={`Confidence ${(a.confidence * 100).toFixed(0)}%`}
                                  >
                                    ⚠ weak
                                  </span>
                                )}
                              </span>
                            </li>
                          ))}
                        </ul>
                      </div>
                    )}

                    {/* Subsection 2: Detected form controls
                        (sorted by element type for narrative coherence). */}
                    {sceneActions.controls.length > 0 && (
                      <div className="space-y-1.5">
                        <div className="flex items-center gap-2">
                          <p className="text-[10px] font-black uppercase tracking-widest text-blue-700">
                            Detected form controls
                          </p>
                          <span className="text-[10px] text-slate-500">
                            ({sceneActions.controls.length})
                          </span>
                          <span className="text-[9px] text-slate-500 ml-auto">
                            (yellow ⚡ = automation-ready)
                          </span>
                        </div>
                        <ul
                          className="space-y-1 overflow-y-auto pr-1"
                          style={{ maxHeight: '180px' }}
                        >
                          {sceneActions.controls.map((c) => (
                            <li key={c.control_id} className="flex items-start gap-2 text-[11px] leading-snug py-1 px-1.5 rounded"
                              style={{ background: 'rgba(255,255,255,0.7)' }}
                            >
                              <span className="flex-1 min-w-0">
                                <div className="flex items-baseline gap-1.5 flex-wrap">
                                  {c.verb && (
                                    <span className="font-bold text-slate-900">{c.verb}</span>
                                  )}
                                  <span className="font-bold text-slate-900 break-words">
                                    {c.target}
                                  </span>
                                  <span
                                    className="px-1 py-px rounded text-[8px] uppercase font-black tracking-wider"
                                    style={{
                                      background: 'rgba(56,189,248,0.12)',
                                      color: '#0369a1',
                                    }}
                                  >
                                    {c.type_label}
                                  </span>
                                  {c.automation_ready && (
                                    <span
                                      className="px-1 py-px rounded text-[8px] uppercase font-black tracking-wider flex items-center gap-0.5"
                                      style={{
                                        background: 'rgba(245,158,11,0.12)',
                                        color: '#a16207',
                                      }}
                                      title="Selector is grounded and ready for Playwright/Cypress automation"
                                    >
                                      <Zap className="h-2.5 w-2.5" />
                                      auto-ready
                                    </span>
                                  )}
                                </div>
                                {c.value && (
                                  <div
                                    className="text-[10px] text-slate-700 font-mono mt-0.5 px-2 py-0.5 rounded inline-block max-w-full break-words"
                                    style={{
                                      background: 'rgba(10,37,64,0.04)',
                                      border: '1px solid rgba(10,37,64,0.08)',
                                    }}
                                  >
                                    {c.value.length > 80 ? c.value.slice(0, 80) + '…' : c.value}
                                  </div>
                                )}
                              </span>
                            </li>
                          ))}
                        </ul>
                      </div>
                    )}
                  </details>
                )}

                {/* Phase D.1 — triangulated user-action timeline.  Renders
                    one card per detected step with confidence ribbon,
                    evidence-signal chips, and frame thumbnails.  Falls
                    back to an explicit "no steps yet" message rather
                    than fabricating a label. */}
                <div className="pt-3 mt-1" style={{ borderTop: '1px solid rgba(38,112,163,0.12)' }}>
                  <p className="text-[9px] font-black uppercase tracking-widest text-slate-500 mb-2">
                    User actions inside this scene
                  </p>
                  <EvidenceStepsPanel
                    steps={stepsForPanel}
                    selectedStepId={selectedStepId}
                    onSelectStep={handleStepSelect}
                    thumbnailFor={thumbnailFor}
                    totalArtifactSteps={evidenceSteps.length}
                    sceneId={selectedSceneId ?? undefined}
                  />
                </div>

                {/* Phase D.2 — selected-step replay overlay.  Renders the
                    cursor click coordinate as a small badge so the user
                    can confirm WHERE the click landed.  When the artifact
                    has no cursor_events, the badge is hidden. */}
                {activeCursorMark && selectedStepId && (
                  <div className="mt-2 inline-flex items-center gap-2 px-3 py-1.5 rounded-md text-[10px]"
                    style={{ background: 'rgba(56,189,248,0.1)', border: '1px solid rgba(56,189,248,0.3)' }}>
                    <MousePointerClick className="h-3 w-3" style={{ color: '#0369a1' }} />
                    <span style={{ color: '#0369a1' }} className="font-bold">
                      Cursor at ({activeCursorMark.x}, {activeCursorMark.y})
                    </span>
                    <span className="text-slate-500">
                      · t = {(activeCursorMark.timestampMs / 1000).toFixed(2)}s
                    </span>
                  </div>
                )}
              </div>
            </div>
          </div>
        )}
      </div>
        </>
      )}
    </div>
  );
}
