# Canonical Workflow Contract (Phase 1)

This document is the authoritative reference for the workflow layer
added in Phase 1. Engines, orchestrators, and operator tooling all
treat it as a contract.

## Why a workflow layer

Phase 0 engines accepted a single job, ran a one-shot internal
pipeline, and emitted a single result. That worked at low throughput
but failed against the Phase 1 acceptance criteria:

- Workers that crash mid-job leak state (no checkpoint outside the pod).
- Hot/cold paths share a queue (a slow GPU job head-of-line blocks fast
  CPU work).
- No deadline enforcement; a worker that hangs holds the slot until
  Redis idle-timeout fires.
- A `BackgroundTasks` failure is lost; no DLQ.

Phase 1 introduces an **orchestration plane** that owns the workflow
end-to-end. Engines become single-step workers.

## Data shapes

### `WorkflowPlan`

The input handed to `POST /api/v1/canonical-workflows`. Either a canonical plan
(via `kind`) or a `custom_steps` list. Fields:

| field            | type        | notes                                 |
|------------------|-------------|---------------------------------------|
| kind             | string      | `audio.canonicalize` / etc.           |
| tenant_id        | string      | required; RLS-scoped                  |
| session_id       | string      | logical correlation id                |
| profile          | string      | `fast` / `standard` / `deep`          |
| initial_state    | dict        | first checkpoint                      |
| metadata         | dict        | request_id / trace_id / correlation   |
| custom_steps     | list[Step]? | overrides the canonical plan          |
| deadline_seconds | int?        | overrides the canonical SLO           |

Each step (`StepPlan`) carries `{name, engine, kind, deadline_seconds, max_attempts, params}`.

### `JobEnvelope`

What the orchestrator hands a worker for one step. Wire format
(JSON-serialisable) is defined in
[`nexus_sdk.workflows.models.JobEnvelope`](../sdk/nexus-sdk/nexus_sdk/workflows/models.py).
The worker reads `checkpoint` as input and `params` as step-local
configuration; it returns a `StepResult` with the new `checkpoint`.

### `StepResult`

The worker's reply. Fields:

| field         | type    | notes                                                   |
|---------------|---------|---------------------------------------------------------|
| success       | bool    | `true` advances to next step, `false` retries           |
| checkpoint    | dict    | on success — replaces the workflow's `checkpoint`       |
| error         | string  | on failure                                              |
| error_context | dict    | extra fields preserved into the DLQ payload             |
| duration_ms   | int     | wall-clock duration for SLO observability               |
| fatal         | bool    | true = skip retries, quarantine immediately             |

## Lifecycle

```
            ┌────────────┐
            │  PENDING   │   create() persists plan + initial state
            └─────┬──────┘
                  │ next_envelope() (orchestrator)
                  ▼
            ┌────────────┐
            │  RUNNING   │   worker holds the envelope
            └─────┬──────┘
                  │ record_result()
        ┌─────────┴─────────┐
        │                   │
   success                 failure
        │                   │
        │            attempt < max?
        │            ┌─yes──┐  no
        ▼            ▼      ▼
   advance step   PENDING  QUARANTINED
        │           │
        ▼           ▼
  more steps?    dispatch_next re-enqueues
        │
   ┌────┴───┐
   yes      no
   │        │
   ▼        ▼
RUNNING  COMPLETED
```

Operator-driven transitions:
- `cancel(reason)` → CANCELLED
- DLQ replay endpoint → PENDING from QUARANTINED

## Queue topology

The dispatcher writes envelopes to `nexus:queue:<engine>.<kind>` (e.g.
`nexus:queue:eyes.gpu`). Each lane has:

- A consumer group `nexus:workers:<engine>.<kind>` per engine.
- A DLQ stream `nexus:queue:<engine>.<kind>:dlq`.

KEDA's `ScaledObject` per engine watches the lane stream and scales
the matching Deployment based on `pendingEntriesCount`.

## Worker contract

A worker pod:

1. Connects to a `JobQueue` for its `<engine>.<kind>` lane.
2. Registers handlers via `WorkflowWorker.register(step_name, handler)`.
3. Calls `worker.run()` to consume.

The framework guarantees:
- Each envelope hands you `checkpoint` and `params`.
- A heartbeat task POSTs `/heartbeat` every `envelope.heartbeat_seconds`.
- The handler is run inside `asyncio.wait_for(seconds_remaining)`.
- If the handler raises or returns success=False, the worker reports
  failure and lets the orchestrator decide retry vs quarantine.

A handler **must not**:
- Mutate workflow state directly. The orchestrator is the only writer.
- Persist a "checkpoint" anywhere other than the `StepResult.checkpoint`
  field. The DB transaction binding result + state is what guarantees
  exactly-once-per-checkpoint semantics.
- Hold the envelope longer than `seconds_remaining`. The wait_for
  wrapper will cancel the handler; if your work isn't cancellable, you
  need to subdivide the step.

## Orchestrator sweeper

[`WorkflowSweeper`](../sdk/nexus-sdk/nexus_sdk/workflows/sweeper.py) runs
three concurrent loops:

| Loop      | Tick (default) | Action                                     |
|-----------|----------------|--------------------------------------------|
| deadline  | 10 s           | quarantine workflows past `deadline_at`    |
| orphan    | 15 s           | re-dispatch workflows with stale heartbeat |
| dlq_audit | 60 s           | emit gauge `workflow_dlq_depth` per lane   |

The orphan threshold (`orphan_threshold_seconds`, default 120) is the
longest gap a worker may go without heartbeating before the
orchestrator assumes the worker died. Pick higher than your slowest
single step's max duration.

## DLQ and quarantine

Two failure categories, both surfaced through
[`build_dlq_router`](../sdk/nexus-sdk/nexus_sdk/workflows/dlq.py):

1. **Quarantined workflows** — workflow-level state == `quarantined`.
   Replay via `POST /api/v1/canonical-admin/dlq/workflows/{id}/replay`. The
   replay resets `attempt` and resumes from the workflow's current
   `step_index` using the existing checkpoint.

2. **Queue DLQ entries** — envelopes the queue layer gave up on after
   `max_retries`. Inspect via `GET /api/v1/canonical-admin/dlq/queues`, replay
   via `POST /api/v1/canonical-admin/dlq/queues/{lane}/replay/{msg_id}`, purge
   via `DELETE /api/v1/canonical-admin/dlq/queues/{lane}/{msg_id}`.

All three endpoints require the `platform-admin` role.

## Acceptance proof

A Phase 1 release ships only if:

- `pytest tests/regression -m "not soak"` is green for every PR.
- The nightly soak gate in
  [`.github/workflows/canonical-regression.yml`](../.github/workflows/canonical-regression.yml)
  reports `sustained_per_hour ≥ 100`, `p95 ≤ 900 s`, `completed_ratio ≥ 0.99`.
- `tests/regression/test_recovery.py` is green with
  `NEXUS_REGRESSION_ALLOW_KILL=1` — proves no stuck workflow survives a
  worker restart.

The Argo CD pre-sync hook for the production Application reads the
soak report from the previous 24 h; promotion fails if the gate isn't
green.

## Migration notes for engines

Engines retain their existing one-shot endpoints during the migration.
A new `workflow_worker` entry point is added in parallel:

```python
# engines/<engine>/main.py
from nexus_sdk.workflows import WorkflowWorker, WorkerConfig, StepKind

async def on_startup(self):
    self._workflow_worker = WorkflowWorker(
        WorkerConfig(
            engine_name="eyes",
            kind=StepKind.GPU,
            orchestrator_url=os.environ["NEXUS_ORCHESTRATOR_URL"],
            auth_token=os.environ.get("NEXUS_WORKER_TOKEN", ""),
        ),
        queue=self._build_lane_queue("eyes.gpu"),
    )
    self._workflow_worker.register("eyes.analyze_scenes", self._step_analyze_scenes)
    self._workflow_worker.register("eyes.extract_frames", self._step_extract_frames)
    asyncio.create_task(self._workflow_worker.run())
```

Existing legacy endpoints are removed when their callers (gateway,
client) migrate to `POST /api/v1/canonical-workflows`. A removal is a single PR
that drops the old route — the workflow is the source of truth.
