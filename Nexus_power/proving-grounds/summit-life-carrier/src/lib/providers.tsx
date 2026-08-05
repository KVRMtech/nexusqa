'use client';

import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { useState, createContext, useContext, useEffect, type ReactNode } from 'react';
import type { AdminUser } from './types';
import { adminUsers } from './data';

const queryClient = new QueryClient({
  defaultOptions: { queries: { staleTime: 30_000, refetchOnWindowFocus: false } },
});

interface AuthContextValue {
  user: AdminUser | null;
  isAuthenticated: boolean;
  login: (email: string) => void;
  logout: () => void;
}

const AuthContext = createContext<AuthContextValue>({
  user: null,
  isAuthenticated: false,
  login: () => {},
  logout: () => {},
});

export function useAuth() {
  return useContext(AuthContext);
}

function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<AdminUser | null>(null);

  useEffect(() => {
    const stored = sessionStorage.getItem('slc_current_user');
    if (stored) {
      try { setUser(JSON.parse(stored)); } catch { /* ignore */ }
    }
  }, []);

  const login = (email: string) => {
    const found = adminUsers.find(u => u.email === email) || adminUsers[0];
    setUser(found);
    sessionStorage.setItem('slc_current_user', JSON.stringify(found));
    sessionStorage.setItem('slc_access_token', `eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9.${btoa(JSON.stringify({ sub: found.id, email: found.email, iat: Math.floor(Date.now() / 1000) }))}.mock-signature`);
  };

  const logout = () => {
    setUser(null);
    sessionStorage.removeItem('slc_current_user');
    sessionStorage.removeItem('slc_access_token');
  };

  return (
    <AuthContext.Provider value={{ user, isAuthenticated: !!user, login, logout }}>
      {children}
    </AuthContext.Provider>
  );
}

export function Providers({ children }: { children: ReactNode }) {
  const [qc] = useState(() => queryClient);
  return (
    <QueryClientProvider client={qc}>
      <AuthProvider>{children}</AuthProvider>
    </QueryClientProvider>
  );
}
