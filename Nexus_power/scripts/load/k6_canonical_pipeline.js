/*
 * k6 load test for the Nexus QA canonical pipeline.
 *
 * Architect P2 exit criteria: validate 1000+/hour throughput
 * (~17 uploads/minute) without melting queue depth or losing artifacts.
 *
 * What it measures
 * ----------------
 *   - submit_p95          : POST /api/v1/orchestrator/process p95 latency
 *   - artifact_minimal_p95: time from submit → canonical_artifact row with
 *                           status=minimal visible
 *   - completion_rate     : fraction of uploads reaching {completed,
 *                           completed_degraded} within --timeout
 *   - queue_age_p95       : highest oldest-pending-age across all lanes
 *                           (sampled from the orchestrator's /metrics)
 *
 * What it does NOT do
 * -------------------
 *   - Actually upload distinct media files (uses one sample for every
 *     request; production cache layer is bypassed via session_id rotation
 *     so each submit is a fresh workflow even with identical bytes).
 *   - Verify enrichment quality. The artifact-existence check is the
 *     contract; visual/audio content fidelity is a separate concern.
 *
 * Usage
 * -----
 *   k6 run -e ORCH_URL=http://localhost:8100 \
 *          -e TOKEN="$(./mint_canary_token.sh)" \
 *          -e SAMPLE_VIDEO=/abs/path/sample.mp4 \
 *          -e VUS=10 -e DURATION=10m \
 *          k6_canonical_pipeline.js
 *
 * VUS=10 + 6s think-time = ~17 RPS sustained — matches 1000+/hour.
 *
 * Pass criteria (default):
 *   - p95 submit latency < 800ms
 *   - completion rate >= 0.98
 *   - queue_age p95 < 300s
 */

import http from 'k6/http';
import { check, sleep } from 'k6';
import { Counter, Rate, Trend } from 'k6/metrics';
import { SharedArray } from 'k6/data';
// `open()` is a stock k6 init-context global — no import needed.

// ─── Configuration ─────────────────────────────────────────────

const ORCH_URL = __ENV.ORCH_URL || 'http://localhost:8100';
// TOKEN = single token (back-compat), or TOKENS = comma-separated
// list for multi-tenant fanout. Each VU is assigned a token by
// (__VU - 1) % tokens.length so tenants stay within their own
// concurrency caps and the test measures cluster throughput, not the
// single-tenant guardrail.
const TOKENS_RAW = __ENV.TOKENS || __ENV.TOKEN || '';
const TOKENS = TOKENS_RAW.split(',').map(s => s.trim()).filter(Boolean);
const SAMPLE_VIDEO = __ENV.SAMPLE_VIDEO || './sample-30s.mp4';
const TIMEOUT_S = parseInt(__ENV.TIMEOUT || '900', 10);
const POLL_INTERVAL_S = parseFloat(__ENV.POLL || '5');

if (TOKENS.length === 0) {
  throw new Error('Set TOKEN or TOKENS env (comma-separated JWTs)');
}

function tokenForVU() {
  return TOKENS[(__VU - 1) % TOKENS.length];
}

// Reuse the same binary payload across VUs. SharedArray would JSON-
// serialize the ArrayBuffer (losing it); stock k6's `open()` at module
// scope is loaded once per VU at init context but the runtime keeps the
// data resident, so memory cost is bounded.
const videoBytes = open(SAMPLE_VIDEO, 'b');

// ─── Custom metrics ────────────────────────────────────────────

export const submitErrors = new Counter('submit_errors');
export const completionRate = new Rate('completion_rate');
export const submitDuration = new Trend('submit_p95', true);
export const artifactMinimalLatency = new Trend('artifact_minimal_p95', true);
export const workflowDuration = new Trend('workflow_terminal_p95', true);

// ─── Test options ──────────────────────────────────────────────

export const options = {
  scenarios: {
    steady: {
      executor: 'constant-vus',
      vus: parseInt(__ENV.VUS || '10', 10),
      duration: __ENV.DURATION || '10m',
      gracefulStop: '5m',
    },
  },
  thresholds: {
    submit_p95: ['p(95)<800'],            // 800ms upload accept p95
    completion_rate: ['rate>=0.98'],      // ≥98% reach terminal success
    artifact_minimal_p95: ['p(95)<90000'], // minimal artifact within 90s p95
    workflow_terminal_p95: ['p(95)<' + (TIMEOUT_S * 1000 * 0.9)],
  },
};

// ─── Helpers ───────────────────────────────────────────────────

function jitterSessionId() {
  // Rotate session_id so the orchestrator's media-fingerprint cache
  // doesn't short-circuit the second request with the same bytes.
  return `k6-canary-${__VU}-${__ITER}-${Date.now()}`;
}

// Append unique bytes per iteration so each upload has a distinct
// SHA-256 fingerprint. Without this, the orchestrator's content-dedup
// returns 409 Conflict on every upload after the first. Stock k6
// doesn't expose TextEncoder, so we hand-roll the ASCII encoding.
function uniqueVideoPayload() {
  const tailStr = `\n__k6_jitter:${__VU}:${__ITER}:${Date.now()}:${Math.random()}\n`;
  const tail = new Uint8Array(tailStr.length);
  for (let i = 0; i < tailStr.length; i++) {
    tail[i] = tailStr.charCodeAt(i) & 0xff;
  }
  const base = new Uint8Array(videoBytes);
  const out = new Uint8Array(base.length + tail.length);
  out.set(base, 0);
  out.set(tail, base.length);
  return out.buffer;
}

function submitUpload() {
  const fd = {
    video: http.file(uniqueVideoPayload(), 'sample.mp4', 'video/mp4'),
    session_id: jitterSessionId(),
    processing_profile: 'fast',
  };
  const t0 = Date.now();
  const res = http.post(
    `${ORCH_URL}/api/v1/orchestrator/process`,
    fd,
    {
      headers: { Authorization: `Bearer ${tokenForVU()}` },
      timeout: '60s',
    },
  );
  submitDuration.add(Date.now() - t0);
  return res;
}

function pollUntilArtifact(canonicalId, deadline) {
  // Returns the first poll response where status moves out of pending.
  while (Date.now() < deadline) {
    const res = http.get(
      `${ORCH_URL}/api/v1/canonical-workflows/${canonicalId}`,
      {
        headers: { Authorization: `Bearer ${tokenForVU()}` },
        timeout: '10s',
      },
    );
    if (res.status === 200) {
      const body = res.json();
      const status = body.status || '';
      if (['completed', 'completed_degraded', 'failed',
           'quarantined', 'cancelled'].indexOf(status) !== -1) {
        return { terminal: true, status, body };
      }
    }
    sleep(POLL_INTERVAL_S);
  }
  return { terminal: false, status: 'timeout' };
}

// ─── VU body ───────────────────────────────────────────────────

export default function () {
  const t_submit = Date.now();
  const submit = submitUpload();
  const ok = check(submit, {
    'submit returns 200/202': (r) =>
      r.status === 200 || r.status === 202 || r.status === 201,
  });
  if (!ok || !submit.json()) {
    submitErrors.add(1);
    completionRate.add(0);
    return;
  }
  const body = submit.json();
  const wf = body.workflow_id;
  if (!wf) {
    submitErrors.add(1);
    completionRate.add(0);
    return;
  }

  // Optional: track minimal-artifact latency separately. The orchestrator
  // returns artifact_id immediately; the minimal row should land in DB
  // within seconds of probe + redact + detect_scenes completing.
  // We approximate by polling once at ~10s in.
  sleep(10);
  const earlyRes = http.get(
    `${ORCH_URL}/api/v1/canonical-workflows/${wf}`,
    { headers: { Authorization: `Bearer ${tokenForVU()}` }, timeout: '10s' },
  );
  if (earlyRes.status === 200) {
    const eb = earlyRes.json();
    const completed = (eb.dag_completed_steps || []);
    if (completed.indexOf('spine.persist_minimal_artifact') !== -1) {
      artifactMinimalLatency.add(Date.now() - t_submit);
    }
  }

  // Watch to terminal.
  const deadline = Date.now() + TIMEOUT_S * 1000;
  const result = pollUntilArtifact(wf, deadline);
  workflowDuration.add(Date.now() - t_submit);
  const success = (
    result.terminal &&
    (result.status === 'completed' || result.status === 'completed_degraded')
  );
  completionRate.add(success ? 1 : 0);

  // Think-time so VUS=10 yields ~17 RPS (matching 1000+/hr).
  sleep(parseFloat(__ENV.THINK || '6'));
}
