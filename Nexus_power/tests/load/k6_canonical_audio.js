// k6_canonical_audio.js — audio-only workflow load test.
//
// Each VU represents one tenant submitting audio workflows over a
// ramp-up + sustain + ramp-down profile. Default: 50 VUs over 10 min.
//
// Acceptance (Phase 9):
//   p95 < 600s for 60-min audio workflows under sustained load
//   failure rate < 0.5%
//   queue depth stays bounded (verify separately via Grafana)

import http from 'k6/http';
import { check, sleep, fail } from 'k6';
import { Trend, Counter, Rate } from 'k6/metrics';
import { mintBatch } from './generate_jwts.js';

const BASE_URL = __ENV.BASE_URL || 'http://localhost:8080';
const JWT_SECRET = __ENV.NEXUS_JWT_SECRET || 'test-secret-do-not-use-in-production';
const VU_COUNT = parseInt(__ENV.VUS || '50', 10);
const DURATION = __ENV.DURATION || '10m';
const POLL_INTERVAL_S = parseFloat(__ENV.POLL_INTERVAL_S || '5');
const MAX_WAIT_S = parseInt(__ENV.MAX_WAIT_S || '900', 10);

// Mix of audio lengths weighted by realistic distribution.
const FIXTURE_MIX = [
  { path: __ENV.AUDIO_5MIN  || './corpus/audio-5min.wav',  weight: 0.5, label: '5min' },
  { path: __ENV.AUDIO_30MIN || './corpus/audio-30min.wav', weight: 0.3, label: '30min' },
  { path: __ENV.AUDIO_60MIN || './corpus/audio-60min.wav', weight: 0.2, label: '60min' },
];
const fixtures = FIXTURE_MIX.map(f => ({ ...f, data: open(f.path, 'b') }));

const e2eDuration = new Trend('canonical_audio_e2e_seconds', true);
const submitDuration = new Trend('canonical_audio_submit_seconds', true);
const workflowsCompleted = new Counter('canonical_audio_completed');
const workflowsFailed = new Counter('canonical_audio_failed');
const failRate = new Rate('canonical_audio_failure_rate');

export const options = {
  scenarios: {
    sustain: {
      executor: 'ramping-vus',
      startVUs: 0,
      stages: [
        { duration: '1m', target: VU_COUNT },         // ramp up
        { duration: DURATION, target: VU_COUNT },     // sustain
        { duration: '30s', target: 0 },               // ramp down
      ],
      gracefulRampDown: '1m',
    },
  },
  thresholds: {
    // Phase 9 acceptance criteria as k6 thresholds. CI fails the test
    // if any are breached.
    'canonical_audio_e2e_seconds': ['p(95)<600'],     // p95 < 10 min for any audio length
    'canonical_audio_failure_rate': ['rate<0.005'],   // < 0.5% failures
    'http_req_failed': ['rate<0.02'],                 // < 2% HTTP errors
  },
};

export function setup() {
  // Allocate one JWT per VU so tenants are distinct and the
  // workflow plane's per-tenant rate limiter shapes the load
  // the same way it shapes real client load.
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

function tokenForVU(data) {
  // __VU is 1-indexed; map to the same per-VU token across iterations
  // so the same tenant submits repeatedly (cache-hit testing).
  return data.tokens[(__VU - 1) % data.tokens.length];
}

function createSession(token, label) {
  const res = http.post(
    `${BASE_URL}/v1/sessions`,
    JSON.stringify({ display_name: `loadtest-${label}-${__VU}-${__ITER}` }),
    {
      headers: { 'Authorization': `Bearer ${token}`, 'Content-Type': 'application/json' },
      tags: { name: 'CreateSession' },
    },
  );
  if (!check(res, { 'session create 2xx': (r) => r.status < 300 })) {
    failRate.add(1);
    return null;
  }
  return JSON.parse(res.body).session_id;
}

function submit(token, sessionId, fixture) {
  const start = Date.now();
  const res = http.post(
    `${BASE_URL}/v1/orchestrator/process`,
    {
      session_id: sessionId,
      audio: http.file(fixture.data, `${fixture.label}.wav`, 'audio/wav'),
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
  submitDuration.add((Date.now() - start) / 1000);

  if (!check(res, { 'submit 2xx': (r) => r.status < 300 })) {
    failRate.add(1);
    return null;
  }
  return JSON.parse(res.body).workflow_id;
}

function pollUntilTerminal(token, workflowId, fixtureLabel) {
  const start = Date.now();
  const deadline = start + MAX_WAIT_S * 1000;
  while (Date.now() < deadline) {
    const res = http.get(`${BASE_URL}/v1/workflows/${workflowId}`, {
      headers: { 'Authorization': `Bearer ${token}` },
      tags: { name: 'PollWorkflow' },
    });
    if (res.status === 200) {
      const status = (JSON.parse(res.body).status || '').toLowerCase();
      if (status === 'completed' || status === 'success') {
        e2eDuration.add((Date.now() - start) / 1000, { fixture: fixtureLabel });
        workflowsCompleted.add(1);
        failRate.add(0);
        return true;
      }
      if (status === 'failed' || status === 'cancelled' || status === 'quarantined') {
        workflowsFailed.add(1);
        failRate.add(1);
        return false;
      }
    }
    sleep(POLL_INTERVAL_S);
  }
  workflowsFailed.add(1);
  failRate.add(1);
  return false;
}

export default function (data) {
  const token = tokenForVU(data);
  const fixture = chooseFixture();
  const sessionId = createSession(token, fixture.label);
  if (!sessionId) return;

  const workflowId = submit(token, sessionId, fixture);
  if (!workflowId) return;

  pollUntilTerminal(token, workflowId, fixture.label);
}
