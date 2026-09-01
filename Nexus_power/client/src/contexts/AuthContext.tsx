import React, { createContext, useContext, useState, useEffect, useCallback } from 'react';
import type { User } from '../types';
import api from '../services/api';

interface AuthState {
  user: User | null;
  token: string | null;
  isAuthenticated: boolean;
  isLoading: boolean;
}

interface AuthContextValue extends AuthState {
  login: (email: string, password: string) => Promise<void>;
  logout: () => void;
  register: (tenantName: string, email: string, password: string, name: string) => Promise<void>;
}

const AuthContext = createContext<AuthContextValue | undefined>(undefined);

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [state, setState] = useState<AuthState>({
    user: null,
    token: null,
    isAuthenticated: false,
    isLoading: true,
  });

  // Restore session from sessionStorage
  useEffect(() => {
    const token = sessionStorage.getItem('nexus_token');
    const userJson = sessionStorage.getItem('nexus_user');
    if (token && userJson) {
      try {
        const user = JSON.parse(userJson) as User;
        setState({ user, token, isAuthenticated: true, isLoading: false });
      } catch {
        sessionStorage.removeItem('nexus_token');
        sessionStorage.removeItem('nexus_user');
        setState((s) => ({ ...s, isLoading: false }));
      }
    } else {
      setState((s) => ({ ...s, isLoading: false }));
    }
  }, []);

  const login = useCallback(async (email: string, password: string) => {
    const data = await api.login(email, password);
    const u = data.user || data;
    const user: User = {
      user_id: u.user_id,
      tenant_id: u.tenant_id,
      email: u.email,
      name: u.name || email.split('@')[0],
      role: u.role || 'user',
      permissions: u.permissions || [],
    };
    sessionStorage.setItem('nexus_token', data.access_token);
    sessionStorage.setItem('nexus_user', JSON.stringify(user));
    setState({ user, token: data.access_token, isAuthenticated: true, isLoading: false });
  }, []);

  const register = useCallback(
    async (tenantName: string, email: string, password: string, name: string) => {
      await api.register(tenantName, email, password, name);
      // Auto-login after registration
      await login(email, password);
    },
    [login],
  );

  const logout = useCallback(() => {
    sessionStorage.removeItem('nexus_token');
    sessionStorage.removeItem('nexus_user');
    setState({ user: null, token: null, isAuthenticated: false, isLoading: false });
  }, []);

  return (
    <AuthContext.Provider value={{ ...state, login, logout, register }}>
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth() {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error('useAuth must be used within AuthProvider');
  return ctx;
}
