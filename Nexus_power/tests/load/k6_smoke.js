// k6_smoke.js — pre-flight smoke for the canonical pipeline.
//
// One tenant, one short audio + one short video workflow.
// Runs in CI on every PR (gated by integration.yml).
// Exit non-zero if either workflow fails or takes > 3 min end-to-end.

import http from 'k6/http';
import { check, sleep, fail } from 'k6';
import { Trend, Counter } from 'k6/metrics';
import { mintTenantJWT } from './generate_jwts.js';

const BASE_URL = __ENV.BASE_URL || 'http://localhost:8080';
const JWT_SECRET = __ENV.NEXUS_JWT_SECRET || 'test-secret-do-not-use-in-production';
const POLL_INTERVAL_S = parseFloat(__ENV.POLL_INTERVAL_S || '2');
const MAX_WAIT_S = parseInt(__ENV.MAX_WAIT_S || '180', 10);

const e2eDuration = new Trend('canonical_e2e_seconds', true);
const submitDuration = new Trend('canonical_submit_seconds', true);
const workflowsCompleted = new Counter('canonical_workflows_completed');
const workflowsFailed = new Counter('canonical_workflows_failed');

export const options = {
  vus: 1,
  iterations: 2,           // one audio + one video
  thresholds: {
    'canonical_e2e_seconds': ['p(95)<180'],     // 3 min hard ceiling for smoke fixtures
    'canonical_workflows_failed': ['count==0'], // any failure is a smoke fail
    'http_req_failed': ['rate<0.01'],
  },
};

const token = mintTenantJWT(JWT_SECRET, /*tenantIndex=*/0);

// Smoke fixtures — kept tiny so CI runs <3 min.
const AUDIO_FIXTURE = open(__ENV.AUDIO_FIXTURE || './corpus/audio-smoke.wav', 'b');
const VIDEO_FIXTURE = open(__ENV.VIDEO_FIXTURE || './corpus/video-smoke.mp4', 'b');

function createSession(label) {
  const res = http.post(
    `${BASE_URL}/v1/sessions`,
    JSON.stringify({ display_name: `loadtest-smoke-${label}` }),
    {
      headers: { 'Authorization': `Bearer ${token}`, 'Content-Type': 'application/json' },
    },
  );
  check(res, { 'session create 200/201': (r) => r.status === 200 || r.status === 201 })
    || fail(`session create failed: ${res.status} ${res.body}`);
  return JSON.parse(res.body).session_id;
}

function submitCanonical(sessionId, audio, video) {
  const form = {
    session_id: sessionId,
    processing_profile: 'fast',
  };
  if (audio) form.audio = http.file(audio, 'audio.wav', 'audio/wav');
  if (video) form.video = http.file(video, 'video.mp4', 'video/mp4');

  const start = Date.now();
  const res = http.post(`${BASE_URL}/v1/orchestrator/process`, form, {
    headers: {
      'Authorization': `Bearer ${token}`,
      'Idempotency-Key': `loadtest-${Date.now()}-${Math.random()}`,
    },
  });
  submitDuration.add((Date.now() - start) / 1000);

  check(res, { 'submit 200/202': (r) => r.status === 200 || r.status === 202 })
    || fail(`submit failed: ${res.status} ${res.body}`);

  return JSON.parse(res.body).workflow_id;
}

function pollUntilTerminal(workflowId) {
  const start = Date.now();
  const deadline = start + MAX_WAIT_S * 1000;
  while (Date.now() < deadline) {
    const res = http.get(`${BASE_URL}/v1/workflows/${workflowId}`, {
      headers: { 'Authorization': `Bearer ${token}` },
    });
    if (res.status !== 200) {
      sleep(POLL_INTERVAL_S);
      continue;
    }
    const body = JSON.parse(res.body);
    const status = body.status || body.workflow_status;
    if (status === 'completed' || status === 'success') {
      e2eDuration.add((Date.now() - start) / 1000);
      workflowsCompleted.add(1);
      return { ok: true, body };
    }
    if (status === 'failed' || status === 'cancelled' || status === 'quarantined') {
      workflowsFailed.add(1);
      return { ok: false, body };
    }
    sleep(POLL_INTERVAL_S);
  }
  workflowsFailed.add(1);
  return { ok: false, body: { error: `timeout after ${MAX_WAIT_S}s` } };
}

export default function (data) {
  const iteration = __ITER;            // 0 = audio, 1 = video
  const label = iteration === 0 ? 'audio' : 'video';
  const sessionId = createSession(label);

  const workflowId = submitCanonical(
    sessionId,
    iteration === 0 ? AUDIO_FIXTURE : null,
    iteration === 1 ? VIDEO_FIXTURE : null,
  );
  console.log(`[smoke] ${label} workflow=${workflowId} session=${sessionId}`);

  const result = pollUntilTerminal(workflowId);
  if (!result.ok) {
    fail(`${label} workflow ${workflowId} did not complete: ${JSON.stringify(result.body)}`);
  }
}
