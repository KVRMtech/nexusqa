"""M3.3 / T-FL-02 — the explorer WORKER REGISTRY (fleet infrastructure).

WHAT THIS REPLACES
==================
Scheduling used ``QEC_EXPLORER_POOL``: a STATIC JSON array of worker URLs read
from the environment. A static list cannot express anything the scheduler needs
once concurrency is real — it does not know whether a worker is alive, how much
capacity it has, how much of that capacity is already in use, or whether it is
eligible for a given tenant. Dispatch therefore walked the list in a fixed order
and discovered a worker was busy only by being refused, so worker[0] absorbed
every attempt and the fleet had no notion of "least loaded".

WHAT THIS TABLE IS
==================
``explorer_workers`` is a live registry: each explorer registers itself, declares
its capacity, and heartbeats. The scheduler reads utilisation and liveness from
here and prefers the LEAST-LOADED ELIGIBLE worker.

WHY IT HAS NO ``tenant_id`` COLUMN (deliberate, and reviewable)
==============================================================
This is FLEET INFRASTRUCTURE, not tenant data. A worker is a process with a URL
and a capacity; it belongs to the deployment, not to a customer. It holds no
tenant business data of any kind.

Giving it a ``tenant_id`` column would be actively harmful. Every ``tenant_id``
table in this schema carries FORCE ROW LEVEL SECURITY keyed on the
``nexus.current_tenant_id`` GUC (correctly — see the standing coverage gate in
``tests/contract/test_rls_coverage_complete.py``). The scheduler's read is
FLEET-WIDE by nature: it must compare every worker to pick the least loaded. A
fleet-wide read of an RLS table with no GUC set returns ZERO rows — which is
precisely the T-FL-05 defect this same milestone fixes in ``_scan_fleet``. An
RLS'd worker table would reintroduce that failure in the dispatch path, where it
would present as "no worker available" on a completely healthy fleet.

``tenant_affinity`` is therefore NOT a tenant-ownership column and is
deliberately not named ``tenant_id``. It is a CAPABILITY DECLARATION — an
operator pinning a dedicated worker to one tenant (isolation-by-hardware for a
customer who requires it). Empty means "eligible for any tenant", which is the
default and today's behaviour. The scheduler treats it as a FILTER on
eligibility; tenant isolation itself is enforced where it belongs and already is:
the per-worker egress fence (T-FL-04), the worker's own owner-scoped reservation
(M0.5 T-SEC-07), and RLS on every table that really does hold tenant rows.

This table is added to the coverage gate's ``_NO_TENANT_COLUMN`` allowlist with
that reason, which is the sanctioned, reviewable way to declare a tenant-free
table — not an exemption from isolation.

PURELY ADDITIVE — no existing table is touched.

Revision ID: qec_022
Revises: qec_021
Create Date: 2026-08-19
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers
revision: str = "qec_022"
down_revision: Union[str, None] = "qec_021"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "explorer_workers",
        # Identity. Stable across restarts (derived from the pod name in K8s),
        # so a restarted pod re-registers as ITSELF rather than leaking a second
        # phantom worker into the capacity total.
        sa.Column("worker_id", sa.String(64), nullable=False),
        sa.Column("url", sa.String(500), nullable=False),
        # The worker's OWN squid allowlist file. Per-worker egress isolation
        # (T-FL-04): a shared file would be raced by concurrent crawls and the
        # fence would stop fencing. Carried in the registry so the scheduler
        # always fences the file belonging to the worker it actually chose.
        sa.Column("allowlist_path", sa.String(500), nullable=False,
                  server_default=""),
        # Capacity + live utilisation. in_flight is maintained by the reserve /
        # release path and is the number the least-loaded choice sorts on.
        sa.Column("capacity", sa.Integer, nullable=False, server_default="1"),
        sa.Column("in_flight", sa.Integer, nullable=False, server_default="0"),
        # 'active' | 'draining' (finish what you have, accept nothing new) |
        # 'disabled' (operator-parked).
        sa.Column("status", sa.String(20), nullable=False,
                  server_default="active"),
        # NOT a tenant-ownership column — see the module docstring. Empty ⇒
        # eligible for every tenant (the default, and today's behaviour).
        sa.Column("tenant_affinity", sa.String(64), nullable=False,
                  server_default=""),
        # Liveness. A worker that stops heartbeating stops receiving work; its
        # capacity is excluded from the fleet total so the queue does not hold
        # crawls for a worker that will never come back.
        sa.Column("last_heartbeat_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.text("now()")),
        sa.Column("registered_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        # Free-form: image tag, node, zone, browser version. Diagnostics only —
        # never consulted by a scheduling decision.
        sa.Column("meta", JSONB, nullable=False, server_default="{}"),
        sa.PrimaryKeyConstraint("worker_id"),
        # A worker can never report more work in flight than it can hold, and
        # capacity is meaningless at zero. Enforced by the DATABASE so a buggy
        # heartbeat cannot inflate fleet capacity and make the queue drain into
        # a worker that cannot take the work.
        sa.CheckConstraint("capacity > 0", name="ck_explorer_workers_capacity"),
        sa.CheckConstraint("in_flight >= 0 AND in_flight <= capacity",
                           name="ck_explorer_workers_in_flight"),
    )
    # The scheduler's hot path: "eligible, alive, least loaded". Ordered to match
    # the query — filter on status, then heartbeat recency, then sort on load.
    op.create_index(
        "ix_explorer_workers_scheduling",
        "explorer_workers",
        ["status", "last_heartbeat_at", "in_flight"],
    )
    # Affinity lookup for a tenant with dedicated workers.
    op.create_index(
        "ix_explorer_workers_affinity", "explorer_workers", ["tenant_affinity"],
    )


def downgrade() -> None:
    op.drop_index("ix_explorer_workers_affinity", table_name="explorer_workers")
    op.drop_index("ix_explorer_workers_scheduling", table_name="explorer_workers")
    op.drop_table("explorer_workers")
