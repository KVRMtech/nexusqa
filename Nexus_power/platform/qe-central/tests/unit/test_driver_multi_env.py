"""Multi-env (crawl-once/run-many) — the cycle driver binds an app's designated
run environment on the RUN step, and REFUSES to auto-heal an env-bound failure
against the default env (a green-wash).

Drives the DB-free :func:`execute_cycle` with a fake CycleClient (no network, no
DB), plus a pure :meth:`AppConfig.from_row` check for the ``schedule.run_environment``
selector.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from app.controlplane.cycle.driver import (
    AppConfig,
    CycleBudget,
    execute_cycle,
    CYCLE_STATE_DONE,
)


class _FailingClient:
    """One mappable case T1 that FAILS the run (no per-scenario timeline ⇒ the
    conservative fail-safe treats the selected case as failed). Records the
    env_context handed to the run and whether auto_heal was invoked."""

    def __init__(self) -> None:
        self.run_calls = 0
        self.auto_heal_calls = 0
        self.last_env_context = "UNSET"
        self.heal_env_context = "UNSET"
        self.verify_base_urls: list[str] = []

    async def fetch_journey_graph(self, *, tenant_id, artifact_id):
        return {"pages": []}

    async def get_rtm(self, *, tenant_id, artifact_id):
        return {
            "artifact_id": artifact_id,
            "tests": [{
                "test_id": "T1",
                "rows": [{
                    "emitted_assertions": [{"code": "toHaveURL(/\\/home/)"}],
                    "observed_label": "Home",
                }],
            }],
        }

    async def generate(self, *, tenant_id, artifact_id, answer_key=None):
        return {"success": True, "generated": 1, "demonstrated": 1}

    async def run_playwright(self, *, tenant_id, artifact_id, test_ids, base_url, env_context=None):
        self.run_calls += 1
        self.last_env_context = env_context
        return {"run_id": "run-1", "status": "queued", "scripts": list(test_ids)}

    async def poll_run(self, *, tenant_id, artifact_id, run_id):
        return {"run_id": run_id, "status": "failed"}  # non-green ⇒ failure

    async def get_run(self, *, tenant_id, artifact_id, run_id):
        # No per-scenario timeline ⇒ _failed_scenarios falls back to run status.
        return {"run_header": {"run_id": run_id, "duration_ms": 1000}}

    async def auto_heal(self, *, tenant_id, artifact_id, test_ids, base_url, env_context=None):
        self.auto_heal_calls += 1
        self.heal_env_context = env_context
        return {"run_id": "heal-1", "status": "passed"}

    async def verify(self, *, tenant_id, artifact_id, test_id, base_url):
        self.verify_base_urls.append(base_url)
        return {"decision": "CERTIFIED", "certification_level": "CERTIFIED-EVIDENCED"}

    async def triage(self, *, tenant_id, artifact_id):
        return {"classes": {}}


def _app() -> AppConfig:
    return AppConfig(
        app_id="a1", tenant_id="t1", base_url="https://default.example",
        canonical_host="example", max_rps=1.0, latest_artifact_id="art1",
        baseline_page_fingerprints={}, repo_binding={}, budgets={}, fences={},
    )


_UAT_CTX = {"name": "uat", "base_url": "https://uat.example",
            "cookies": [{"name": "gloo", "value": "uat"}]}


def test_env_context_flows_to_the_run_step():
    client = _FailingClient()
    asyncio.run(execute_cycle(
        cycle_id="c1", mode="full", trigger="manual",
        app=_app(), client=client,
        budget=CycleBudget({}, hard_wallclock_ceiling_s=3600.0),
        env_context=_UAT_CTX,
    ))
    assert client.run_calls == 1
    # The SAME env_context reached the runner (crawl-once/run-many rebind).
    assert client.last_env_context == _UAT_CTX


def test_env_bound_failure_heals_against_the_bound_env():
    client = _FailingClient()
    outcome = asyncio.run(execute_cycle(
        cycle_id="c2", mode="full", trigger="manual",
        app=_app(), client=client,
        budget=CycleBudget({}, hard_wallclock_ceiling_s=3600.0),
        env_context=_UAT_CTX,
    ))
    assert outcome.state == CYCLE_STATE_DONE
    # HEAL now RUNS for an env-bound cycle, rebinding the re-runs to the SAME env the
    # graded run used — so a fix is proven against uat, never the default env.
    assert client.auto_heal_calls == 1
    assert client.heal_env_context == _UAT_CTX
    assert not (outcome.result.get("heal") or {}).get("skipped")
    # VERIFY's live readiness probe stays suppressed (base_url="") — making it env-aware
    # for a cookie-routed env needs a preflight change; an advisory signal against the
    # DEFAULT env would be a wrong-env signal, so we emit none rather than a wrong one.
    assert client.verify_base_urls and all(b == "" for b in client.verify_base_urls)


def test_single_env_cycle_still_auto_heals():
    # env_context=None (today's default) ⇒ auto-heal proceeds exactly as before.
    client = _FailingClient()
    outcome = asyncio.run(execute_cycle(
        cycle_id="c3", mode="full", trigger="manual",
        app=_app(), client=client,
        budget=CycleBudget({}, hard_wallclock_ceiling_s=3600.0),
    ))
    assert client.last_env_context is None
    assert client.auto_heal_calls == 1                     # heal ran (not skipped)
    assert not (outcome.result.get("heal") or {}).get("skipped")
    # Single-env: verify DOES probe the app base_url (today's behavior, unchanged).
    assert client.verify_base_urls and all(b == "https://default.example" for b in client.verify_base_urls)


def test_from_row_reads_run_environment_from_schedule():
    row = SimpleNamespace(
        app_id="a1", tenant_id="t1", base_url="https://x.example",
        canonical_host="x.example", fences={}, latest_artifact_id="art1",
        repo_binding={}, budgets={}, answer_key={},
        schedule={"cadence": "on_push", "run_environment": "uat"},
    )
    cfg = AppConfig.from_row(row, baseline_page_fingerprints={})
    assert cfg.run_environment == "uat"

    row_none = SimpleNamespace(
        app_id="a1", tenant_id="t1", base_url="https://x.example",
        canonical_host="x.example", fences={}, latest_artifact_id="art1",
        repo_binding={}, budgets={}, answer_key={}, schedule={"cadence": "on_push"},
    )
    assert AppConfig.from_row(row_none, baseline_page_fingerprints={}).run_environment == ""
