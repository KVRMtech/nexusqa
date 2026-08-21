"""A32 + A33 — the fleet egress fence, red-teamed with REAL Chromium and REAL Squid.

WHY THIS FILE EXISTS
====================
``test_t_fl_08_concurrency_redteam.py`` proves the CONTROL PLANE: the queue, the
worker registry, capacity accounting, RLS, and that each worker is handed its own
fence file. It says so honestly in its own docstring — the explorer it runs is "a
coroutine that takes a registry slot, reads the fence it was given, 'crawls' for a
moment, publishes evidence, and releases."

A coroutine cannot violate an egress fence. It has no network stack, no DNS, no
TLS, and no proxy setting. So the property the milestone actually cares about —
*a browser under load cannot reach a host outside its worker's fence* — was
asserted about an object structurally incapable of failing it.

This harness closes that gap. Everything in the egress path is the real thing:

  REAL Squid          the production image (``ubuntu/squid:latest``) running the
                      repository's own ``engines/qe-explorer/squid.conf`` bytes
                      — verified CR-free before the run, so they are the bytes a
                      Linux deployment loads and not a Windows checkout's
                      rewrite of them (see assert_config_is_production_bytes),
                      started by the same entrypoint + HUP watcher that
                      ``docker-compose.qec.yml`` uses in production.
  REAL Chromium       Playwright-launched browsers, one browser context per
                      worker, each proxied through ITS OWN worker's Squid.
  REAL origins        separate containers on a Docker network with DNS names, so
                      "a different destination" is a genuinely different host
                      that Squid must resolve and decide about.
  REAL fence files    written into the running Squid container, exactly as
                      qe-central writes them into the shared allowlist volume.

WHAT A32 ASSERTS
================
Under N concurrent workers navigating simultaneously, every worker reaches its
OWN destination and NO worker reaches any other worker's destination. A single
cross-fence success is a breach and fails the run.

WHAT A33 ASSERTS
================
That a fence REWRITE takes effect on a LIVE browser without restarting Squid.
The proof is deliberately not a file timestamp or a config parse: the same
already-open Chromium context navigates, the fence is rewritten underneath it,
and the *subsequent* navigation flips outcome — allowed becomes denied and denied
becomes allowed. Squid's container start time and PID-1 start time are captured
before and after and asserted UNCHANGED, so "it reloaded" cannot be satisfied by
a restart.

THE NEGATIVE CONTROL — WITHOUT IT THIS PROVES NOTHING
=====================================================
A harness that only ever observes "denied" can be passing because egress is
broken everywhere: a wrong proxy port, a dead origin, or a Chromium that never
left the machine all produce a perfect score. So every worker must FIRST reach
its own origin and read back that origin's unique body. Only once a fence is
proven PERMEABLE in the allowed direction does a denial mean the fence did it.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
import time
from pathlib import Path

# This harness runs on Windows, whose console defaults to cp1252. A single
# non-encodable character in the REPORT raised UnicodeEncodeError and discarded
# a run that had already completed successfully — the security proof was done
# and the process died printing it. Encoding is therefore pinned here rather
# than by policing every string in the file.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:      # pragma: no cover — older/redirected streams
    pass

_HERE = Path(__file__).resolve()
_REPO_NEXUS = _HERE.parents[4]           # …/Nexus_power
SQUID_CONF = _REPO_NEXUS / "engines" / "qe-explorer" / "squid.conf"

NETWORK = "gate4-egress"
SQUID_IMAGE = "ubuntu/squid:latest"
ORIGIN_IMAGE = "nginx:alpine"
ALLOWLIST = "/etc/squid/allowlist/allowed_domains.txt"

#: The production entrypoint, copied from docker-compose.qec.yml. Kept as one
#: string so any drift from the deployed command is visible in a diff.
SQUID_COMMAND = (
    "mkdir -p /etc/squid/allowlist && "
    "{ [ -s /etc/squid/allowlist/allowed_domains.txt ] || "
    "printf '%s\\n' '# fail-closed default' '.qec-egress-denied.invalid' "
    "> /etc/squid/allowlist/allowed_domains.txt; } && "
    "{ chown proxy:proxy /dev/stdout /dev/stderr 2>/dev/null || true; } && "
    "{ A=/etc/squid/allowlist/allowed_domains.txt; L=; while :; do "
    "C=$(stat -c %Y \"$A\" 2>/dev/null || echo 0); "
    "[ \"$C\" != \"$L\" ] && { L=$C; "
    "kill -HUP 1 2>/dev/null || squid -k reconfigure 2>/dev/null || true; }; "
    "sleep 1; done & } && "
    "exec squid -N -f /etc/squid/squid.conf -d1"
)


def sh(*args: str, check: bool = True, timeout: int = 120) -> str:
    p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if check and p.returncode != 0:
        raise RuntimeError(f"{' '.join(args)}\n{p.stdout}\n{p.stderr}")
    return (p.stdout or "").strip()


def docker(*args: str, **kw) -> str:
    return sh("docker", *args, **kw)


# ══════════════════════════════════════════════════════════════════════════
# Infrastructure
# ══════════════════════════════════════════════════════════════════════════

class Worker:
    """One fleet worker: its own Squid, its own fence file, its own browser."""

    def __init__(self, idx: int, tenant: str, origin_host: str, port: int):
        self.idx = idx
        self.tenant = tenant
        self.origin_host = origin_host          # the host this worker MAY reach
        self.port = port                        # host port for this worker's squid
        self.container = f"gate4-squid-{idx}"

    @property
    def proxy(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def write_fence(self, *domains: str) -> None:
        """Rewrite this worker's allowlist exactly as qe-central does."""
        body = "\\n".join(domains)
        docker("exec", self.container, "sh", "-c",
               f"printf '%s\\n' {' '.join(repr(d) for d in domains)} > {ALLOWLIST}")
        _ = body

    def read_fence(self) -> str:
        return docker("exec", self.container, "cat", ALLOWLIST)

    def identity(self) -> dict:
        """Container start time + PID-1 start time — the anti-restart evidence."""
        started = docker("inspect", self.container, "--format", "{{.State.StartedAt}}")
        pid1 = docker("exec", self.container, "sh", "-c",
                      "cat /proc/1/stat | awk '{print $22}'")
        return {"container_started_at": started, "pid1_starttime_jiffies": pid1}


def assert_config_is_production_bytes() -> dict:
    """Refuse to run against a squid.conf this platform has rewritten.

    THE DEFECT THIS CLOSES, WHICH THIS HARNESS ITSELF SHIPPED.
    ``squid.conf`` had no ``eol`` attribute, so with ``core.autocrlf=true`` a
    Windows checkout produced a working copy with 71 CR bytes while the
    committed blob is LF. This harness ``docker cp``s that working copy into
    ``ubuntu/squid`` — so the first green A32/A33 run proved the fence against
    CRLF config that no Linux deployment has ever loaded, while its own
    docstring claimed it ran "the repository's own squid.conf bytes".

    It passed, which is the problem: a security proof that runs against
    different bytes than production is not a proof about production, and
    nothing failed to say so. ``.gitattributes`` now pins the file to
    ``eol=lf``; this check is the belt to that braces, because an attribute can
    be missed again and a silently-passing proof is the failure mode.
    """
    raw = SQUID_CONF.read_bytes()
    crs = raw.count(b"\r")
    if crs:
        raise RuntimeError(
            f"REFUSING TO RUN: {SQUID_CONF} contains {crs} CR byte(s). The "
            f"container would be handed CRLF config while production loads LF, "
            f"so this run would prove the fence for bytes nobody deploys. Fix "
            f"the checkout (.gitattributes pins this file to eol=lf; try "
            f"`git add --renormalize` then re-checkout).")
    import hashlib
    return {"path": str(SQUID_CONF), "bytes": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(), "cr_bytes": 0}


def teardown(names: list[str]) -> None:
    for n in names:
        subprocess.run(["docker", "rm", "-f", n],
                       capture_output=True, text=True, timeout=60)


def build_infra(n_workers: int, base_port: int) -> tuple[list[Worker], list[str]]:
    """Bring up the network, the origins and one Squid per worker."""
    created: list[str] = []
    subprocess.run(["docker", "network", "create", NETWORK],
                   capture_output=True, text=True, timeout=60)

    workers: list[Worker] = []
    for i in range(n_workers):
        tenant = f"tenant-{chr(ord('a') + i)}"
        host = f"origin-{chr(ord('a') + i)}.gate4.test"
        name = f"gate4-origin-{i}"
        teardown([name])
        # A DISTINCT BODY per origin. This is what turns "the page loaded" into
        # "the page loaded FROM THE HOST THIS WORKER WAS FENCED TO".
        docker("run", "-d", "--name", name, "--network", NETWORK,
               "--network-alias", host, ORIGIN_IMAGE, "sh", "-c",
               f"echo GATE4-ORIGIN-{i}-{tenant} > /usr/share/nginx/html/index.html "
               f"&& exec nginx -g 'daemon off;'")
        created.append(name)
        workers.append(Worker(i, tenant, host, base_port + i))

    for w in workers:
        teardown([w.container])
        docker("create", "--name", w.container, "--network", NETWORK,
               "-p", f"127.0.0.1:{w.port}:3128",
               "--entrypoint", "/bin/sh", SQUID_IMAGE, "-c", SQUID_COMMAND)
        # The REAL config bytes, copied in rather than bind-mounted so no host
        # path translation can alter what Squid actually parses.
        docker("cp", str(SQUID_CONF), f"{w.container}:/etc/squid/squid.conf")
        docker("start", w.container)
        created.append(w.container)
    return workers, created


def wait_for_squid(workers: list[Worker], timeout_s: int = 90) -> None:
    """Ready = the published port ACCEPTS, and the proxy is already fail-closed.

    The readiness probe is a real TCP connect from the host rather than a
    command inside the container: ``ubuntu/squid`` ships neither ``netstat`` nor
    ``ss``, so an in-container port check reports "down" forever against a
    perfectly healthy Squid. (That is exactly what it did on this harness's
    first run, and it looked like Squid was broken.)

    The second half is the more useful assertion. Before any fence is written,
    the seeded sentinel allowlist must already DENY a real internet host. If a
    fresh proxy allowed egress by default, every "denied" this harness later
    records would be meaningless.
    """
    import socket
    import urllib.error
    import urllib.request

    deadline = time.time() + timeout_s
    for w in workers:
        while time.time() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", w.port), timeout=2):
                    break
            except OSError:
                time.sleep(1)
        else:
            logs = docker("logs", "--tail", "30", w.container, check=False)
            raise RuntimeError(f"squid {w.container} never accepted on {w.port}\n{logs}")

        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": w.proxy}))
        status = None
        for _ in range(15):
            try:
                opener.open("http://example.com/", timeout=5)
                status = 200
            except urllib.error.HTTPError as e:
                status = e.code
            except Exception:
                time.sleep(1)
                continue
            break
        if status != 403:
            raise RuntimeError(
                f"{w.container} did not fail closed on a fresh allowlist "
                f"(got {status}, expected 403). Every later denial would be "
                f"unattributable.")


# ══════════════════════════════════════════════════════════════════════════
# The browser side
# ══════════════════════════════════════════════════════════════════════════

async def fetch(context, url: str, timeout_ms: int = 15000) -> dict:
    """Navigate and classify the outcome as REACHED or REFUSED.

    A refusal is whatever the fence produces: Squid answers 403 for a
    non-allowlisted plain-HTTP host, and Chromium raises for a failed CONNECT.
    Both are 'the fence held'. Anything that returns the origin's body is
    'reached', which for a foreign origin is a BREACH.
    """
    page = await context.new_page()
    try:
        try:
            resp = await page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")
        except Exception as exc:
            return {"outcome": "REFUSED", "how": "navigation_error",
                    "detail": str(exc)[:160], "status": None, "body": ""}
        status = resp.status if resp else None
        body = (await page.content())[:4000]
        if status is not None and status >= 400:
            return {"outcome": "REFUSED", "how": f"http_{status}",
                    "detail": "", "status": status, "body": body[:200]}
        return {"outcome": "REACHED", "how": f"http_{status}",
                "detail": "", "status": status, "body": body}
    finally:
        await page.close()


def body_marker(idx: int, tenant: str) -> str:
    return f"GATE4-ORIGIN-{idx}-{tenant}"


async def run(n_workers: int, base_port: int, rounds: int, out: str) -> int:
    from playwright.async_api import async_playwright

    config = assert_config_is_production_bytes()
    workers, created = build_infra(n_workers, base_port)
    findings: dict = {"a32": {}, "a33": {}, "squid_conf": config}
    violations: list[dict] = []
    try:
        wait_for_squid(workers)
        # Each worker is fenced to EXACTLY its own origin.
        for w in workers:
            w.write_fence(w.origin_host)
        time.sleep(3)   # the 1s mtime watcher + HUP

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            contexts = []
            for w in workers:
                ctx = await browser.new_context(proxy={"server": w.proxy},
                                                ignore_https_errors=True)
                contexts.append(ctx)

            # ── NEGATIVE CONTROL ───────────────────────────────────────────
            # Prove each fence is PERMEABLE before trusting any denial.
            control = []
            for w, ctx in zip(workers, contexts):
                r = await fetch(ctx, f"http://{w.origin_host}/")
                ok = (r["outcome"] == "REACHED"
                      and body_marker(w.idx, w.tenant) in r["body"])
                control.append({"worker": w.idx, "host": w.origin_host,
                                "reached_own_origin": ok, **{k: r[k] for k in
                                                             ("outcome", "how")}})
            findings["a32"]["negative_control"] = control
            if not all(c["reached_own_origin"] for c in control):
                findings["a32"]["verdict"] = (
                    "INVALID - a worker could not reach its OWN origin, so every "
                    "'denied' below could be an infrastructure failure rather "
                    "than a fence decision. No security claim is made.")
                raise SystemExit(_emit(findings, out, 3))

            # ── THE RED TEAM ───────────────────────────────────────────────
            # Every worker attacks every OTHER worker's origin, all at once,
            # repeatedly, so the fences are under simultaneous cross-pressure.
            attempts = []
            for rnd in range(rounds):
                jobs = []
                for w, ctx in zip(workers, contexts):
                    for target in workers:
                        jobs.append((w, target,
                                     fetch(ctx, f"http://{target.origin_host}/")))
                results = await asyncio.gather(*[j[2] for j in jobs])
                for (w, target, _), r in zip(jobs, results):
                    own = (w.idx == target.idx)
                    reached = (r["outcome"] == "REACHED"
                               and body_marker(target.idx, target.tenant) in r["body"])
                    rec = {"round": rnd, "worker": w.idx, "worker_tenant": w.tenant,
                           "target": target.origin_host, "own_fence": own,
                           "outcome": r["outcome"], "how": r["how"],
                           "reached": reached}
                    attempts.append(rec)
                    if not own and reached:
                        violations.append(rec)
                    if own and not reached:
                        violations.append({**rec, "kind": "own_origin_lost"})
            findings["a32"].update({
                "workers": n_workers, "rounds": rounds,
                "attempts": len(attempts),
                "cross_fence_attempts": sum(1 for a in attempts if not a["own_fence"]),
                "violations": violations,
                "verdict": "PASS - zero fence violations" if not violations
                           else f"FAIL - {len(violations)} violation(s)",
            })

            # ── A33 · LIVE FENCE RELOAD ────────────────────────────────────
            # One already-open context, mid-session, no restart.
            w0, ctx0 = workers[0], contexts[0]
            other = workers[1] if len(workers) > 1 else None
            if other is None:
                findings["a33"]["verdict"] = "SKIPPED - needs >= 2 workers"
            else:
                before_id = w0.identity()
                pre_own = await fetch(ctx0, f"http://{w0.origin_host}/")
                pre_other = await fetch(ctx0, f"http://{other.origin_host}/")

                # THE REWRITE — swap the fence to the other origin.
                w0.write_fence(other.origin_host)
                fence_after = w0.read_fence()
                # The watcher polls mtime once a second; allow it to fire.
                time.sleep(4)

                post_own = await fetch(ctx0, f"http://{w0.origin_host}/")
                post_other = await fetch(ctx0, f"http://{other.origin_host}/")
                after_id = w0.identity()

                flipped = (
                    pre_own["outcome"] == "REACHED"
                    and pre_other["outcome"] == "REFUSED"
                    and post_own["outcome"] == "REFUSED"
                    and post_other["outcome"] == "REACHED"
                )
                no_restart = (before_id == after_id)
                findings["a33"] = {
                    "fence_before": w0.origin_host,
                    "fence_after_file": fence_after,
                    "before": {"own": pre_own["outcome"], "other": pre_other["outcome"]},
                    "after": {"own": post_own["outcome"], "other": post_other["outcome"]},
                    "squid_identity_before": before_id,
                    "squid_identity_after": after_id,
                    "restarted": not no_restart,
                    "verdict": (
                        "PASS - the rewritten fence took effect on a live browser "
                        "with no Squid restart" if (flipped and no_restart)
                        else f"FAIL - flipped={flipped} no_restart={no_restart}"),
                }
            for ctx in contexts:
                await ctx.close()
            await browser.close()
    finally:
        for w in workers:
            findings.setdefault("squid_logs", {})[w.container] = docker(
                "logs", "--tail", "40", w.container, check=False)
        teardown(created)
        subprocess.run(["docker", "network", "rm", NETWORK],
                       capture_output=True, text=True, timeout=60)

    bad = bool(violations) or "FAIL" in findings.get("a33", {}).get("verdict", "")
    return _emit(findings, out, 1 if bad else 0)


def _emit(findings: dict, out: str, code: int) -> int:
    if out:
        Path(out).write_text(json.dumps(findings, indent=2), encoding="utf-8")
    a32 = findings.get("a32", {})
    print("\n═══ A32 — N-concurrent egress red team (real Chromium, real Squid) ═══")
    for c in a32.get("negative_control", []):
        print(f"  control: worker {c['worker']} → own origin {c['host']}: "
              f"{'REACHED' if c['reached_own_origin'] else 'FAILED'}")
    print(f"  workers={a32.get('workers')} rounds={a32.get('rounds')} "
          f"attempts={a32.get('attempts')} "
          f"cross-fence={a32.get('cross_fence_attempts')}")
    print(f"  VERDICT: {a32.get('verdict')}")
    for v in a32.get("violations", [])[:10]:
        print(f"    !! {v}")
    a33 = findings.get("a33", {})
    print("\n═══ A33 — live Squid fence reload ═══")
    if "before" in a33:
        print(f"  before rewrite: own={a33['before']['own']} other={a33['before']['other']}")
        print(f"  after  rewrite: own={a33['after']['own']} other={a33['after']['other']}")
        print(f"  squid restarted: {a33.get('restarted')}")
    print(f"  VERDICT: {a33.get('verdict')}")
    if out:
        print(f"\nevidence → {out}")
    return code


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--base-port", type=int, default=53128)
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--out", default="")
    a = ap.parse_args()
    raise SystemExit(asyncio.run(run(a.workers, a.base_port, a.rounds, a.out)))
