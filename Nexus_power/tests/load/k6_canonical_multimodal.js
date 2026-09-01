// k6_canonical_multimodal.js — the 100-tenant claim gate.
//
// Submits combined video+audio workflows from 100 simulated tenants
// concurrently for 10 minutes. This is the test we run before claiming
// "100+ clients × 1000+ requests."
//
// Until Phase 12 (DAG planner with parallel video+audio branches), each
// multimodal workflow runs video-then-audio serially. Expect wall times
// in the 15-25 minute range. After Phase 12, expect ~max(video, audio).

import http from 'k6/http';
import { check, sleep } from 'k6';
import { Trend, Counter, Rate } from 'k6/metrics';
import { mintBatch } from './generate_jwts.js';

const BASE_URL = __ENV.BASE_URL || 'http://localhost:8080';
const JWT_SECRET = __ENV.NEXUS_JWT_SECRET || 'test-secret-do-not-use-in-production';
const VU_COUNT = parseInt(__ENV.VUS || '100', 10);
const DURATION = __ENV.DURATION || '10m';
const POLL_INTERVAL_S = parseFloat(__ENV.POLL_INTERVAL_S || '15');
const MAX_WAIT_S = parseInt(__ENV.MAX_WAIT_S || '2400', 10);  // 40 min cap

// One fixture for the multimodal scenario — a 15-min screen recording
// with the audio track included. Larger fixture mix on demand.
const MULTIMODAL_FIXTURE = open(
  __ENV.MULTIMODAL_FIXTURE || './corpus/multimodal-15min.mp4',
  'b',
);

const e2eDuration = new Trend('canonical_multimodal_e2e_seconds', true);
const submitDuration = new Trend('canonical_multimodal_submit_seconds', true);
const workflowsCompleted = new Counter('canonical_multimodal_completed');
const workflowsFailed = new Counter('canonical_multimodal_failed');
const failRate = new Rate('canonical_multimodal_failure_rate');
const cacheHits = new Counter('canonical_multimodal_cache_hits');

export const options = {
  scenarios: {
    sustain: {
      executor: 'ramping-vus',
      startVUs: 0,
      stages: [
        { duration: '2m', target: VU_COUNT },
        { duration: DURATION, target: VU_COUNT },
        { duration: '2m', target: 0 },
      ],
      gracefulRampDown: '2m',
    },
  },
  thresholds: {
    // BEFORE Phase 12, multimodal is serial — wide threshold.
    // After Phase 12 lands, tighten to <900s p95.
    'canonical_multimodal_e2e_seconds': ['p(95)<2100'],
    'canonical_multimodal_failure_rate': ['rate<0.01'],
    'http_req_failed': ['rate<0.02'],
  },
};

export function setup() {
  console.log(`Provisioning ${VU_COUNT} tenant tokens...`);
  return { tokens: mintBatch(JWT_SECRET, VU_COUNT) };
}

export default function (data) {
  const tokenEntry = data.tokens[(__VU - 1) % data.tokens.length];
  const token = tokenEntry.token;

  // Session
  const sessionRes = http.post(
    `${BASE_URL}/v1/sessions`,
    JSON.stringify({ display_name: `loadtest-mm-${__VU}-${__ITER}` }),
    {
      headers: { 'Authorization': `Bearer ${token}`, 'Content-Type': 'application/json' },
      tags: { name: 'CreateSession' },
    },
  );
  if (!check(sessionRes, { 'session 2xx': (r) => r.status < 300 })) {
    failRate.add(1);
    return;
  }
  const sessionId = JSON.parse(sessionRes.body).session_id;

  // Submit (idempotency key changes each iteration — no cache hit)
  const submitStart = Date.now();
  const submitRes = http.post(
    `${BASE_URL}/v1/orchestrator/process`,
    {
      session_id: sessionId,
      video: http.file(MULTIMODAL_FIXTURE, 'multimodal.mp4', 'video/mp4'),
      processing_profile: 'fast',
    },
    {
      headers: {
        'Authorization': `Bearer ${token}`,
        'Idempotency-Key': `lt-mm-${__VU}-${__ITER}-${Date.now()}`,
      },
      tags: { name: 'SubmitCanonical' },
    },
  );
  submitDuration.add((Date.now() - submitStart) / 1000);

  if (!check(submitRes, { 'submit 2xx': (r) => r.status < 300 })) {
    failRate.add(1);
    return;
  }
  const submitBody = JSON.parse(submitRes.body);
  const workflowId = submitBody.workflow_id;
  const cached = submitBody.status === 'completed';
  if (cached) {
    cacheHits.add(1);
    e2eDuration.add(0);
    workflowsCompleted.add(1);
    failRate.add(0);
    return;
  }

  // Poll
  const start = Date.now();
  const deadline = start + MAX_WAIT_S * 1000;
  while (Date.now() < deadline) {
    const r = http.get(`${BASE_URL}/v1/workflows/${workflowId}`, {
      headers: { 'Authorization': `Bearer ${token}` },
      tags: { name: 'PollWorkflow' },
    });
    if (r.status === 200) {
      const status = (JSON.parse(r.body).status || '').toLowerCase();
      if (status === 'completed' || status === 'success') {
        e2eDuration.add((Date.now() - start) / 1000);
        workflowsCompleted.add(1);
        failRate.add(0);
        return;
      }
      if (status === 'failed' || status === 'cancelled' || status === 'quarantined') {
        workflowsFailed.add(1);
        failRate.add(1);
        return;
      }
    }
    sleep(POLL_INTERVAL_S);
  }
  workflowsFailed.add(1);
  failRate.add(1);
}

export function handleSummary(data) {
  // Emit a one-line summary suitable for pasting into LOAD_TEST_REPORT.md.
  const m = data.metrics;
  const p95 = m.canonical_multimodal_e2e_seconds && m.canonical_multimodal_e2e_seconds.values['p(95)'];
  const p99 = m.canonical_multimodal_e2e_seconds && m.canonical_multimodal_e2e_seconds.values['p(99)'];
  const failed = m.canonical_multimodal_failed && m.canonical_multimodal_failed.values.count;
  const completed = m.canonical_multimodal_completed && m.canonical_multimodal_completed.values.count;
  const cacheHit = m.canonical_multimodal_cache_hits && m.canonical_multimodal_cache_hits.values.count || 0;

  const summary = {
    timestamp: new Date().toISOString(),
    vu_count: VU_COUNT,
    duration: DURATION,
    completed,
    failed,
    cache_hits: cacheHit,
    p95_seconds: p95,
    p99_seconds: p99,
    failure_rate: failed / Math.max(completed + failed, 1),
  };

  return {
    'stdout': '\n────── canonical-multimodal load summary ──────\n'
      + JSON.stringify(summary, null, 2) + '\n',
    'results-multimodal-summary.json': JSON.stringify(summary, null, 2),
  };
}
