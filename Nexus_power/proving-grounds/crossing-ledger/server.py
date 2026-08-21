"""A REAL application whose irreversible action can be COUNTED.

WHY A NEW PROVING GROUND
========================
A35 asks for proof that a fault-injected crawl does not submit twice. That
question can only be answered where a submission actually lands: **the server**.
Counting in the crawler answers a different and much weaker question — whether
the crawler *believes* it submitted once.

The existing grounds cannot answer it:

  * ``acme-life`` and ``questionnaire-life`` are STATIC HTML. Their "Bind policy"
    button changes the DOM and nothing else, so a double-submit is invisible by
    construction — the test would pass whether or not the journal worked.
  * ``vkpower-life`` and ``summit-life-carrier`` do have servers, but they are
    other squads' fixtures with recorded goldens attached to them; adding a
    ledger endpoint to either would change evidence that is not mine.

So this ground exists for exactly one purpose: to make "how many times was the
irreversible thing done?" a question with a factual answer.

IT DELIBERATELY DOES NOT DEDUPLICATE
====================================
There is no idempotency key, no request-id check, no "already bound" guard. That
is not an oversight — it is the whole point. If the application refused a second
bind, then "zero double-submits" would be a property of the APPLICATION, and the
crossing journal could be entirely broken while the test stayed green. The
server here will faithfully record every bind it is asked to perform, so the
count is a measurement of the CRAWLER's behaviour and nothing else.

THE DELAY IS THE FAULT-INJECTION WINDOW
=======================================
``/bind`` records the bind, then sleeps ``BIND_DELAY_MS`` before answering. That
reproduces the genuinely dangerous shape of an irreversible action: the effect
has happened on the server, and the client does not yet know. A crawler killed in
that window comes back with a reservation and no outcome — which is precisely the
ambiguity exactly-once semantics exist to resolve, and the state a naive "retry
what did not complete" would resolve by binding a second policy.
"""
from __future__ import annotations

import argparse
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

LEDGER_LOCK = threading.Lock()
LEDGER_PATH = Path(os.environ.get("CROSSING_LEDGER_PATH", "crossing_ledger.jsonl"))
BIND_DELAY_MS = int(os.environ.get("CROSSING_BIND_DELAY_MS", "4000"))

FORM_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Meridian Life - Apply</title>
<style>body{font-family:system-ui;margin:2rem;max-width:40rem}
label{display:block;margin:.75rem 0 .25rem}input,select{padding:.4rem;width:100%%}
button{margin-top:1rem;padding:.6rem 1.2rem;font-size:1rem}
.gold{background:#b8860b;color:#fff;border:0;border-radius:4px}</style></head>
<body>
<h1>Meridian Life - Term Application</h1>
<p>Review your details, then bind the policy.</p>
<form method="POST" action="/bind">
  <label for="fullName">Full name</label>
  <input id="fullName" name="fullName" type="text" value="Dana Whitfield" required>
  <label for="email">Email</label>
  <input id="email" name="email" type="email" value="dana@example.test" required>
  <label for="coverage">Coverage amount</label>
  <select id="coverage" name="coverage">
    <option value="250000">$250,000</option>
    <option value="500000" selected>$500,000</option>
  </select>
  <button class="gold" id="confirmBind" type="submit">Bind policy</button>
</form>
<p id="ledgerCount">binds recorded: %(count)d</p>
</body></html>
"""

BOUND_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Policy bound</title></head>
<body><h1>Policy bound</h1>
<p id="policyNumber">Policy number: %(policy)s</p>
<p id="ledgerCount">binds recorded: %(count)d</p>
</body></html>
"""


def _entries() -> list[dict]:
    if not LEDGER_PATH.exists():
        return []
    out = []
    for line in LEDGER_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
    return out


def _record(fields: dict) -> dict:
    """Append one bind. No deduplication, on purpose (see module docstring)."""
    with LEDGER_LOCK:
        existing = _entries()
        entry = {
            "seq": len(existing) + 1,
            "policy": f"MRD-{len(existing) + 1:06d}",
            "at_ms": int(time.time() * 1000),
            "fields": fields,
        }
        with LEDGER_PATH.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")
        return entry


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):        # keep the harness output readable
        pass

    def _send(self, code: int, body: bytes, ctype: str = "text/html; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):                          # noqa: N802
        if self.path.startswith("/_ledger"):
            entries = _entries()
            self._send(200, json.dumps(
                {"binds": len(entries), "entries": entries}).encode(),
                "application/json")
            return
        if self.path.startswith("/_reset"):
            with LEDGER_LOCK:
                LEDGER_PATH.write_text("", encoding="utf-8")
            self._send(200, b'{"reset":true}', "application/json")
            return
        # POST-REDIRECT-GET, and the reason it matters here.
        #
        # The first version of this ground served the APPLICATION FORM for every
        # GET, including GET /bind. A crawler that bound a policy and then
        # navigated back to /bind was therefore shown a second, fully-populated
        # "Bind policy" button at a URL it had not visited before — so it bound
        # a second policy, and the ledger showed 2. That looked exactly like a
        # product defect ("one grant, two irreversible actions") and it was not:
        # it was this fixture inventing a second boundary.
        #
        # A real carrier does not re-offer the bind step after binding. Serving
        # the confirmation for GET /bind removes the artefact, so anything the
        # ledger reports afterwards is attributable to the crawler.
        entries = _entries()
        if self.path.startswith("/bind") and entries:
            last = entries[-1]
            self._send(200, (BOUND_PAGE % {"policy": last["policy"],
                                           "count": len(entries)}).encode())
            return
        self._send(200, (FORM_PAGE % {"count": len(entries)}).encode())

    def do_POST(self):                         # noqa: N802
        if not self.path.startswith("/bind"):
            self._send(404, b"not found")
            return
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8", "replace") if length else ""
        fields = {k: v[0] for k, v in parse_qs(raw).items()}

        # THE EFFECT HAPPENS FIRST, THE ANSWER COMES LATER. A crawler killed in
        # between has caused the bind without ever learning that it did.
        entry = _record(fields)
        if BIND_DELAY_MS > 0:
            time.sleep(BIND_DELAY_MS / 1000.0)
        self._send(200, (BOUND_PAGE % {"policy": entry["policy"],
                                       "count": len(_entries())}).encode())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8710)
    ap.add_argument("--ledger", default="")
    args = ap.parse_args()
    global LEDGER_PATH
    if args.ledger:
        LEDGER_PATH = Path(args.ledger)
    LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not LEDGER_PATH.exists():
        LEDGER_PATH.write_text("", encoding="utf-8")
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"crossing-ledger on http://127.0.0.1:{args.port} "
          f"ledger={LEDGER_PATH} bind_delay_ms={BIND_DELAY_MS}", flush=True)
    srv.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
