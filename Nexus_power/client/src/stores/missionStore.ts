// ═══════════════════════════════════════════════════════════════
//  NEXUS AI ENGINE FACTORY — Mission Store (Zustand)
// ═══════════════════════════════════════════════════════════════
import { create } from 'zustand';
import type {
  Mission,
  MissionSummary,
  MissionStage,
  MissionArtifact,
  MissionMessage,
  MissionDashboard,
  MissionListResponse,
  CreateMissionRequest,
  UpdateMissionRequest,
  SendMessageResponse,
  AdvanceStageResponse,
} from '../types';
import api from '../services/api';

// ── State & Actions ─────────────────────────────────────────

interface MissionState {
  // List view
  missions: MissionSummary[];
  totalMissions: number;
  listOffset: number;
  listLimit: number;
  statusFilter: string | undefined;
  personaFilter: string | undefined;
  // Detail view
  activeMission: Mission | null;
  activeStages: MissionStage[];
  activeMessages: MissionMessage[];
  activeArtifacts: MissionArtifact[];
  // Dashboard
  dashboard: MissionDashboard | null;
  // Loading / error
  isLoadingList: boolean;
  isLoadingDetail: boolean;
  isLoadingDashboard: boolean;
  isSendingMessage: boolean;
  error: string | null;
}

interface MissionActions {
  // List
  fetchMissions: (params?: {
    status?: string;
    persona_id?: string;
    limit?: number;
    offset?: number;
  }) => Promise<void>;
  setFilters: (filters: { status?: string; persona_id?: string }) => void;
  setPage: (offset: number) => void;
  // Detail
  fetchMission: (missionId: string) => Promise<void>;
  clearActiveMission: () => void;
  // CRUD
  createMission: (body: CreateMissionRequest) => Promise<Mission>;
  updateMission: (missionId: string, body: UpdateMissionRequest) => Promise<void>;
  deleteMission: (missionId: string) => Promise<void>;
  // Stages
  fetchStages: (missionId: string) => Promise<void>;
  startStage: (missionId: string, stageNumber: number, inputs?: Record<string, unknown>) => Promise<void>;
  completeStage: (missionId: string, stageNumber: number, outputs?: Record<string, unknown>) => Promise<void>;
  advanceMission: (missionId: string, skipCurrent?: boolean) => Promise<AdvanceStageResponse>;
  // Artifacts
  fetchArtifacts: (missionId: string) => Promise<void>;
  addArtifact: (
    missionId: string,
    body: { artifact_type: string; name: string; description?: string; content_json?: Record<string, unknown>; content_text?: string; item_count?: number },
  ) => Promise<MissionArtifact>;
  // Messages
  fetchMessages: (missionId: string) => Promise<void>;
  sendMessage: (missionId: string, content: string, contentType?: string) => Promise<SendMessageResponse>;
  // Dashboard
  fetchDashboard: () => Promise<void>;
  // Reset
  clearMissions: () => void;
}

export type MissionStore = MissionState & MissionActions;

// ── Selectors ───────────────────────────────────────────────

export const selectActiveStage = (state: MissionStore) =>
  state.activeStages.find((s) => s.status === 'active') ?? null;

export const selectCurrentStageNumber = (state: MissionStore) =>
  state.activeMission?.current_stage ?? 1;

export const selectCompletedStages = (state: MissionStore) =>
  state.activeStages.filter((s) => s.status === 'completed');

export const selectMissionProgress = (state: MissionStore) =>
  state.activeMission?.progress_pct ?? 0;

export const selectStageArtifacts = (stageNumber: number) => (state: MissionStore) =>
  state.activeArtifacts.filter((a) => {
    const stage = state.activeStages.find((s) => s.stage_number === stageNumber);
    return stage && a.stage_id === stage.stage_id;
  });

// ── Store ───────────────────────────────────────────────────

const INITIAL_STATE: MissionState = {
  missions: [],
  totalMissions: 0,
  listOffset: 0,
  listLimit: 20,
  statusFilter: undefined,
  personaFilter: undefined,
  activeMission: null,
  activeStages: [],
  activeMessages: [],
  activeArtifacts: [],
  dashboard: null,
  isLoadingList: false,
  isLoadingDetail: false,
  isLoadingDashboard: false,
  isSendingMessage: false,
  error: null,
};

export const useMissionStore = create<MissionStore>()((set, get) => ({
  ...INITIAL_STATE,

  // ── List ────────────────────────────────────────────────

  fetchMissions: async (params) => {
    const state = get();
    const query = {
      status: params?.status ?? state.statusFilter,
      persona_id: params?.persona_id ?? state.personaFilter,
      limit: params?.limit ?? state.listLimit,
      offset: params?.offset ?? state.listOffset,
    };
    set({ isLoadingList: true, error: null });
    try {
      const resp: MissionListResponse = await api.listMissions(query);
      set({
        missions: resp.missions ?? [],
        totalMissions: resp.total ?? 0,
        isLoadingList: false,
      });
    } catch (err: any) {
      set({ isLoadingList: false, error: err?.message || 'Failed to fetch missions' });
    }
  },

  setFilters: (filters) =>
    set((state) => ({
      statusFilter: filters.status ?? state.statusFilter,
      personaFilter: filters.persona_id ?? state.personaFilter,
      listOffset: 0, // Reset to first page on filter change
    })),

  setPage: (offset) => set({ listOffset: offset }),

  // ── Detail ──────────────────────────────────────────────

  fetchMission: async (missionId) => {
    set({ isLoadingDetail: true, error: null });
    try {
      const data = await api.getMission(missionId);
      set({
        activeMission: data,
        activeStages: data.stages ?? [],
        activeMessages: data.messages ?? [],
        isLoadingDetail: false,
      });
    } catch (err: any) {
      set({ isLoadingDetail: false, error: err?.message || 'Failed to fetch mission' });
    }
  },

  clearActiveMission: () =>
    set({ activeMission: null, activeStages: [], activeMessages: [], activeArtifacts: [] }),

  // ── CRUD ────────────────────────────────────────────────

  createMission: async (body) => {
    const mission = await api.createMission(body);
    set((state) => ({
      missions: [mission, ...state.missions],
      totalMissions: state.totalMissions + 1,
    }));
    return mission;
  },

  updateMission: async (missionId, body) => {
    const updated = await api.updateMission(missionId, body);
    set((state) => ({
      missions: state.missions.map((m) => (m.mission_id === missionId ? { ...m, ...updated } : m)),
      activeMission:
        state.activeMission?.mission_id === missionId
          ? { ...state.activeMission, ...updated }
          : state.activeMission,
    }));
  },

  deleteMission: async (missionId) => {
    await api.deleteMission(missionId);
    set((state) => ({
      missions: state.missions.filter((m) => m.mission_id !== missionId),
      totalMissions: Math.max(0, state.totalMissions - 1),
      activeMission:
        state.activeMission?.mission_id === missionId ? null : state.activeMission,
    }));
  },

  // ── Stages ──────────────────────────────────────────────

  fetchStages: async (missionId) => {
    try {
      const data = await api.listMissionStages(missionId);
      set({ activeStages: Array.isArray(data) ? data : [] });
    } catch (err: any) {
      set({ error: err?.message || 'Failed to fetch stages' });
    }
  },

  startStage: async (missionId, stageNumber, inputs) => {
    const data = await api.startStage(missionId, stageNumber, inputs);
    // Refresh stages and mission to get updated status
    await Promise.all([get().fetchStages(missionId), get().fetchMission(missionId)]);
    return data;
  },

  completeStage: async (missionId, stageNumber, outputs) => {
    await api.completeStage(missionId, stageNumber, outputs);
    await Promise.all([get().fetchStages(missionId), get().fetchMission(missionId)]);
  },

  advanceMission: async (missionId, skipCurrent = false) => {
    const resp: AdvanceStageResponse = await api.advanceMission(missionId, {
      skip_current: skipCurrent,
    });
    await get().fetchMission(missionId);
    return resp;
  },

  // ── Artifacts ───────────────────────────────────────────

  fetchArtifacts: async (missionId) => {
    try {
      const data = await api.listMissionArtifacts(missionId);
      set({ activeArtifacts: Array.isArray(data) ? data : [] });
    } catch (err: any) {
      set({ error: err?.message || 'Failed to fetch artifacts' });
    }
  },

  addArtifact: async (missionId, body) => {
    const artifact = await api.addMissionArtifact(missionId, body);
    set((state) => ({ activeArtifacts: [...state.activeArtifacts, artifact] }));
    return artifact;
  },

  // ── Messages ────────────────────────────────────────────

  fetchMessages: async (missionId) => {
    try {
      const data = await api.listMissionMessages(missionId);
      set({ activeMessages: Array.isArray(data) ? data : [] });
    } catch (err: any) {
      set({ error: err?.message || 'Failed to fetch messages' });
    }
  },

  sendMessage: async (missionId, content, contentType = 'text') => {
    set({ isSendingMessage: true });
    try {
      const resp: SendMessageResponse = await api.sendMissionMessage(missionId, {
        content,
        content_type: contentType,
      });
      set((state) => ({
        activeMessages: [
          ...state.activeMessages,
          resp.user_message,
          resp.assistant_message,
        ],
        isSendingMessage: false,
      }));
      return resp;
    } catch (err: any) {
      set({ isSendingMessage: false, error: err?.message || 'Failed to send message' });
      throw err;
    }
  },

  // ── Dashboard ───────────────────────────────────────────

  fetchDashboard: async () => {
    set({ isLoadingDashboard: true, error: null });
    try {
      const data = await api.getMissionDashboard();
      set({ dashboard: data, isLoadingDashboard: false });
    } catch (err: any) {
      set({ isLoadingDashboard: false, error: err?.message || 'Failed to fetch dashboard' });
    }
  },

  // ── Reset ───────────────────────────────────────────────

  clearMissions: () => set(INITIAL_STATE),
}));
