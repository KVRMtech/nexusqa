/*
 * Nexus QA — K6 Load Test Suite
 *
 * Cloud-native load testing alternative to Locust.
 * k6 provides better distributed execution, built-in thresholds,
 * and native cloud integration (k6 Cloud, Grafana Cloud k6).
 *
 * Profiles:
 *   smoke    — 5 VUs, 30s  (pre-flight validation)
 *   load     — 100 VUs, 5m (production SLA gate)
 *   stress   — 500 VUs, 10m (breaking point)
 *   soak     — 50 VUs, 30m (memory leak detection)
 *
 * Usage:
 *   k6 run tests/load/k6-load.js                      # default: load profile
 *   k6 run tests/load/k6-load.js --env PROFILE=smoke  # smoke test
 *   k6 run tests/load/k6-load.js --env PROFILE=stress # stress test
 *   k6 run tests/load/k6-load.js --env BASE_URL=http://staging:8080
 */

import http from 'k6/http';
import { check, sleep, group } from 'k6';
import { Rate, Trend, Counter } from 'k6/metrics';

// ─── Custom Metrics ──────────────────────────────────────────
const errorRate = new Rate('nexus_errors');
const apiLatency = new Trend('nexus_api_latency', true);
const sessionCreateLatency = new Trend('nexus_session_create_latency', true);
const healthCheckLatency = new Trend('nexus_health_latency', true);
const knowledgeQueryLatency = new Trend('nexus_knowledge_query_latency', true);
const requestCount = new Counter('nexus_requests');

// ─── Configuration ───────────────────────────────────────────
const BASE_URL = __ENV.BASE_URL || 'http://localhost:8080';
const PROFILE = (__ENV.PROFILE || 'load').toLowerCase();

const PROFILES = {
  smoke: {
    stages: [
      { duration: '10s', target: 5 },
      { duration: '20s', target: 5 },
    ],
    thresholds: {
      http_req_duration: ['p(95)<2000'],
      nexus_errors: ['rate<0.1'],
    },
  },
  load: {
    stages: [
      { duration: '30s', target: 20 },   // ramp up
      { duration: '1m', target: 50 },     // mid load
      { duration: '2m', target: 100 },    // peak load
      { duration: '1m', target: 50 },     // ramp down
      { duration: '30s', target: 0 },     // drain
    ],
    thresholds: {
      http_req_duration: ['p(95)<500', 'p(99)<2000'],
      nexus_errors: ['rate<0.01'],
      nexus_api_latency: ['p(95)<500'],
      nexus_health_latency: ['p(99)<200'],
    },
  },
  stress: {
    stages: [
      { duration: '1m', target: 100 },
      { duration: '2m', target: 250 },
      { duration: '3m', target: 500 },
      { duration: '2m', target: 250 },
      { duration: '1m', target: 50 },
      { duration: '1m', target: 0 },
    ],
    thresholds: {
      http_req_duration: ['p(95)<3000'],
      nexus_errors: ['rate<0.05'],
    },
  },
  soak: {
    stages: [
      { duration: '2m', target: 50 },    // ramp up
      { duration: '26m', target: 50 },   // sustained load
      { duration: '2m', target: 0 },     // drain
    ],
    thresholds: {
      http_req_duration: ['p(95)<1000'],
      nexus_errors: ['rate<0.01'],
    },
  },
};

const profile = PROFILES[PROFILE] || PROFILES.load;

export const options = {
  stages: profile.stages,
  thresholds: profile.thresholds,
  noConnectionReuse: false,
  userAgent: 'NexusQA-K6-LoadTest/1.0',
};

// ─── Auth Helper ─────────────────────────────────────────────
let authToken = '';

function getAuthHeaders() {
  return {
    'Content-Type': 'application/json',
    'Authorization': authToken ? `Bearer ${authToken}` : '',
  };
}

export function setup() {
  // Authenticate once at the start
  const loginPayload = JSON.stringify({
    email: __ENV.TEST_EMAIL || 'loadtest@nexusqa.dev',
    password: __ENV.TEST_PASSWORD || 'LoadTest2026!',
  });

  const loginRes = http.post(`${BASE_URL}/api/v1/auth/login`, loginPayload, {
    headers: { 'Content-Type': 'application/json' },
  });

  if (loginRes.status === 200) {
    const body = loginRes.json();
    return { token: body.access_token || body.token || '' };
  }

  console.warn(`Login failed (${loginRes.status}), running tests without auth`);
  return { token: '' };
}

// ─── Main Test Scenario ──────────────────────────────────────
export default function (data) {
  authToken = data.token;
  const headers = getAuthHeaders();

  // Weighted scenarios — simulate realistic traffic mix
  const roll = Math.random();

  if (roll < 0.30) {
    healthChecks();
  } else if (roll < 0.55) {
    sessionOperations(headers);
  } else if (roll < 0.75) {
    knowledgeQueries(headers);
  } else if (roll < 0.88) {
    insightsAndAnalytics(headers);
  } else {
    adminOperations(headers);
  }

  sleep(Math.random() * 2 + 0.5); // 0.5-2.5s think time
}

// ─── Scenario: Health Checks (30%) ──────────────────────────
function healthChecks() {
  group('Health Checks', () => {
    // Gateway health
    let res = http.get(`${BASE_URL}/health`);
    healthCheckLatency.add(res.timings.duration);
    requestCount.add(1);
    check(res, {
      'gateway health 200': (r) => r.status === 200,
      'gateway healthy': (r) => {
        try { return r.json().status === 'healthy'; }
        catch (e) { return false; }
      },
    }) || errorRate.add(1);

    // Engine status
    res = http.get(`${BASE_URL}/api/v1/engines/status`, { headers: getAuthHeaders() });
    healthCheckLatency.add(res.timings.duration);
    requestCount.add(1);
    check(res, {
      'engine status 200': (r) => r.status === 200,
    }) || errorRate.add(1);
  });
}

// ─── Scenario: Session Operations (25%) ──────────────────────
function sessionOperations(headers) {
  group('Session Operations', () => {
    // List sessions
    let res = http.get(`${BASE_URL}/api/v1/platform/sessions`, { headers });
    apiLatency.add(res.timings.duration);
    requestCount.add(1);
    check(res, {
      'list sessions 200': (r) => r.status === 200,
      'list sessions <500ms': (r) => r.timings.duration < 500,
    }) || errorRate.add(1);

    // Create session
    const sessionPayload = JSON.stringify({
      title: `K6 Load Test Session ${Date.now()}`,
      session_type: 'knowledge_transfer',
      sme_name: 'K6 Test SME',
    });
    res = http.post(`${BASE_URL}/api/v1/platform/sessions`, sessionPayload, { headers });
    sessionCreateLatency.add(res.timings.duration);
    requestCount.add(1);
    check(res, {
      'create session 2xx': (r) => r.status >= 200 && r.status < 300,
      'create session <1000ms': (r) => r.timings.duration < 1000,
    }) || errorRate.add(1);

    // Get single session
    if (res.status >= 200 && res.status < 300) {
      try {
        const sessionId = res.json().session_id;
        if (sessionId) {
          res = http.get(`${BASE_URL}/api/v1/platform/sessions/${sessionId}`, { headers });
          apiLatency.add(res.timings.duration);
          requestCount.add(1);
          check(res, {
            'get session 200': (r) => r.status === 200,
          }) || errorRate.add(1);
        }
      } catch (e) { /* session ID not in response */ }
    }
  });
}

// ─── Scenario: Knowledge Queries (20%) ──────────────────────
function knowledgeQueries(headers) {
  group('Knowledge Queries', () => {
    // Search knowledge base
    const queries = [
      'compliance requirements',
      'audit procedures',
      'safety protocols',
      'quality standards',
      'training materials',
    ];
    const query = queries[Math.floor(Math.random() * queries.length)];

    let res = http.get(
      `${BASE_URL}/api/v1/backbone/search?q=${encodeURIComponent(query)}&limit=10`,
      { headers },
    );
    knowledgeQueryLatency.add(res.timings.duration);
    requestCount.add(1);
    check(res, {
      'knowledge search 2xx': (r) => r.status >= 200 && r.status < 300,
      'knowledge search <2000ms': (r) => r.timings.duration < 2000,
    }) || errorRate.add(1);

    // Get insights
    res = http.get(`${BASE_URL}/api/v1/backbone/insights`, { headers });
    apiLatency.add(res.timings.duration);
    requestCount.add(1);
    check(res, {
      'insights 2xx': (r) => r.status >= 200 && r.status < 300,
    }) || errorRate.add(1);
  });
}

// ─── Scenario: Insights & Analytics (13%) ────────────────────
function insightsAndAnalytics(headers) {
  group('Insights & Analytics', () => {
    // Dashboard stats
    let res = http.get(`${BASE_URL}/api/v1/platform/stats`, { headers });
    apiLatency.add(res.timings.duration);
    requestCount.add(1);
    check(res, {
      'stats 2xx': (r) => r.status >= 200 && r.status < 300,
    }) || errorRate.add(1);

    // Guardrails
    res = http.get(`${BASE_URL}/api/v1/platform/guardrails`, { headers });
    apiLatency.add(res.timings.duration);
    requestCount.add(1);
    check(res, {
      'guardrails 2xx': (r) => r.status >= 200 && r.status < 300,
    }) || errorRate.add(1);

    // Traceability
    res = http.get(`${BASE_URL}/api/v1/platform/traceability`, { headers });
    apiLatency.add(res.timings.duration);
    requestCount.add(1);
    check(res, {
      'traceability 2xx': (r) => r.status >= 200 && r.status < 300,
    }) || errorRate.add(1);
  });
}

// ─── Scenario: Admin Operations (12%) ────────────────────────
function adminOperations(headers) {
  group('Admin Operations', () => {
    // List users
    let res = http.get(`${BASE_URL}/api/v1/auth/users`, { headers });
    apiLatency.add(res.timings.duration);
    requestCount.add(1);
    check(res, {
      'list users 2xx': (r) => r.status >= 200 && r.status < 400,
    }) || errorRate.add(1);

    // Test management
    res = http.get(`${BASE_URL}/api/v1/platform/test-suites`, { headers });
    apiLatency.add(res.timings.duration);
    requestCount.add(1);
    check(res, {
      'test suites 2xx': (r) => r.status >= 200 && r.status < 400,
    }) || errorRate.add(1);
  });
}

// ─── Teardown ────────────────────────────────────────────────
export function teardown(data) {
  console.log('Load test complete');
}
