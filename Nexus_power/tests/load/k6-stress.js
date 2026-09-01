/*
 * Nexus QA — K6 Stress Test Suite
 *
 * Finds the system breaking point by progressively increasing load
 * far beyond normal capacity. Used for capacity planning, NOT for
 * CI/CD gating (use k6-load.js for that).
 *
 * Three phases:
 *   1. Ramp up to 2x normal capacity
 *   2. Push to 5x normal capacity (find degradation point)
 *   3. Push to 10x (find failure point)
 *
 * Usage:
 *   k6 run tests/load/k6-stress.js
 *   k6 run tests/load/k6-stress.js --env BASE_URL=http://staging:8080
 */

import http from 'k6/http';
import { check, sleep, group } from 'k6';
import { Rate, Trend, Counter } from 'k6/metrics';

const errorRate = new Rate('nexus_errors');
const responseTime = new Trend('nexus_response_time', true);
const requestsPerSecond = new Counter('nexus_rps');

const BASE_URL = __ENV.BASE_URL || 'http://localhost:8080';

export const options = {
  stages: [
    // Phase 1: Warm up to baseline (100 VUs = normal peak)
    { duration: '1m', target: 50 },
    { duration: '2m', target: 100 },

    // Phase 2: 2x capacity (200 VUs)
    { duration: '2m', target: 200 },
    { duration: '3m', target: 200 },  // hold

    // Phase 3: 5x capacity (500 VUs)
    { duration: '2m', target: 500 },
    { duration: '3m', target: 500 },  // hold

    // Phase 4: 10x capacity (1000 VUs) — find failure
    { duration: '2m', target: 1000 },
    { duration: '3m', target: 1000 }, // hold

    // Recovery: ramp down
    { duration: '3m', target: 50 },
    { duration: '1m', target: 0 },
  ],

  // No hard thresholds — this is exploration, not gating
  thresholds: {
    // Only alert if >50% errors (indicates total system collapse)
    nexus_errors: ['rate<0.50'],
    // P95 up to 10s (stress testing pushes beyond normal SLAs)
    http_req_duration: ['p(95)<10000'],
  },
};

let authToken = '';

export function setup() {
  const loginRes = http.post(
    `${BASE_URL}/api/v1/auth/login`,
    JSON.stringify({
      email: __ENV.TEST_EMAIL || 'loadtest@nexusqa.dev',
      password: __ENV.TEST_PASSWORD || 'LoadTest2026!',
    }),
    { headers: { 'Content-Type': 'application/json' } },
  );

  if (loginRes.status === 200) {
    try {
      const body = loginRes.json();
      return { token: body.access_token || body.token || '' };
    } catch (e) { /* parsing error */ }
  }
  return { token: '' };
}

export default function (data) {
  authToken = data.token;
  const headers = {
    'Content-Type': 'application/json',
    'Authorization': authToken ? `Bearer ${authToken}` : '',
  };

  // Simulate mixed traffic under stress
  const roll = Math.random();

  if (roll < 0.40) {
    // 40%: Read-heavy traffic (most common under stress)
    group('Read Operations', () => {
      let res = http.get(`${BASE_URL}/health`);
      responseTime.add(res.timings.duration);
      requestsPerSecond.add(1);
      check(res, { 'health ok': (r) => r.status === 200 }) || errorRate.add(1);

      res = http.get(`${BASE_URL}/api/v1/platform/sessions?limit=10`, { headers });
      responseTime.add(res.timings.duration);
      requestsPerSecond.add(1);
      check(res, { 'sessions ok': (r) => r.status < 500 }) || errorRate.add(1);
    });

  } else if (roll < 0.65) {
    // 25%: Search operations (backend-heavy)
    group('Search Operations', () => {
      const queries = ['compliance', 'safety', 'audit', 'quality', 'training'];
      const q = queries[Math.floor(Math.random() * queries.length)];

      const res = http.get(
        `${BASE_URL}/api/v1/backbone/search?q=${q}&limit=5`,
        { headers },
      );
      responseTime.add(res.timings.duration);
      requestsPerSecond.add(1);
      check(res, { 'search ok': (r) => r.status < 500 }) || errorRate.add(1);
    });

  } else if (roll < 0.85) {
    // 20%: Write operations (DB/Redis heavy)
    group('Write Operations', () => {
      const payload = JSON.stringify({
        title: `Stress Test ${__VU}-${__ITER}`,
        session_type: 'knowledge_transfer',
        sme_name: 'Stress VU',
      });

      const res = http.post(`${BASE_URL}/api/v1/platform/sessions`, payload, { headers });
      responseTime.add(res.timings.duration);
      requestsPerSecond.add(1);
      check(res, { 'create ok': (r) => r.status < 500 }) || errorRate.add(1);
    });

  } else {
    // 15%: Analytics/aggregate queries (CPU heavy)
    group('Analytics', () => {
      let res = http.get(`${BASE_URL}/api/v1/platform/stats`, { headers });
      responseTime.add(res.timings.duration);
      requestsPerSecond.add(1);
      check(res, { 'stats ok': (r) => r.status < 500 }) || errorRate.add(1);

      res = http.get(`${BASE_URL}/api/v1/platform/guardrails`, { headers });
      responseTime.add(res.timings.duration);
      requestsPerSecond.add(1);
      check(res, { 'guardrails ok': (r) => r.status < 500 }) || errorRate.add(1);
    });
  }

  sleep(Math.random() * 1.5 + 0.2); // 0.2-1.7s think time (faster than normal)
}

export function teardown(data) {
  console.log('Stress test complete — check results for degradation points');
}
