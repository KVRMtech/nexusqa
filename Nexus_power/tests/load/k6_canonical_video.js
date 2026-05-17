// k6_canonical_video.js — video-only workflow load test.
//
// Same shape as k6_canonical_audio.js but submits video fixtures.
// Notably longer wall times — Phase 5-A split would shrink these
// dramatically. Until then, the wide MAX_WAIT_S reflects the
// monolithic eyes pipeline.

import http from 'k6/http';
import { check, sleep } from 'k6';
import { Trend, Counter, Rate } from 'k6/metrics';
import { mintBatch } from './generate_jwts.js';

const BASE_URL = __ENV.BASE_URL || 'http://localhost:8080';
const JWT_SECRET = __ENV.NEXUS_JWT_SECRET || 'test-secret-do-not-use-in-production';
const VU_COUNT = parseInt(__ENV.VUS || '50', 10);
const DURATION = __ENV.DURATION || '15m';
const POLL_INTERVAL_S = parseFloat(__ENV.POLL_INTERVAL_S || '10');
const MAX_WAIT_S = parseInt(__ENV.MAX_WAIT_S || '1500', 10);   // 25 min — eyes monolith on long videos

const FIXTURE_MIX = [
  { path: __ENV.VIDEO_1MIN  || './corpus/video-1min.mp4',  weight: 0.4, label: '1min' },
  { path: __ENV.VIDEO_5MIN  || './corpus/video-5min.mp4',  weight: 0.4, label: '5min' },
  { path: __ENV.VIDEO_15MIN || './corpus/video-15min.mp4', weight: 0.2, label: '15min' },
];
const fixtures = FIXTURE_MIX.map(f => ({ ...f, data: open(f.path, 'b') }));

const e2eDuration = new Trend('canonical_video_e2e_seconds', true);
const workflowsCompleted = new Counter('canonical_video_completed');
const workflowsFailed = new Counter('canonical_video_failed');
const failRate = new Rate('canonical_video_failure_rate');

export const options = {
  scenarios: {
    sustain: {
      executor: 'ramping-vus',
      startVUs: 0,
      stages: [
        { duration: '2m', target: VU_COUNT },
        { duration: DURATION, target: VU_COUNT },
        { duration: '1m', target: 0 },
      ],
      gracefulRampDown: '2m',
    },
  },
  thresholds: {
    // Threshold reflects today's monolithic eyes pipeline. Tighten
    // after Phase 5-A — target should drop to <600s p95 for a 15-min
    // video once true per-step parallelism is in place.
    'canonical_video_e2e_seconds': ['p(95)<1500'],
    'canonical_video_failure_rate': ['rate<0.01'],   // 1% for now; 0.5% after P5-A
    'http_req_failed': ['rate<0.02'],
  },
};

export function setup() {
  return { tokens: mintBatch(JWT_SECRET, VU_COUNT) };
}

function chooseFixture() {
  const r = Math.random();
  let acc = 0;
  for (const f of fixtures) {
    acc += f.weight;
    if (r <= acc) return f;
  }
  return fixtures[fixtures.length - 1];
}

export default function (data) {
  const token = data.tokens[(__VU - 1) % data.tokens.length].token;
  const fixture = chooseFixture();

  // Session
  const sessionRes = http.post(
    `${BASE_URL}/v1/sessions`,
    JSON.stringify({ display_name: `loadtest-video-${__VU}-${__ITER}` }),
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

  // Submit
  const submitRes = http.post(
    `${BASE_URL}/v1/orchestrator/process`,
    {
      session_id: sessionId,
      video: http.file(fixture.data, `${fixture.label}.mp4`, 'video/mp4'),
      processing_profile: 'fast',
    },
    {
      headers: {
        'Authorization': `Bearer ${token}`,
        'Idempotency-Key': `lt-${__VU}-${__ITER}-${Date.now()}`,
      },
      tags: { name: 'SubmitCanonical', fixture: fixture.label },
    },
  );
  if (!check(submitRes, { 'submit 2xx': (r) => r.status < 300 })) {
    failRate.add(1);
    return;
  }
  const workflowId = JSON.parse(submitRes.body).workflow_id;

  // Poll until terminal
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
        e2eDuration.add((Date.now() - start) / 1000, { fixture: fixture.label });
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
