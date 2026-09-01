"""First-run bootstrap: a freshly-crawled artifact has substrate but no compiled
suite, so its RTM is empty.  The cycle must GENERATE the suite itself, then
select + run the fresh cases — so ``crawl → cycle`` is one click with no manual
``/generate`` trigger.  Regression guard for the bootstrap gap where the empty
RTM short-circuited to an empty ``done`` before the generate phase ever fired.

Drives the DB-free :func:`execute_cycle` with a fake CycleClient whose RTM is
empty until ``generate`` is called; no network, no database.
"""
from __future__ import annotations

import asyncio

from app.controlplane.cycle.driver import (
    AppConfig,
    CycleBudget,
    execute_cycle,
    CYCLE_STATE_DONE,
)


class _EmptyUntilGeneratedClient:
    """RTM is empty until ``generate`` runs, then exposes ONE mappable case.

    ``generate_calls`` counts compile invocations so the test can prove the
    bootstrap generates EXACTLY once (step 3 must not regenerate)."""

    def __init__(self) -> None:
        self.generate_calls = 0
        self.run_calls = 0

    async def fetch_journey_graph(self, *, tenant_id, artifact_id):
        return {"pages": []}

    async def get_rtm(self, *, tenant_id, artifact_id):
        if self.generate_calls == 0:
            return {"artifact_id": artifact_id, "tests": []}
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
        self.generate_calls += 1
        return {"success": True, "generated": 1, "demonstrated": 1}

    async def run_playwright(self, *, tenant_id, artifact_id, test_ids, base_url, env_context=None):
        self.run_calls += 1
        self.last_env_context = env_context
        return {"run_id": "run-1", "status": "queued", "scripts": list(test_ids)}

    async def poll_run(self, *, tenant_id, artifact_id, run_id):
        return {"run_id": run_id, "status": "passed"}

    async def get_run(self, *, tenant_id, artifact_id, run_id):
        return {
            "run_header": {"run_id": run_id, "duration_ms": 1200},
            "scenarios": [{"scenario_id": "T1", "status": "passed"}],
        }

    async def auto_heal(self, *, tenant_id, artifact_id, test_ids, base_url, env_context=None):
        return {"run_id": "heal-1", "status": "passed"}

    async def verify(self, *, tenant_id, artifact_id, test_id, base_url):
        return {"decision": "CERTIFIED", "certification_level": "CERTIFIED-EVIDENCED"}

    async def triage(self, *, tenant_id, artifact_id):
        return {"classes": {}}


def _app() -> AppConfig:
    return AppConfig(
        app_id="a1", tenant_id="t1", base_url="https://uat.example",
        canonical_host="example", max_rps=1.0, latest_artifact_id="art1",
        baseline_page_fingerprints={}, repo_binding={}, budgets={}, fences={},
    )


def test_empty_rtm_autogenerates_then_runs_the_fresh_cases():
    # mode=auto (the realistic first-cycle path): no baseline, empty RTM.
    client = _EmptyUntilGeneratedClient()
    outcome = asyncio.run(execute_cycle(
        cycle_id="c1", mode="auto", trigger="manual",
        app=_app(), client=client,
        budget=CycleBudget({}, hard_wallclock_ceiling_s=3600.0),
    ))
    # The cycle reached a real DONE having RUN the freshly-generated case…
    assert outcome.state == CYCLE_STATE_DONE
    assert outcome.result.get("selected_count") == 1
    assert (outcome.result.get("run") or {}).get("run_id") == "run-1"
    assert client.run_calls == 1
    # …by generating the suite itself, EXACTLY once (no manual trigger, no
    # double-compile in step 3).
    assert client.generate_calls == 1


def test_empty_rtm_that_generates_nothing_is_an_honest_done_not_a_silent_green():
    # A bootstrap that compiles zero grounded cases must DONE honestly with a
    # "no grounded cases" note — never a run, never a silent green.
    class _NeverGenerates(_EmptyUntilGeneratedClient):
        async def get_rtm(self, *, tenant_id, artifact_id):
            return {"artifact_id": artifact_id, "tests": []}  # always empty

        async def generate(self, *, tenant_id, artifact_id, answer_key=None):
            self.generate_calls += 1
            return {"success": True, "generated": 0, "demonstrated": 0}

    client = _NeverGenerates()
    outcome = asyncio.run(execute_cycle(
        cycle_id="c2", mode="auto", trigger="manual",
        app=_app(), client=client,
        budget=CycleBudget({}, hard_wallclock_ceiling_s=3600.0),
    ))
    assert outcome.state == CYCLE_STATE_DONE
    assert outcome.result.get("selected_count") == 0
    assert client.run_calls == 0  # nothing ran
    assert client.generate_calls == 1  # it TRIED to bootstrap
    assert "no grounded cases" in (outcome.result.get("note") or "")
