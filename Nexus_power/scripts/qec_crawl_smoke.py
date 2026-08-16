#!/usr/bin/env python3
"""QE-Central — CRAWL SMOKE: one real crawl, end to end, every run.

WHY THIS EXISTS. Every other gate in CI reasons about the crawl from fixtures:
hand-authored RawControl dicts, a golden manifest.jsonl, mocked Playwright. All
of them stay green while the actual funnel is broken, because a fixture models
what someone BELIEVED the browser does. This job is the one place where a real
Chromium loads a real page, the real injected inventory JS runs inside it, the
real crawler writes a real manifest, and the real qe-central mapper turns that
manifest into the bundle the substrate would persist.

It proves, in order, and refuses to pass on any of them:

  1. the explorer service comes up and reports healthy;
  2. a crawl DISPATCHES through the real POST /api/v1/explore contract;
  3. the crawl COMPLETES (a terminal stop_reason, not a timeout);
  4. a manifest was WRITTEN to the work dir;
  5. the manifest contains page_state records -> PAGES > 0;
  6. qe-central's REAL manifest mapper ingests it into a bundle -> the
     substrate would receive page visits, not an empty write;
  7. the HMAC-signed completion callback FIRED.

Deliberately NOT asserted: HTTP 200. A 202 from /explore proves only that a
request was accepted. Every assertion here is made against evidence the crawl
produced, which is the difference between a smoke test and a green light.

Usage:
    python scripts/qec_crawl_smoke.py                       # docker proving ground
    python scripts/qec_crawl_smoke.py --serve-static        # no docker; stdlib server
    python scripts/qec_crawl_smoke.py --keep                # leave services running

Exit 0 = the funnel is open. Non-zero = it is not, and the message says where.
"""
from __future__ import annotations

import argparse
import functools
import http.server
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any, NoReturn, Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
EXPLORER_DIR = REPO_ROOT / "engines" / "qe-explorer"
QE_CENTRAL_DIR = REPO_ROOT / "platform" / "qe-central"
GROUND_DIR = REPO_ROOT / "proving-grounds" / "acme-life"

EXPLORER_TOKEN = "crawl-smoke-token-not-for-prod"
ACME_CONTAINER = "qec-crawl-smoke-acme"


# ── tiny output helpers ─────────────────────────────────────────────────────
def step(msg: str) -> None:
    print(f"\n\033[1;36m== {msg}\033[0m", flush=True)


def ok(msg: str) -> None:
    print(f"   \033[32mOK\033[0m  {msg}", flush=True)


def die(msg: str) -> NoReturn:
    print(f"\n\033[1;31mCRAWL SMOKE FAILED:\033[0m {msg}\n", file=sys.stderr, flush=True)
    raise SystemExit(1)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def wait_http(url: str, timeout: float, what: str) -> None:
    """Poll until `url` answers, or fail with the last error seen."""
    deadline = time.monotonic() + timeout
    last = "no attempt made"
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=3) as r:
                if r.status < 500:
                    return
                last = f"HTTP {r.status}"
        except Exception as exc:  # connection refused while booting
            last = f"{type(exc).__name__}: {exc}"
        time.sleep(0.3)
    die(f"{what} never became ready at {url} within {timeout:.0f}s (last: {last})")


# ── the proving ground ──────────────────────────────────────────────────────
class StaticGround:
    """Serve proving-grounds/acme-life from the stdlib (no docker needed)."""

    def __init__(self, port: int) -> None:
        handler = functools.partial(http.server.SimpleHTTPRequestHandler,
                                    directory=str(GROUND_DIR))
        self._srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
        self._srv.daemon_threads = True
        self._thread = threading.Thread(target=self._srv.serve_forever, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._srv.shutdown()
        self._srv.server_close()


def start_docker_ground(port: int) -> None:
    """Build + run the REAL acme-life image (compose service `acme-life`)."""
    subprocess.run(["docker", "rm", "-f", ACME_CONTAINER],
                   capture_output=True, check=False)
    build = subprocess.run(
        ["docker", "build", "-q", "-t", "qec-crawl-smoke-acme:ci", str(GROUND_DIR)],
        capture_output=True, text=True)
    if build.returncode != 0:
        die(f"docker build of the acme-life proving ground failed:\n{build.stderr}")
    run = subprocess.run(
        ["docker", "run", "-d", "--name", ACME_CONTAINER,
         "-p", f"{port}:80", "qec-crawl-smoke-acme:ci"],
        capture_output=True, text=True)
    if run.returncode != 0:
        die(f"docker run of the acme-life proving ground failed:\n{run.stderr}")


def stop_docker_ground() -> None:
    subprocess.run(["docker", "rm", "-f", ACME_CONTAINER],
                   capture_output=True, check=False)


# ── the completion-callback sink ────────────────────────────────────────────
class CallbackSink:
    """Stand in for qe-central and RECORD the completion callback.

    The explorer fires this best-effort (a failure is only logged), so its
    arrival is not implied by the crawl finishing — it has to be observed.
    """

    def __init__(self, port: int) -> None:
        self.received: list[dict[str, Any]] = []
        received = self.received

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 - stdlib API
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b"{}"
                try:
                    body = json.loads(raw.decode("utf-8") or "{}")
                except Exception:
                    body = {"_unparsed": raw[:2000].decode("utf-8", "replace")}
                received.append({"path": self.path, "body": body})
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"status":"ok"}')

            def log_message(self, *_args: Any) -> None:
                pass  # keep CI output about the crawl, not about the sink

        self._srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self._srv.daemon_threads = True
        self._thread = threading.Thread(target=self._srv.serve_forever, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._srv.shutdown()
        self._srv.server_close()


# ── the explorer service ────────────────────────────────────────────────────
def start_explorer(port: int, work_dir: Path, callback_url: str,
                   log_path: Path) -> subprocess.Popen:
    env = dict(os.environ)
    env.update({
        "QEC_EXPLORER_PORT": str(port),
        "QEC_EXPLORER_TOKEN": EXPLORER_TOKEN,
        "QEC_CALLBACK_URL": callback_url,
        "WORK_DIR": str(work_dir),
        # No squid in CI: the proving ground is on loopback, so the browser must
        # talk to it DIRECTLY. An unset proxy is what `EGRESS_PROXY=""` means to
        # main.py's launch args (proxy=None), so this is the documented path,
        # not a bypass of the fence — there is no egress to fence here.
        "EGRESS_PROXY": "",
        "QEC_EXPLORER_LOG_LEVEL": "INFO",
        # Keep the smoke fast and bounded; still far more than acme-life needs.
        "QEC_MAX_STATES": "25",
        "QEC_MAX_DEPTH": "3",
        "QEC_MAX_WALL_MS": "180000",
        "PYTHONUNBUFFERED": "1",
        "PYTHONPATH": str(EXPLORER_DIR),
    })
    log = log_path.open("wb")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app",
         "--host", "127.0.0.1", "--port", str(port), "--log-level", "info"],
        cwd=str(EXPLORER_DIR), env=env, stdout=log, stderr=subprocess.STDOUT)
    return proc


def post_json(url: str, payload: dict[str, Any], token: str) -> tuple[int, dict[str, Any]]:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("X-QEC-Token", token)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        return e.code, {"error": e.read().decode("utf-8", "replace")}


def get_json(url: str, token: str) -> tuple[int, dict[str, Any]]:
    req = urllib.request.Request(url, method="GET")
    req.add_header("X-QEC-Token", token)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        return e.code, {"error": e.read().decode("utf-8", "replace")}


# ── the ingest half: qe-central's REAL manifest mapper ──────────────────────
def map_through_qe_central(records: list[dict[str, Any]], crawl_dir: Path) -> int:
    """Feed the manifest to the production mapper; return the page-visit count.

    This is the same function the internal ingest route calls, so a manifest the
    crawler can write but qe-central cannot ingest fails HERE rather than in
    production.
    """
    sys.path.insert(0, str(QE_CENTRAL_DIR))
    try:
        from app.clients.manifest_mapper import map_manifest_records_to_bundle
    except Exception as exc:
        die(f"could not import qe-central's manifest mapper: {exc}")
    def load_screenshot(path: str) -> bytes:
        """Return the REAL PNG bytes the crawl staged for this frame.

        Not a stub: the mapper fails closed on an empty/absent screenshot
        (screenshot_missing_data), which is the behaviour that stops a crawl
        with no evidence from being ingested as if it had some. Feeding it a
        canned PNG would defeat exactly the check worth exercising here, so the
        loader resolves the file the crawler actually wrote.
        """
        candidate = Path(path)
        if not candidate.is_absolute():
            candidate = crawl_dir / candidate.name
        if not candidate.is_file():
            matches = list(crawl_dir.rglob(Path(path).name))
            if not matches:
                die(f"the manifest references screenshot {path!r} but the crawl "
                    f"wrote no such file under {crawl_dir}")
            candidate = matches[0]
        data = candidate.read_bytes()
        if not data:
            die(f"the crawl staged an EMPTY screenshot at {candidate}")
        return data

    try:
        bundle = map_manifest_records_to_bundle(records, screenshot_loader=load_screenshot)
    except Exception as exc:
        die(f"qe-central REFUSED the crawl's own manifest: {type(exc).__name__}: {exc}")
    mapped = getattr(bundle, "pages", None)
    if mapped is None:
        die("mapper returned a bundle with no `pages` attribute — the "
            "ExplorationBundle contract changed and this smoke is now blind")
    return len(mapped)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--serve-static", action="store_true",
                    help="serve the proving ground from the stdlib instead of docker")
    ap.add_argument("--keep", action="store_true", help="leave services running on exit")
    ap.add_argument("--timeout", type=float, default=300.0,
                    help="seconds to wait for the crawl to finish (default 300)")
    args = ap.parse_args()

    if not GROUND_DIR.is_dir():
        die(f"proving ground not found at {GROUND_DIR}")

    use_docker = not args.serve_static and shutil.which("docker") is not None
    ground_port = free_port()
    sink_port = free_port()
    explorer_port = free_port()
    work_dir = Path(tempfile.mkdtemp(prefix="qec-crawl-smoke-"))
    explorer_log = work_dir / "explorer.log"
    crawl_id = f"smoke-{uuid.uuid4().hex[:12]}"

    ground: Optional[StaticGround] = None
    sink: Optional[CallbackSink] = None
    explorer: Optional[subprocess.Popen] = None

    try:
        # ── 1. proving ground ────────────────────────────────────────────
        step(f"Starting the acme-life proving ground on :{ground_port} "
             f"({'docker' if use_docker else 'stdlib static server'})")
        if use_docker:
            start_docker_ground(ground_port)
        else:
            ground = StaticGround(ground_port)
            ground.start()
        target_url = f"http://127.0.0.1:{ground_port}/"
        wait_http(target_url, 90, "the acme-life proving ground")
        ok(f"proving ground serving at {target_url}")

        # ── 2. callback sink ─────────────────────────────────────────────
        sink = CallbackSink(sink_port)
        sink.start()
        ok(f"completion-callback sink listening on :{sink_port}")

        # ── 3. explorer service ──────────────────────────────────────────
        step(f"Starting qe-explorer on :{explorer_port}")
        explorer = start_explorer(explorer_port, work_dir,
                                  f"http://127.0.0.1:{sink_port}", explorer_log)
        base = f"http://127.0.0.1:{explorer_port}"
        wait_http(f"{base}/health", 120, "qe-explorer")
        _, health = get_json(f"{base}/health", EXPLORER_TOKEN)
        ok(f"qe-explorer healthy: version={health.get('version')} "
           f"refuse_pack={health.get('refuse_pack_version')}")

        # ── 4. dispatch ONE real crawl ───────────────────────────────────
        step("Dispatching a crawl through POST /api/v1/explore")
        status, body = post_json(f"{base}/api/v1/explore", {
            "crawl_id": crawl_id,
            "tenant_id": "crawl-smoke",
            "exploration_id": "crawl-smoke-exploration",
            "target_url": target_url,
            "allowed_hosts": ["127.0.0.1", "localhost"],
            "budgets": {"max_states": 25, "max_depth": 3, "max_wall_ms": 180000},
            "phase": "explore",
            "env_kind": "disposable",
        }, EXPLORER_TOKEN)
        if status != 202:
            die(f"dispatch rejected: HTTP {status} {body}")
        ok(f"crawl accepted: id={crawl_id} fingerprint={body.get('config_fingerprint','')[:24]}...")

        # ── 5. wait for a TERMINAL state ─────────────────────────────────
        step("Waiting for the crawl to complete")
        deadline = time.monotonic() + args.timeout
        summary: dict[str, Any] = {}
        last: dict[str, Any] = {}
        # The status route is owner-scoped: a shared fleet token proves the caller
        # is qe-central but never WHICH tenant it acts for, so tenant_id is
        # required and must match the reserving tenant.
        status_url = f"{base}/api/v1/explore/{crawl_id}?tenant_id=crawl-smoke"
        while time.monotonic() < deadline:
            code, prog = get_json(status_url, EXPLORER_TOKEN)
            last = prog
            if code >= 400:
                die(f"status poll rejected: HTTP {code} {prog}\n"
                    f"--- explorer log tail ---\n{tail(explorer_log)}")
            if prog.get("summary"):
                summary = prog["summary"]
                break
            if prog.get("error"):
                die(f"the crawl reported an error: {prog['error']}\n"
                    f"--- explorer log tail ---\n{tail(explorer_log)}")
            time.sleep(1.0)
        else:
            die(f"the crawl never reached a terminal state within {args.timeout:.0f}s; "
                f"last progress={last}\n--- explorer log tail ---\n{tail(explorer_log)}")

        stop_reason = summary.get("stop_reason", "")
        states = int(summary.get("states") or 0)
        actions = int(summary.get("actions") or 0)
        ok(f"crawl completed: stop_reason={stop_reason!r} states={states} actions={actions}")

        # ── 6. the manifest must EXIST ───────────────────────────────────
        step("Verifying the crawl manifest")
        manifest = Path(summary.get("manifest_path") or "")
        if not manifest.is_file():
            found = list(work_dir.rglob("manifest.jsonl"))
            if not found:
                die(f"no manifest was written anywhere under {work_dir}\n"
                    f"--- explorer log tail ---\n{tail(explorer_log)}")
            manifest = found[0]
        records = [json.loads(line) for line in
                   manifest.read_text(encoding="utf-8").splitlines() if line.strip()]
        ok(f"manifest at {manifest} ({len(records)} records)")

        # ── 7. PAGES > 0 (the assertion that matters) ────────────────────
        pages = [r for r in records if r.get("type") == "page_state"]
        if not pages:
            kinds: dict[str, int] = {}
            for r in records:
                kinds[str(r.get("type"))] = kinds.get(str(r.get("type")), 0) + 1
            die("the manifest contains ZERO page_state records — the crawl "
                f"produced no pages. record types seen: {kinds}\n"
                f"--- explorer log tail ---\n{tail(explorer_log)}")
        ok(f"manifest contains {len(pages)} page_state record(s) -> PAGES > 0")

        # ── 8. qe-central must be able to INGEST it ──────────────────────
        step("Ingesting the manifest through qe-central's real mapper")
        visits = map_through_qe_central(records, manifest.parent)
        if visits <= 0:
            die("qe-central mapped the manifest to ZERO pages — the "
                "substrate write would have been empty")
        ok(f"mapper produced {visits} substrate page(s)")

        # ── 9. the completion callback must have fired ───────────────────
        step("Verifying the completion callback")
        for _ in range(50):
            if sink.received:
                break
            time.sleep(0.2)
        if not sink.received:
            die("the HMAC-signed completion callback never arrived at the sink")
        ok(f"callback received on {sink.received[0]['path']}")

        # ── verdict ──────────────────────────────────────────────────────
        print("\n\033[1;32m==== CRAWL SMOKE PASSED ====\033[0m")
        print(f"  crawl dispatched : yes ({crawl_id})")
        print(f"  crawl completed  : yes (stop_reason={stop_reason!r})")
        print("  manifest ingested: yes")
        print(f"  pages discovered : {len(pages)}")
        print(f"  bundle pages     : {visits}")
        print(f"  actions recorded : {actions}\n")
        return 0

    finally:
        if not args.keep:
            if explorer is not None:
                explorer.terminate()
                try:
                    explorer.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    explorer.kill()
            if sink is not None:
                sink.stop()
            if ground is not None:
                ground.stop()
            if use_docker:
                stop_docker_ground()
            shutil.rmtree(work_dir, ignore_errors=True)


def tail(path: Path, lines: int = 40) -> str:
    try:
        return "\n".join(path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:])
    except Exception:
        return "(no log captured)"


if __name__ == "__main__":
    sys.exit(main())
