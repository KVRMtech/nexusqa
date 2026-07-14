/* Auth shim for the ported Test Studio panels.

   The video portal's panels read `const { user } = useAuth()` (a role-bearing
   principal). The verdict portal models the same principal as `session` on its
   own auth context (src/lib/auth.ts). This adapter exposes the video-portal
   shape over the verdict session so the panels port UNCHANGED — one source of
   truth, no second auth store. */
import { useAuth as useVerdictAuth } from '../lib/auth';

export interface StudioUser {
  role: string;
  email: string;
  sub: string;
  /** Stable principal id — the panels use it to scope per-user UI prefs. */
  user_id: string;
  tenantId: string;
}

export function useAuth(): { user: StudioUser | null } {
  const { session } = useVerdictAuth();
  return {
    user: session
      ? {
          role: session.role,
          email: session.email,
          sub: session.sub,
          user_id: session.sub,
          tenantId: session.tenantId,
        }
      : null,
  };
}

export default useAuth;
