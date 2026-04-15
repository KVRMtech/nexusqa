// ═══════════════════════════════════════════════════════════════
//  NEXUS AI ENGINE FACTORY — Store Barrel Export
// ═══════════════════════════════════════════════════════════════
export { useAuthStore, selectUser, selectTenantId, selectIsAuthenticated, selectIsLoading, selectAuthError } from './authStore';
export { useEngineStore, selectEngine, selectAllHealthy, selectHealthSummary } from './engineStore';
export { useNotificationStore, notify, notifySuccess, notifyError, notifyWarning, notifyInfo } from './notificationStore';
export { useSessionStore } from './sessionStore';
export { useUIStore } from './uiStore';
export { usePersonaStore, selectSystemPersonas, selectCustomPersonas, selectActivePersonas } from './personaStore';
export { useMissionStore, selectActiveStage, selectCurrentStageNumber, selectCompletedStages, selectMissionProgress, selectStageArtifacts } from './missionStore';

// Re-export types
export type { AuthStore } from './authStore';
export type { EngineStore, EngineName } from './engineStore';
export type { NotificationStore, Notification, NotificationType } from './notificationStore';
export type { SessionStore } from './sessionStore';
export type { UIStore } from './uiStore';
export type { PersonaStore } from './personaStore';
export type { MissionStore } from './missionStore';
