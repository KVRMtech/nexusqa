/** App — routing. Public /login; everything else lives inside the Shell. */
import type { ReactNode } from 'react';
import { Navigate, Route, Routes, useLocation } from 'react-router-dom';

import { useAuth } from './lib/auth';
import { Loading } from './components';
import Shell from './app/Shell';
import LoginPage from './app/LoginPage';
import FleetCommandCenter from './features/fleet';
import AppSituation from './features/situation';
import OnboardingWizard from './features/onboarding';

function FullPageLoading() {
  return (
    <div className="min-h-full flex items-center justify-center bg-bg">
      <Loading label="Initializing Verdict Command Center…" />
    </div>
  );
}

function RequireAuth({ children }: { children: ReactNode }) {
  const { isAuthenticated, isLoading } = useAuth();
  const location = useLocation();
  if (isLoading) return <FullPageLoading />;
  if (!isAuthenticated) return <Navigate to="/login" replace state={{ from: location.pathname }} />;
  return <>{children}</>;
}

export default function App() {
  const { isAuthenticated, isLoading } = useAuth();

  return (
    <Routes>
      <Route
        path="/login"
        element={isLoading ? <FullPageLoading /> : isAuthenticated ? <Navigate to="/" replace /> : <LoginPage />}
      />
      <Route
        element={
          <RequireAuth>
            <Shell />
          </RequireAuth>
        }
      >
        <Route index element={<FleetCommandCenter />} />
        <Route path="/apps/:id" element={<AppSituation />} />
        <Route path="/onboard" element={<OnboardingWizard />} />
      </Route>
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );
}
