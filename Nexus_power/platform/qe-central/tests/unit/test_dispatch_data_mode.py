"""WHO ANSWERS A QUESTION THE CLIENT NEVER ANSWERED FOR US.

``data_mode='user'`` leaves every semantic choice — a radio group, a select —
unanswered, files it as residue, and the product then asks a human for N values
and requires the crawl to be RUN AGAIN. qe-central sent ``'user'`` for every app,
because the app row almost never carries the key.

On an attested test environment that default is backwards. A missing value is the
most ordinary thing a crawl meets; turning it into a stop-and-ask-and-restart
cycle makes the ordinary case the expensive one, which is not what an agentic
platform should do with a form it can honestly answer.

The policy pinned here:

  * an OPERATOR'S EXPLICIT CHOICE is never overridden, in either direction;
  * an attested non-prod environment answers by default;
  * an environment nobody attested is untouched — fail-closed, as before.

Honesty is preserved by PROVENANCE, not by refusing to answer: the explorer
stamps every generated value as ``synthesized`` in the field ledger (pinned in
``qe-explorer/tests/test_agentic_fill.py``), so a journey completed on invented
data stays a clearly-labelled one.
"""
from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest

from app.db import utc_now
from app.security import prod_guard


def _app(env_kind: str = "", schedule: dict | None = None) -> SimpleNamespace:
    att: dict = {}
    if env_kind:
        att = {
            "env_kind": env_kind,
            "attested_by": "sre@client.example",
            "expires_at": (utc_now() + timedelta(hours=24)).isoformat(),
        }
    return SimpleNamespace(
        app_id="app-1", env_attestation=att, fences={},
        schedule=dict(schedule or {}), status="active",
    )


def _resolved_data_mode(row) -> str:
    """The dispatcher's rule, exercised exactly as ``start_exploration`` runs it."""
    traversal = prod_guard.traversal_posture(row)
    declared = str((row.schedule or {}).get("data_mode") or "").strip().lower()
    return declared or ("agent" if traversal == prod_guard.TRAVERSAL_FULL else "user")


# ── the operator's explicit choice is never overridden ──────────────────────

def test_an_operator_who_chose_user_keeps_user_on_a_test_environment():
    """THE ONE THAT MUST NOT REGRESS. Some clients want to decide their own
    business paths — a radio group is a semantic choice, and an operator who has
    said so must not be silently upgraded into letting an agent choose."""
    row = _app("disposable", schedule={"data_mode": "user"})
    assert prod_guard.traversal_posture(row) == prod_guard.TRAVERSAL_FULL
    assert _resolved_data_mode(row) == "user"


def test_an_operator_who_chose_agent_keeps_agent_without_an_attestation():
    row = _app(schedule={"data_mode": "agent"})
    assert prod_guard.traversal_posture(row) == prod_guard.TRAVERSAL_PROBE
    assert _resolved_data_mode(row) == "agent"


@pytest.mark.parametrize("declared", ["USER", "  agent  ", "Agent"])
def test_an_explicit_choice_is_normalised_not_discarded(declared):
    row = _app("disposable", schedule={"data_mode": declared})
    assert _resolved_data_mode(row) == declared.strip().lower()


# ── the default follows the attestation ─────────────────────────────────────

@pytest.mark.parametrize("kind", sorted(prod_guard.NON_PROD_ENV_KINDS))
def test_an_attested_test_environment_answers_by_default(kind):
    assert _resolved_data_mode(_app(kind)) == "agent"


def test_an_unattested_app_is_unchanged():
    """Fail-closed: no signed statement about this environment means the previous
    behaviour, exactly."""
    assert _resolved_data_mode(_app()) == "user"


def test_an_expired_attestation_is_unchanged():
    row = _app("disposable")
    row.env_attestation["expires_at"] = (utc_now() - timedelta(hours=1)).isoformat()
    assert _resolved_data_mode(row) == "user"


def test_production_is_never_switched_to_agent_fill():
    """Production is catalogued, never driven. Nothing about this default may
    reach an environment whose posture is observe-only."""
    row = _app("prod")
    assert prod_guard.traversal_posture(row) == prod_guard.TRAVERSAL_OBSERVE
    assert _resolved_data_mode(row) == "user"


def test_the_dispatcher_uses_exactly_this_rule():
    """Tripwire: this file models the dispatcher's decision, so it is only worth
    anything while the dispatcher still makes it here."""
    import inspect

    from app.routers import explorations

    src = inspect.getsource(explorations)
    assert "declared_data_mode" in src
    assert 'if traversal == prod_guard.TRAVERSAL_FULL else "user"' in src
    assert "data_mode=data_mode," in src
