"""Merge migration — joins the parallel branches that grew out of
``021_echo_mvp``.

Two branches existed:

    021_echo_mvp
    ├── 022_e2e_scenario_state (P3 — Test Studio Lifecycle)
    │     └── 023_test_runs   (P6 — Execution Feedback Loop)
    └── 022_knowledge_cards
          └── 023_product_atlas
                └── 024_org_awareness
                      └── 025_action_layer
                            └── 026_marketplace_and_sovereign

Each branch evolved independently of the other.  This merge migration
joins them into a single head so ``alembic upgrade head`` works again.
There are no schema changes here — this revision is *only* a merge
marker.

Revision ID: 027_merge_e2e_and_knowledge_heads
Revises: 023_test_runs, 026_marketplace_and_sovereign
Create Date: 2026-05-13
"""
from typing import Sequence, Union


revision: str = "027_merge_heads"
# Two parents = alembic recognises this as a merge revision
down_revision: Union[str, Sequence[str], None] = (
    "023_test_runs",
    "026_marketplace_and_sovereign",
)
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Merge revision — no schema changes."""
    pass


def downgrade() -> None:
    """Merge revision — no schema changes."""
    pass
