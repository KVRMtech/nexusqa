"""API/network mining — add ``page_visits.network_calls`` (XHR/fetch evidence).

Carries ``[{method, url, has_query, status, resource_type, request_mime,
response_mime, response_bytes, timestamp_ms}]`` — the XHR/fetch calls the app
made during a visit, so the app's real API surface is grounded evidence (an
endpoint/method/status a DOM-only walker never sees).  Query strings are DROPPED
and paths PII-scrubbed at source (engine ``_network_calls``); DIAGNOSTICS-ONLY —
no compiler rung, the UNCHANGED factory chain does not read it.  ``nullable=False``
+ server default ``[]`` so every existing row stays valid; the ORM
(:class:`nexus_sdk.db.models.PageVisitRow`) mirrors it as ``JSON`` (same ORM-JSON /
DB-JSONB pairing as ``displayed_values`` / ``form_snapshot``).
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "039_page_visits_network_calls"
down_revision: Union[str, None] = "038_page_visits_displayed_values"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "page_visits",
        sa.Column(
            "network_calls", JSONB, nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )


def downgrade() -> None:
    op.drop_column("page_visits", "network_calls")
