/**
 * Product mode — controls which features are visible/active in the UI.
 *
 * "canonical" (default):  Single-product canonical processing only.
 * "full":                 Full historical platform (all zones, all modules).
 *
 * Set via VITE_PRODUCT_MODE environment variable.
 */

export type ProductMode = 'canonical' | 'full';

export const PRODUCT_MODE: ProductMode =
  (import.meta.env.VITE_PRODUCT_MODE as ProductMode) || 'canonical';

export const isCanonicalOnly = PRODUCT_MODE === 'canonical';

/**
 * Engines polled for health in each mode.
 * Canonical mode only checks the engines required by the canonical chain.
 */
export const CANONICAL_ENGINES = [
  'ears', 'eyes', 'shield', 'spine', 'brain',
] as const;

export const ALL_ENGINES = [
  'ears', 'eyes', 'shield', 'heart', 'backbone',
  'nerves', 'legs', 'hands', 'spine', 'mouth', 'brain',
] as const;

export const ACTIVE_ENGINES = isCanonicalOnly ? CANONICAL_ENGINES : ALL_ENGINES;

/**
 * Control-plane services required for the canonical stack to function.
 * These are checked separately from engines (different health endpoint pattern).
 * Gateway health is implicit (if API calls work, gateway is up), but we check
 * it explicitly for the health banner.
 */
export const CANONICAL_CONTROL_PLANE = [
  { name: 'gateway', healthPath: '/health' },
  { name: 'auth', healthPath: '/v1/auth/health' },
  { name: 'orchestrator', healthPath: '/v1/orchestrator/health' },
  { name: 'platform-api', healthPath: '/v1/sessions' },
] as const;

export const FULL_CONTROL_PLANE = CANONICAL_CONTROL_PLANE;

export const ACTIVE_CONTROL_PLANE = isCanonicalOnly ? CANONICAL_CONTROL_PLANE : FULL_CONTROL_PLANE;
