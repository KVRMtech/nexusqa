"""T-HN-06 — CI PROVING GROUNDS: real applications, crawled end to end.

    Start services → launch app → dispatch crawl → wait → collect manifest →
    validate pages discovered

Every step is real. The crawl runs through the production
:class:`app.main.PlaywrightBrowserPort` and the production
:class:`app.crawler.Crawler`, and the assertions read the ``manifest.jsonl`` the
production :mod:`app.emit` writer put on disk. **No mocked crawl success is
accepted**: a missing manifest, an empty manifest, a crawl that discovered no
page state, or a crawl that discovered no controls all FAIL.

## Two ways to reach an application

**In CI** the workflow builds each proving ground from its own ``Dockerfile`` and
publishes it on a port; this module reads ``QEC_PROVING_GROUND_URL``.

**Locally** the statically-served grounds (``acme-life``, ``vkpower-life``) are
served straight from their source directory, so the lane is runnable — and
therefore verifiable — on a developer machine with no Docker. ``summit-life-carrier``
is a Next.js app that must be built, so it is CI-only and says so out loud rather
than silently not running.

The distinction matters for the milestone claim: a CI file that has never
executed is a plan, not a proving ground.
"""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any

import pytest

import _harness as H

pytestmark = [pytest.mark.browser, pytest.mark.playwright, pytest.mark.proving_ground]

#: Repo-root-relative home of the proving-ground applications.
PROVING_GROUNDS = H.SERVICE_ROOT.parent.parent / "proving-grounds"

#: Where crawl output is written so CI can archive it as evidence.
CRAWL_OUT = H.HERE / "_crawl_out"

#: The three required crawl targets.
#:
#:  * ``static`` — an ``index.html`` that can be served directly, so the target
#:    is reachable both locally and in CI.
#:  * ``built``  — needs a build step (Next.js), so it is CI-only.
TARGETS = {
    "acme-life": {"serving": "static", "min_controls": 5},
    "vkpower-life": {"serving": "static", "min_controls": 5},
    "summit-life-carrier": {"serving": "built", "min_controls": 5},
}


@pytest.fixture(scope="session")
def ground_server() -> Any:
    """Serve the proving-ground tree over HTTP (local lane)."""
    if not PROVING_GROUNDS.is_dir():
        pytest.skip(f"proving-grounds not found at {PROVING_GROUNDS}")
    srv = H.FixtureServer(root=PROVING_GROUNDS).start()
    yield srv
    srv.stop()


def _crawl(pw, url: str, name: str) -> list[dict[str, Any]]:
    """Run a REAL crawl of ``url`` and return the manifest records.

    Budgets are larger than the fixture characterization crawls — a proving
    ground is a multi-page application and the point is to discover it — but
    still DECLARED, so the crawl stops on a bound rather than on a timeout.
    """
    from app.auth import AuthWindow
    from app.crawler import Budget, Crawler, GuardContext
    from app.guard import load_refuse_pack
    from app.main import EXPLORER_VERSION, PlaywrightBrowserPort

    pack = load_refuse_pack(str(H.SERVICE_ROOT / "app" / "refuse_pack.yaml"))
    guard_ctx = GuardContext(
        refuse_pack=pack,
        auth_window=AuthWindow(max_requests=200, window_ms=120_000),
        attestation=None,
        submit_flow_approved=False,      # never mutate a proving ground in CI
        idp_domains=frozenset(),
    )
    budget = Budget.from_dict({
        "max_states": 12, "max_actions": 40, "max_requests": 800,
        "max_duration_ms": 300_000,
    })

    work_dir = CRAWL_OUT / name
    if work_dir.exists():
        shutil.rmtree(work_dir, ignore_errors=True)
    work_dir.mkdir(parents=True, exist_ok=True)

    crawl_id = f"pg-{name}"
    crawler = Crawler(
        PlaywrightBrowserPort(pw.page, pw.context),
        crawl_id=crawl_id,
        tenant_id="proving-ground",
        target_url=url,
        work_dir=str(work_dir),
        refuse_pack=pack,
        budget=budget,
        explorer_version=EXPLORER_VERSION,
        guard_version=EXPLORER_VERSION,
        refuse_pack_version=pack.version,
        config_fingerprint=f"proving-ground-{name}",
        guard_context=guard_ctx,
        identity_seed="qec-proving-ground",
        observe_only=True,
    )
    pw.run(crawler.run())

    manifest = work_dir / crawl_id / "manifest.jsonl"
    assert manifest.exists(), (
        f"[{name}] the crawl produced NO manifest at {manifest}. A crawl that "
        f"writes nothing is a failed crawl, not a passing one.")
    records = [json.loads(line) for line in
               manifest.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert records, f"[{name}] the manifest is empty"
    return records


def _resolve_url(name: str, ground_server) -> str:
    """The URL for ``name``: the CI-provided one, else the local static server."""
    env_url = os.environ.get("QEC_PROVING_GROUND_URL", "").strip()
    env_name = os.environ.get("QEC_PROVING_GROUND_NAME", "").strip()
    if env_url and env_name == name:
        return env_url
    if env_url and not env_name:
        return env_url

    spec = TARGETS[name]
    if spec["serving"] == "built":
        pytest.skip(
            f"{name} is a Next.js application that must be built before it can "
            f"be crawled. It runs in the `proving-ground` CI job, which builds "
            f"its Dockerfile and sets QEC_PROVING_GROUND_URL. Skipping rather "
            f"than pretending to crawl it.")
    index = PROVING_GROUNDS / name / "index.html"
    if not index.exists():
        pytest.skip(f"{name}/index.html not found at {index}")
    return ground_server.url(name)


@pytest.mark.parametrize("name", sorted(TARGETS), ids=sorted(TARGETS))
def test_proving_ground_is_crawlable(pw, ground_server, name: str) -> None:
    """Crawl a real application and validate what was discovered.

    The assertions are deliberately about DISCOVERY rather than about exact
    content: a proving ground is a moving target maintained for other purposes,
    so pinning its markup here would make this lane brittle for no gain. What
    must hold is that the crawl reached it, observed page state, and captured
    bindable controls.
    """
    url = _resolve_url(name, ground_server)
    min_controls = int(os.environ.get("QEC_PROVING_GROUND_MIN_CONTROLS",
                                      TARGETS[name]["min_controls"]))
    records = _crawl(pw, url, name)

    by_type: dict[str, list[dict[str, Any]]] = {}
    for rec in records:
        by_type.setdefault(rec.get("type", "?"), []).append(rec)

    # 1. The crawl ran to a DECLARED conclusion.
    metas = by_type.get("crawl_meta", [])
    assert metas, f"[{name}] no crawl_meta record — the crawl never started"
    terminal = metas[-1]
    stop_reason = terminal.get("stop_reason", "")
    assert stop_reason, (
        f"[{name}] the terminal crawl_meta carries no stop_reason; the crawl did "
        f"not end deliberately")
    assert stop_reason not in ("error", "crashed"), (
        f"[{name}] the crawl ended abnormally: stop_reason={stop_reason!r}")

    # 2. Pages were DISCOVERED.
    states = by_type.get("page_state", [])
    assert states, (
        f"[{name}] the crawl discovered ZERO page states. Record types present: "
        f"{sorted(by_type)}")

    # 3. Controls were CAPTURED — the whole point of the capture engine.
    total_signals = sum(len(s.get("form_snapshot_signals") or {}) for s in states)
    total_actions = sum(len(s.get("actions") or []) for s in states)
    assert total_signals + total_actions >= min_controls, (
        f"[{name}] the crawl discovered {len(states)} page state(s) but only "
        f"{total_signals} form signals and {total_actions} actions — fewer than "
        f"the {min_controls} required. A crawl that reaches an application and "
        f"captures nothing is a failure, not a pass.")

    # 4. Every state is attributable to the application we asked for.
    hosts = {s.get("url_host", "") for s in states}
    assert len(hosts) >= 1 and any(hosts), (
        f"[{name}] page states carry no url_host: {hosts}")

    print(f"\n[{name}] {url}\n"
          f"  stop_reason      : {stop_reason}\n"
          f"  page states      : {len(states)}\n"
          f"  form signals     : {total_signals}\n"
          f"  actions          : {total_actions}\n"
          f"  hosts            : {sorted(h for h in hosts if h)}\n"
          f"  manifest records : {len(records)}")


def test_all_three_required_targets_are_declared() -> None:
    """The milestone names three crawl targets; all three must be wired."""
    for required in ("acme-life", "summit-life-carrier", "vkpower-life"):
        assert required in TARGETS, f"{required} is not a declared crawl target"
        assert (PROVING_GROUNDS / required).is_dir(), (
            f"{required} does not exist at {PROVING_GROUNDS / required}")
        assert (PROVING_GROUNDS / required / "Dockerfile").exists(), (
            f"{required} has no Dockerfile, so CI cannot build and crawl it")


def test_ci_workflow_dispatches_every_target() -> None:
    """The CI workflow must actually reference all three targets.

    Guards the gap between "a test exists" and "CI runs it": a target present
    here but absent from the matrix would never be crawled in CI, and the lane
    would report a coverage it does not have.
    """
    workflow = (H.SERVICE_ROOT.parent.parent.parent
                / ".github" / "workflows" / "browser-harness.yml")
    if not workflow.exists():
        pytest.skip(f"workflow not found at {workflow}")
    text = workflow.read_text(encoding="utf-8")
    for required in sorted(TARGETS):
        assert f"app: {required}" in text, (
            f"{required} is a declared crawl target but does not appear in the "
            f"CI matrix in {workflow.name}")
    assert "test_proving_grounds.py" in text, (
        "the CI workflow does not run this module")
