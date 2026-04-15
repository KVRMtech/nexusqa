/// <reference types="vitest/globals" />
// ── Test utilities & shared mocks ─────────────────────────
import { render, RenderOptions } from '@testing-library/react';
import { BrowserRouter } from 'react-router-dom';
import { ReactElement } from 'react';

// Mock AuthContext
const mockUser = {
  user_id: 'u-1',
  email: 'admin@nexus.ai',
  name: 'Admin',
  role: 'admin',
  tenant_id: 't-1',
};

vi.mock('../contexts/AuthContext', () => ({
  useAuth: () => ({
    user: mockUser,
    isAuthenticated: true,
    loading: false,
    login: vi.fn(),
    logout: vi.fn(),
    register: vi.fn(),
  }),
}));

// Mock Zustand stores
vi.mock('../stores/authStore', () => ({
  useAuthStore: Object.assign(
    vi.fn(() => ({
      user: mockUser,
      token: 'mock-token',
      isAuthenticated: true,
      isLoading: false,
      error: null,
      login: vi.fn(),
      logout: vi.fn(),
      register: vi.fn(),
      clearError: vi.fn(),
      setUser: vi.fn(),
    })),
    { getState: vi.fn(() => ({ token: 'mock-token', user: mockUser })) },
  ),
  selectUser: (state: any) => state.user,
  selectIsAuthenticated: (state: any) => state.isAuthenticated,
  selectTenantId: (state: any) => state.user?.tenant_id,
}));

vi.mock('../stores/engineStore', () => ({
  useEngineStore: Object.assign(
    vi.fn(() => ({
      engines: {},
      lastUpdated: null,
      isPolling: false,
      allHealthy: true,
      healthyCount: 10,
      totalCount: 10,
      startPolling: vi.fn(),
      stopPolling: vi.fn(),
      updateEngine: vi.fn(),
      fetchAll: vi.fn(),
    })),
    { getState: vi.fn(() => ({ engines: {}, allHealthy: true })) },
  ),
}));

vi.mock('../stores/notificationStore', () => {
  const store = {
    notifications: [],
    unreadCount: 0,
    addNotification: vi.fn(),
    markRead: vi.fn(),
    markAllRead: vi.fn(),
    dismiss: vi.fn(),
    clearAll: vi.fn(),
  };
  return {
    useNotificationStore: Object.assign(vi.fn(() => store), { getState: vi.fn(() => store) }),
    notify: vi.fn(),
    notifySuccess: vi.fn(),
    notifyError: vi.fn(),
    notifyWarning: vi.fn(),
    notifyInfo: vi.fn(),
  };
});

// Mock PersonaStore
vi.mock('../stores/personaStore', () => {
  const store = {
    personas: [],
    selectedPersona: null,
    isLoading: false,
    error: null,
    fetchPersonas: vi.fn(),
    selectPersona: vi.fn(),
    createPersona: vi.fn(),
    updatePersona: vi.fn(),
    deletePersona: vi.fn(),
    getPersonaById: vi.fn(),
    clearPersonas: vi.fn(),
  };
  return {
    usePersonaStore: Object.assign(vi.fn(() => store), { getState: vi.fn(() => store) }),
    selectSystemPersonas: vi.fn(() => []),
    selectCustomPersonas: vi.fn(() => []),
    selectActivePersonas: vi.fn(() => []),
  };
});

// Mock MissionStore
vi.mock('../stores/missionStore', () => {
  const store = {
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
    fetchMissions: vi.fn(),
    setFilters: vi.fn(),
    setPage: vi.fn(),
    fetchMission: vi.fn(),
    clearActiveMission: vi.fn(),
    createMission: vi.fn(),
    updateMission: vi.fn(),
    deleteMission: vi.fn(),
    fetchStages: vi.fn(),
    startStage: vi.fn(),
    completeStage: vi.fn(),
    advanceMission: vi.fn(),
    fetchArtifacts: vi.fn(),
    addArtifact: vi.fn(),
    fetchMessages: vi.fn(),
    sendMessage: vi.fn(),
    fetchDashboard: vi.fn(),
    clearMissions: vi.fn(),
  };
  return {
    useMissionStore: Object.assign(vi.fn(() => store), { getState: vi.fn(() => store) }),
    selectActiveStage: vi.fn(() => null),
    selectCurrentStageNumber: vi.fn(() => 1),
    selectCompletedStages: vi.fn(() => []),
    selectMissionProgress: vi.fn(() => 0),
    selectStageArtifacts: vi.fn(() => vi.fn(() => [])),
  };
});

// Mock API — all calls return empty/fallback so pages use demo data
vi.mock('../services/api', () => ({
  default: {
    listSessions: vi.fn().mockRejectedValue(new Error('mock')),
    getSessionEvents: vi.fn().mockRejectedValue(new Error('mock')),
    getSessionTranscript: vi.fn().mockRejectedValue(new Error('mock')),
    listSMEProfiles: vi.fn().mockRejectedValue(new Error('mock')),
    searchKnowledge: vi.fn().mockRejectedValue(new Error('mock')),
    listContradictions: vi.fn().mockRejectedValue(new Error('mock')),
    getReviewQueue: vi.fn().mockRejectedValue(new Error('mock')),
    listTraces: vi.fn().mockRejectedValue(new Error('mock')),
    listTestSuites: vi.fn().mockRejectedValue(new Error('mock')),
    listTestRuns: vi.fn().mockRejectedValue(new Error('mock')),
    listDataForgeConfigs: vi.fn().mockRejectedValue(new Error('mock')),
    listDataForgeResults: vi.fn().mockRejectedValue(new Error('mock')),
    listComplianceJurisdictions: vi.fn().mockRejectedValue(new Error('mock')),
    getEngineStatus: vi.fn().mockRejectedValue(new Error('mock')),
    getWeeklyTrend: vi.fn().mockRejectedValue(new Error('mock')),
    getAdminEngines: vi.fn().mockRejectedValue(new Error('mock')),
    getAuditLog: vi.fn().mockRejectedValue(new Error('mock')),
    getAdminUsers: vi.fn().mockRejectedValue(new Error('mock')),
    getAdminIntegrations: vi.fn().mockRejectedValue(new Error('mock')),
    uploadAudio: vi.fn(),
    getGraphStats: vi.fn().mockRejectedValue(new Error('mock')),
    // QI Portal: Personas
    listPersonas: vi.fn().mockResolvedValue([]),
    getPersona: vi.fn().mockRejectedValue(new Error('mock')),
    createPersona: vi.fn().mockRejectedValue(new Error('mock')),
    updatePersona: vi.fn().mockRejectedValue(new Error('mock')),
    deletePersona: vi.fn().mockRejectedValue(new Error('mock')),
    // QI Portal: Missions
    listMissions: vi.fn().mockResolvedValue({ missions: [], total: 0, limit: 20, offset: 0 }),
    getMission: vi.fn().mockRejectedValue(new Error('mock')),
    getMissionDashboard: vi.fn().mockResolvedValue({
      total_missions: 0,
      status_counts: {},
      stage_distribution: {},
      total_artifacts: 0,
      recent_missions: [],
    }),
    createMission: vi.fn().mockRejectedValue(new Error('mock')),
    updateMission: vi.fn().mockRejectedValue(new Error('mock')),
    deleteMission: vi.fn().mockRejectedValue(new Error('mock')),
    listMissionStages: vi.fn().mockResolvedValue([]),
    getMissionStage: vi.fn().mockRejectedValue(new Error('mock')),
    startStage: vi.fn().mockRejectedValue(new Error('mock')),
    completeStage: vi.fn().mockRejectedValue(new Error('mock')),
    advanceMission: vi.fn().mockRejectedValue(new Error('mock')),
    listMissionArtifacts: vi.fn().mockResolvedValue([]),
    getMissionArtifact: vi.fn().mockRejectedValue(new Error('mock')),
    addMissionArtifact: vi.fn().mockRejectedValue(new Error('mock')),
    listMissionMessages: vi.fn().mockResolvedValue([]),
    sendMissionMessage: vi.fn().mockRejectedValue(new Error('mock')),
  },
  api: {
    listSessions: vi.fn().mockRejectedValue(new Error('mock')),
  },
}));

/**
 * Render with BrowserRouter wrapper for components that use react-router.
 */
export function renderWithRouter(ui: ReactElement, options?: RenderOptions) {
  return render(ui, {
    wrapper: ({ children }) => <BrowserRouter>{children}</BrowserRouter>,
    ...options,
  });
}

export { mockUser };
