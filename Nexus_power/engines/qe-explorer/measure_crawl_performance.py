"""MEASURE: what does a whole crawl cost?

    python measure_crawl_performance.py                 # the default app set, 3 reps
    python measure_crawl_performance.py --reps 5 --apps acme-life,vkpower-life
    python measure_crawl_performance.py --baseline      # rewrite the committed baseline

WHY THIS EXISTS.  ``measure.py`` measures the FILL ENGINE against a fake port:
twenty fields, no browser, no navigation, no network.  That is a useful number
and it is not a crawl.  Nothing in this repository has ever recorded what the
production crawler costs end to end, so there is no reference against which a
future change can be called a regression -- only opinion about whether a run
"felt slow".  Gate 0 / A4 exists to replace that opinion with a number.

WHAT IS REAL HERE.  Everything the crawl does.  The production ``Crawler``, the
production ``PlaywrightBrowserPort``, real headless Chromium, the real refuse
pack, the real inventory JavaScript, real navigation and real screenshots
against real proving-ground applications served over real HTTP.  The ONLY
stand-in is the tier-3 advance oracle, which is a deterministic function rather
than an LLM -- a network round trip to a model would make the wall-clock number
measure somebody's API latency instead of this repository's code.  That
substitution is named in the report rather than hidden in it.

HOW IT MEASURES.  The port is wrapped in a counting/timing proxy
(:class:`TimedPort`) that records every call the crawler makes and how long it
took.  The proxy adds an attribute lookup and two ``perf_counter`` reads per
call; against operations that cost milliseconds-to-seconds in a browser that is
noise, and it is the only way to attribute cost to a PHASE (extraction versus
navigation versus screenshot) without editing production code to instrument
itself.  Memory and CPU are sampled on a background thread across this process
AND its children, because the browser is a child process and a crawl's real
footprint is both.

WHY REPETITIONS.  A single timing is an anecdote.  Each application is crawled
``--reps`` times and the report carries median, mean, P95 and worst so a reader
can see the SPREAD -- a change that moves the median 5% but doubles the worst
case is a regression this format shows and a single average hides.

THIS INSTRUMENT ASSERTS NOTHING.  It is not a gate.  It prints and records what
happened, including the parts that are inconvenient.  The committed baseline it
writes is the reference a later gate can be built against; turning a
measurement into a threshold is a separate, deliberate decision.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import shutil
import statistics
import sys
import threading
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

EXPLORER = Path(__file__).resolve().parent
sys.path.insert(0, str(EXPLORER))
sys.path.insert(0, str(EXPLORER / "tests" / "browser"))

import _harness as H                                              # noqa: E402
from app.auth import AuthWindow, Credentials                      # noqa: E402
from app.crawl_constants import TRAVERSAL_FULL                    # noqa: E402
from app.crawler import Budget, Crawler, GuardContext             # noqa: E402
from app.guard import load_refuse_pack                            # noqa: E402
from app.main import EXPLORER_VERSION, PlaywrightBrowserPort      # noqa: E402
from tests.characterization.harness import disposable_attestation  # noqa: E402

try:                                                # pragma: no cover - optional
    import psutil
except ImportError:                                 # pragma: no cover
    psutil = None

PG = EXPLORER.parent.parent / "proving-grounds"
BASELINE = EXPLORER / "perf" / "crawl_baseline.json"

#: The applications crawled by default.
#:
#: These four are every proving ground that serves STATICALLY.
#: ``summit-life-carrier`` is deliberately absent: it is a Next.js SSR build with
#: no ``index.html``, so it needs a Docker lane to run at all. Benchmarking it
#: would measure a container boot as well as a crawl, and silently folding that
#: into the same numbers would make the baseline dishonest. It is recorded as a
#: named gap in the report instead of being quietly dropped.
DEFAULT_APPS = ("acme-life", "vkpower-life", "questionnaire-life", "catalog-evidence")

CREDS = {"username": "qec.perf@example.test", "password": "Perf!Passw0rd"}

#: Phase attribution for every BrowserPort method the crawler can call. A method
#: absent from this map is still timed -- it lands in "other", which is what makes
#: a newly added port method visible in the report rather than silently untimed.
PHASES: dict[str, str] = {
    "goto": "navigation",
    "current_url": "navigation",
    "title": "navigation",
    "active_page_token": "navigation",
    "collect_controls": "dom_extraction",
    "collect_controls_result": "dom_extraction",
    "collect_displayed_values": "dom_extraction",
    "collect_pii_regions": "dom_extraction",
    "visible_texts": "dom_extraction",
    "error_texts": "dom_extraction",
    "status_texts": "dom_extraction",
    "dialog_flags": "dom_extraction",
    "materialize": "dom_extraction",
    "screenshot_png": "screenshot",
    "click": "interaction",
    "click_at": "interaction",
    "hover": "interaction",
    "drag": "interaction",
    "draw_stroke": "interaction",
    "press_keys": "interaction",
    "scroll_until": "interaction",
    "set_input_files": "interaction",
    "fill": "interaction",
    "select_option": "interaction",
    "set_checked": "interaction",
    "drain_network": "network_drain",
    "drain_browser_events": "network_drain",
    "storage_state": "other",
}


# ─── The timing proxy ────────────────────────────────────────────────────────

class TimedPort:
    """Wraps a real ``BrowserPort`` and records the cost of every call.

    Deliberately a ``__getattr__`` proxy rather than a subclass: the crawler
    talks to the port through a Protocol with ~30 methods, and a subclass would
    have to override each one -- which means a port method added next month
    would be measured as free.  Anything not explicitly handled here is still
    forwarded and still timed.
    """

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        #: method name -> list of durations in ms
        self.calls: dict[str, list[float]] = defaultdict(list)

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._inner, name)
        if not callable(attr) or name.startswith("_"):
            return attr

        if asyncio.iscoroutinefunction(attr):
            async def _timed_async(*a: Any, **kw: Any) -> Any:
                t0 = time.perf_counter()
                try:
                    return await attr(*a, **kw)
                finally:
                    self.calls[name].append((time.perf_counter() - t0) * 1000.0)
            return _timed_async

        def _timed_sync(*a: Any, **kw: Any) -> Any:
            t0 = time.perf_counter()
            try:
                return attr(*a, **kw)
            finally:
                self.calls[name].append((time.perf_counter() - t0) * 1000.0)
        return _timed_sync

    # ── reporting ──
    def by_phase(self) -> dict[str, dict[str, float]]:
        agg: dict[str, list[float]] = defaultdict(list)
        for name, durations in self.calls.items():
            agg[PHASES.get(name, "other")].extend(durations)
        return {phase: _describe(v) for phase, v in sorted(agg.items())}

    def total_calls(self) -> int:
        return sum(len(v) for v in self.calls.values())

    def total_ms(self) -> float:
        return sum(sum(v) for v in self.calls.values())


# ─── Resource sampling ───────────────────────────────────────────────────────

class ResourceSampler:
    """Samples RSS and CPU across this process AND its children on a thread.

    The browser is a CHILD process. Sampling only ``os.getpid()`` would report
    the memory of the Python driver and call it the crawl's footprint, which
    would understate it by roughly the size of Chromium -- the single largest
    consumer in the run.
    """

    def __init__(self, interval_s: float = 0.25) -> None:
        self.interval = interval_s
        self.samples_rss_mb: list[float] = []
        self.samples_cpu_pct: list[float] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._proc = psutil.Process(os.getpid()) if psutil else None
        self._cpu0: Any = None

    def _tree(self) -> list[Any]:
        if not self._proc:
            return []
        try:
            return [self._proc, *self._proc.children(recursive=True)]
        except Exception:                            # pragma: no cover - races
            return [self._proc]

    def _loop(self) -> None:
        while not self._stop.wait(self.interval):
            rss = 0.0
            cpu = 0.0
            for p in self._tree():
                try:
                    rss += p.memory_info().rss
                    cpu += p.cpu_percent(None)
                except Exception:                    # pragma: no cover - exited
                    continue
            if rss:
                self.samples_rss_mb.append(rss / (1024 * 1024))
            self.samples_cpu_pct.append(cpu)

    def __enter__(self) -> "ResourceSampler":
        if not psutil:
            return self
        for p in self._tree():
            try:
                p.cpu_percent(None)                  # prime the counter
            except Exception:
                pass
        self._cpu0 = self._proc.cpu_times() if self._proc else None
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)

    def report(self) -> dict[str, Any]:
        if not psutil:
            return {"available": False,
                    "reason": "psutil is not installed; memory and CPU not measured"}
        return {
            "available": True,
            "samples": len(self.samples_rss_mb),
            "rss_mb_peak": round(max(self.samples_rss_mb), 1) if self.samples_rss_mb else None,
            "rss_mb_mean": round(statistics.fmean(self.samples_rss_mb), 1) if self.samples_rss_mb else None,
            "cpu_pct_peak": round(max(self.samples_cpu_pct), 1) if self.samples_cpu_pct else None,
            "cpu_pct_mean": round(statistics.fmean(self.samples_cpu_pct), 1) if self.samples_cpu_pct else None,
            "cpu_pct_note": "sum across the process tree; 100% == one fully busy core",
        }


# ─── Statistics ──────────────────────────────────────────────────────────────

def _p95(values: list[float]) -> float:
    """P95 by nearest-rank.

    Nearest-rank rather than an interpolating quantile on purpose: with three
    repetitions an interpolated P95 invents a value between two observations and
    reads as more precise than the sample supports. Nearest-rank always returns a
    number that was actually MEASURED.
    """
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(1, int(-(-95 * len(ordered) // 100)))     # ceil(0.95 * n)
    return ordered[min(rank, len(ordered)) - 1]


def _describe(values: list[float]) -> dict[str, Any]:
    if not values:
        return {"n": 0}
    return {
        "n": len(values),
        "total_ms": round(sum(values), 1),
        "median_ms": round(statistics.median(values), 2),
        "mean_ms": round(statistics.fmean(values), 2),
        "p95_ms": round(_p95(values), 2),
        "worst_ms": round(max(values), 2),
    }


# ─── The deterministic tier-3 stand-in ───────────────────────────────────────

FORWARD = ("quote", "continue", "next", "proceed", "apply", "review", "start", "see")


async def stub_advance_oracle(candidates: Any, page_title: str, page_url: str) -> dict[str, Any]:
    """Identical in rule to ``measure_boundary_crossing.py``'s stand-in, and
    silent -- a per-pick print would add console I/O to a timed section."""
    names = [str(c.get("name") or "") for c in candidates]
    for want_button in (True, False):
        for i, (n, c) in enumerate(zip(names, candidates)):
            if (str(c.get("kind") or "") == "button") is not want_button:
                continue
            if any(w in n.lower() for w in FORWARD):
                return {"status": "picked", "index": i, "signature": "stub-forward"}
    return {"status": "none", "signature": "stub"}


# ─── One crawl ───────────────────────────────────────────────────────────────

async def crawl_once(app: str, url: str, rep: int, work_root: Path) -> dict[str, Any]:
    from playwright.async_api import async_playwright
    from app.playwright_port import context_defaults

    pack = load_refuse_pack(str(EXPLORER / "app" / "refuse_pack.yaml"))
    guard_ctx = GuardContext(
        refuse_pack=pack,
        auth_window=AuthWindow(max_requests=400, window_ms=240_000),
        attestation=disposable_attestation(),
        submit_flow_approved=False,        # read-only: no crossing, no grants
        walk_authorization=None,
        idp_domains=frozenset(),
    )
    budget = Budget.from_dict({"max_states": 40, "max_actions": 250,
                               "max_requests": 4000, "max_duration_ms": 420_000})
    work = work_root / f"{app}-rep{rep}"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)

    # ── browser startup ──
    t_boot = time.perf_counter()
    pw = await async_playwright().start()
    browser = await pw.chromium.launch(headless=True)
    ctx = await browser.new_context(**context_defaults())
    page = await ctx.new_page()
    browser_startup_ms = (time.perf_counter() - t_boot) * 1000.0
    browser_version = browser.version

    port = TimedPort(PlaywrightBrowserPort(page, ctx))
    crawler = Crawler(
        port,
        crawl_id=f"perf-{app}-{rep}", tenant_id="perf", target_url=url,
        work_dir=str(work), refuse_pack=pack, budget=budget,
        explorer_version=EXPLORER_VERSION, guard_version=EXPLORER_VERSION,
        refuse_pack_version=pack.version,
        config_fingerprint=f"perf-{app}",
        guard_context=guard_ctx, identity_seed="qec-perf",
        observe_only=False,
        traversal=TRAVERSAL_FULL,
        advance_oracle=stub_advance_oracle,
        credentials=Credentials.from_payload(CREDS),
    )

    with ResourceSampler() as res:
        t0 = time.perf_counter()
        try:
            await crawler.run()
            crawl_error = None
        except Exception as exc:                       # instrument, never mask
            crawl_error = f"{type(exc).__name__}: {exc}"
        crawl_wall_ms = (time.perf_counter() - t0) * 1000.0

        # ── artifact generation, measured separately from the walk ──
        t_art = time.perf_counter()
        cov = crawler._coverage.build()
        artifact_ms = (time.perf_counter() - t_art) * 1000.0

    await ctx.close()
    await browser.close()
    await pw.stop()

    manifest_bytes = sum(f.stat().st_size for f in work.rglob("*") if f.is_file())
    manifest_files = sum(1 for f in work.rglob("*") if f.is_file())

    states = len(cov.get("states") or [])
    navigations = len(port.calls.get("goto", []))
    interactions = sum(len(port.calls.get(m, []))
                       for m, ph in PHASES.items() if ph == "interaction")
    net_events = cov.get("network_events_observed")
    if not isinstance(net_events, int):
        net_events = len(cov.get("endpoints") or [])
    secs = crawl_wall_ms / 1000.0 or 1e-9

    return {
        "app": app, "rep": rep, "error": crawl_error,
        "browser_startup_ms": round(browser_startup_ms, 1),
        "crawl_wall_ms": round(crawl_wall_ms, 1),
        "artifact_generation_ms": round(artifact_ms, 2),
        "states_discovered": states,
        "navigations": navigations,
        "interactions": interactions,
        "network_requests": net_events,
        "forms_found": cov.get("forms_found"),
        "port_calls": port.total_calls(),
        "port_ms_total": round(port.total_ms(), 1),
        "port_share_of_wall_pct": round(100.0 * port.total_ms() / crawl_wall_ms, 1)
        if crawl_wall_ms else None,
        # throughput
        "states_per_s": round(states / secs, 3),
        "pages_per_s": round(navigations / secs, 3),
        "network_req_per_s": round((net_events or 0) / secs, 3),
        "port_calls_per_s": round(port.total_calls() / secs, 2),
        # artifacts
        "artifact_files": manifest_files,
        "artifact_bytes": manifest_bytes,
        # breakdowns
        "phases": port.by_phase(),
        "slowest_methods": dict(sorted(
            ((m, _describe(v)) for m, v in port.calls.items()),
            key=lambda kv: kv[1].get("total_ms", 0), reverse=True)[:8]),
        "resources": res.report(),
        "browser_version": browser_version,
    }


# ─── Environment ─────────────────────────────────────────────────────────────

def environment(browser_version: str | None) -> dict[str, Any]:
    import playwright
    env: dict[str, Any] = {
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "machine": platform.machine(),
        "processor": platform.processor() or "unknown",
        "playwright": getattr(playwright, "__version__", "unknown"),
        "chromium": browser_version or "unknown",
        "explorer_version": EXPLORER_VERSION,
    }
    if psutil:
        env["cpu_logical"] = psutil.cpu_count(logical=True)
        env["cpu_physical"] = psutil.cpu_count(logical=False)
        env["ram_total_gb"] = round(psutil.virtual_memory().total / 1024**3, 1)
    else:
        env["cpu_logical"] = os.cpu_count()
        env["psutil"] = "absent — memory and CPU were NOT measured"
    return env


# ─── Aggregation ─────────────────────────────────────────────────────────────

SUMMARY_FIELDS = ("crawl_wall_ms", "browser_startup_ms", "artifact_generation_ms",
                  "states_per_s", "pages_per_s", "network_req_per_s",
                  "port_calls_per_s", "port_share_of_wall_pct")


def aggregate(runs: list[dict[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for field in SUMMARY_FIELDS:
        vals = [r[field] for r in runs if isinstance(r.get(field), (int, float))]
        if vals:
            out[field] = _describe(vals)
    for field in ("states_discovered", "navigations", "interactions",
                  "network_requests", "port_calls", "artifact_files"):
        vals = [r[field] for r in runs if isinstance(r.get(field), (int, float))]
        if vals:
            out[field] = {"median": statistics.median(vals),
                          "min": min(vals), "max": max(vals),
                          "stable": len(set(vals)) == 1}
    peaks = [r["resources"].get("rss_mb_peak") for r in runs
             if r.get("resources", {}).get("rss_mb_peak")]
    if peaks:
        out["rss_mb_peak"] = _describe(peaks)
    cpus = [r["resources"].get("cpu_pct_mean") for r in runs
            if r.get("resources", {}).get("cpu_pct_mean")]
    if cpus:
        out["cpu_pct_mean"] = _describe(cpus)
    return out


def _fmt(d: dict[str, Any], unit: str = "ms") -> str:
    if "median_ms" not in d:
        return str(d)
    return (f"median {d['median_ms']:>9.2f} | mean {d['mean_ms']:>9.2f} | "
            f"p95 {d['p95_ms']:>9.2f} | worst {d['worst_ms']:>9.2f}  ({unit})")


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apps", default=",".join(DEFAULT_APPS),
                    help="comma-separated proving-ground names")
    ap.add_argument("--reps", type=int, default=3,
                    help="repetitions per application (default 3)")
    ap.add_argument("--baseline", action="store_true",
                    help=f"write the result to {BASELINE.relative_to(EXPLORER)}")
    ap.add_argument("--out", default=None, help="also write the raw JSON here")
    args = ap.parse_args()

    apps = [a.strip() for a in args.apps.split(",") if a.strip()]
    work_root = Path(os.environ.get("QEC_PERF_WORK") or (EXPLORER / "_perf_work"))
    shutil.rmtree(work_root, ignore_errors=True)
    work_root.mkdir(parents=True, exist_ok=True)

    srv = H.FixtureServer(root=PG).start()
    all_runs: list[dict[str, Any]] = []
    per_app: dict[str, Any] = {}
    browser_version = None
    try:
        for app in apps:
            if not (PG / app / "index.html").exists():
                print(f"\n!! SKIPPING {app}: no index.html — it does not serve "
                      f"statically and needs a Docker lane.", flush=True)
                per_app[app] = {"skipped": "no index.html; needs a Docker lane"}
                continue
            url = srv.url(app)
            print(f"\n=== {app}  ({args.reps} reps)  {url}", flush=True)
            runs = []
            for rep in range(1, args.reps + 1):
                r = await crawl_once(app, url, rep, work_root)
                browser_version = browser_version or r["browser_version"]
                runs.append(r)
                all_runs.append(r)
                err = f"  ERROR {r['error']}" if r["error"] else ""
                print(f"  rep {rep}: wall {r['crawl_wall_ms']:>8.0f} ms | "
                      f"states {r['states_discovered']:>3} | "
                      f"nav {r['navigations']:>3} | "
                      f"net {r['network_requests']} | "
                      f"port-calls {r['port_calls']:>4} | "
                      f"rss-peak {r['resources'].get('rss_mb_peak')} MB{err}",
                      flush=True)
            per_app[app] = {"runs": runs, "summary": aggregate(runs)}
    finally:
        srv.stop()

    report = {
        "schema": "qec.crawl_perf_baseline/1",
        "environment": environment(browser_version),
        "configuration": {
            "reps_per_app": args.reps,
            "apps_requested": apps,
            "budget": {"max_states": 40, "max_actions": 250,
                       "max_requests": 4000, "max_duration_ms": 420_000},
            "observe_only": False,
            "submit_flow_approved": False,
            "boundary_approvals": None,
            "advance_oracle": "deterministic stand-in (NOT a model) — see module docstring",
            "traversal": "TRAVERSAL_FULL",
        },
        "methodology": {
            "timing": "TimedPort proxy around the production PlaywrightBrowserPort; "
                      "two perf_counter reads per port call",
            "resources": "psutil sampling this process AND its children every 250 ms",
            "p95": "nearest-rank over measured samples (never interpolated)",
            "reproduce": "python measure_crawl_performance.py --reps 3",
            "not_measured": [
                "summit-life-carrier — Next.js SSR, needs a Docker lane (Gate 2 / A16)",
                "tier-3 oracle latency — deliberately stubbed",
                "qe-central ingest — a separate service, not in this process",
            ],
        },
        "per_app": per_app,
        "overall": aggregate(all_runs) if all_runs else {},
    }

    # ── console report ──
    print("\n" + "=" * 78)
    print("CRAWL PERFORMANCE BASELINE")
    print("=" * 78)
    e = report["environment"]
    print(f"  {e['platform']}  |  {e.get('cpu_logical')} logical CPUs  |  "
          f"{e.get('ram_total_gb')} GB RAM")
    print(f"  python {e['python']}  playwright {e['playwright']}  "
          f"chromium {e['chromium']}")
    for app, data in per_app.items():
        if "skipped" in data:
            print(f"\n  {app}: SKIPPED — {data['skipped']}")
            continue
        s = data["summary"]
        print(f"\n  {app}")
        for f in ("crawl_wall_ms", "browser_startup_ms", "artifact_generation_ms"):
            if f in s:
                print(f"    {f:<24} {_fmt(s[f])}")
        for f in ("states_discovered", "navigations", "network_requests", "port_calls"):
            if f in s:
                d = s[f]
                flag = "" if d["stable"] else "   <- VARIES BETWEEN RUNS"
                print(f"    {f:<24} median {d['median']}  "
                      f"[{d['min']}..{d['max']}]{flag}")
        if "rss_mb_peak" in s:
            print(f"    {'rss_mb_peak':<24} {_fmt(s['rss_mb_peak'], 'MB')}")
        ph = data["runs"][0]["phases"]
        print("    phase breakdown (rep 1):")
        for phase, d in sorted(ph.items(), key=lambda kv: -kv[1].get("total_ms", 0)):
            print(f"      {phase:<18} n={d['n']:<5} total {d['total_ms']:>9.1f} ms  "
                  f"median {d['median_ms']:>7.2f}  p95 {d['p95_ms']:>8.2f}")

    if args.out:
        Path(args.out).write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"\nraw JSON -> {args.out}")
    if args.baseline:
        BASELINE.parent.mkdir(parents=True, exist_ok=True)
        BASELINE.write_text(json.dumps(report, indent=2) + "\n",
                            encoding="utf-8", newline="\n")
        print(f"\nbaseline -> {BASELINE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
