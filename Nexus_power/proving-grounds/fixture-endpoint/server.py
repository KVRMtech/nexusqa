"""A CLIENT'S ENVIRONMENT, STANDING IN — the two-endpoint REST fixture (I1).

WHAT THIS IS. ``env_data_transports.RestProvider`` asks a client's own system for
the values a crawl needs, over a contract deliberately small enough that a client
can implement it in an afternoon:

    GET {base}/slots            -> {"slots": ["member number", ...]}
    GET {base}/value/{slot_key} -> {"value": "25000001"}

Two read-only endpoints, no schema to agree, nothing to export. Until now there
was nothing on the other end of it in this repository, so the whole
environment-provider path could only be exercised against a mock written by the
same person as the code under test. This is the other end.

WRITTEN AGAINST THE CONSUMER, NOT AGAINST AN IDEA OF IT. Every behaviour below is
one ``RestProvider`` actually depends on, read off
``platform/qe-central/app/services/env_data_transports.py``:

  * **Bearer token.** ``_headers`` sends ``Authorization: Bearer <token>`` when a
    token is configured. A fixture that ignored it would let a broken auth path
    pass its own test.
  * **200 or nothing.** Any other status is treated as a decline, so an unknown
    slot must be a clean 404 rather than ``{"value": null}`` — the provider reads
    a 200 body and would take ``None`` as an answer it had received.
  * **A JSON OBJECT.** ``body if isinstance(body, dict) else None`` — a bare list
    or string is a decline.
  * **512 characters.** ``MAX_VALUE_CHARS``; the provider treats anything longer
    as a misconfiguration rather than a value, so this refuses to serve one.

STDLIB ONLY, on purpose. A proving ground has to run from a bare
``python:3.11-slim`` with no install step, and a fixture that needs a dependency
tree is a fixture that will be skipped in CI.

VALUES ARE FICTIONAL AND SAY SO. Nothing here is a real person's data; the point
is provenance, not plausibility. A field answered from this endpoint arrives in
the ledger as ``env``-provenance, which is exactly the thing a crawl cannot fake
by inventing a value itself.

Run:  python server.py --port 8130 --token dev-token
"""
from __future__ import annotations

import argparse
import json
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlsplit

logger = logging.getLogger("fixture-endpoint")

#: The provider's own cap. Serving more is a misconfiguration, not a value.
MAX_VALUE_CHARS = 512

#: The golden set's fields, keyed the way a page LABELS them — the resolver
#: matches on the client's wording, not on a schema nobody agreed.
SLOTS: dict[str, str] = {
    "member number": "25000001",
    "policy number": "VK-2026-000117",
    "first name": "Ada",
    "last name": "Lovelace",
    "date of birth": "1970-04-12",
    "email address": "ada.lovelace@example.test",
    "gender": "Female",
    "annual income": "82000",
    "face amount": "250000",
    "employer name": "Analytical Engines Ltd",
    "occupation": "Mathematician",
}


class Handler(BaseHTTPRequestHandler):
    server_version = "qec-fixture-endpoint/1.0"
    token = ""

    # ── plumbing ────────────────────────────────────────────────────────────
    def log_message(self, fmt: str, *args) -> None:      # noqa: A003
        logger.info("%s - %s", self.address_string(), fmt % args)

    def _json(self, status: int, body: dict) -> None:
        raw = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _authorised(self) -> bool:
        """No token configured means an open fixture, which is a deliberate
        local-development shape. A configured token is ENFORCED — a fixture that
        accepted anything would let a broken Authorization header pass."""
        if not self.token:
            return True
        return self.headers.get("Authorization", "") == f"Bearer {self.token}"

    # ── the two endpoints ───────────────────────────────────────────────────
    def do_GET(self) -> None:                            # noqa: N802
        path = unquote(urlsplit(self.path).path)
        if not self._authorised():
            # 401, not 200-with-an-error: the provider reads a 200 body and
            # would treat an error object as an answer it had received.
            self._json(401, {"error": "unauthorised"})
            return
        if path == "/slots":
            self._json(200, {"slots": sorted(SLOTS)})
            return
        if path.startswith("/value/"):
            key = path[len("/value/"):].strip().lower()
            value = SLOTS.get(key)
            if value is None:
                # A CLEAN 404. Returning {"value": null} with a 200 would hand
                # the resolver a None it had successfully fetched, and an
                # unanswerable slot would read as an answered one.
                self._json(404, {"error": "no such slot"})
                return
            if len(value) > MAX_VALUE_CHARS:
                self._json(500, {"error": "value exceeds the provider's cap"})
                return
            self._json(200, {"value": value})
            return
        if path == "/healthz":
            self._json(200, {"ok": True, "slots": len(SLOTS)})
            return
        self._json(404, {"error": "not found"})


def build_server(port: int = 0, token: str = "") -> ThreadingHTTPServer:
    """Bind and return; the caller runs it. ``port=0`` picks a free port, which
    is what the tests use so two runs never collide."""
    handler = type("BoundHandler", (Handler,), {"token": token})
    return ThreadingHTTPServer(("127.0.0.1", port), handler)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8130)
    parser.add_argument("--token", default="", help="Bearer token to require")
    parser.add_argument("--host", default="0.0.0.0")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    handler = type("BoundHandler", (Handler,), {"token": args.token})
    server = ThreadingHTTPServer((args.host, args.port), handler)
    logger.info("fixture endpoint on http://%s:%d — %d slots, auth=%s",
                args.host, args.port, len(SLOTS), "on" if args.token else "off")
    server.serve_forever()


if __name__ == "__main__":
    main()
