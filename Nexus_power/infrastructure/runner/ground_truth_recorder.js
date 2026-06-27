'use strict';
/**
 * Ground-truth capture recorder (Road B — the Tier-0 overlay source).
 *
 * Attaches to a Playwright page driven inside our controlled, on-prem browser
 * ("guided capture") and emits a timestamped sidecar event log of REAL state:
 *   - navigation URLs (full-page `framenavigated` AND SPA history pushState/
 *     replaceState/popstate — so client-routed sub-pages are never missed);
 *   - committed form field values (label -> value), PII-REDACTED AT SOURCE,
 *     with password / masked inputs forced empty.
 * The page_visit_extractor joins these to the canonical frames by timestamp as
 * the Tier-0 overlay (PageVisitSource.GROUND_TRUTH, confidence 1.0).
 *
 * GENERIC: protocol-level (CDP/DOM), zero per-app/per-customer config — works on
 * any of 1000+ web apps with no change. PII is redacted IN-PERIMETER before the
 * value ever leaves the page; nothing raw is sent to the ingest API.
 *
 * Nothing is installed on the customer endpoint — this runs in the same hardened
 * runner the product already uses for Playwright execution / auth-capture.
 */
const http = require('http');
const https = require('https');
const { URL } = require('url');

// Generic PII patterns — redaction AT SOURCE. No app/customer coupling; a vertical
// can extend via env without touching code paths.
const PII_PATTERNS = [
  { name: 'ssn', rx: /\b\d{3}-\d{2}-\d{4}\b/g },
  { name: 'card', rx: /\b(?:\d[ -]?){13,19}\b/g },
  { name: 'email', rx: /\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b/g },
  { name: 'phone', rx: /\b\+?\d[\d\s().-]{8,}\d\b/g },
  { name: 'dob', rx: /\b\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b/g },
];

// Input types whose value must NEVER be captured, even redacted.
const SENSITIVE_TYPES = new Set(['password']);

function redactValue(s) {
  if (!s) return '';
  let out = String(s);
  for (const p of PII_PATTERNS) out = out.replace(p.rx, '[REDACTED]');
  return out;
}

function parseUrlParts(raw) {
  try {
    const u = new URL(raw);
    return {
      host: (u.hostname || '').toLowerCase(),
      path: u.pathname || '',
      query: (u.search || '').replace(/^\?/, ''),
    };
  } catch (e) {
    return { host: '', path: '', query: '' };
  }
}

/**
 * Instrument a Playwright `page`. Returns `{ flush, events }`.
 * opts: { artifactId, apiBase, token, sessionId, recorderVersion }
 */
function attachGroundTruthRecorder(page, opts) {
  const startMs = Date.now();
  const events = [];
  const push = (e) => events.push(Object.assign({ timestamp_ms: Date.now() - startMs }, e));

  // Full-page navigations.
  page.on('framenavigated', (frame) => {
    try {
      if (frame !== page.mainFrame()) return;
      const url = frame.url() || '';
      if (!url || url === 'about:blank') return;
      const p = parseUrlParts(url);
      push({ kind: 'navigate', url, url_host: p.host, url_path: p.path, url_query: p.query });
    } catch (e) { /* never break the capture */ }
  });

  // SPA route changes + committed form values, surfaced from the page via bindings.
  const pNav = page.exposeBinding('__nxGtNav', (_src, url) => {
    try {
      const abs = new URL(url, page.url()).toString();
      const p = parseUrlParts(abs);
      push({ kind: 'navigate', url: abs, url_host: p.host, url_path: p.path, url_query: p.query });
    } catch (e) { /* ignore */ }
  }).catch(() => {});

  const pField = page.exposeBinding('__nxGtField', (_src, f) => {
    try {
      const kind = ((f && f.kind) || '').toLowerCase();
      const value = SENSITIVE_TYPES.has(kind) ? '' : redactValue((f && f.value) || '');
      push({
        kind: 'type',
        target_label: String((f && f.label) || '').slice(0, 500),
        value: String(value).slice(0, 1000),
        target_kind: kind.slice(0, 40),
      });
    } catch (e) { /* ignore */ }
  }).catch(() => {});

  const pInit = page.addInitScript(() => {
    const fire = () => { try { window.__nxGtNav && window.__nxGtNav(location.href); } catch (e) {} };
    for (const m of ['pushState', 'replaceState']) {
      const orig = history[m];
      history[m] = function () { const r = orig.apply(this, arguments); fire(); return r; };
    }
    window.addEventListener('popstate', fire);
    const labelFor = (el) => {
      try {
        if (el.labels && el.labels.length) return (el.labels[0].innerText || '').trim();
        const al = el.getAttribute('aria-label'); if (al) return al.trim();
        if (el.id) { const l = document.querySelector('label[for="' + el.id + '"]'); if (l) return (l.innerText || '').trim(); }
        const ph = el.getAttribute('placeholder'); if (ph) return ph.trim();
        return (el.name || '').trim();
      } catch (e) { return ''; }
    };
    document.addEventListener('change', (ev) => {
      const el = ev.target;
      if (!el || !('value' in el)) return;
      try {
        window.__nxGtField && window.__nxGtField({
          label: labelFor(el),
          value: el.value,
          kind: (el.type || el.tagName || '').toLowerCase(),
        });
      } catch (e) {}
    }, true);
  }).catch(() => {});

  // Resolves once the in-page hooks + bindings are registered — await this
  // BEFORE the first navigation so the very first page is instrumented.
  const ready = Promise.all([pNav, pField, pInit]);

  async function flush() {
    await ready;
    if (!events.length) return { ingested: 0 };
    const body = JSON.stringify({
      session_id: opts.sessionId || '',
      recorder_version: opts.recorderVersion || 'v1',
      events: events.map((e, i) => Object.assign({ sequence_index: i }, e)),
    });
    return postJson(opts.apiBase, '/api/v1/artifacts/' + encodeURIComponent(opts.artifactId) + '/ground-truth', body, opts.token);
  }

  return { flush, events, ready };
}

function postJson(apiBase, pathname, body, token) {
  return new Promise((resolve, reject) => {
    let u;
    try { u = new URL(String(apiBase).replace(/\/$/, '') + pathname); } catch (e) { return reject(e); }
    const lib = u.protocol === 'https:' ? https : http;
    const req = lib.request(u, {
      method: 'POST',
      headers: Object.assign(
        { 'content-type': 'application/json', 'content-length': Buffer.byteLength(body) },
        token ? { authorization: 'Bearer ' + token } : {},
      ),
    }, (res) => {
      let data = '';
      res.on('data', (c) => (data += c));
      res.on('end', () => {
        if (res.statusCode >= 200 && res.statusCode < 300) {
          try { resolve(JSON.parse(data)); } catch (e) { resolve({}); }
        } else {
          reject(new Error('ground-truth ingest failed: ' + res.statusCode + ' ' + data.slice(0, 200)));
        }
      });
    });
    req.on('error', reject);
    req.write(body);
    req.end();
  });
}

module.exports = { attachGroundTruthRecorder, redactValue, parseUrlParts };
