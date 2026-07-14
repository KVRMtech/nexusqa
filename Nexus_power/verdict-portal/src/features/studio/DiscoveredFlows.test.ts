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
});
