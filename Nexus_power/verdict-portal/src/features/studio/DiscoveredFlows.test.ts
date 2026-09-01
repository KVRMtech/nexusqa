/**
 * Never-green-wash guard for the Discovered Flows classifier.
 *
 * The one invariant that matters: a flow is 'proven' ONLY on a concrete, clean,
 * green run. Any ambiguity (no run, unknown status, flaky, a recent failure)
 * must NOT read as proven. These cases pin that so a refactor can't silently
 * promote an unproven flow to green.
 */
import { describe, expect, it } from 'vitest';

import { classify } from './DiscoveredFlows';

describe('DiscoveredFlows.classify — never green-wash', () => {
  it('no run record → candidate (never proven)', () => {
    expect(classify(undefined).status).toBe('candidate');
    expect(classify({}).status).toBe('candidate');
    expect(classify({ runs: [] }).status).toBe('candidate');
  });

  it('a clean green run → proven', () => {
    const r = classify({ runs: [{ status: 'passed', at: '2026-07-14T00:00:00Z' }], last_run_status: 'passed', last_run_at: '2026-07-14T00:00:00Z' });
    expect(r.status).toBe('proven');
  });

  it('accepts common pass spellings', () => {
    for (const s of ['passed', 'pass', 'success', 'green', 'ok', 'PASSED']) {
      expect(classify({ last_run_status: s }).status).toBe('proven');
    }
  });

  it('a passing but FLAKY run is NOT proven', () => {
    expect(classify({ last_run_status: 'passed', is_flaky: true, flake_rate_pct: 40 }).status).toBe('attention');
  });

  it('a passing run with consecutive failures is NOT proven', () => {
    expect(classify({ last_run_status: 'passed', consecutive_failures: 2 }).status).toBe('attention');
  });

  it('a failing / regressed run → attention', () => {
    expect(classify({ last_run_status: 'failed' }).status).toBe('attention');
    expect(classify({ last_run_status: 'regression' }).status).toBe('attention');
  });

  it('an unknown / empty status with run evidence is NOT proven', () => {
    expect(classify({ runs: [{ status: 'running', at: null }] }).status).toBe('attention');
  });

  it('every branch carries a human reason', () => {
    for (const s of [undefined, {}, { last_run_status: 'passed' }, { last_run_status: 'failed' }] as const) {
      expect(classify(s).reason.length).toBeGreaterThan(0);
    }
  });

  // F4/P1.4 — honest blame: a provably product-side oracle defect names ITSELF
  // as the cause, never the client's application — and the flow still needs
  // attention (it did fail; attribution is honesty, not absolution).
  it('a product-side script defect says so — and stays attention, never proven', () => {
    const r = classify({
      last_run_status: 'failed',
      consecutive_failures: 2,
      failure_attribution: {
        attribution: 'script_defect',
        cause: 'url_as_text_oracle',
        blame: 'product',
        detail: 'generated oracle asserts URL text no page renders',
        category: 'product_script_defect',
      },
    });
    expect(r.status).toBe('attention');
    expect(r.reason).toContain('product-side script defect');
    expect(r.reason).toContain('not an application failure');
  });

  // P0.1 — neutral-by-default: an UNATTRIBUTED failure never implicitly points
  // at the application; it says the cause is under analysis.
  it('an unattributed failure is neutral — cause under analysis, no implicit app blame', () => {
    const r = classify({ last_run_status: 'failed', failure_attribution: null });
    expect(r.status).toBe('attention');
    expect(r.reason).toContain('cause under analysis');
    expect(r.reason).not.toContain('product-side');
  });

  it('a passing-but-flaky run keeps the stability wording (no blame implied)', () => {
    const r = classify({ last_run_status: 'passed', is_flaky: true, flake_rate_pct: 40 });
    expect(r.status).toBe('attention');
    expect(r.reason).toContain('Ran but not clean');
  });

  // P1.4 — category wording: environment + application evidence phrased honestly.
  it('an environment-attributed failure says environment, not application', () => {
    const r = classify({
      last_run_status: 'failed',
      failure_attribution: {
        attribution: 'environment_confirmed', cause: 'target_unreachable',
        blame: 'environment', detail: '', category: 'environment',
      },
    });
    expect(r.reason).toContain('environment was unreachable');
    expect(r.reason).toContain('Not an application failure');
  });

  it('an application-attributed failure states the grounded evidence', () => {
    const r = classify({
      last_run_status: 'failed',
      failure_attribution: {
        attribution: 'application_defect_candidate', cause: 'grounded_navigation_broken',
        blame: 'application_defect', detail: '', category: 'application_defect',
      },
    });
    expect(r.reason).toContain('application change');
    expect(r.reason).toContain('grounded oracle broke');
  });

  // P0.3 — certification + quarantine surfacing.
  it('a quarantined flow says the product is repairing it — never the app', () => {
    const r = classify({
      quarantined: true,
      certification: {
        status: 'failed', at: '2026-07-24T05:00:00Z',
        attribution: {
          attribution: 'script_defect', cause: 'url_as_text_oracle',
          blame: 'product', detail: '', category: 'product_script_defect',
        },
      },
    });
    expect(r.status).toBe('attention');
    expect(r.reason).toContain('Quarantined');
    expect(r.reason).toContain('NOT implicated');
  });

  it('a certified never-client-run flow reads as certified candidate', () => {
    const r = classify({
      certification: { status: 'certified', at: '2026-07-24T05:00:00Z' },
    });
    expect(r.status).toBe('candidate');
    expect(r.reason).toContain('Certified');
  });

  it('a certification-found application regression is attention (real signal, never hidden)', () => {
    const r = classify({
      certification: {
        status: 'failed', at: '2026-07-24T05:00:00Z',
        attribution: {
          attribution: 'application_defect_candidate', cause: 'grounded_navigation_broken',
          blame: 'application_defect', detail: '', category: 'application_defect',
        },
      },
    });
    expect(r.status).toBe('attention');
    expect(r.reason).toContain('application regression');
  });

  // P0.2 — soft-oracle misses are visible on a proven run, never silent.
  it('soft oracle misses are surfaced on a proven run', () => {
    const r = classify({ last_run_status: 'passed', soft_oracle_misses: 2 });
    expect(r.status).toBe('proven');
    expect(r.reason).toContain('2 soft oracle hints recorded');
  });
});
