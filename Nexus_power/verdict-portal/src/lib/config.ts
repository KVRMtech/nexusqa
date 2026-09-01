/**
 * Env-driven runtime config. NOTHING here is a secret — the portal ships no
 * server credentials; the only authority it carries is the operator's own JWT,
 * entered at sign-in. Read from Vite's `import.meta.env` (see .env.example).
 */

/** Base origin of the QE-Central API. Empty = same-origin relative requests
 *  (dev proxy / prod nginx routes /api + /health). Trailing slash stripped. */
export const QEC_API_URL: string = (import.meta.env.VITE_QEC_API_URL ?? '').replace(/\/+$/, '');

/** Mock mode: the API client returns representative design data, no network. */
export const QEC_MOCK: boolean =
  import.meta.env.VITE_QEC_MOCK === '1' || import.meta.env.VITE_QEC_MOCK === 'true';

/** The JWT `aud` the QE-Central gate expects (displayed; the server verifies). */
export const QEC_AUDIENCE: string = import.meta.env.VITE_QEC_AUDIENCE ?? 'vkpower-verdict';
