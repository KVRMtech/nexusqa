"""SandboxRunner — end-to-end with in-memory repo + fake Legs client."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import pytest


# ── In-memory doubles ───────────────────────────────────────────


@dataclass
class _FakeRepo:
    invocations: dict[str, dict[str, Any]] = field(default_factory=dict)
    quota_used: int = 0
    mark_running_calls: list[str] = field(default_factory=list)
    mark_completed_calls: list[dict[str, Any]] = field(default_factory=list)

    async def open_invocation(
        self,
        *,
        tenant_id,
        kind,
        request,
        trigger_dispatch_id=None,
        trigger_user_id=None,
        trace_id=None,
        idempotency_key=None,
    ):
        # Idempotent replay path: if the key matches a completed row,
        # return the existing row so the caller short-circuits.
        if idempotency_key:
            for inv in self.invocations.values():
                if (
                    inv.get("idempotency_key") == idempotency_key
                    and inv.get("kind") == kind
                ):
                    return inv
        invocation_id = f"inv-{len(self.invocations)+1}"
        row = {
            "invocation_id": invocation_id,
            "tenant_id": tenant_id,
            "kind": kind,
            "trigger_dispatch_id": trigger_dispatch_id,
            "trigger_user_id": trigger_user_id,
            "trace_id": trace_id,
            "idempotency_key": idempotency_key,
            "request": request,
            "result": {},
            "status": "queued",
            "error": None,
            "latency_ms": None,
            "created_at": datetime.now(timezone.utc),
        }
        self.invocations[invocation_id] = row
        return row

    async def mark_running(self, *, tenant_id, invocation_id):  # noqa: ARG002
        self.invocations[invocation_id]["status"] = "running"
        self.mark_running_calls.append(invocation_id)

    async def mark_completed(
        self,
        *,
        tenant_id,
        invocation_id,
        status,
        result,
        error=None,
        latency_ms=None,
    ):  # noqa: ARG002
        row = self.invocations[invocation_id]
        row["status"] = status
        row["result"] = result
        row["error"] = error
        row["latency_ms"] = latency_ms
        self.mark_completed_calls.append(
            {
                "invocation_id": invocation_id,
                "status": status,
                "error": error,
            }
        )
        return row

    async def quota_count_since(self, *, tenant_id, kind, since, only_running_or_succeeded=True):  # noqa: ARG002
        return self.quota_used


class _FakeLegs:
    """Stub for ``_LegsClientProtocol``."""

    def __init__(
        self,
        *,
        response: Optional[dict[str, Any]] = None,
        raise_with: Optional[Exception] = None,
    ):
        self.response = response or {"ok": True, "run_id": "run-1"}
        self.raise_with = raise_with
        self.calls: list[dict[str, Any]] = []

    async def run_scenario(
        self,
        *,
        tenant_id,
        scenario_id,
        params,
        timeout_seconds,
        idempotency_key,
        trace_id,
    ):
        self.calls.append(
            {
                "tenant_id": tenant_id,
                "scenario_id": scenario_id,
                "params": params,
                "timeout_seconds": timeout_seconds,
                "idempotency_key": idempotency_key,
                "trace_id": trace_id,
            }
        )
        if self.raise_with:
            raise self.raise_with
        return self.response


# ── Tests ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_succeeds_records_invocation() -> None:
    from app.actions import SandboxRequest, SandboxRunner

    repo = _FakeRepo()
    legs = _FakeLegs(response={"ok": True, "run_id": "run-7"})
    runner = SandboxRunner(repo=repo, client=legs)  # type: ignore[arg-type]
    result = await runner.run(
        tenant_id="t1",
        request=SandboxRequest(scenario_id="ca_tobacco_quote", params={}),
    )
    assert result.status == "succeeded"
    assert result.legs_run_id == "run-7"
    assert repo.mark_running_calls == [result.invocation_id]
    assert repo.mark_completed_calls[0]["status"] == "succeeded"


@pytest.mark.asyncio
async def test_run_failure_records_error() -> None:
    from app.actions import SandboxClientError, SandboxRequest, SandboxRunner

    repo = _FakeRepo()
    legs = _FakeLegs(raise_with=SandboxClientError("legs down"))
    runner = SandboxRunner(repo=repo, client=legs)  # type: ignore[arg-type]
    result = await runner.run(
        tenant_id="t1",
        request=SandboxRequest(scenario_id="x", params={}),
    )
    assert result.status == "failed"
    assert "legs down" in (result.error or "")
    assert repo.mark_completed_calls[0]["status"] == "failed"


@pytest.mark.asyncio
async def test_run_legs_returns_not_ok_marks_failed() -> None:
    from app.actions import SandboxRequest, SandboxRunner

    repo = _FakeRepo()
    legs = _FakeLegs(response={"ok": False, "error": "scenario_invalid"})
    runner = SandboxRunner(repo=repo, client=legs)  # type: ignore[arg-type]
    result = await runner.run(
        tenant_id="t1",
        request=SandboxRequest(scenario_id="x", params={}),
    )
    assert result.status == "failed"
    assert result.error == "scenario_invalid"


@pytest.mark.asyncio
async def test_quota_exceeded_blocks_run() -> None:
    from app.actions import (
        SandboxQuotaExceeded,
        SandboxRequest,
        SandboxRunner,
        SandboxRunnerConfig,
    )

    repo = _FakeRepo()
    repo.quota_used = 100
    legs = _FakeLegs()
    runner = SandboxRunner(
        repo=repo,  # type: ignore[arg-type]
        client=legs,
        config=SandboxRunnerConfig(daily_quota_per_tenant=100),
    )
    with pytest.raises(SandboxQuotaExceeded) as exc:
        await runner.run(
            tenant_id="t1",
            request=SandboxRequest(scenario_id="x", params={}),
        )
    assert exc.value.used == 100
    assert exc.value.limit == 100
    # Legs is never called when quota blocks.
    assert legs.calls == []


@pytest.mark.asyncio
async def test_idempotent_replay_returns_existing_row() -> None:
    from app.actions import SandboxRequest, SandboxRunner

    repo = _FakeRepo()
    repo.invocations["inv-prior"] = {
        "invocation_id": "inv-prior",
        "tenant_id": "t1",
        "kind": "sandbox_run",
        "idempotency_key": "key-x",
        "request": {"scenario_id": "x", "params": {}},
        "result": {"ok": True, "run_id": "run-prior"},
        "status": "succeeded",
        "error": None,
        "latency_ms": 250,
    }
    legs = _FakeLegs()
    runner = SandboxRunner(repo=repo, client=legs)  # type: ignore[arg-type]
    result = await runner.run(
        tenant_id="t1",
        request=SandboxRequest(
            scenario_id="x", params={}, idempotency_key="key-x"
        ),
    )
    assert result.status == "succeeded"
    assert result.invocation_id == "inv-prior"
    # Legs not called on idempotent replay.
    assert legs.calls == []


@pytest.mark.asyncio
async def test_sandbox_request_validates_scenario_id() -> None:
    from app.actions import SandboxRequest

    with pytest.raises(Exception):
        SandboxRequest(scenario_id="", params={})
    with pytest.raises(Exception):
        SandboxRequest(scenario_id="x", params={}, timeout_seconds=0)
    with pytest.raises(Exception):
        SandboxRequest(scenario_id="x", params={}, timeout_seconds=10_000)
