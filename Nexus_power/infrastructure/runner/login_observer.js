'use strict';
/**
 * Login observer — records the CHOREOGRAPHY of an interactive login.
 *
 * When a tester logs in by hand inside the runner's headed auth-capture browser we
 * already keep the resulting session (cookies). That session is ONE member. This
 * observer records, in order, *how* the login was performed — which fields were
 * filled and which controls were pressed — so the same login can later be REPLAYED
 * with any other member's credentials ("record once, run with N member numbers").
 *
 * SECURITY — this module never captures a credential VALUE. Not redacted: never
 * read. Only field IDENTIFIERS (name/id/label/type/autocomplete) and control names
 * leave the page. A login recipe is choreography, never secrets; the secrets live
 * only in the envelope-encrypted credential card.
 *
 * GENERIC — protocol-level DOM instrumentation, zero per-app config. A field is
 * whatever the app calls it (member_number | email | username | policy_no | pin |
 * password | …); the observer reports the identifier verbatim and never assumes a
 * vocabulary. Works unchanged on any of 1000+ apps.
 *
 * DUMB BY DESIGN — this emits a flat, ordered event log and nothing else. Turning
 * events into a recipe (which fill is a credential slot, which click is the submit,
 * which trailing clicks are an optional verify-documents interstitial) is done in
 * `login_observation.py` on the platform side, where it is unit-tested. Keeping the
 * inference out of the runner keeps this hot-deployed file small and low-risk.
 *
 * Companion to ground_truth_recorder.js — same proven binding/init-script
 * technique, different purpose; that one feeds the page-visit overlay and DOES
 * capture (redacted) values, this one feeds the login recipe and captures none.
 */

/** Field identifier resolution, most-stable first. The winner becomes the recipe's
 *  slot name AND the fingerprint's field name, so record-time and match-time keys
 *  agree (see login_fingerprint._field_signature). */
const IDENT_ORDER = ['name', 'id', 'autocomplete', 'label'];

/** Origin + path only. An OIDC/SAML redirect carries the authorization code,
 *  id_token or SAMLResponse in the query or fragment; the recipe only ever needs
 *  the path, so the secret-bearing parts are dropped AT THE SOURCE and never
 *  reach the event log, the observation route, or the database. */
function safeUrl(raw) {
  const text = String(raw || '');
  try {
    const u = new URL(text);
    return (u.origin || '') + (u.pathname || '');
  } catch (e) {
    return text.split('?')[0].split('#')[0].slice(0, 2000);
  }
}

/**
 * Instrument a Playwright BrowserContext (preferred) or a single page for login
 * observation. Returns `{ events, ready, snapshot }` — `events` is the live
 * ordered log, `snapshot()` returns a defensive copy plus the current URL.
 *
 * Attach to the CONTEXT, not a page: an enterprise SSO login (Okta/Ping/Entra)
 * routinely opens a second tab, and page-scoped bindings would record nothing
 * for it. Context scope also registers the init script before the first page
 * exists, so the login page itself is instrumented.
 */
function attachLoginObserver(target, opts) {
  const options = opts || {};
  const startMs = Date.now();
  const events = [];
  const MAX_EVENTS = Number(options.maxEvents || 400);   // bound memory on a long session

  // Duck-type: a BrowserContext has newPage(), a Page does not.
  const isContext = !!(target && typeof target.newPage === 'function');
  const ctx = isContext ? target : (target && target.context ? target.context() : target);

  const push = (e) => {
    if (events.length >= MAX_EVENTS) return;
    events.push(Object.assign({
      timestamp_ms: Date.now() - startMs,
      sequence_index: events.length,
    }, e));
  };

  // ── Navigations (full page). SPA route changes arrive via __nxLoginNav below. ──
  const wirePage = (p) => {
    try {
      p.on('framenavigated', (frame) => {
        try {
          if (frame !== p.mainFrame()) return;
          const url = frame.url() || '';
          if (!url || url === 'about:blank') return;
          push({ kind: 'navigate', url: safeUrl(url) });
        } catch (e) { /* never break the capture */ }
      });
    } catch (e) { /* ignore */ }
  };
  try { ctx.on('page', wirePage); } catch (e) { /* single-page fallback */ }
  try { (ctx.pages ? ctx.pages() : [target]).forEach(wirePage); } catch (e) { /* ignore */ }
  if (!isContext && target) wirePage(target);

  const pNav = ctx.exposeBinding('__nxLoginNav', (src, url) => {
    try {
      const base = (src && src.page && src.page.url && src.page.url()) || '';
      const abs = new URL(String(url), base || undefined).toString();
      push({ kind: 'navigate', url: safeUrl(abs) });
    } catch (e) { /* ignore */ }
  }).catch(() => {});

  // ── Field commits — IDENTIFIERS ONLY, never the value. ──────────────────────
  const pField = ctx.exposeBinding('__nxLoginField', (_src, f) => {
    try {
      const field = f || {};
      push({
        kind: 'fill',
        name: String(field.name || '').slice(0, 200),
        id: String(field.id || '').slice(0, 200),
        label: String(field.label || '').slice(0, 300),
        type: String(field.type || '').toLowerCase().slice(0, 40),
        autocomplete: String(field.autocomplete || '').slice(0, 100),
        // `filled` records only THAT a value was entered — never any part of it.
        filled: !!field.filled,
      });
    } catch (e) { /* ignore */ }
  }).catch(() => {});

  // ── Control presses — the submit ladder and any interstitial acknowledgement. ─
  const pClick = ctx.exposeBinding('__nxLoginClick', (_src, c) => {
    try {
      const ctl = c || {};
      push({
        kind: 'click',
        text: String(ctl.text || '').slice(0, 200),
        role: String(ctl.role || '').slice(0, 40),
        name: String(ctl.name || '').slice(0, 200),
        id: String(ctl.id || '').slice(0, 200),
        type: String(ctl.type || '').toLowerCase().slice(0, 40),
      });
    } catch (e) { /* ignore */ }
  }).catch(() => {});

  const pInit = ctx.addInitScript(() => {
    const fire = () => { try { window.__nxLoginNav && window.__nxLoginNav(location.href); } catch (e) {} };
    for (const m of ['pushState', 'replaceState']) {
      const orig = history[m];
      history[m] = function () { const r = orig.apply(this, arguments); fire(); return r; };
    }
    window.addEventListener('popstate', fire);

    // Human-readable name for a control/field, best-effort, most-explicit first.
    const labelFor = (el) => {
      try {
        if (el.labels && el.labels.length) return (el.labels[0].innerText || '').trim();
        const al = el.getAttribute('aria-label'); if (al) return al.trim();
        if (el.id) {
          const l = document.querySelector('label[for="' + (window.CSS && CSS.escape ? CSS.escape(el.id) : el.id) + '"]');
          if (l) return (l.innerText || '').trim();
        }
        const ph = el.getAttribute('placeholder'); if (ph) return ph.trim();
        return (el.name || '').trim();
      } catch (e) { return ''; }
    };

    // A committed field. We read el.value ONLY to test emptiness, never to send it.
    const seenFields = new Set();
    // Controls that are NOT credential slots. A "Remember me" checkbox always
    // reports value "on", so without this every login would record a phantom
    // slot no card can ever fill. A <select> cannot be replayed by a text fill
    // either. Skipping them is honest: they are not credentials.
    const NOT_A_SLOT = new Set(['submit', 'button', 'hidden', 'checkbox', 'radio',
      'reset', 'image', 'file', 'range', 'color']);
    const reportField = (el) => {
      if (!el || !('value' in el)) return;
      try {
        const tag = (el.tagName || '').toLowerCase();
        if (tag === 'button' || tag === 'select') return;
        const type = (el.type || tag || '').toLowerCase();
        if (NOT_A_SLOT.has(type) || type.indexOf('select') === 0) return;
        const filled = !!(el.value && String(el.value).length);
        if (!filled) return;
        // De-dupe: `input` fires per keystroke and `change` again on blur; one
        // event per field is what the choreography needs. Keyed on every
        // identifier we ship, so two label-only fields do not collide.
        const key = (el.name || '') + '|' + (el.id || '') + '|' + type + '|'
          + (labelFor(el) || '') + '|' + ((el.getAttribute && el.getAttribute('autocomplete')) || '');
        if (seenFields.has(key)) return;
        seenFields.add(key);
        window.__nxLoginField && window.__nxLoginField({
          name: el.name || '',
          id: el.id || '',
          label: labelFor(el),
          type: type,
          autocomplete: el.getAttribute && (el.getAttribute('autocomplete') || ''),
          filled: true,
        });
      } catch (e) {}
    };
    // `change` alone is not enough: it fires on commit/blur, so a div-based login
    // where the operator types and presses Enter without ever blurring would
    // record ZERO fields. `input` catches those; the de-dupe above keeps them one.
    document.addEventListener('change', (ev) => reportField(ev.target), true);
    document.addEventListener('input', (ev) => reportField(ev.target), true);
    // Enter-to-submit is a press of the form's default control, not a click —
    // record it so the submit rung is never missing.
    document.addEventListener('keydown', (ev) => {
      try {
        if (ev.key !== 'Enter') return;
        const el = ev.target;
        if (!el || (el.tagName || '').toLowerCase() !== 'input') return;
        reportField(el);
        // Resolve the control Enter actually activates — the form's default
        // button. If we cannot NAME it, emit nothing: a step called "Enter"
        // matches no control on replay, and a fabricated rung is worse than a
        // missing one (the derivation surfaces "no submit observed" honestly).
        const form = el.form;
        if (!form) return;
        const btn = form.querySelector('button[type="submit"], input[type="submit"], button:not([type])');
        if (!btn) return;
        const name = (btn.getAttribute('aria-label') || btn.innerText || btn.value
          || btn.getAttribute('title') || '').trim();
        if (!name) return;
        // A real click event follows when the browser activates the button; the
        // click handler de-dupes on its own, so mark this as keyboard-origin.
        window.__nxLoginClick && window.__nxLoginClick({
          text: name, role: 'button', name: btn.name || '', id: btn.id || '',
          type: 'submit', via: 'keyboard',
        });
      } catch (e) {}
    }, true);

    // A pressed control. Walk up from the event target so a click on the <span>
    // inside a <button> still resolves to the button.
    document.addEventListener('click', (ev) => {
      try {
        let el = ev.target;
        for (let hops = 0; el && hops < 5; hops++) {
          const tag = (el.tagName || '').toLowerCase();
          const role = (el.getAttribute && el.getAttribute('role')) || '';
          const type = (el.type || '').toLowerCase();
          const isControl = tag === 'button' || tag === 'a' || role === 'button'
            || (tag === 'input' && (type === 'submit' || type === 'button'));
          if (isControl) {
            // An icon-only submit has no innerText; without the aria-label/title
            // fallback its click is recorded nameless and silently dropped, so
            // the recipe loses its submit rung entirely.
            const text = ((el.getAttribute && el.getAttribute('aria-label')) || el.innerText
              || el.value || (el.getAttribute && el.getAttribute('title')) || '').trim();
            window.__nxLoginClick && window.__nxLoginClick({
              text: text,
              role: role || tag,
              name: el.name || '',
              id: el.id || '',
              type: type,
            });
            return;
          }
          el = el.parentElement;
        }
      } catch (e) {}
    }, true);
  }).catch(() => {});

  // Resolves once bindings + in-page hooks are registered — await BEFORE the first
  // navigation so the login page itself is instrumented.
  const ready = Promise.all([pNav, pField, pClick, pInit]);

  function snapshot() {
    // The landing page is wherever the login FINISHED — with an SSO popup that is
    // the last page in the context, not the one we started on.
    let currentUrl = '';
    try {
      const pages = ctx.pages ? ctx.pages() : [];
      currentUrl = (pages.length ? pages[pages.length - 1].url()
        : (target && target.url ? target.url() : '')) || '';
    } catch (e) { currentUrl = ''; }
    return {
      observer_version: 'v1',
      start_url: safeUrl(options.startUrl || ''),
      current_url: safeUrl(currentUrl),
      truncated: events.length >= MAX_EVENTS,
      events: events.map((e) => Object.assign({}, e)),
    };
  }

  return { events, ready, snapshot };
}

module.exports = { attachLoginObserver };
