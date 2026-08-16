#!/usr/bin/env node
/**
 * jsdom EXECUTION LANE for the QE Explorer capture engine (T-HN-01).
 *
 * This runner executes the PRODUCTION injected JavaScript — the exact string
 * held in `app/inventory_js.py` (INVENTORY_JS / OPAQUE_JS / DISPLAYED_VALUES_JS)
 * — inside a real JavaScript engine against a real DOM.  Nothing about the
 * capture logic is re-implemented, patched or wrapped here: the Python side
 * writes the constant verbatim to a file, this runner reads it and hands it to
 * `window.eval`, which is semantically what Playwright's `page.evaluate(<expr>)`
 * does with an expression string.
 *
 * Contract (stdin → stdout, both single-line JSON):
 *
 *   IN  { "url": "http://127.0.0.1:PORT/fixture/index.html",
 *         "js_path": "/abs/path/to/production-snippet.js",
 *         "timeout_ms": 10000 }
 *
 *   OUT { "ok": true, "result": <whatever the production snippet returned>,
 *         "capabilities": { ... }, "console": [...] }
 *   OUT { "ok": false, "error": "...", "stack": "..." }
 *
 * The fixture is fetched over HTTP (`JSDOM.fromURL`) rather than read off disk
 * so that ONE fixture set drives both this lane and the Playwright lane —
 * same URL, same origin semantics, same subresource loading.
 *
 * DOCUMENTED jsdom LIMITATIONS (probed, never silently papered over — see the
 * `capabilities` block returned with every run; `tests/browser/
 * test_jsdom_execution.py::test_jsdom_capability_probe` pins them):
 *
 *   * `HTMLElement.innerText` is NOT implemented by jsdom.  The production
 *     `accText()` helper deliberately falls back to `textContent` when
 *     innerText yields nothing, so the walker still runs — but the RENDERED-text
 *     accessible-name semantics (block children contributing "A B", not "AB")
 *     cannot be observed in this lane.  Those assertions live in the Playwright
 *     lane, which has a real layout engine.  NOT polyfilled: faking innerText
 *     would fabricate the exact behaviour fixture 05 exists to adjudicate.
 *   * jsdom 24 ships NO `CSS` object at all, so `CSS.escape` is a ReferenceError.
 *     This one IS supplied (see `CSS_ESCAPE_SHIM`) because without it the
 *     production `label[for]` rung throws into its own try/catch on EVERY
 *     element and silently returns no name — the jsdom lane would then measure
 *     an artefact of the runtime rather than the walker.  The shim is the
 *     CSSOM-spec algorithm, installed on the ENVIRONMENT before the production
 *     snippet is read; the production snippet is never touched.
 *   * jsdom has no true cross-origin iframe isolation, so the cross-origin
 *     fixture is a Playwright-lane fixture.
 *   * `getBoundingClientRect()` returns zeros (no layout), so OPAQUE_JS's
 *     size-gated detections are Playwright-lane only.
 *
 * The rule, stated once: a missing API is reported as `capability = false` and
 * the affected assertion is routed to the Playwright lane.  EXACTLY ONE API is
 * supplied instead of routed — `CSS.escape` — and only because its absence
 * silently disables the walker's first naming rung for every element, which
 * would make this lane measure jsdom rather than the capture engine.  Anything
 * whose absence merely means "jsdom cannot see this" is left absent, because
 * supplying it would fabricate the behaviour under test.
 */
"use strict";

const fs = require("fs");
const { JSDOM, VirtualConsole } = require("jsdom");

const DEFAULT_TIMEOUT_MS = 15000;

function readStdin() {
  return new Promise((resolve, reject) => {
    let buf = "";
    process.stdin.setEncoding("utf8");
    process.stdin.on("data", (c) => { buf += c; });
    process.stdin.on("end", () => resolve(buf));
    process.stdin.on("error", reject);
  });
}

/** Resolve once the document AND every reachable same-origin iframe are loaded.
 *
 * Explicit readiness, never a sleep: we poll a *condition* (document.readyState
 * plus each iframe's contentDocument.readyState) on the macrotask queue and fail
 * loudly on timeout, so a fixture that never settles is a hard error rather than
 * a flaky pass. */
function waitForReady(window, timeoutMs) {
  return new Promise((resolve, reject) => {
    const deadline = Date.now() + timeoutMs;
    const check = () => {
      let ready = false;
      try {
        ready = window.document.readyState === "complete";
        if (ready) {
          const frames = window.document.querySelectorAll("iframe");
          for (let i = 0; i < frames.length; i++) {
            let cdoc = null;
            try { cdoc = frames[i].contentDocument; } catch (e) { cdoc = null; }
            // A null contentDocument is a cross-origin (or blocked) frame: the
            // production walker skips it honestly, so it is "ready" for us too.
            if (cdoc && cdoc.readyState !== "complete") { ready = false; break; }
          }
        }
      } catch (e) { /* transient during teardown */ }
      if (ready) { resolve(); return; }
      if (Date.now() > deadline) {
        reject(new Error("timed out waiting for document + iframes to reach readyState=complete"));
        return;
      }
      setTimeout(check, 10);
    };
    check();
  });
}

/** CSSOM `CSS.escape`, installed ONLY when the runtime lacks it.
 *
 * jsdom 24 has no `CSS` object, so the production walker's first
 * accessible-name rung — `doc.querySelectorAll('label[for="' + CSS.escape(el.id)
 * + '"]')` — throws a ReferenceError caught by its own `try {} catch (e) {}`.
 * Every labelled control would come back unnamed, and the jsdom lane would be
 * measuring the runtime rather than the walker.
 *
 * This is the CSSOM specification algorithm (§ "the escape() operation"),
 * transcribed, not an approximation. It is installed on `window` BEFORE the
 * production snippet is evaluated and is reported as
 * `capabilities.css_escape_polyfilled` so no result from this lane can be
 * mistaken for one obtained on a real browser. Chromium provides the real thing,
 * so the Playwright lane never sees this code.
 */
const CSS_ESCAPE_SHIM = `(() => {
  if (typeof CSS !== "undefined" && typeof CSS.escape === "function") return false;
  var escape = function (value) {
    var string = String(value);
    var length = string.length;
    var index = -1, codeUnit, result = "";
    var firstCodeUnit = string.charCodeAt(0);
    if (length === 1 && firstCodeUnit === 0x002D) return "\\\\" + string;
    while (++index < length) {
      codeUnit = string.charCodeAt(index);
      if (codeUnit === 0x0000) { result += "\\uFFFD"; continue; }
      if ((codeUnit >= 0x0001 && codeUnit <= 0x001F) || codeUnit === 0x007F ||
          (index === 0 && codeUnit >= 0x0030 && codeUnit <= 0x0039) ||
          (index === 1 && codeUnit >= 0x0030 && codeUnit <= 0x0039 &&
           firstCodeUnit === 0x002D)) {
        result += "\\\\" + codeUnit.toString(16) + " ";
        continue;
      }
      if (codeUnit >= 0x0080 || codeUnit === 0x002D || codeUnit === 0x005F ||
          (codeUnit >= 0x0030 && codeUnit <= 0x0039) ||
          (codeUnit >= 0x0041 && codeUnit <= 0x005A) ||
          (codeUnit >= 0x0061 && codeUnit <= 0x007A)) {
        result += string.charAt(index);
        continue;
      }
      result += "\\\\" + string.charAt(index);
    }
    return result;
  };
  if (typeof CSS === "undefined") { window.CSS = {}; }
  window.CSS.escape = escape;
  return true;
})()`;

/** Probe which browser APIs this jsdom build actually provides.
 *
 * Returned with every run so a jsdom-lane result is never mistaken for a
 * full-browser result. Runs inside the page realm, touching only read-only APIs. */
const CAPABILITY_PROBE = `(() => {
  var probe = document.createElement("div");
  probe.innerHTML = "<b>A</b>";
  var caps = {};
  caps.innerText = (function () {
    try { return typeof probe.innerText === "string"; } catch (e) { return false; }
  })();
  caps.css_escape = (function () {
    try { return typeof CSS !== "undefined" && typeof CSS.escape === "function"; }
    catch (e) { return false; }
  })();
  caps.attach_shadow = typeof Element.prototype.attachShadow === "function";
  caps.get_root_node = typeof Node.prototype.getRootNode === "function";
  caps.computed_style = (function () {
    try { return !!window.getComputedStyle(document.body); } catch (e) { return false; }
  })();
  caps.layout_boxes = (function () {
    try {
      var r = document.body.getBoundingClientRect();
      return !!(r && (r.width > 0 || r.height > 0));
    } catch (e) { return false; }
  })();
  caps.closest = typeof Element.prototype.closest === "function";
  caps.is_content_editable = "isContentEditable" in HTMLElement.prototype;
  return caps;
})()`;

/**
 * `--selftest` — prove node + jsdom can execute a snippet AT ALL.
 *
 * The point is diagnostic separation: when the jsdom lane goes red, this says
 * whether the RUNTIME broke (node/jsdom missing, wrong version, install
 * half-done) or the CAPTURE logic regressed. Without it both look identical.
 *
 * It deliberately does NOT read stdin and does NOT need the fixture server:
 * `main()` blocks on stdin, so invoking the runner with no job (which is what
 * `npm run selftest` does) previously hung until the CI job timed out, or exited
 * 2 with "bad job JSON" where stdin was closed. Either way the step could never
 * pass. This branch runs before that read.
 *
 * Exercises the same machinery a real job uses — DOM construction, window.eval,
 * the CSS.escape shim, the capability probe, structured-clone round-trip — so a
 * green selftest is a real statement about the lane, not a print.
 */
function selftest() {
  const checks = [];
  const record = (name, pass, detail) => {
    checks.push({ name, pass: !!pass, detail: detail === undefined ? "" : String(detail) });
  };

  let dom = null;
  try {
    dom = new JSDOM(
      `<!doctype html><html><body>
         <label for="probe">Probe label</label><input id="probe">
         <button>Probe button</button>
       </body></html>`,
      { url: "https://selftest.local/", runScripts: "outside-only" }
    );
    const w = dom.window;

    record("jsdom constructs a document", !!w.document);
    record("window.eval executes JavaScript", w.eval("1 + 1") === 2);
    record("querySelectorAll reaches the DOM",
      w.eval("document.querySelectorAll('input,button').length") === 2);

    const polyfilled = w.eval(CSS_ESCAPE_SHIM);
    record("CSS.escape is callable after the shim",
      typeof w.CSS === "object" && typeof w.CSS.escape === "function",
      "polyfilled=" + polyfilled);
    record("CSS.escape escapes selector metacharacters",
      w.eval("CSS.escape('a:b.c')") === "a\\:b\\.c");

    // The rung the shim exists for: label[for] with a plain id must resolve.
    record("label[for] selector resolves",
      w.eval("!!document.querySelector('label[for=\"' + CSS.escape('probe') + '\"]')"));

    const caps = w.eval(CAPABILITY_PROBE);
    record("capability probe returns an object", caps && typeof caps === "object",
      "keys=" + (caps ? Object.keys(caps).length : 0));

    // Structured-clone round-trip — the same hop every real result makes.
    const cloned = JSON.parse(JSON.stringify({ ok: true, caps: caps }));
    record("result survives the JSON round-trip", cloned && cloned.ok === true);
  } catch (err) {
    record("selftest ran without throwing", false, err && err.message ? err.message : String(err));
  } finally {
    try { if (dom) dom.window.close(); } catch (e) { /* best effort */ }
  }

  const failed = checks.filter((c) => !c.pass);
  process.stdout.write(JSON.stringify({
    ok: failed.length === 0,
    selftest: true,
    node: process.version,
    jsdom: (() => {
      try { return require("jsdom/package.json").version; } catch (e) { return "unknown"; }
    })(),
    checks: checks,
  }, null, 2) + "\n");
  process.exitCode = failed.length === 0 ? 0 : 1;
}

async function main() {
  if (process.argv.includes("--selftest")) {
    selftest();
    return;
  }

  const raw = (await readStdin()).trim();
  let job;
  try {
    job = JSON.parse(raw);
  } catch (e) {
    process.stdout.write(JSON.stringify({ ok: false, error: "bad job JSON: " + e.message }) + "\n");
    process.exitCode = 2;
    return;
  }

  const timeoutMs = Number(job.timeout_ms) || DEFAULT_TIMEOUT_MS;
  const logs = [];
  const virtualConsole = new VirtualConsole();
  virtualConsole.on("jsdomError", (err) => {
    // Fixture script errors must be visible, not swallowed — a fixture whose
    // setup script threw would otherwise produce a silently empty inventory.
    logs.push("jsdomError: " + (err && err.message ? err.message : String(err)));
  });
  for (const level of ["log", "warn", "error", "info"]) {
    virtualConsole.on(level, (...args) => {
      logs.push(level + ": " + args.map((a) => String(a)).join(" "));
    });
  }

  let dom = null;
  try {
    const source = fs.readFileSync(job.js_path, "utf8");

    dom = await JSDOM.fromURL(job.url, {
      runScripts: "dangerously",   // fixture setup scripts (attachShadow, etc.)
      resources: "usable",          // fetch <iframe src>, <link>, <script src>
      pretendToBeVisual: true,      // rAF + a visual-ish default view
      virtualConsole,
    });

    await waitForReady(dom.window, timeoutMs);

    // Environment repair BEFORE the production snippet is read. Reports whether
    // it actually did anything, so the result carries its own provenance.
    const cssEscapePolyfilled = dom.window.eval(CSS_ESCAPE_SHIM);

    const capabilities = dom.window.eval(CAPABILITY_PROBE);
    capabilities.css_escape_polyfilled = !!cssEscapePolyfilled;

    // ── THE PRODUCTION SNIPPET, VERBATIM ──────────────────────────────────
    // `source` is the byte-for-byte value of app.inventory_js.INVENTORY_JS
    // (or OPAQUE_JS / DISPLAYED_VALUES_JS).  window.eval of an expression is
    // the jsdom equivalent of Playwright's page.evaluate(<expression string>).
    const result = dom.window.eval(source);

    // Structured-clone across the realm boundary the same way Playwright's
    // JSON serialisation does, so both lanes hand Python identical shapes.
    const plain = JSON.parse(JSON.stringify(result === undefined ? null : result));

    process.stdout.write(JSON.stringify({
      ok: true,
      result: plain,
      capabilities: capabilities,
      console: logs,
    }) + "\n");
  } catch (err) {
    process.stdout.write(JSON.stringify({
      ok: false,
      error: err && err.message ? err.message : String(err),
      stack: err && err.stack ? String(err.stack) : "",
      console: logs,
    }) + "\n");
    process.exitCode = 1;
  } finally {
    try { if (dom) dom.window.close(); } catch (e) { /* best effort */ }
  }
}

main();
