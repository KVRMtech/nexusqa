import { lazy, Suspense } from 'react';
import { Routes, Route, Navigate } from 'react-router-dom';
import { useAuth } from './contexts/AuthContext';
import { RouteErrorBoundary } from './components/RouteErrorBoundary';
import { PageSkeleton } from './components/Skeleton';
import AppLayout from './layouts/AppLayout';
import { isCanonicalOnly } from './productMode';

// ── Eagerly loaded (auth) ──────────────────────────────────
import LoginPage from './pages/LoginPage';
import RegisterPage from './pages/RegisterPage';

// ── Lazy-loaded pages (code-split per route) ───────────────
// Canonical Processing (always active)
const SessionCommandPage = lazy(() => import('./pages/SessionCommandPage'));
const SessionReplayPage = lazy(() => import('./pages/SessionReplayPage'));
const CanonicalResultPage = lazy(() => import('./pages/CanonicalResultPage'));
const PersonaWorkspacePage = lazy(() => import('./pages/PersonaWorkspacePage'));
const TestStrategyWorkspacePage = lazy(() => import('./pages/TestStrategyWorkspacePage'));
const E2EArchitectWorkspacePage = lazy(() => import('./pages/E2EArchitectWorkspacePage'));
const BatchLightsOutPage = lazy(() => import('./pages/BatchLightsOutPage'));
const VisualFlowDiagramPage = lazy(() => import('./pages/VisualFlowDiagramPage'));
const VisualE2ETestsPage = lazy(() => import('./pages/VisualE2ETestsPage'));

// Legacy modules (loaded only in full mode)
const SMEProfilesPage = lazy(() => import('./pages/SMEProfilesPage'));
const KnowledgeGraphPage = lazy(() => import('./pages/KnowledgeGraphPage'));
const ContradictionRadarPage = lazy(() => import('./pages/ContradictionRadarPage'));
const AIConfidencePage = lazy(() => import('./pages/AIConfidencePage'));
const TraceabilityMatrixPage = lazy(() => import('./pages/TraceabilityMatrixPage'));
const TestExecutionCenterPage = lazy(() => import('./pages/TestExecutionCenterPage'));
const DataForgePage = lazy(() => import('./pages/DataForgePage'));
const ComplianceCockpitPage = lazy(() => import('./pages/ComplianceCockpitPage'));
const ExecutiveInsightsPage = lazy(() => import('./pages/ExecutiveInsightsPage'));
const AdminPage = lazy(() => import('./pages/AdminPage'));
const BrainDashboardPage = lazy(() => import('./pages/BrainDashboardPage'));
const MissionDashboardPage = lazy(() => import('./pages/MissionDashboardPage'));
const MissionDetailPage = lazy(() => import('./pages/MissionDetailPage'));
const PersonaGalleryPage = lazy(() => import('./pages/PersonaGalleryPage'));

// Utility pages
const NotFoundPage = lazy(() => import('./pages/NotFoundPage'));

// ── Suspense wrapper with route error boundary ─────────────
function LazyPage({ name, children }: { name: string; children: React.ReactNode }) {
  return (
    <RouteErrorBoundary pageName={name}>
      <Suspense fallback={<PageSkeleton />}>{children}</Suspense>
    </RouteErrorBoundary>
  );
}

function ProtectedRoute({ children }: { children: React.ReactNode }) {
  const { isAuthenticated, isLoading } = useAuth();
  if (isLoading) {
    return (
      <div className="flex h-screen items-center justify-center bg-[#f5f7fa]">
        <div className="flex flex-col items-center gap-4">
          <div className="h-10 w-10 animate-spin rounded-full border-4 border-nexus-500 border-t-transparent" />
          <p className="text-sm text-slate-500 animate-pulse">Initializing VKPower Engine Factory...</p>
        </div>
      </div>
    );
  }
  return isAuthenticated ? <>{children}</> : <Navigate to="/login" replace />;
}

export default function App() {
  return (
    <Routes>
      {/* Public routes */}
      <Route path="/login" element={<LoginPage />} />
      <Route path="/register" element={<RegisterPage />} />

      {/* Protected app — 12 Module Dashboard */}
      <Route
        path="/"
        element={
          <ProtectedRoute>
            <AppLayout />
          </ProtectedRoute>
        }
      >
        {/* ── Canonical Processing (always active) ─────────────── */}

        {/* Landing page: Canonical Sessions workspace */}
        <Route index element={<Navigate to="/sessions" replace />} />

        <Route path="sessions" element={<LazyPage name="Canonical Sessions"><SessionCommandPage /></LazyPage>} />
        <Route path="sessions/:sessionId/replay" element={<LazyPage name="Session Replay"><SessionReplayPage /></LazyPage>} />
        <Route path="sessions/:sessionId/canonical" element={<LazyPage name="Canonical Result"><CanonicalResultPage /></LazyPage>} />
        <Route path="sessions/:sessionId/persona-workspace" element={<LazyPage name="Persona Workspace"><PersonaWorkspacePage /></LazyPage>} />
        <Route path="sessions/:sessionId/test-strategy" element={<LazyPage name="Test Strategy"><TestStrategyWorkspacePage /></LazyPage>} />
        <Route path="sessions/:sessionId/e2e-architect" element={<LazyPage name="E2E Architect"><E2EArchitectWorkspacePage /></LazyPage>} />
        <Route path="sessions/:sessionId/batch-lights-out" element={<LazyPage name="Batch Lights-Out"><BatchLightsOutPage /></LazyPage>} />
        <Route path="sessions/:sessionId/visual-flow" element={<LazyPage name="Visual Flow Diagram"><VisualFlowDiagramPage /></LazyPage>} />
        <Route path="visual-e2e-tests" element={<LazyPage name="Visual E2E Tests"><VisualE2ETestsPage /></LazyPage>} />

        {/* ── Legacy modules (full mode only) ──────────────────── */}
        {!isCanonicalOnly && (
          <>
            <Route path="sme-profiles" element={<LazyPage name="SME Profiles"><SMEProfilesPage /></LazyPage>} />
            <Route path="knowledge-graph" element={<LazyPage name="Knowledge Graph"><KnowledgeGraphPage /></LazyPage>} />
            <Route path="contradictions" element={<LazyPage name="Contradiction Radar"><ContradictionRadarPage /></LazyPage>} />
            <Route path="ai-confidence" element={<LazyPage name="AI Confidence"><AIConfidencePage /></LazyPage>} />
            <Route path="traceability" element={<LazyPage name="Traceability Matrix"><TraceabilityMatrixPage /></LazyPage>} />
            <Route path="test-center" element={<LazyPage name="Test Center"><TestExecutionCenterPage /></LazyPage>} />
            <Route path="data-forge" element={<LazyPage name="Data Forge"><DataForgePage /></LazyPage>} />
            <Route path="compliance" element={<LazyPage name="Compliance Cockpit"><ComplianceCockpitPage /></LazyPage>} />
            <Route path="brain" element={<LazyPage name="Brain Dashboard"><BrainDashboardPage /></LazyPage>} />
            <Route path="admin" element={<LazyPage name="System Admin"><AdminPage /></LazyPage>} />
            <Route path="qi/missions" element={<LazyPage name="Mission Dashboard"><MissionDashboardPage /></LazyPage>} />
            <Route path="qi/missions/:missionId" element={<LazyPage name="Mission Detail"><MissionDetailPage /></LazyPage>} />
            <Route path="qi/personas" element={<LazyPage name="Persona Gallery"><PersonaGalleryPage /></LazyPage>} />
          </>
        )}
      </Route>

      {/* 404 — proper not-found page */}
      <Route path="*" element={<Suspense fallback={null}><NotFoundPage /></Suspense>} />
    </Routes>
  );
}
