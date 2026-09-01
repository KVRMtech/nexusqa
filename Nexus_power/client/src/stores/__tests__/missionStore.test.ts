import { describe, it, expect, beforeEach, vi } from 'vitest';
import { act } from '@testing-library/react';

vi.unmock('../../stores/missionStore');

// Mock the api module
vi.mock('../../services/api', () => ({
  default: {
    listMissions: vi.fn().mockResolvedValue({ missions: [], total: 0, limit: 20, offset: 0 }),
    getMission: vi.fn().mockResolvedValue({
      mission_id: 'm-1',
      title: 'Test Mission',
      stages: [],
      messages: [],
    }),
    getMissionDashboard: vi.fn().mockResolvedValue({
      total_missions: 5,
      status_counts: { active: 2, completed: 3 },
      stage_distribution: {},
      total_artifacts: 10,
      recent_missions: [],
    }),
    createMission: vi.fn().mockResolvedValue({ mission_id: 'm-new', title: 'New Mission' }),
    updateMission: vi.fn().mockResolvedValue({ mission_id: 'm-1', title: 'Updated' }),
    deleteMission: vi.fn().mockResolvedValue({}),
    listMissionStages: vi.fn().mockResolvedValue([]),
    startStage: vi.fn().mockResolvedValue({}),
    completeStage: vi.fn().mockResolvedValue({}),
    advanceMission: vi.fn().mockResolvedValue({ advanced: true, current_stage: 2 }),
    listMissionArtifacts: vi.fn().mockResolvedValue([]),
    addMissionArtifact: vi.fn().mockResolvedValue({ artifact_id: 'a-1', name: 'Test' }),
    listMissionMessages: vi.fn().mockResolvedValue([]),
    sendMissionMessage: vi.fn().mockResolvedValue({
      user_message: { message_id: 'msg-1', role: 'user', content: 'Hello' },
      assistant_message: { message_id: 'msg-2', role: 'assistant', content: 'Hi' },
    }),
  },
}));

let useMissionStore: any;

beforeEach(async () => {
  vi.resetModules();
  const module = await import('../../stores/missionStore');
  useMissionStore = module.useMissionStore;
});

describe('missionStore', () => {
  it('starts with initial state', () => {
    const state = useMissionStore.getState();
    expect(state.missions).toEqual([]);
    expect(state.totalMissions).toBe(0);
    expect(state.activeMission).toBeNull();
    expect(state.dashboard).toBeNull();
    expect(state.isLoadingList).toBe(false);
    expect(state.isLoadingDetail).toBe(false);
  });

  it('fetchMissions updates mission list', async () => {
    const api = (await import('../../services/api')).default;
    (api.listMissions as any).mockResolvedValueOnce({
      missions: [{ mission_id: 'm-1', title: 'Mission 1' }],
      total: 1,
      limit: 20,
      offset: 0,
    });

    await act(async () => {
      await useMissionStore.getState().fetchMissions();
    });

    const state = useMissionStore.getState();
    expect(state.missions).toHaveLength(1);
    expect(state.totalMissions).toBe(1);
    expect(state.isLoadingList).toBe(false);
  });

  it('fetchMissions handles error', async () => {
    const api = (await import('../../services/api')).default;
    (api.listMissions as any).mockRejectedValueOnce(new Error('fail'));

    await act(async () => {
      await useMissionStore.getState().fetchMissions();
    });

    expect(useMissionStore.getState().error).toBeTruthy();
    expect(useMissionStore.getState().isLoadingList).toBe(false);
  });

  it('fetchMission loads detail', async () => {
    await act(async () => {
      await useMissionStore.getState().fetchMission('m-1');
    });

    const state = useMissionStore.getState();
    expect(state.activeMission).not.toBeNull();
    expect(state.activeMission.mission_id).toBe('m-1');
    expect(state.isLoadingDetail).toBe(false);
  });

  it('fetchDashboard loads dashboard data', async () => {
    await act(async () => {
      await useMissionStore.getState().fetchDashboard();
    });

    const state = useMissionStore.getState();
    expect(state.dashboard).not.toBeNull();
    expect(state.dashboard.total_missions).toBe(5);
    expect(state.dashboard.total_artifacts).toBe(10);
    expect(state.isLoadingDashboard).toBe(false);
  });

  it('createMission adds to list', async () => {
    await act(async () => {
      await useMissionStore.getState().createMission({
        title: 'New Mission',
        persona_id: 'p-1',
      });
    });

    const state = useMissionStore.getState();
    expect(state.missions.some((m: any) => m.mission_id === 'm-new')).toBe(true);
    expect(state.totalMissions).toBe(1);
  });

  it('deleteMission removes from list', async () => {
    const api = (await import('../../services/api')).default;
    (api.listMissions as any).mockResolvedValueOnce({
      missions: [
        { mission_id: 'm-1', title: 'A' },
        { mission_id: 'm-2', title: 'B' },
      ],
      total: 2,
    });

    await act(async () => {
      await useMissionStore.getState().fetchMissions();
    });
    expect(useMissionStore.getState().missions).toHaveLength(2);

    await act(async () => {
      await useMissionStore.getState().deleteMission('m-1');
    });
    expect(useMissionStore.getState().missions).toHaveLength(1);
  });

  it('sendMessage appends both user and assistant messages', async () => {
    await act(async () => {
      await useMissionStore.getState().sendMessage('m-1', 'Hello');
    });

    const state = useMissionStore.getState();
    expect(state.activeMessages).toHaveLength(2);
    expect(state.activeMessages[0].role).toBe('user');
    expect(state.activeMessages[1].role).toBe('assistant');
    expect(state.isSendingMessage).toBe(false);
  });

  it('setFilters resets offset', () => {
    act(() => {
      useMissionStore.getState().setPage(40);
    });
    expect(useMissionStore.getState().listOffset).toBe(40);

    act(() => {
      useMissionStore.getState().setFilters({ status: 'active' });
    });
    expect(useMissionStore.getState().statusFilter).toBe('active');
    expect(useMissionStore.getState().listOffset).toBe(0);
  });

  it('clearActiveMission resets detail state', () => {
    act(() => {
      useMissionStore.setState({ activeMission: { mission_id: 'm-1' } });
    });
    expect(useMissionStore.getState().activeMission).not.toBeNull();

    act(() => {
      useMissionStore.getState().clearActiveMission();
    });
    expect(useMissionStore.getState().activeMission).toBeNull();
    expect(useMissionStore.getState().activeStages).toEqual([]);
    expect(useMissionStore.getState().activeMessages).toEqual([]);
  });

  it('clearMissions resets all state', () => {
    act(() => {
      useMissionStore.setState({ totalMissions: 5, statusFilter: 'active' });
    });

    act(() => {
      useMissionStore.getState().clearMissions();
    });

    const state = useMissionStore.getState();
    expect(state.missions).toEqual([]);
    expect(state.totalMissions).toBe(0);
    expect(state.statusFilter).toBeUndefined();
  });

  it('addArtifact appends to active artifacts', async () => {
    await act(async () => {
      await useMissionStore.getState().addArtifact('m-1', {
        artifact_type: 'test_cases',
        name: 'Login Tests',
      });
    });

    const state = useMissionStore.getState();
    expect(state.activeArtifacts).toHaveLength(1);
    expect(state.activeArtifacts[0].artifact_id).toBe('a-1');
  });
});
