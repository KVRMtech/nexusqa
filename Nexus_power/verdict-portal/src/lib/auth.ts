/* ══════════════════════════════════════════════════════════════════════════
   VKPower Verdict — auth: JWT + tenant context.

   QE-Central has no login endpoint of its own — it validates an HS256 JWT that
   is minted by platform-api (audience `vkpower-verdict`, a required `tenant_id`
   claim; see platform/qe-central/app/auth.py). So this module is a token-first
   surface: the operator PASTES a token (login stub), we decode its claims for
   display + tenant scoping, and store it in sessionStorage (cleared on logout /
   tab close). The signature is NEVER verified here — verification is the
   server's job; the portal only reads claims.

   The API client reads the active token + tenant SYNCHRONOUSLY from the same
   sessionStorage this module owns (getAuthToken / getActiveTenantId), so there
   is one source of truth and no React coupling in the transport layer.
   ════════════════════════════════════════════════════════════════════════ */
import {
  createContext,
  createElement,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
} from 'react';
import type { ReactNode } from 'react';

import { QEC_AUDIENCE, QEC_MOCK } from './config';

const TOKEN_KEY = 'verdict_token';
const TENANT_KEY = 'verdict_tenant';

/** Decoded, display-only view of a principal token. */
export interface AuthSession {
  token: string;
  sub: string;
  email: string;
  role: string;
  /** Active tenant used for scoping (defaults to the token's tenant_id). */
  tenantId: string;
  /** The tenant_id claim carried by the JWT (immutable for this token). */
  tokenTenantId: string;
  aud: string | null;
  /** Expiry (unix seconds), or null when the token carries no `exp`. */
  exp: number | null;
  availableTenants: string[];
  mock: boolean;
}

interface JwtClaims {
  sub?: string;
  email?: string;
  role?: string;
  tenant_id?: string;
  aud?: string | string[];
  exp?: number;
  [key: string]: unknown;
}

// ── Pure JWT helpers (no verification — display + scoping only) ─────────────

function b64urlToUtf8(segment: string): string {
  const pad = segment.length % 4 === 0 ? '' : '='.repeat(4 - (segment.length % 4));
  const b64 = segment.replace(/-/g, '+').replace(/_/g, '/') + pad;
  const binary = atob(b64);
  const bytes = Uint8Array.from(binary, (c) => c.charCodeAt(0));
  return new TextDecoder().decode(bytes);
}

/** Decode a JWT's payload claims, or null when it is not a well-formed JWT. */
export function decodeJwt(token: string): JwtClaims | null {
  const parts = token.split('.');
  if (parts.length !== 3) return null;
  try {
    const claims = JSON.parse(b64urlToUtf8(parts[1])) as JwtClaims;
    return claims && typeof claims === 'object' ? claims : null;
  } catch {
    return null;
  }
}

export class AuthError extends Error {}

function sessionFromToken(token: string, activeTenant?: string): AuthSession {
  const claims = decodeJwt(token);
  if (!claims) {
    throw new AuthError('That does not look like a JWT (expected three dot-separated segments).');
  }
  const tokenTenantId = String(claims.tenant_id ?? '').trim();
  if (!tokenTenantId) {
    // The QE-Central gate REJECTS an untenanted principal (auth.py:120-123).
    throw new AuthError('Token is missing a tenant_id claim — QE-Central requires a tenant-scoped principal.');
  }
  const aud = Array.isArray(claims.aud) ? claims.aud[0] ?? null : (claims.aud ?? null);
  return {
    token,
    sub: String(claims.sub ?? 'anonymous'),
    email: String(claims.email ?? ''),
    role: String(claims.role ?? 'viewer'),
    tenantId: (activeTenant && activeTenant.trim()) || tokenTenantId,
    tokenTenantId,
    aud: aud === null ? null : String(aud),
    exp: typeof claims.exp === 'number' ? claims.exp : null,
    availableTenants: Array.from(new Set([tokenTenantId, ...(activeTenant ? [activeTenant] : [])])),
    mock: false,
  };
}

/** True when the token's `exp` is in the past (with a small skew tolerance). */
export function isSessionExpired(session: AuthSession | null): boolean {
  if (!session || session.exp === null) return false;
  return session.exp * 1000 <= Date.now() - 5_000;
}

// ── Synchronous store (sessionStorage) — the transport layer reads this ─────

/** The bearer token the API client attaches, or null when signed out. */
export function getAuthToken(): string | null {
  try {
    return sessionStorage.getItem(TOKEN_KEY);
  } catch {
    return null;
  }
}

/** The active tenant id (an `X-Tenant-Id` hint on every request), or null. */
export function getActiveTenantId(): string | null {
  try {
    return sessionStorage.getItem(TENANT_KEY);
  } catch {
    return null;
  }
}

function persist(session: AuthSession): void {
  sessionStorage.setItem(TOKEN_KEY, session.token);
  sessionStorage.setItem(TENANT_KEY, session.tenantId);
}

function clearStore(): void {
  sessionStorage.removeItem(TOKEN_KEY);
  sessionStorage.removeItem(TENANT_KEY);
}

function restoreSession(): AuthSession | null {
  const token = getAuthToken();
  if (!token) return null;
  try {
    return sessionFromToken(token, getActiveTenantId() ?? undefined);
  } catch {
    clearStore();
    return null;
  }
}

// ── Mock session (demo / VITE_QEC_MOCK=1) ───────────────────────────────────

const MOCK_TENANTS = ['acme-life', 'guardian-mutual', 'northshore-fin'];

function base64url(input: string): string {
  return btoa(input).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}

/** Build a display-only, unsigned demo JWT so the portal runs without a backend. */
function makeMockToken(tenantId: string): string {
  const header = base64url(JSON.stringify({ alg: 'HS256', typ: 'JWT' }));
  const payload = base64url(
    JSON.stringify({
      sub: 'founder@vkpower.demo',
      email: 'founder@vkpower.demo',
      role: 'admin',
      tenant_id: tenantId,
      aud: QEC_AUDIENCE,
      exp: Math.floor(Date.now() / 1000) + 60 * 60 * 8,
    }),
  );
  return `${header}.${payload}.mock-signature-not-verified`;
}

function makeMockSession(tenantId: string = MOCK_TENANTS[0]): AuthSession {
  const session = sessionFromToken(makeMockToken(tenantId), tenantId);
  return { ...session, availableTenants: MOCK_TENANTS, mock: true };
}

// ── React context ───────────────────────────────────────────────────────────

export interface AuthContextValue {
  session: AuthSession | null;
  isAuthenticated: boolean;
  isLoading: boolean;
  isExpired: boolean;
  /** Login stub: decode + store a pasted JWT. Throws AuthError on a bad token. */
  loginWithToken: (token: string) => void;
  /** Sign in with a synthetic demo principal (mock mode). */
  loginMock: (tenantId?: string) => void;
  logout: () => void;
  /** Switch the active tenant (sent as an X-Tenant-Id hint + used by mock data). */
  switchTenant: (tenantId: string) => void;
}

const AuthContext = createContext<AuthContextValue | undefined>(undefined);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [session, setSession] = useState<AuthSession | null>(null);
  const [isLoading, setIsLoading] = useState(true);

  useEffect(() => {
    setSession(restoreSession());
    setIsLoading(false);
  }, []);

  const loginWithToken = useCallback((token: string) => {
    const next = sessionFromToken(token.trim());
    persist(next);
    setSession(next);
  }, []);

  const loginMock = useCallback((tenantId?: string) => {
    const next = makeMockSession(tenantId);
    persist(next);
    setSession(next);
  }, []);

  const logout = useCallback(() => {
    clearStore();
    setSession(null);
  }, []);

  const switchTenant = useCallback((tenantId: string) => {
    setSession((current) => {
      if (!current) return current;
      const next: AuthSession = {
        ...current,
        tenantId,
        availableTenants: Array.from(new Set([...current.availableTenants, tenantId])),
      };
      persist(next);
      return next;
    });
  }, []);

  const value = useMemo<AuthContextValue>(
    () => ({
      session,
      isAuthenticated: session !== null,
      isLoading,
      isExpired: isSessionExpired(session),
      loginWithToken,
      loginMock,
      logout,
      switchTenant,
    }),
    [session, isLoading, loginWithToken, loginMock, logout, switchTenant],
  );

  // `createElement` (not JSX) keeps this a .ts file — no TSX needed for a provider.
  return createElement(AuthContext.Provider, { value }, children);
}

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext);
  if (ctx === undefined) {
    throw new Error('useAuth must be used within an <AuthProvider>');
  }
  return ctx;
}

export { QEC_MOCK };
