import { describe, it, expect } from 'vitest';
import type {
  Persona,
  Mission,
  MissionStage,
  MissionArtifact,
  MissionMessage,
  MissionDashboard,
  CreateMissionRequest,
  MissionListResponse,
  SendMessageResponse,
  AdvanceStageResponse,
} from '../../types/qi';
import { STAGE_TYPES, STAGE_LABELS, STAGE_DESCRIPTIONS } from '../../types/qi';

describe('QI Portal Types', () => {
  it('exports STAGE_TYPES with 5 stages', () => {
    expect(STAGE_TYPES).toHaveLength(5);
    expect(STAGE_TYPES).toContain('capture');
    expect(STAGE_TYPES).toContain('understand');
    expect(STAGE_TYPES).toContain('strategize');
    expect(STAGE_TYPES).toContain('generate');
    expect(STAGE_TYPES).toContain('validate');
  });

  it('exports STAGE_LABELS for stages 1-5', () => {
    expect(STAGE_LABELS[1]).toBe('Capture');
    expect(STAGE_LABELS[2]).toBe('Understand');
    expect(STAGE_LABELS[3]).toBe('Strategize');
    expect(STAGE_LABELS[4]).toBe('Generate');
    expect(STAGE_LABELS[5]).toBe('Validate');
  });

  it('exports STAGE_DESCRIPTIONS for stages 1-5', () => {
    expect(STAGE_DESCRIPTIONS[1]).toBeTruthy();
    expect(STAGE_DESCRIPTIONS[2]).toBeTruthy();
    expect(STAGE_DESCRIPTIONS[3]).toBeTruthy();
    expect(STAGE_DESCRIPTIONS[4]).toBeTruthy();
    expect(STAGE_DESCRIPTIONS[5]).toBeTruthy();
  });

  it('Persona type is structurally valid', () => {
    const persona: Persona = {
      persona_id: 'p-1',
      tenant_id: '__system__',
      name: 'Analyst',
      slug: 'qi-analyst',
      description: 'Test',
      avatar_icon: '🔬',
      system_prompt: 'You are...',
      capabilities: ['analysis'],
      stage_config: {
        capture: { engines: ['ears'], auto_advance: false },
      },
      specialty_domains: ['api'],
      is_system: true,
      is_active: true,
      sort_order: 1,
      created_at: '2024-01-01T00:00:00Z',
      updated_at: '2024-01-01T00:00:00Z',
    };
    expect(persona.persona_id).toBe('p-1');
    expect(persona.is_system).toBe(true);
  });

  it('Mission type is structurally valid', () => {
    const mission: Mission = {
      mission_id: 'm-1',
      tenant_id: 't-1',
      title: 'Test',
      description: '',
      objective: '',
      status: 'active',
      current_stage: 2,
      priority: 'high',
      tags: ['api'],
      context: {},
      summary: '',
      progress_pct: 35.5,
      created_at: '2024-01-01T00:00:00Z',
      updated_at: '2024-01-01T00:00:00Z',
    };
    expect(mission.status).toBe('active');
    expect(mission.progress_pct).toBe(35.5);
  });

  it('MissionDashboard type is structurally valid', () => {
    const dashboard: MissionDashboard = {
      total_missions: 10,
      status_counts: { draft: 2, active: 3, paused: 0, completed: 4, failed: 1, cancelled: 0 },
      stage_distribution: { capture: 1, understand: 2 },
      total_artifacts: 25,
      recent_missions: [],
    };
    expect(dashboard.total_missions).toBe(10);
  });

  it('CreateMissionRequest type is structurally valid', () => {
    const req: CreateMissionRequest = {
      title: 'New Mission',
      persona_id: 'p-1',
    };
    expect(req.title).toBe('New Mission');
  });
});
