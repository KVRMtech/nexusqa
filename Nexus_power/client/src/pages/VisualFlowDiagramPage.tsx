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
}

function SceneCard3D({
  scene, controls, isSelected, onClick, stepNumber, color, appName, index,
  actionLabel, actionIcon: ActionIcon,
}: SceneCardProps) {
  const autoReadyCount = controls.filter((c) => c.automation_ready).length;
  const imgUrl = scene.representative_frame_asset_path
    ? api.getFrameImageUrl(scene.representative_frame_asset_path)
    : scene.representative_frame_id
      ? api.getFrameImageUrl(scene.representative_frame_id)
      : null;
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
              onError={(e) => { (e.target as HTMLImageElement).style.display = 'none'; }}
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
    | 'journey';
  const [viewMode, setViewMode] = useState<'storyboard' | 'journey'>(initialMode);
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
    api.getVisualEvidenceGraph(artifactId)
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
  const appNameMap = useMemo(() => {
    if (!graph) return new Map<string, string>();
    const m = new Map<string, string>();
    graph.app_instances.forEach((inst) => {
      let name = inst.app_name ?? inst.app_type ?? 'Application';
      if (name.length > 24) name = name.slice(0, 21) + '\u2026';
      m.set(inst.instance_id, name);
    });
    return m;
  }, [graph]);

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

  const flowGroups = useMemo(() => {
    if (!graph) return [];
    const flows = graph.flows ?? [];
    const groups: Array<{ flow: VisualFlow; scenes: VisualScene[]; edges: VisualFlowEdge[] }> = [];
    for (const flow of flows) {
      if (activeFlowId && flow.flow_id !== activeFlowId) continue;
      const scenes = graph.scenes.filter((s) => s.flow_id === flow.flow_id).sort((a, b) => a.scene_index - b.scene_index);
      const sceneIds = new Set(scenes.map((s) => s.scene_id));
      const edges = graph.edges.filter((e) => sceneIds.has(e.from_scene_id) && sceneIds.has(e.to_scene_id));
      if (scenes.length > 0) groups.push({ flow, scenes, edges });
    }
    if (!activeFlowId) {
      const flowSceneIds = new Set(flows.flatMap((f) => graph.scenes.filter((s) => s.flow_id === f.flow_id).map((s) => s.scene_id)));
      const unassigned = graph.scenes.filter((s) => !flowSceneIds.has(s.scene_id));
      if (unassigned.length > 0) {
        groups.push({
          flow: { flow_id: '__unassigned__', flow_label: 'Other Screens', scene_count: unassigned.length, is_noise: true } as VisualFlow,
          scenes: unassigned.sort((a, b) => a.scene_index - b.scene_index),
          edges: [],
        });
      }
    }
    return groups;
  }, [graph, activeFlowId]);

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
    return deriveActionLabel(
      selectedScene,
      selectedControls,
      selectedPrevScene,
      selectedIncomingEdge?.edge_type,
      selectedIncomingEdge,
    );
  }, [selectedScene, selectedControls, selectedPrevScene, selectedIncomingEdge]);

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

      {/* ─────────── VIEW MODE TABS (Storyboard vs 3D Journey) ────────────── */}
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

      {viewMode === 'journey' && (
        <>
      {/* ─────────── FLOW TABS ────────────── */}
      {graph.flows && graph.flows.length > 1 && (
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
            <span className="text-[10px] opacity-50">({graph.summary.total_scenes})</span>
          </button>
          {graph.flows.map((flow: VisualFlow, fi: number) => {
            const fc = FLOW_COLORS[fi % FLOW_COLORS.length];
            const isActive = activeFlowId === flow.flow_id;
            return (
              <button
                key={flow.flow_id}
                onClick={() => setActiveFlowId(flow.flow_id)}
                className={clsx(
                  'flex items-center gap-2.5 rounded-xl px-5 py-2.5 text-xs font-bold transition-all whitespace-nowrap',
                  isActive ? 'text-slate-900' : 'text-slate-500 hover:text-slate-800',
                  flow.is_noise && 'opacity-40',
                )}
                style={isActive ? {
                  background: `linear-gradient(135deg, ${fc.bg}, ${fc.bg.replace('0.15', '0.06')})`,
                  border: `1px solid ${fc.ring}`,
                  boxShadow: `0 4px 20px ${fc.bg}`,
                } : { border: '1px solid rgba(10,37,64,0.06)' }}
              >
                <span className="w-3.5 h-3.5 rounded-full shrink-0" style={{ background: `linear-gradient(135deg, ${fc.from}, ${fc.to})`, boxShadow: `0 0 8px rgba(${fc.glow},0.4)` }} />
                {flow.flow_label}
                {flow.is_interleaved && (
                  <span className="text-[8px] font-black px-1.5 py-0.5 rounded-full" style={{ background: 'rgba(245,158,11,0.15)', color: '#d97706', border: '1px solid rgba(245,158,11,0.3)' }}>
                    {flow.visit_count}× visits
                  </span>
                )}
                <span className="text-[10px] opacity-50">({flow.scene_count})</span>
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
                {(flowGroups.length > 1 || (graph.flows?.length ?? 0) > 1) && (
                  <div className="px-5 flex items-center gap-2.5 v5-card-enter">
                    <div className="h-8 w-1.5 rounded-full" style={{ background: `linear-gradient(180deg, ${fc.from}, ${fc.to})`, boxShadow: `0 0 12px rgba(${fc.glow},0.3)` }} />
                    <div>
                      <h2 className="text-sm font-black text-slate-900 flex items-center gap-1.5">
                        {flow.flow_label}
                        <ChevronRight className="h-3 w-3 text-slate-600" />
                      </h2>
                      <p className="text-[9px] text-slate-500 font-medium">
                        {flow.scene_count} screen{flow.scene_count !== 1 ? 's' : ''}
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
                      const { action, icon, quality } = deriveActionLabel(scene, ctrls, prevScene, incomingEdge?.edge_type, incomingEdge);

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
      {/* max-h was 280px which clipped the EvidenceStepsPanel out of view
          for any scene with >2 steps. Now 70vh + overflow-y-auto so the
          per-scene action timeline (Gender = Male etc.) is visible and
          scrollable. */}
      <div
        className={clsx(
          'shrink-0 transition-all duration-500 ease-out relative z-10',
          selectedScene ? 'max-h-[70vh] opacity-100 translate-y-0 overflow-y-auto' : 'max-h-0 opacity-0 translate-y-4 overflow-hidden',
        )}
        style={{
          borderTop: selectedScene ? '1px solid rgba(38,112,163,0.15)' : 'none',
          background: selectedScene ? 'linear-gradient(180deg, #ffffff, #f8fafc)' : 'transparent',
          backdropFilter: selectedScene ? 'blur(20px)' : 'none',
        }}
      >
        {selectedScene && (
          <div className="px-5 py-4 v5-card-enter">
            <div className="flex items-start gap-4">
              {/* Thumbnail with 3D tilt */}
              <div
                className="w-48 h-28 rounded-xl overflow-hidden shrink-0 relative group"
                style={{
                  background: 'linear-gradient(135deg, #f8fafc, #eef2f7)',
                  border: '2px solid rgba(38,112,163,0.2)',
                  boxShadow: '0 12px 40px rgba(10,37,64,0.12), 0 0 30px rgba(10,37,64,0.04)',
                  transform: 'none',
                }}
              >
                {(() => {
                  const url = selectedScene.representative_frame_asset_path
                    ? api.getFrameImageUrl(selectedScene.representative_frame_asset_path)
                    : selectedScene.representative_frame_id
                      ? api.getFrameImageUrl(selectedScene.representative_frame_id)
                      : null;
                  return url ? (
                    <img src={url} alt="" className="w-full h-full object-cover group-hover:scale-105 transition-transform duration-500" onError={(e) => { (e.target as HTMLImageElement).style.display = 'none'; }} />
                  ) : (
                    <div className="w-full h-full flex items-center justify-center"><MonitorPlay className="h-8 w-8 text-slate-600/40" /></div>
                  );
                })()}
                <div className="absolute inset-0 bg-gradient-to-t from-black/30 to-transparent" />
              </div>

              {/* Info */}
              <div className="flex-1 min-w-0 space-y-3">
                <div>
                  <h3 className="text-sm font-black text-slate-900">
                    {humanScreenName(selectedScene)}
                  </h3>
                  <div className="flex items-center gap-4 mt-2 text-xs text-slate-600">
                    <span className="flex items-center gap-1.5">
                      <Clock className="h-3.5 w-3.5 text-slate-600/60" />
                      {fmtMs(selectedScene.start_ms)} {'\u2192'} {fmtMs(selectedScene.end_ms)}
                    </span>
                    {selectedScene.detected_url && (
                      <span className="flex items-center gap-1.5 text-blue-400/80">
                        <ExternalLink className="h-3.5 w-3.5" />
                        {extractDomain(selectedScene.detected_url)}
                      </span>
                    )}
                  </div>
                  {selectedScene.detected_url && (
                    <p className="text-[10px] text-slate-600 mt-1.5 font-mono truncate">{selectedScene.detected_url}</p>
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
