"""Browser Test Harness (M0.2) — the shared machinery for both execution lanes.

This module owns four things and NO capture logic of its own:

  1. :class:`FixtureServer` — a deterministic local HTTP origin (plus a second,
     genuinely-foreign origin) serving ``tests/browser/fixtures``.  Both lanes
     load the SAME fixture over the SAME URL, so a jsdom result and a Chromium
     result are comparable.
  2. :func:`production_snippet` — reads the injected JavaScript straight out of
     :mod:`app.inventory_js`.  Nothing is copied, patched or re-implemented
     here; the harness is a courier for the production constant.
  3. :func:`run_jsdom` / :func:`collect_via_production_port` — the two execution
     lanes.  The Playwright lane goes through the REAL
     :class:`app.main.PlaywrightBrowserPort`, i.e. the same object the crawl
     entrypoint constructs, so the injection path under test is the production
     one and not a test-local re-creation of it.
  4. Normalisation + structured comparison for characterization snapshots.

Nothing in here sleeps, samples a clock, or asserts on serialized HTML.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
from dataclasses import dataclass
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence

# ─── Layout ──────────────────────────────────────────────────────────────────

HERE = Path(__file__).resolve().parent
FIXTURES_DIR = HERE / "fixtures"
GOLDEN_DIR = HERE / "golden"
SERVICE_ROOT = HERE.parent.parent                      # …/engines/qe-explorer
HARNESS_DIR = SERVICE_ROOT / "browser_harness"
JSDOM_RUNNER = HARNESS_DIR / "jsdom_runner.js"

#: A fixture is COMPLETE when it has both an app and a declared contract.
_FIXTURE_APP = "index.html"
_FIXTURE_CONTRACT = "expected.json"


def fixture_names() -> list[str]:
    """Every COMPLETE fixture, in fixture order.

    Requires BOTH ``index.html`` and ``expected.json``. Discovery is from disk so
    a new fixture is picked up by adding a directory — but a half-written one
    must not be picked up: this library is edited by more than one author, and a
    directory caught mid-creation would otherwise raise FileNotFoundError inside
    every parametrised test in the suite, burying real results under an
    unrelated crash.

    Incomplete directories are not silently ignored either — they are reported
    as ONE named, actionable failure by
    ``test_fixture_library.py::test_no_fixture_is_half_written``.
    """
    return sorted(
        p.name for p in FIXTURES_DIR.iterdir()
        if p.is_dir() and (p / _FIXTURE_APP).exists() and (p / _FIXTURE_CONTRACT).exists()
    )


def incomplete_fixture_names() -> dict[str, list[str]]:
    """Fixture directories missing a required file → what they are missing."""
    out: dict[str, list[str]] = {}
    for p in sorted(FIXTURES_DIR.iterdir()):
        if not p.is_dir():
            continue
        missing = [f for f in (_FIXTURE_APP, _FIXTURE_CONTRACT) if not (p / f).exists()]
        if missing and len(missing) < 2:      # a dir with NEITHER is not a fixture
            out[p.name] = missing
    return out


def fixture_spec(name: str) -> dict[str, Any]:
    """The fixture's declared contract (``expected.json``)."""
    return json.loads((FIXTURES_DIR / name / "expected.json").read_text(encoding="utf-8"))


# ─── The production snippets (read, never copied) ────────────────────────────

_SNIPPETS = ("INVENTORY_JS", "OPAQUE_JS", "DISPLAYED_VALUES_JS",
             "CAPTURE_HOOKS_JS")


def production_snippet(name: str = "INVENTORY_JS") -> str:
    """Return the LIVE value of an injected-JS constant from ``app.inventory_js``.

    Imported at call time from the production module.  If this module ever grew
    its own copy of the JavaScript, every test in the harness would be testing
    the copy — which is the specific failure mode the existing string-assertion
    tests already have.  There is exactly one source of truth and this is a read
    of it.
    """
    if name not in _SNIPPETS:
        raise ValueError(f"unknown snippet {name!r}; expected one of {_SNIPPETS}")
    from app import inventory_js                      # noqa: PLC0415 — deliberate
    return getattr(inventory_js, name)


def snippet_to_tempfile(name: str, tmp_dir: Path) -> Path:
    """Write the production snippet verbatim to a file for the node runner.

    ``newline=""`` + ``write_text`` of the exact string: no reformatting, no
    trailing-newline normalisation, no sourceURL comment appended.  The bytes
    node evaluates are the bytes Playwright evaluates.
    """
    tmp_dir.mkdir(parents=True, exist_ok=True)
    path = tmp_dir / f"{name}.js"
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(production_snippet(name))
    return path


# ─── Fixture HTTP origins ────────────────────────────────────────────────────

_ALT_TOKEN = "__ALT_ORIGIN__"
_SELF_TOKEN = "__SELF_ORIGIN__"


class _FixtureHandler(SimpleHTTPRequestHandler):
    """Serve the fixture tree, substituting origin tokens in HTML.

    Token substitution is what lets fixture 04 embed a genuinely cross-origin
    iframe without hard-coding a port. It happens server-side, so the fixture on
    disk stays a plain static file that a developer can open directly.
    """

    server_version = "QECFixtureServer/1.0"
    #: set by FixtureServer before serving
    alt_origin = ""
    self_origin = ""
    #: M2.5 — per-origin counters behind the ``/__net/`` endpoints, so a retry
    #: sequence is DETERMINISTIC (503, 503, 200) rather than timing-dependent.
    #: Class-level and lock-guarded because ``ThreadingHTTPServer`` serves each
    #: request on its own thread and the browser fires these concurrently; a
    #: plain dict would make the fixture flaky in exactly the way a fixture about
    #: request ordering must never be.
    _net_counts: dict = {}
    _net_lock = threading.Lock()

    #: The scripted status sequence for each dynamic endpoint. The LAST entry
    #: repeats once the sequence is exhausted, so a crawl that clicks a control
    #: more times than the script anticipated still gets a defined answer.
    _NET_SCRIPT = {
        "/quote": [503, 503, 200],
        "/status": [200],
        "/limited": [429, 429, 200],
        "/claim": [500],
    }

    @classmethod
    def reset_net_counts(cls) -> None:
        """Forget every scripted sequence — one test must not inherit another's."""
        with cls._net_lock:
            cls._net_counts.clear()

    def log_message(self, fmt: str, *args: Any) -> None:      # silence the test run
        pass

    # ─── M2.5 · the dynamic ``/__net/`` namespace ────────────────────────────

    def _net_route(self) -> str:
        """The scripted route this request is for, or ``""``.

        Matched on the path only: the fixture's own query strings are irrelevant
        here, and the capture layer drops them anyway.
        """
        path = (self.path or "").split("?", 1)[0]
        marker = "/__net"
        idx = path.find(marker)
        if idx < 0:
            return ""
        route = path[idx + len(marker):]
        return route if route in self._NET_SCRIPT else ""

    def _serve_net(self, route: str, *, method: str) -> None:
        """Answer one scripted call, advancing that route's counter by one.

        The response deliberately carries headers a real API carries — a
        ``Retry-After`` on the rate-limited route, a request id, a rate-limit
        budget — because the point of the fixture is to give the capture layer
        real headers to allow-list and redact, not synthetic ones.
        """
        script = self._NET_SCRIPT[route]
        with self._net_lock:
            n = self._net_counts.get(route, 0)
            self._net_counts[route] = n + 1
        status = script[n] if n < len(script) else script[-1]

        # Drain the request body so the client is never left writing into a
        # closed socket (which would surface as a spurious network failure).
        length = 0
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            length = 0
        if length > 0:
            try:
                self.rfile.read(length)
            except Exception:
                pass

        payload = json.dumps({
            "route": route, "attempt": n + 1, "status": status,
        }).encode("utf-8")

        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("X-Request-Id", f"srv-{route.strip('/')}-{n + 1}")
        if status == 429:
            self.send_header("Retry-After", "1")
            self.send_header("X-RateLimit-Limit", "3")
            self.send_header("X-RateLimit-Remaining", "0")
        # A credential-shaped response header, so redaction has a real
        # `Set-Cookie` to reduce to a presence marker.
        self.send_header("Set-Cookie", "session=abc123secret; Path=/")
        self.end_headers()
        if method != "HEAD":
            try:
                self.wfile.write(payload)
            except Exception:
                pass

    def do_POST(self) -> None:                                 # noqa: N802
        route = self._net_route()
        if route:
            self._serve_net(route, method="POST")
            return
        self.send_error(405, "only the /__net/ namespace accepts POST")

    def do_GET(self) -> None:                                  # noqa: N802
        route = self._net_route()
        if route:
            self._serve_net(route, method="GET")
            return
        super().do_GET()

    def end_headers(self) -> None:
        # A cached fixture would make a characterization run depend on what a
        # previous run left in the browser cache — the opposite of deterministic.
        self.send_header("Cache-Control", "no-store, must-revalidate")
        self.send_header("Pragma", "no-cache")
        super().end_headers()

    def send_head(self):                                       # type: ignore[override]
        path = self.translate_path(self.path)
        if os.path.isdir(path):
            path = os.path.join(path, "index.html")
        if not path.endswith(".html") or not os.path.exists(path):
            return super().send_head()
        raw = Path(path).read_text(encoding="utf-8")
        body = raw.replace(_ALT_TOKEN, self.alt_origin).replace(_SELF_TOKEN, self.self_origin)
        encoded = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        import io
        return io.BytesIO(encoded)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class FixtureServer:
    """Two local HTTP origins over the same fixture tree.

    The second origin exists solely so fixture 04's iframe is *genuinely*
    cross-origin (different port ⇒ different origin under the same-origin
    policy), rather than a simulation of one.
    """

    def __init__(self, root: Path = FIXTURES_DIR) -> None:
        self._root = root
        self._servers: list[ThreadingHTTPServer] = []
        self._threads: list[threading.Thread] = []
        self.origin = ""
        self.alt_origin = ""

    def start(self) -> "FixtureServer":
        port_a, port_b = _free_port(), _free_port()
        self.origin = f"http://127.0.0.1:{port_a}"
        # A DIFFERENT HOSTNAME as well as a different port: 127.0.0.1 vs
        # localhost resolve to the same machine but are distinct origins, which
        # keeps the cross-origin fixture cross-origin even if a browser ever
        # started treating same-host different-port as same-origin.
        self.alt_origin = f"http://localhost:{port_b}"

        for port in (port_a, port_b):
            handler = type(
                "_BoundFixtureHandler", (_FixtureHandler,),
                {"alt_origin": self.alt_origin, "self_origin": self.origin},
            )
            root = str(self._root)

            def _factory(*args: Any, _h=handler, _r=root, **kwargs: Any):
                return _h(*args, directory=_r, **kwargs)

            srv = ThreadingHTTPServer(("127.0.0.1", port), _factory)
            srv.daemon_threads = True
            th = threading.Thread(target=srv.serve_forever, daemon=True)
            th.start()
            self._servers.append(srv)
            self._threads.append(th)
        return self

    def url(self, fixture: str, page: str = "index.html") -> str:
        return f"{self.origin}/{fixture}/{page}"

    def stop(self) -> None:
        for srv in self._servers:
            try:
                srv.shutdown()
                srv.server_close()
            except Exception:
                pass
        self._servers.clear()


# ─── Lane 1: jsdom ───────────────────────────────────────────────────────────

class JsdomUnavailable(RuntimeError):
    """node or the jsdom dependency is not installed."""


def jsdom_available() -> tuple[bool, str]:
    if shutil.which("node") is None:
        return False, "node is not on PATH"
    if not (HARNESS_DIR / "node_modules" / "jsdom").exists():
        return False, f"jsdom not installed — run `npm ci` in {HARNESS_DIR}"
    return True, ""


@dataclass(frozen=True)
class JsdomResult:
    result: Any
    capabilities: dict[str, bool]
    console: list[str]


def run_jsdom(url: str, snippet: str, js_path: Path, timeout_ms: int = 15000,
              hooks_path: Optional[Path] = None) -> JsdomResult:
    """Execute a PRODUCTION snippet inside jsdom against ``url``.

    Raises on any runner failure — a jsdom lane that silently returned ``[]``
    on error would report every fixture as "no controls found" and pass its
    ``forbid`` assertions while proving nothing.
    """
    ok, why = jsdom_available()
    if not ok:
        raise JsdomUnavailable(why)

    # M3.2 / T-FR-02 — the CAPTURE INIT SCRIPT, handed to the runner so it can
    # install it in `beforeParse`: jsdom's equivalent of `add_init_script`, i.e.
    # before the fixture's own scripts construct their components. The ORDERING
    # is the behaviour under test, so this lane has to reproduce it rather than
    # approximate it; a runner that evaluated the hooks after load would prove
    # the opposite of what the fixture claims.
    job = json.dumps({"url": url, "js_path": str(js_path), "timeout_ms": timeout_ms,
                      "hooks_path": str(hooks_path) if hooks_path else ""})
    # PROCESS slack on top of the in-jsdom budget, covering node startup, module
    # resolution and jsdom construction — none of which the in-page timeout can
    # see. It was a hard-coded +30s, which is a statement about how busy the
    # machine is, not about the code under test. Observed failing: on a box also
    # running four other pytest processes, `test_jsdom_capability_probe` blew the
    # 45s ceiling and reported as a capture failure; the same test passes alone in
    # under four seconds. A shared runner or a developer laptop mid-build hits the
    # same wall, and the traceback (`subprocess.TimeoutExpired`) points at node
    # rather than at the load that actually caused it.
    slack_s = float(os.environ.get("QEC_JSDOM_PROC_SLACK_S") or 90)
    try:
        proc = subprocess.run(
            ["node", str(JSDOM_RUNNER)],
            input=job, capture_output=True, text=True,
            cwd=str(HARNESS_DIR), timeout=timeout_ms / 1000.0 + slack_s,
        )
    except subprocess.TimeoutExpired as exc:
        # Deliberately NOT retried. A retry would paper over a genuine runner hang
        # and turn a reproducible failure into an intermittent one. Name the cause
        # instead, so the reader does not go looking for a capture bug.
        raise RuntimeError(
            f"the jsdom runner did not finish within "
            f"{timeout_ms / 1000.0 + slack_s:.0f}s for {snippet} @ {url}." + chr(10) +
            f"The in-jsdom budget is {timeout_ms / 1000.0:.0f}s and the process "
            f"slack is {slack_s:.0f}s (QEC_JSDOM_PROC_SLACK_S)." + chr(10) +
            f"This is a WALL-CLOCK failure, not a capture failure: node never "
            f"reported back. The usual cause is machine contention — another test "
            f"run, a build, or a busy shared runner. Re-run this test alone before "
            f"investigating the snippet; if it passes alone, raise the slack rather "
            f"than changing capture."
        ) from exc
    out = (proc.stdout or "").strip().splitlines()
    if not out:
        raise RuntimeError(
            f"jsdom runner produced no output for {snippet} @ {url}\n"
            f"exit={proc.returncode}\nstderr={proc.stderr[:2000]}"
        )
    payload = json.loads(out[-1])
    if not payload.get("ok"):
        raise RuntimeError(
            f"jsdom runner failed for {snippet} @ {url}: {payload.get('error')}\n"
            f"{payload.get('stack', '')[:2000]}\nconsole={payload.get('console')}"
        )
    return JsdomResult(
        result=payload.get("result"),
        capabilities=dict(payload.get("capabilities") or {}),
        console=list(payload.get("console") or []),
    )


# ─── Lane 2: Playwright, through the PRODUCTION port ─────────────────────────

def playwright_available() -> tuple[bool, str]:
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:                                   # pragma: no cover
        return False, f"playwright python package unavailable: {exc}"
    try:
        with sync_playwright() as p:
            exe = p.chromium.executable_path
        if not exe or not os.path.exists(exe):
            return False, "chromium not installed — run `python -m playwright install chromium`"
    except Exception as exc:                                   # pragma: no cover
        return False, f"chromium probe failed: {exc}"
    return True, ""


async def install_production_capture_hooks(context: Any) -> bool:
    """Install the capture hooks on a lane context, through PRODUCTION code.

    A thin read of :func:`app.playwright_port.install_capture_hooks`, for the
    same reason :func:`production_snippet` is a read rather than a copy: the
    harness must never own a second way of configuring a browser context, or it
    would go on proving that ITS way works while production's broke.
    """
    from app.playwright_port import install_capture_hooks   # noqa: PLC0415
    return await install_capture_hooks(context)


async def collect_via_production_port(page: Any, context: Any, url: str,
                                      *, what: str = "controls") -> list[dict[str, Any]]:
    """Navigate and collect using the REAL production adapter.

    ``PlaywrightBrowserPort`` is imported from ``app.main`` — the same class the
    crawl entrypoint (`_run_job`) constructs. ``goto`` here is the production
    ``goto`` (including its 429 backoff and its ``_settle()`` quiescence wait),
    and ``collect_controls`` is the production ``page.evaluate(INVENTORY_JS)``.
    The harness supplies no JavaScript of its own on this path.
    """
    from app.main import PlaywrightBrowserPort               # noqa: PLC0415

    port = PlaywrightBrowserPort(page, context)
    nav = await port.goto(url)
    if not nav.ok:
        raise RuntimeError(f"production goto failed for {url}: {nav.error}")
    if what == "controls":
        return await port.collect_controls()
    if what == "opaque":
        return await port.collect_opaque()
    if what == "displayed_values":
        return await port.collect_displayed_values()
    raise ValueError(f"unknown collection {what!r}")


# ─── Structured comparison ───────────────────────────────────────────────────

def find_control(controls: Sequence[Mapping[str, Any]],
                 where: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Every control whose fields match the ``where`` subset."""
    out = []
    for c in controls:
        if all(c.get(k) == v for k, v in where.items()):
            out.append(dict(c))
    return out


def _fmt_where(where: Mapping[str, Any]) -> str:
    return ", ".join(f"{k}={v!r}" for k, v in where.items())


def runs_in_lane(spec: Mapping[str, Any], lane: str) -> bool:
    """Does this expectation apply to ``lane``?

    An expectation may narrow the fixture-level ``lanes`` with its own key — used
    where ONE control on an otherwise cross-lane fixture depends on an API the
    weaker runtime lacks.  The declaration is per-expectation and explicit, so an
    assertion is never quietly weakened to make a lane pass.
    """
    lanes = spec.get("lanes")
    return True if lanes is None else lane in lanes


def assert_control(controls: Sequence[Mapping[str, Any]], spec: Mapping[str, Any],
                   *, context: str = "") -> dict[str, Any]:
    """Assert ONE expected-control spec against a captured inventory.

    Compares STRUCTURED fields only — never whitespace, never serialized HTML,
    never a source string. The failure message carries the nearest candidates so
    a red test is diagnosable without a rerun.
    """
    where = spec["where"]
    matches = find_control(controls, where)
    if not matches:
        near = [
            {k: c.get(k) for k in ("tag", "role", "name", "css_hint", "frame_selector")}
            for c in controls
        ]
        raise AssertionError(
            f"{context}no control matched {{{_fmt_where(where)}}}\n"
            f"captured {len(controls)} controls:\n"
            + "\n".join(f"  {json.dumps(n, sort_keys=True)}" for n in near[:40])
        )
    if len(matches) > 1:
        raise AssertionError(
            f"{context}{len(matches)} controls matched {{{_fmt_where(where)}}} — "
            f"the selector must identify exactly one"
        )
    got = matches[0]

    for key, want in (spec.get("fields") or {}).items():
        assert got.get(key) == want, (
            f"{context}control {{{_fmt_where(where)}}} field {key!r}:\n"
            f"  expected: {want!r}\n"
            f"  actual:   {got.get(key)!r}"
        )
    for key, want_len in (spec.get("list_lengths") or {}).items():
        actual = got.get(key) or []
        assert len(actual) == want_len, (
            f"{context}control {{{_fmt_where(where)}}} list {key!r} length:\n"
            f"  expected: {want_len}\n  actual:   {len(actual)}"
        )
    for key, edges in (spec.get("list_edges") or {}).items():
        actual = list(got.get(key) or [])
        assert actual, f"{context}control {{{_fmt_where(where)}}} list {key!r} is empty"
        if "first" in edges:
            assert actual[0] == edges["first"], (
                f"{context}{key}[0]: expected {edges['first']!r}, got {actual[0]!r}")
        if "last" in edges:
            assert actual[-1] == edges["last"], (
                f"{context}{key}[-1]: expected {edges['last']!r}, got {actual[-1]!r}")
    if "href_suffix" in spec:
        href = got.get("href") or ""
        assert href.endswith(spec["href_suffix"]), (
            f"{context}control {{{_fmt_where(where)}}} href:\n"
            f"  expected to end with: {spec['href_suffix']!r}\n"
            f"  actual:               {href!r}"
        )
    return got


def assert_displayed_values(values: Sequence[Mapping[str, Any]],
                            spec: Mapping[str, Any], *, context: str = "") -> None:
    """Assert a fixture's DISPLAYED_VALUES_JS contract.

    Matched on the ``(selector, text)`` PAIR, never on selector alone. The
    selector a value node reports is a diagnostic hint, not an identity — a page
    with three unlabelled ``<p>`` figures reports ``"p"`` three times, so keying
    a lookup on it collapses them and silently asserts one expectation three
    times while the other two go unchecked. That is exactly what happened when
    fixture 13 grew from two value nodes to twelve.
    """
    remaining = [dict(v) for v in values]
    for want in spec.get("expect", []):
        matches = [v for v in remaining
                   if v["selector"] == want["selector"] and v["text"] == want["text"]]
        rung = f"\n  rung: {want['rung']}" if "rung" in want else ""
        assert matches, (
            f"{context}no displayed value {want['text']!r} at "
            f"{want['selector']!r}.{rung}\n  captured: "
            + ", ".join(f"{v['selector']}={v['text']}" for v in values))
        got = matches[0]
        remaining.remove(got)                  # consume, so N copies need N expectations
        if "label" in want:
            assert got["label"] == want["label"], (
                f"{context}{want['text']} at {want['selector']}: expected label "
                f"{want['label']!r}, got {got['label']!r}{rung}")

    for banned in spec.get("forbid", []):
        offenders = [v for v in values if v["text"] == banned["text"]]
        assert not offenders, (
            f"{context}{banned['text']} MUST NOT be captured: {banned['why']}\n"
            f"  got {offenders}")


def assert_absent(controls: Sequence[Mapping[str, Any]],
                  spec: Mapping[str, Any], *, context: str = "") -> None:
    where = spec["where"]
    matches = find_control(controls, where)
    assert not matches, (
        f"{context}control {{{_fmt_where(where)}}} MUST NOT be captured, but "
        f"{len(matches)} were:\n" + json.dumps(matches[:3], indent=2, sort_keys=True)
    )


# ─── Characterization: normalisation + golden I/O ────────────────────────────

#: Fields whose values are legitimately unstable between two identical runs.
#: ONLY these are normalised — nothing functional is touched, because a
#: characterization test that normalised behaviour would pass through the very
#: change it exists to catch.
#:
#: This is an explicit ALLOWLIST rather than a pattern such as "any key ending
#: ``_ms``", and that distinction is load-bearing: the manifest holds both clock
#: READINGS (``first_seen_ms``, ``elapsed_ms`` — unstable) and declared BOUNDS
#: (``max_wall_ms`` — the configured budget, entirely functional). A blanket
#: suffix rule would erase the bound, and a crawl silently reconfigured to a
#: different budget would then pass its own golden unchanged.
#: :data:`_DELIBERATELY_NOT_NORMALIZED` records that decision, and
#: ``test_declared_bounds_are_not_normalised`` pins it.
_UNSTABLE_KEYS = frozenset({
    # identity minted per run
    "crawl_id", "run_id", "state_id", "to_state", "from_state", "job_id",
    # clock READINGS
    "ts_ms", "started_ms", "finished_ms", "duration_ms", "elapsed_ms",
    "timestamp_ms", "at_ms", "opened_ms", "closed_ms",
    "first_seen_ms", "last_seen_ms",
    # per-run filesystem + network identity
    "path", "screenshot", "screenshot_after", "screenshot_before", "work_dir",
    "png_base64", "port",
})

#: Time-shaped keys that are DECLARED CONFIGURATION, not readings. Listed so the
#: exclusion is a recorded decision rather than an oversight someone later
#: "fixes" by normalising everything that ends in ``_ms``.
_DELIBERATELY_NOT_NORMALIZED = frozenset({
    "max_wall_ms",        # the configured crawl budget — a functional bound
})

_UUID_RX = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")
_PORT_RX = re.compile(r"(https?://(?:127\.0\.0\.1|localhost)):\d{2,5}")
_HEX32_RX = re.compile(r"\b[0-9a-f]{32,64}\b")

#: PLAYWRIGHT'S OWN ERROR PROSE, not ours.
#:
#: When a click times out, the port stores Playwright's exception text verbatim
#: in the record's ``detail``, and that text contains a "Call log:" block whose
#: lines Playwright itself formats. The bullet prefix on those lines is not
#: stable across Playwright builds:
#:
#:     recorded:  "Call log:\n  - waiting for get_by_role(\"button\", …)"
#:     observed:  "Call log:\nwaiting for get_by_role(\"button\", …)"
#:
#: Two goldens (11-confirm-gated-step, 13-canvas) failed on exactly that, with
#: the locator, the timeout, ``navigated: false`` and ``outcome: "error"`` all
#: identical — a third-party punctuation change reported as "captured behaviour
#: changed". requirements.txt pins playwright==1.48.0 and CI installs precisely
#: that, so the goldens had simply been recorded against a different build.
#:
#: Only the bullet is removed, and only inside a string that actually contains a
#: Call log block, so the locator text, the error class and the timeout value —
#: everything that says what OUR crawler did — still diff normally. This is the
#: same principle as _UNSTABLE_KEYS: normalise what another system's clock or
#: formatter owns, never what Capture decided.
_PW_CALL_LOG_BULLET_RX = re.compile(r"(?m)^[ \t]*-[ \t]+(?=waiting for )")


def normalize_value(value: Any, key: str = "") -> Any:
    """Replace ONLY run-varying values with stable tokens.

    Three classes, each named and bounded:

      * a key in :data:`_UNSTABLE_KEYS` → ``"<normalized:key>"``
      * a UUID anywhere in a string     → ``"<uuid>"``
      * an ephemeral localhost port     → ``"<port>"``
      * Playwright's own Call-log bullet prefix (see
        :data:`_PW_CALL_LOG_BULLET_RX`) — third-party formatting, not behaviour

    Functional output — names, roles, options, group keys, outcomes, counts,
    coverage — is returned untouched. That is the whole contract: a behavioural
    change MUST survive normalisation and show up as a diff.
    """
    if key in _UNSTABLE_KEYS:
        return f"<normalized:{key}>"
    if isinstance(value, str):
        v = _UUID_RX.sub("<uuid>", value)
        v = _PORT_RX.sub(r"\1:<port>", v)
        v = _HEX32_RX.sub("<hash>", v)
        if "Call log:" in v:
            v = _PW_CALL_LOG_BULLET_RX.sub("", v)
        return v
    if isinstance(value, dict):
        return {k: normalize_value(v, k) for k, v in sorted(value.items())}
    if isinstance(value, list):
        return [normalize_value(v, key) for v in value]
    return value


def normalize_manifest(records: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Normalise a whole manifest record stream, preserving record ORDER.

    Order is functional output — it is the sequence in which the crawl observed
    the application — so it is never sorted away.
    """
    return [normalize_value(dict(r)) for r in records]


def canonical_json(obj: Any) -> str:
    """One byte-stable serialisation, so goldens are compared byte-for-byte."""
    return json.dumps(obj, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def golden_path(name: str) -> Path:
    return GOLDEN_DIR / f"{name}.json"


def read_golden(name: str) -> Optional[str]:
    p = golden_path(name)
    return p.read_text(encoding="utf-8") if p.exists() else None


def write_golden(name: str, payload: str) -> Path:
    GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
    p = golden_path(name)
    with open(p, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(payload)
    return p


UPDATE_GOLDENS = os.environ.get("QEC_UPDATE_GOLDENS", "").strip().lower() in ("1", "true", "yes")


def assert_golden(name: str, actual_obj: Any) -> None:
    """Byte-compare a normalised payload against its stored golden.

    Set ``QEC_UPDATE_GOLDENS=1`` to (re)record. Re-recording is a deliberate,
    reviewable act: the diff lands in git and a human decides whether the
    behaviour change was intended.
    """
    payload = canonical_json(actual_obj)
    existing = read_golden(name)
    if existing is None or UPDATE_GOLDENS:
        write_golden(name, payload)
        if existing is None:
            return                                    # first record — nothing to compare
        if existing != payload:
            return
        return
    if existing == payload:
        return

    exp_lines = existing.splitlines()
    act_lines = payload.splitlines()
    import difflib
    diff = "\n".join(difflib.unified_diff(
        exp_lines, act_lines, fromfile=f"golden/{name}.json (recorded)",
        tofile=f"golden/{name}.json (this run)", lineterm="", n=3))
    raise AssertionError(
        f"CHARACTERIZATION DIFF for {name!r} — captured behaviour changed.\n"
        f"If the change is intended, review this diff and re-record with\n"
        f"  QEC_UPDATE_GOLDENS=1 python -m pytest tests/browser -k {name}\n\n"
        f"{diff[:20000]}"
    )
