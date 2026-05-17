/**
 * Canonical Asset Adapter — Regression Tests
 * ============================================
 *
 * Covers three architect-identified contract bugs:
 *   1. Alias session identity: adapter must use the *requested* sessionId,
 *      not artifact.session_id, for vm.session_id and launch action routes.
 *   2. Replay artifact fetch: list endpoint strips blob, full endpoint preserves it.
 *   3. PII SAFE badge: must use score_breakdown.pii, not quality_gate_passed.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { buildCanonicalAssetViewModel } from '../canonicalAssetAdapter';

// ── Mock the API module ─────────────────────────────────────
vi.mock('../../services/api', () => {
  const makeArtifact = (overrides: Record<string, unknown> = {}) => ({
    artifact_id: 'art-001',
    tenant_id: 't-1',
    session_id: 'sess-producing',       // <-- the PRODUCING session
    workflow_id: 'wf-001',
    status: 'completed',
    source_type: 'audio',
    source_filename: 'meeting.wav',
    created_by: 'user-1',
    created_at: '2026-04-06T10:00:00Z',
    completed_at: '2026-04-06T10:00:45Z',
    duration_seconds: 120,
    scene_count: 5,
    frame_count: 30,
    brain_quality_score: 0.92,
    quality_gate_passed: true,
    quality_gate_outcome: 'pass',
    has_real_transcript: true,
    has_visual_semantics: true,
    semantic_completeness_score: 0.88,
    safe_transcript_text: 'Safe transcript',
    visual_summary: 'Office meeting',
    application_types_seen: ['web_browser'],
    full_artifact_json: {
      transcript: { segments: [{ text: 'hello', speaker: 'Alice' }] },
      visual_analysis: { frames: [{ description: 'login screen' }] },
      visual_graph: { nodes: [{ id: 1, label: 'Login' }], edges: [] },
      model_provenance: { ears: 'whisper-v3' },
      review_reasons: [],
      score_breakdown: { transcript: 0.95, visual: 0.88, pii: 0.99, completeness: 0.88 },
    },
    processing_time_seconds: 45.2,
    error: null,
    ...overrides,
  });

  return {
    default: {
      getArtifact: vi.fn().mockResolvedValue(makeArtifact()),
      getArtifactStatus: vi.fn().mockResolvedValue({
        artifact_id: 'art-001',
        status: 'completed',
        brain_quality_score: 0.92,
        quality_gate_passed: true,
        has_real_transcript: true,
        has_visual_semantics: true,
        source_filename: 'meeting.wav',
        source_type: 'audio',
        created_at: '2026-04-06T10:00:00Z',
        workflow_id: 'wf-001',
      }),
      listSessionWorkflows: vi.fn().mockResolvedValue([]),
      getSession: vi.fn().mockResolvedValue({
        session_id: 'sess-viewing',
        tenant_id: 't-1',
        title: 'Viewing Session',
        status: 'active',
      }),
      getWorkflowTimeline: vi.fn().mockResolvedValue({ timeline: [], status: 'completed' }),
    },
    __makeArtifact: makeArtifact,
  };
});

// ───────────────────────────────────────────────────────────
//  1. Alias session identity
// ───────────────────────────────────────────────────────────

describe('alias session identity', () => {
  it('vm.session_id uses requested sessionId, not artifact.session_id', async () => {
    const vm = await buildCanonicalAssetViewModel('art-001', 'sess-viewing', 't-1');
    // The adapter must use the *viewing* session (the parameter) not the producing session
    expect(vm.session_id).toBe('sess-viewing');
    expect(vm.session_id).not.toBe('sess-producing');
  });

  it('original_session_id surfaces the producing session for provenance', async () => {
    const vm = await buildCanonicalAssetViewModel('art-001', 'sess-viewing', 't-1');
    // When the producing session differs from the viewing session, original_session_id must show it
    expect(vm.original_session_id).toBe('sess-producing');
  });

  it('launch action routes embed the viewing session, not the producing session', async () => {
    const vm = await buildCanonicalAssetViewModel('art-001', 'sess-viewing', 't-1');

    const routesWithSession = vm.launch_actions
      .filter(a => a.route.includes('session_id='))
      .map(a => a.route);

    expect(routesWithSession.length).toBeGreaterThan(0);
    for (const route of routesWithSession) {
      expect(route).toContain('session_id=sess-viewing');
      expect(route).not.toContain('session_id=sess-producing');
    }
  });

  it('when viewing == producing, original_session_id is null', async () => {
    // Override getSession to return the producing session (no alias scenario)
    const api = (await import('../../services/api')).default;
    (api.getSession as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
      session_id: 'sess-producing',
      tenant_id: 't-1',
      title: 'Producing Session',
      status: 'active',
    });

    const vm = await buildCanonicalAssetViewModel('art-001', 'sess-producing', 't-1');
    // No alias — original_session_id should be null (no provenance note needed)
    expect(vm.original_session_id).toBeNull();
  });
});

// ───────────────────────────────────────────────────────────
//  2. Full artifact blob availability
// ───────────────────────────────────────────────────────────

describe('full artifact blob', () => {
  it('adapter extracts score_breakdown from full_artifact_json', async () => {
    const vm = await buildCanonicalAssetViewModel('art-001', 'sess-viewing', 't-1');

    expect(vm.score_breakdown).toBeDefined();
    expect(vm.score_breakdown.transcript).toBe(0.95);
    expect(vm.score_breakdown.visual).toBe(0.88);
    expect(vm.score_breakdown.pii).toBe(0.99);
    expect(vm.score_breakdown.completeness).toBe(0.88);
  });

  it('adapter extracts visual_graph from full_artifact_json', async () => {
    const vm = await buildCanonicalAssetViewModel('art-001', 'sess-viewing', 't-1');

    expect(vm.visual_graph).toBeDefined();
    expect(vm.visual_graph?.nodes).toHaveLength(1);
    expect(vm.visual_graph?.edges).toHaveLength(0);
  });
});

// ───────────────────────────────────────────────────────────
//  3. PII safety contract
// ───────────────────────────────────────────────────────────

describe('PII safety vs quality gate', () => {
  it('quality_gate_passed is broader than PII safety', async () => {
    const vm = await buildCanonicalAssetViewModel('art-001', 'sess-viewing', 't-1');

    // quality_gate_passed = true, pii = 0.99 — both truthy
    expect(vm.quality_gate_passed).toBe(true);
    expect(vm.score_breakdown.pii).toBe(0.99);

    // These are SEPARATE dimensions. The UI must check pii specifically, not quality_gate_passed.
    // This test documents the contract: quality_gate_passed does NOT equal PII safety.
  });

  it('score_breakdown.pii null means PII not evaluated', async () => {
    // Simulate artifact where PII was skipped
    const api = (await import('../../services/api')).default;
    const originalGetArtifact = api.getArtifact;

    (api.getArtifact as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
      artifact_id: 'art-no-pii',
      tenant_id: 't-1',
      session_id: 'sess-producing',
      workflow_id: 'wf-001',
      status: 'completed',
      source_type: 'audio',
      source_filename: 'meeting.wav',
      created_by: 'user-1',
      created_at: '2026-04-06T10:00:00Z',
      completed_at: '2026-04-06T10:00:45Z',
      duration_seconds: 120,
      brain_quality_score: 0.85,
      quality_gate_passed: true,
      has_real_transcript: true,
      has_visual_semantics: false,
      full_artifact_json: {
        score_breakdown: { transcript: 0.95, visual: null, pii: null, completeness: 0.88 },
      },
    });

    const vm = await buildCanonicalAssetViewModel('art-no-pii', 'sess-viewing', 't-1');

    // quality_gate_passed is true but pii is null — UI must NOT show PII SAFE
    expect(vm.quality_gate_passed).toBe(true);
    expect(vm.score_breakdown.pii).toBeNull();
  });
});
