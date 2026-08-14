"""Phase 7 (record at onboarding) — client_apps.login_recording.

THE COLUMN EXISTED ONLY AS AN AD-HOC SCRIPT. ``scripts/apply_login_recording.sql``
was applied by hand to the running database, so every environment built from that
script has the column while a FRESH, alembic-only bootstrap does not — and the
ORM declares it ``nullable=False``. A new environment would therefore come up
with a model that cannot be written: every app create would fail on a column the
migration chain never made.

This is that script, promoted to a numbered revision so the chain alone is
sufficient. Byte-identical semantics to the script (same type, same NOT NULL,
same ``{}`` default), and IDEMPOTENT via ``ADD COLUMN IF NOT EXISTS`` — the
databases that already ran the script are unaffected, which is what lets this
land without a coordinated migrate-and-deploy.

Holds a RECORDED login until the first crawl mints an artifact: recipes are
artifact-scoped and no artifact exists when an app is created, so a login
recorded on the Access page waits here and is materialised into tp_login_recipes
on crawl completion. Contains NO credential values — steps, slot NAMES and reuse
keys only; the SESSION from the same recording travels separately in the
encrypted creds_blob.

``client_apps`` carries RLS from qec_001; an added column inherits that policy.

Revision ID: qec_016
Revises: qec_015
Create Date: 2026-08-14
"""
from typing import Sequence, Union

from alembic import op

# revision identifiers
revision: str = "qec_016"
down_revision: Union[str, None] = "qec_015"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # IF NOT EXISTS (not op.add_column) so this is a no-op on every database that
    # already ran scripts/apply_login_recording.sql — including production.
    op.execute(
        "ALTER TABLE client_apps "
        "ADD COLUMN IF NOT EXISTS login_recording JSONB NOT NULL DEFAULT '{}'::jsonb"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE client_apps DROP COLUMN IF EXISTS login_recording")
