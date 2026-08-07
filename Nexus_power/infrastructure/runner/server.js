'use strict';
/*
 * Nexus runner — a tiny, internal-only HTTP service that runs a configured
 * Playwright bundle server-side and lets the bundled nexus-reporter post results
 * back to Nexus (grounded triage). Internal-only (nexus network, no host port);
 * one run at a time; hard timeout; restricted file paths; non-root (pwuser).
 *
 * POST /run  { files: {relPath: content}, env: {NEXUS_*}, timeout_ms }
 *   -> { status: passed|failed|timed_out|error, exit_code, output }
 * GET  /health -> { ok: true, busy }
 */
const http = require('http');
const fs = require('fs');
const path = require('path');
const { spawn, execFileSync } = require('child_process');
const crypto = require('crypto');
const { attachGroundTruthRecorder } = require('./ground_truth_recorder');
const { attachLoginObserver } = require('./login_observer');

const PORT = parseInt(process.env.PORT || '4555', 10);
const RUNNER_TOKEN = process.env.RUNNER_TOKEN || '';      // optional shared secret
const ROOT = '/opt/runner';
const WORK = path.join(ROOT, 'work');
const HARD_TIMEOUT = 600000;                              // 10 min absolute cap
const MAX_BODY = 25 * 1024 * 1024;                        // 25 MB bundle cap
const META_IP = '169.254.169.254';                       // cloud metadata — never a test target

let busy = false;

// Only these bundle paths may be written — defends against path traversal /
// writing outside the work dir.
function allowedPath(p) {
  if (typeof p !== 'string' || !p || p.includes('..') || p.startsWith('/') || p.includes('\\')) return false;
  if (p === 'node_modules' || p.startsWith('node_modules/')) return false;  // never write into the shared symlinked install
  if (!p.includes('/')) return true;        // any root-level config/support/dotfile (WORK is wiped each run)
  if (/^tests\//.test(p)) return true;      // any file under tests/
  return false;
}

function rmrf(dir) { try { fs.rmSync(dir, { recursive: true, force: true }); } catch (e) { /* noop */ } }

// Wipe the decrypted auth session promptly after a run so it never lingers as
// plaintext in the work dir (encryption-at-rest only matters if the cleartext
// copy is short-lived). Called from both run paths' close/error handlers.
function wipeAuth() { try { fs.rmSync(path.join(WORK, 'nexus.auth.json'), { force: true }); } catch (e) { /* noop */ } }

function writeBundle(files) {
  rmrf(WORK);
  fs.mkdirSync(WORK, { recursive: true });
  // Resolve @playwright/test (and the browsers) from the pre-installed root.
  try { fs.symlinkSync(path.join(ROOT, 'node_modules'), path.join(WORK, 'node_modules')); } catch (e) { /* noop */ }
  for (const [rel, content] of Object.entries(files || {})) {
    if (!allowedPath(rel)) { console.error('[nexus-runner] writeBundle rejected: ' + rel); throw new Error('rejected path: ' + rel); }
    const dest = path.join(WORK, rel);
    fs.mkdirSync(path.dirname(dest), { recursive: true });
    fs.writeFileSync(dest, String(content));
  }
}

const OUT_TAIL = 8000;
// A login proof must not depend on how chatty a run was.
//
// The [nexus-auth] markers that prove a login happened are printed at the START of
// a run — before any test output. Returning a pure TAIL window therefore drops them
// the moment a run gets talkative, and the reader upstream sees no marker at all and
// reports 'unreadable'. That is exactly how the per-card Verify button came to be
// unable to prove anything: the preflight survived only because it happens to run
// with video and screenshots off, so it stayed under the cap. Proof by luck is not
// proof. Keep every marker line, then the tail.
function clipOutput(out) {
  out = String(out || '');
  if (out.length <= OUT_TAIL) return out;
  const tail = out.slice(-OUT_TAIL);
  // Markers carry identifiers only, never a credential value, so keeping them
  // cannot move a secret into a response that the tail would not already carry.
  const NL = String.fromCharCode(10);
  const kept = out.split(NL)
    .filter((l) => l.indexOf('[nexus-auth]') !== -1 && tail.indexOf(l) === -1);
  return kept.length ? kept.join(NL) + NL + '...[truncated]...' + NL + tail : tail;
}

function runPlaywright(env, timeoutMs) {
  return new Promise((resolve) => {
    const bin = path.join(ROOT, 'node_modules', '.bin', 'playwright');
    const child = spawn(bin, ['test'], { cwd: WORK, env: { ...process.env, ...env, CI: '1' } });
    let out = '';
    const cap = (d) => { out += d.toString(); if (out.length > 400000) out = out.slice(-400000); };
    child.stdout.on('data', cap);
    child.stderr.on('data', cap);
    let timedOut = false;
    const timer = setTimeout(() => { timedOut = true; child.kill('SIGKILL'); },
      Math.min(Math.max(parseInt(timeoutMs, 10) || 240000, 10000), HARD_TIMEOUT));
    child.on('close', (code) => {
      clearTimeout(timer);
      wipeAuth();
      const status = timedOut ? 'timed_out' : (code === 0 ? 'passed' : 'failed');
      resolve({ status, exit_code: code, output: clipOutput(out) });
    });
    child.on('error', (e) => {
      clearTimeout(timer);
      wipeAuth();
      resolve({ status: 'error', exit_code: -1, output: 'spawn error: ' + e.message });
    });
  });
}

// ── LIVE (headed) display stack: Xvfb + x11vnc + websockify/noVNC ────────────
const DISPLAY = process.env.NEXUS_DISPLAY || ':99';
const VNC_GEOMETRY = process.env.NEXUS_VNC_GEOMETRY || '1280x800x24';
const NOVNC_DIR = process.env.NEXUS_NOVNC_DIR || '/usr/share/novnc';

let display = { xvfb: null, x11vnc: null, websockify: null, up: false };
let liveRun = null;   // { run_id, status, exit_code, output }
let liveTimer = null;

function alive(p) { return p && p.exitCode === null && !p.killed; }

// Idempotent: start each daemon only if not already running. Detached so they
// outlive a single test child and serve reconnects (-forever). x11vnc is
// -viewonly: the portal can WATCH but never control the browser; -localhost so
// raw VNC is loopback-only and only websockify (proxied via nginx) reaches it.
function ensureDisplay() {
  if (display.up && alive(display.xvfb) && alive(display.x11vnc) && alive(display.websockify)) return;
  if (!alive(display.xvfb)) {
    const _dn = String(DISPLAY).replace(':', '');
    try { fs.rmSync('/tmp/.X' + _dn + '-lock', { force: true }); } catch (e) { /* stale lock */ }
    try { fs.rmSync('/tmp/.X11-unix/X' + _dn, { force: true }); } catch (e) { /* stale socket */ }
    display.xvfb = spawn('Xvfb', [DISPLAY, '-screen', '0', VNC_GEOMETRY, '-nolisten', 'tcp', '-ac'],
      { stdio: 'ignore', detached: true });
    display.xvfb.unref();
  }
  if (!alive(display.x11vnc)) {
    const _xs = '/tmp/.X11-unix/X' + String(DISPLAY).replace(':', '');
    display.x11vnc = spawn('sh', ['-c',
      'i=0; while [ ! -S "' + _xs + '" ] && [ $i -lt 50 ]; do i=$((i+1)); sleep 0.1; done; ' +
      'exec x11vnc -display ' + DISPLAY + ' -rfbport 5900 -forever -shared -nopw -viewonly -localhost -quiet -noxdamage'],
      { stdio: 'ignore', detached: true });
    display.x11vnc.unref();
  }
  if (!alive(display.websockify)) {
    display.websockify = spawn('websockify', ['--web', NOVNC_DIR, '6080', 'localhost:5900'],
      { stdio: 'ignore', detached: true });
    display.websockify.unref();
  }
  display.up = true;
}

// Fire-and-return: spawn a HEADED test in the background onto :99; status is
// polled via /run-live/status. `busy` is held until the child exits.
function runPlaywrightLive(env, timeoutMs) {
  ensureDisplay();
  const bin = path.join(ROOT, 'node_modules', '.bin', 'playwright');
  const _xsock = '/tmp/.X11-unix/X' + String(DISPLAY).replace(':', '');
  // Wait for the Xvfb X-socket before launching the HEADED browser (else Chrome races the
  // X server and dies with "Missing X server or $DISPLAY"). Bounded ~5s; exec keeps the PID.
  const child = spawn('sh', ['-c',
    'i=0; while [ ! -S "' + _xsock + '" ] && [ $i -lt 50 ]; do i=$((i+1)); sleep 0.1; done; ' +
    'exec "' + bin + '" test --headed'],
    { cwd: WORK, env: { ...process.env, ...env, CI: '1', DISPLAY } });
  liveRun = { run_id: env.NEXUS_RUN_ID || '', status: 'running', exit_code: null, output: '' };
  const cap = (d) => { liveRun.output += d.toString(); if (liveRun.output.length > 400000) liveRun.output = liveRun.output.slice(-400000); };
  child.stdout.on('data', cap);
  child.stderr.on('data', cap);
  let timedOut = false;
  liveTimer = setTimeout(() => { timedOut = true; child.kill('SIGKILL'); },
    Math.min(Math.max(parseInt(timeoutMs, 10) || 240000, 10000), HARD_TIMEOUT));
  child.on('close', (code) => {
    if (liveTimer) { clearTimeout(liveTimer); liveTimer = null; }
    wipeAuth();
    liveRun.status = timedOut ? 'timed_out' : (code === 0 ? 'passed' : 'failed');
    liveRun.exit_code = code;
    liveRun.output = clipOutput(liveRun.output);
    busy = false;
  });
  child.on('error', (e) => {
    if (liveTimer) { clearTimeout(liveTimer); liveTimer = null; }
    wipeAuth();
    liveRun.status = 'error'; liveRun.exit_code = -1;
    liveRun.output = 'spawn error: ' + e.message;
    busy = false;
  });
}

// ── AUTH CAPTURE (interactive headed login → storageState) ───────────────────
// A SEPARATE, ephemeral INTERACTIVE display (x11vnc WITHOUT -viewonly) so the
// operator can log in once — MFA/captcha included — and we then save
// context.storageState(). Distinct from the view-only run stream (:99/6080) and
// torn down when the session ends. One at a time (shares `busy`). Uses the
// Playwright Node API in-process.
const CAPTURE_DISPLAY = process.env.NEXUS_CAPTURE_DISPLAY || ':98';
const CAPTURE_MAX_MS = 600000;  // 10-min hard cap on an open capture session
let capDisp = { xvfb: null, x11vnc: null, websockify: null, up: false };
let capture = null;   // { browser, context, page, timer }

const CAPTURE_VNCPASS_FILE = path.join(ROOT, '.nexus-capture.vncpass');

function ensureCaptureDisplay(authFile) {
  if (capDisp.up && alive(capDisp.xvfb) && alive(capDisp.x11vnc) && alive(capDisp.websockify)) return;
  if (!alive(capDisp.xvfb)) {
    const _cdn = String(CAPTURE_DISPLAY).replace(':', '');
    try { fs.rmSync('/tmp/.X' + _cdn + '-lock', { force: true }); } catch (e) { /* stale lock */ }
    try { fs.rmSync('/tmp/.X11-unix/X' + _cdn, { force: true }); } catch (e) { /* stale socket */ }
    capDisp.xvfb = spawn('Xvfb', [CAPTURE_DISPLAY, '-screen', '0', VNC_GEOMETRY, '-nolisten', 'tcp', '-ac'],
      { stdio: 'ignore', detached: true }); capDisp.xvfb.unref();
  }
  if (!alive(capDisp.x11vnc)) {
    // INTERACTIVE (no -viewonly) so the operator can log in — but PASSWORD-PROTECTED
    // (-rfbauth, a per-capture one-time secret) and SINGLE-viewer (NO -shared) and
    // -localhost: only the operator who holds the one-time password can attach, and
    // only one viewer at a time. Closes the unauthenticated-control exposure.
    const _xsock = '/tmp/.X11-unix/X' + String(CAPTURE_DISPLAY).replace(':', '');
    // Wait for the Xvfb X-socket before starting x11vnc (else x11vnc races Xvfb
    // at startup, fails to open the display, and dies -> dead VNC backend).
    capDisp.x11vnc = spawn('sh', ['-c',
      'i=0; while [ ! -S "' + _xsock + '" ] && [ $i -lt 50 ]; do i=$((i+1)); sleep 0.1; done; ' +
      'exec env DISPLAY=' + CAPTURE_DISPLAY + ' x11vnc -display ' + CAPTURE_DISPLAY +
      ' -rfbport 5901 -forever -rfbauth "' + authFile + '" -localhost -quiet -noxdamage'],
      { stdio: 'ignore', detached: true }); capDisp.x11vnc.unref();
  }
  if (!alive(capDisp.websockify)) {
    capDisp.websockify = spawn('websockify', ['--web', NOVNC_DIR, '6081', 'localhost:5901'],
      { stdio: 'ignore', detached: true }); capDisp.websockify.unref();
  }
  capDisp.up = true;
}

function teardownCaptureDisplay() {
  for (const k of ['websockify', 'x11vnc', 'xvfb']) {
    try { if (capDisp[k]) capDisp[k].kill('SIGKILL'); } catch (e) { /* noop */ }
    capDisp[k] = null;
  }
  capDisp.up = false;
}

async function stopCapture() {
  const c = capture; capture = null;
  if (c) {
    if (c.timer) { try { clearTimeout(c.timer); } catch (e) { /* noop */ } }
    try { if (c.browser) await c.browser.close(); } catch (e) { /* noop */ }
  }
  teardownCaptureDisplay();
  try { fs.rmSync(CAPTURE_VNCPASS_FILE, { force: true }); } catch (e) { /* noop */ }
}

async function startCapture(url, launchArgs, recorderOpts) {
  // Mint a per-capture one-time VNC password and gate the interactive display on it.
  const pw = crypto.randomBytes(8).toString('hex').slice(0, 8);  // 8 chars (RFB DES cap)
  try { execFileSync('x11vnc', ['-storepasswd', pw, CAPTURE_VNCPASS_FILE]); }
  catch (e) { throw new Error('vnc auth setup failed: ' + ((e && e.message) || e)); }
  ensureCaptureDisplay(CAPTURE_VNCPASS_FILE);
  const { chromium } = require('@playwright/test');
  const prev = process.env.DISPLAY;
  process.env.DISPLAY = CAPTURE_DISPLAY;   // mutually exclusive (busy), so safe
  let browser;
  try {
    const args = (launchArgs || process.env.NEXUS_LAUNCH_ARGS || '--no-sandbox --disable-dev-shm-usage')
      .split(' ').filter(Boolean);
    browser = await chromium.launch({ headless: false, args });
  } finally {
    if (prev === undefined) delete process.env.DISPLAY; else process.env.DISPLAY = prev;
  }
  const context = await browser.newContext({ viewport: null });
  // Login observation: record the CHOREOGRAPHY of the login being performed here
  // (field identifiers + control presses, never a credential value) so this one
  // hand-performed login becomes a recipe replayable with any member's card.
  // Attached to the CONTEXT and BEFORE the first page, so the login page itself is
  // instrumented and an SSO popup in a second tab is observed too.
  let loginObserver = null;
  try {
    loginObserver = attachLoginObserver(context, { startUrl: url });
    await loginObserver.ready;
  } catch (e) { loginObserver = null; /* capture must never fail on observation */ }
  const page = await context.newPage();
  // Guided ground-truth capture: instrument the page BEFORE the first navigation
  // so the very first page is recorded (real URL + SPA routes + redacted fields).
  let recorder = null;
  if (recorderOpts && recorderOpts.artifactId && recorderOpts.apiBase) {
    recorder = attachGroundTruthRecorder(page, recorderOpts);
    try { await recorder.ready; } catch (e) { /* best-effort instrumentation */ }
  }
  if (url) {
    try { await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 30000 }); }
    catch (e) { /* keep the session open even if the first nav is slow */ }
  }
  const timer = setTimeout(() => {
    stopCapture().catch(() => {}).finally(() => { busy = false; });
  }, CAPTURE_MAX_MS);
  capture = { browser, context, page, timer, password: pw, recorder, loginObserver };
}

async function saveCapture() {
  if (!capture || !capture.context) throw new Error('no active capture session');
  const state = await capture.context.storageState();
  // storageState() carries cookies and localStorage ONLY — sessionStorage is
  // omitted by design. Apps that keep their sign-in there therefore recorded a
  // login perfectly and handed back an EMPTY session, and the crawl then ran
  // signed out. Observed live on a Next.js app: cookies=0 origins=0 while the
  // login itself was captured fine.
  //
  // Namespaced so it cannot collide with Playwright's own schema; the consumer
  // strips it before handing the state back to Playwright and replays it through
  // an init script instead. Best-effort: a page that forbids the read, or a
  // context already closing, costs us nothing that we had before.
  try {
    const origin = await capture.page.evaluate(() => window.location.origin);
    const entries = await capture.page.evaluate(() => {
      const out = {};
      for (let i = 0; i < sessionStorage.length; i += 1) {
        const k = sessionStorage.key(i);
        if (k) out[k] = sessionStorage.getItem(k);
      }
      return out;
    });
    if (entries && Object.keys(entries).length) {
      state.__nx_session_storage = [{ origin: origin, entries: entries }];
    }
  } catch (e) { /* no sessionStorage to carry — the cookie session still stands */ }
  // Snapshot the login choreography BEFORE stopCapture() closes the browser —
  // page.url() is unreadable once the context is gone. Never fatal: losing the
  // observation must never cost the operator their captured session.
  let observation = null;
  try {
    if (capture.loginObserver) observation = capture.loginObserver.snapshot();
  } catch (e) { observation = null; }
  await stopCapture();
  return { state, observation };
}

const server = http.createServer((req, res) => {
  const send = (code, obj) => { res.writeHead(code, { 'Content-Type': 'application/json' }); res.end(JSON.stringify(obj)); };

  if (req.method === 'GET' && req.url === '/health')
    return send(200, { ok: true, busy, live: liveRun ? { run_id: liveRun.run_id, status: liveRun.status } : null });

  if (req.method === 'POST' && req.url === '/run') {
    if (RUNNER_TOKEN && req.headers['x-runner-token'] !== RUNNER_TOKEN) return send(401, { error: 'unauthorized' });
    if (busy) return send(409, { error: 'runner busy — one run at a time' });
    let body = '';
    req.on('data', (c) => { body += c; if (body.length > MAX_BODY) { req.destroy(); } });
    req.on('end', async () => {
      let parsed;
      try { parsed = JSON.parse(body); } catch (e) { return send(400, { error: 'bad json' }); }
      const env = parsed.env || {};
      if (String(env.NEXUS_BASE_URL || '').includes(META_IP)) return send(400, { error: 'target not allowed' });
      if (busy || capture) return send(409, { error: 'runner busy — one run at a time' });
      busy = true;   // authoritative check+set, atomic (no await between check and set)
      try {
        writeBundle(parsed.files || {});
        const result = await runPlaywright(env, parsed.timeout_ms);
        send(200, result);
      } catch (e) {
        send(500, { status: 'error', exit_code: -1, output: String((e && e.message) || e) });
      } finally {
        busy = false;
      }
    });
    return;
  }

  // ── LIVE headed run (background; streamed via :6080 / nginx /live) ─────────
  if (req.method === 'POST' && req.url === '/run-live') {
    if (RUNNER_TOKEN && req.headers['x-runner-token'] !== RUNNER_TOKEN) return send(401, { error: 'unauthorized' });
    if (busy) return send(409, { error: 'runner busy — one run at a time' });
    let body = '';
    req.on('data', (c) => { body += c; if (body.length > MAX_BODY) { req.destroy(); } });
    req.on('end', () => {
      let parsed;
      try { parsed = JSON.parse(body); } catch (e) { return send(400, { error: 'bad json' }); }
      const env = parsed.env || {};
      if (String(env.NEXUS_BASE_URL || '').includes(META_IP)) return send(400, { error: 'target not allowed' });
      if (busy || capture) return send(409, { error: 'runner busy — one run at a time' });
      busy = true;   // authoritative check+set, atomic; released in the child close/error handler
      try {
        writeBundle(parsed.files || {});
        runPlaywrightLive(env, parsed.timeout_ms);   // returns immediately
        send(202, { status: 'running', run_id: env.NEXUS_RUN_ID || '' });
      } catch (e) {
        busy = false;
        send(500, { status: 'error', exit_code: -1, output: String((e && e.message) || e) });
      }
    });
    return;
  }

  if (req.method === 'GET' && req.url === '/run-live/status') {
    if (!liveRun) return send(200, { status: 'idle' });
    return send(200, { run_id: liveRun.run_id, status: liveRun.status, exit_code: liveRun.exit_code, output: (liveRun.output || '').slice(-4000) });
  }

  // ── AUTH CAPTURE — interactive login → storageState ────────────────────────
  if (req.method === 'POST' && req.url === '/auth-capture/start') {
    if (RUNNER_TOKEN && req.headers['x-runner-token'] !== RUNNER_TOKEN) return send(401, { error: 'unauthorized' });
    if (busy || capture) return send(409, { error: 'runner busy — finish the current run/capture first' });
    let body = '';
    req.on('data', (c) => { body += c; if (body.length > 1048576) req.destroy(); });
    req.on('end', async () => {
      let parsed;
      try { parsed = JSON.parse(body || '{}'); } catch (e) { return send(400, { error: 'bad json' }); }
      const url = String(parsed.url || '');
      if (url.includes(META_IP)) return send(400, { error: 'target not allowed' });
      if (busy || capture) return send(409, { error: 'runner busy — finish the current run/capture first' });
      busy = true;   // authoritative check+set, atomic (no await between check and set)
      try {
        await startCapture(url, parsed.launch_args);
        send(202, { status: 'capturing', vnc_password: capture ? capture.password : '' });
      } catch (e) {
        try { await stopCapture(); } catch (e2) { /* noop */ }
        busy = false;
        send(500, { status: 'error', error: String((e && e.message) || e) });
      }
    });
    return;
  }

  if (req.method === 'POST' && req.url === '/auth-capture/save') {
    if (RUNNER_TOKEN && req.headers['x-runner-token'] !== RUNNER_TOKEN) return send(401, { error: 'unauthorized' });
    (async () => {
      try {
        const saved = await saveCapture();
        send(200, { status: 'saved', storage_state: saved.state,
                    login_observation: saved.observation });
      } catch (e) {
        send(400, { status: 'error', error: String((e && e.message) || e) });
      } finally {
        busy = false;
      }
    })();
    return;
  }

  if (req.method === 'POST' && req.url === '/auth-capture/cancel') {
    if (RUNNER_TOKEN && req.headers['x-runner-token'] !== RUNNER_TOKEN) return send(401, { error: 'unauthorized' });
    (async () => {
      try { await stopCapture(); } catch (e) { /* noop */ }
      finally { busy = false; send(200, { status: 'cancelled' }); }
    })();
    return;
  }

  if (req.method === 'GET' && req.url === '/auth-capture/status') {
    return send(200, { active: !!capture });
  }

  // Live view of the login choreography recorded so far, so the operator can be
  // shown "we captured member_number, password" WHILE still logged in — before
  // committing the session. Identifiers only; never a credential value.
  if (req.method === 'GET' && req.url === '/auth-capture/observation') {
    if (RUNNER_TOKEN && req.headers['x-runner-token'] !== RUNNER_TOKEN) return send(401, { error: 'unauthorized' });
    if (!capture) return send(200, { active: false, observation: null });
    let observation = null;
    try { if (capture.loginObserver) observation = capture.loginObserver.snapshot(); }
    catch (e) { observation = null; }
    return send(200, { active: true, observation });
  }

  // ── GROUND-TRUTH CAPTURE — guided web capture → instrumented sidecar ────────
  // The user drives THEIR app inside our controlled browser; we record the REAL
  // URL + SPA routes + PII-redacted form values and POST them to the platform's
  // ingest endpoint. Nothing is installed on the customer endpoint.
  if (req.method === 'POST' && req.url === '/ground-truth-capture/start') {
    if (RUNNER_TOKEN && req.headers['x-runner-token'] !== RUNNER_TOKEN) return send(401, { error: 'unauthorized' });
    if (busy || capture) return send(409, { error: 'runner busy — finish the current run/capture first' });
    let body = '';
    req.on('data', (c) => { body += c; if (body.length > 1048576) req.destroy(); });
    req.on('end', async () => {
      let parsed;
      try { parsed = JSON.parse(body || '{}'); } catch (e) { return send(400, { error: 'bad json' }); }
      const url = String(parsed.url || '');
      const apiBase = String(parsed.api_base || '');
      if (url.includes(META_IP) || apiBase.includes(META_IP)) return send(400, { error: 'target not allowed' });
      if (!parsed.artifact_id || !apiBase) return send(400, { error: 'artifact_id and api_base required' });
      if (busy || capture) return send(409, { error: 'runner busy' });
      busy = true;
      try {
        await startCapture(url, parsed.launch_args, {
          artifactId: String(parsed.artifact_id),
          apiBase: apiBase,
          token: String(parsed.token || ''),
          sessionId: String(parsed.session_id || ''),
          recorderVersion: String(parsed.recorder_version || 'v1'),
        });
        send(202, { status: 'capturing', vnc_password: capture ? capture.password : '' });
      } catch (e) {
        try { await stopCapture(); } catch (e2) { /* noop */ }
        busy = false;
        send(500, { status: 'error', error: String((e && e.message) || e) });
      }
    });
    return;
  }

  if (req.method === 'POST' && req.url === '/ground-truth-capture/save') {
    if (RUNNER_TOKEN && req.headers['x-runner-token'] !== RUNNER_TOKEN) return send(401, { error: 'unauthorized' });
    (async () => {
      try {
        if (!capture || !capture.recorder) throw new Error('no active ground-truth capture');
        const result = await capture.recorder.flush();
        await stopCapture();
        send(200, { status: 'saved', ingest: result });
      } catch (e) {
        try { await stopCapture(); } catch (e2) { /* noop */ }
        send(500, { status: 'error', error: String((e && e.message) || e) });
      } finally {
        busy = false;
      }
    })();
    return;
  }

  if (req.method === 'POST' && req.url === '/ground-truth-capture/cancel') {
    if (RUNNER_TOKEN && req.headers['x-runner-token'] !== RUNNER_TOKEN) return send(401, { error: 'unauthorized' });
    (async () => {
      try { await stopCapture(); } catch (e) { /* noop */ }
      finally { busy = false; send(200, { status: 'cancelled' }); }
    })();
    return;
  }

  send(404, { error: 'not found' });
});

server.listen(PORT, () => console.log('[nexus-runner] listening on :' + PORT));
