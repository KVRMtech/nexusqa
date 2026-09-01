"""M2.4 / T-GEN-06 — the seeded application the generation proof runs against.

A generated specification is only worth what it CATCHES, so the proof needs an
application that can be broken in a controlled, realistic way.  This is that
application: a two-page quote funnel with a real backend, served over real HTTP,
driven by a real browser.

THE TWO REGRESSIONS IT CAN BE SEEDED WITH, and why they are these two.

``NETWORK_SILENT`` — the click stops calling ``POST /api/quote`` and renders the
same premium from a constant baked into the page.  Every pixel is identical: the
button works, the navigation happens, the result page shows $42.50.  A UI-only
assertion suite passes this in full, and it is a REAL and common regression — a
caching layer, a refactor that inlines a default, a feature flag that short-
circuits a call.  This is the case T-GEN-03 exists for, and the only oracle that
can see it is one that asserts the endpoint was actually reached.

``OUTCOME_DRIFT`` — the API is still called and still answers 200, but the
premium it returns changes.  Navigation is correct, the endpoint is correct, the
page renders; the NUMBER the business cares about is wrong.  This is the case
T-GEN-04 exists for, and it is only caught if the confirmed outcome criterion is
a hard assertion rather than an informational log.

The two are deliberately orthogonal: neither regression is visible to the
other's oracle, so a proof that catches both has demonstrated two independent
capabilities rather than one capability twice.

``BASELINE`` is the unmodified application, and the generated spec must be GREEN
against it — a suite that reds on a healthy application has proven nothing about
either regression.

No third-party dependency: ``http.server`` on a thread, which is the same shape
the explorer's own browser-fixture harness uses.
"""
from __future__ import annotations

import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

#: The healthy application.
BASELINE = "baseline"
#: The click no longer calls the API; the UI is byte-identical.
NETWORK_SILENT = "network_silent"
#: The API is still called; the value it returns is wrong.
OUTCOME_DRIFT = "outcome_drift"

MODES = (BASELINE, NETWORK_SILENT, OUTCOME_DRIFT)

#: The premium the healthy application quotes, and the one the crawl observed.
BASELINE_PREMIUM = "42.50"
#: What ``OUTCOME_DRIFT`` quotes instead — a plausible pricing change, not a
#: crash, so nothing but a value oracle can tell the difference.
DRIFTED_PREMIUM = "39.00"

#: The endpoints this application exposes.  The generated spec asserts these by
#: method and path, so they are named here once and read by both sides.
CONFIG_PATH = "/api/config"
QUOTE_PATH = "/api/quote"

_INDEX_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Quote Start</title></head>
<body>
  <h1>Quote Start</h1>
  <p>Term life quote for a 35 year old.</p>
  <button id="get-quote" type="button">Get Quote</button>
  <script>
    // A page-load read.  The entry step's network assertion is grounded in it.
    fetch('__CONFIG__').then(function (r) { return r.json(); }).catch(function () {});

    var SILENT = __SILENT__;
    document.getElementById('get-quote').addEventListener('click', async function () {
      var premium = '__BASELINE_PREMIUM__';
      if (!SILENT) {
        // The healthy path: the commit actually reaches the backend.
        var res = await fetch('__QUOTE__', {
          method: 'POST',
          headers: { 'content-type': 'application/json' },
          body: JSON.stringify({ age: 35, product: 'term-life' })
        });
        var body = await res.json();
        premium = String(body.premium);
      }
      // NETWORK_SILENT renders the SAME value from the constant above, so the
      // result page is indistinguishable without a network oracle.
      sessionStorage.setItem('premium', premium);
      window.location.href = 'result.html';
    });
  </script>
</body></html>
"""

_RESULT_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Quote Result</title></head>
<body>
  <h1>Quote Result</h1>
  <p>Your monthly premium</p>
  <div id="premium">$<span id="premium-value">--</span></div>
  <script>
    document.getElementById('premium-value').textContent =
      sessionStorage.getItem('premium') || '--';
  </script>
</body></html>
"""


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class QuoteAppServer:
    """The fixture application, restartable in any of the three modes.

    ``mode`` is read on EVERY request rather than captured at construction, so a
    test can seed a regression into a RUNNING server and the browser sees it on
    the next load.  That matters for the proof's honesty: the baseline run and
    the regression run then differ in exactly one thing — the seeded defect —
    and not in the port, the process, or anything else the browser touched.
    """

    def __init__(self, mode: str = BASELINE) -> None:
        if mode not in MODES:
            raise ValueError(f"unknown mode {mode!r}; expected one of {MODES}")
        self.mode = mode
        self.origin = ""
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        #: Every request the server actually answered — the independent record
        #: the proof reads to confirm a network regression really happened,
        #: rather than trusting the test's own assertion about it.
        self.requests: list[tuple[str, str]] = []
        self._lock = threading.Lock()

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> "QuoteAppServer":
        port = _free_port()
        self.origin = f"http://127.0.0.1:{port}"
        app = self

        class _Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, fmt, *args):    # silence the test run
                pass

            def _send(self, status: int, body: bytes, mime: str) -> None:
                self.send_response(status)
                self.send_header("Content-Type", mime)
                self.send_header("Content-Length", str(len(body)))
                # No caching anywhere: a cached index.html would let a seeded
                # regression fail to appear and the proof would silently be
                # re-running the baseline.
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def _record(self, method: str) -> str:
                path = (self.path or "/").split("?", 1)[0]
                with app._lock:
                    app.requests.append((method, path))
                return path

            def do_GET(self):                      # noqa: N802 (stdlib name)
                path = self._record("GET")
                if path == CONFIG_PATH:
                    self._send(200, json.dumps(
                        {"product": "term-life", "currency": "USD"},
                    ).encode(), "application/json")
                    return
                if path in ("/", "/index.html"):
                    self._send(200, app.index_html().encode(), "text/html")
                    return
                if path == "/result.html":
                    self._send(200, _RESULT_HTML.encode(), "text/html")
                    return
                self._send(404, b"not found", "text/plain")

            def do_POST(self):                     # noqa: N802 (stdlib name)
                path = self._record("POST")
                length = int(self.headers.get("Content-Length") or 0)
                if length:
                    self.rfile.read(length)
                if path == QUOTE_PATH:
                    premium = (DRIFTED_PREMIUM if app.mode == OUTCOME_DRIFT
                               else BASELINE_PREMIUM)
                    self._send(200, json.dumps({"premium": premium}).encode(),
                               "application/json")
                    return
                self._send(404, b"not found", "text/plain")

        self._server = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._server is not None:
            try:
                self._server.shutdown()
                self._server.server_close()
            except OSError:
                pass
            self._server = None

    # -- seeding -----------------------------------------------------------

    def seed(self, mode: str) -> None:
        """Introduce (or remove) a regression on the RUNNING application."""
        if mode not in MODES:
            raise ValueError(f"unknown mode {mode!r}; expected one of {MODES}")
        self.mode = mode
        self.clear_requests()

    def clear_requests(self) -> None:
        with self._lock:
            self.requests.clear()

    def calls_to(self, method: str, path: str) -> int:
        with self._lock:
            return sum(1 for m, p in self.requests if m == method and p == path)

    # -- content -----------------------------------------------------------

    def index_html(self) -> str:
        return (_INDEX_HTML
                .replace("__CONFIG__", CONFIG_PATH)
                .replace("__QUOTE__", QUOTE_PATH)
                .replace("__BASELINE_PREMIUM__", BASELINE_PREMIUM)
                .replace("__SILENT__",
                         "true" if self.mode == NETWORK_SILENT else "false"))

    def url(self, page: str = "index.html") -> str:
        return f"{self.origin}/{page}"


__all__ = [
    "BASELINE", "NETWORK_SILENT", "OUTCOME_DRIFT", "MODES",
    "BASELINE_PREMIUM", "DRIFTED_PREMIUM",
    "CONFIG_PATH", "QUOTE_PATH", "QuoteAppServer",
]
