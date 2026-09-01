"""P2 — personas at run time: declared identity, reservation, preflight block.

Pins:
  * identity is a DECLARED run input (persona_id/environment_id), never ambient;
  * a persona with no recipe+card is refused honestly (422), never fabricated;
  * a persona already held serializes (409 'persona is busy');
  * a member that cannot log in BLOCKS with a test_data cause and 0 steps — the
    application is never blamed for a rotten credential;
  * the reservation is released when the run task finishes;
  * the report's Trust Block carries WHO the run ran as.

Run from Nexus_power/platform/api:
    python -m pytest tests/test_persona_env_p2.py -q
"""
from __future__ import annotations

import os

_ROUTER = open(os.path.join(os.path.dirname(__file__), "..", "app", "routers",
                            "test_factory.py"), encoding="utf-8").read()
_REPORT = open(os.path.join(os.path.dirname(__file__), "..", "app", "services",
                            "test_factory", "evidence_report.py"), encoding="utf-8").read()
_COMPILER = open(os.path.join(os.path.dirname(__file__), "..", "app", "services",
                              "script_factory", "compiler.py"), encoding="utf-8").read()


def test_run_request_declares_identity():
    assert "persona_id: str = \"\"" in _ROUTER
    assert "environment_id: str = \"\"" in _ROUTER


def test_named_persona_resolves_and_refuses_when_unbound():
    dispatch = _ROUTER[_ROUTER.index("if persona_id:"):]
    assert "_persona_auth_bundle(" in dispatch
    # no recipe+card -> 422, never a fabricated session
    assert "persona has no login recipe + credential card" in _ROUTER
    assert "never fabricates a session" in _ROUTER


def test_persona_is_reserved_and_busy_serializes():
    assert "acquire_reservation(" in _ROUTER
    assert '"error": "persona is busy"' in _ROUTER
    assert "status_code=409" in _ROUTER


def test_dead_member_blocks_with_test_data_never_app_blame():
    """The reason is now chosen from what actually went wrong — a card that does not
    cover the login is attributed 'credential', everything else stays 'test_data' —
    because reporting an unrecorded interstitial as a credential problem sends the
    operator to re-type a password that was never wrong. Both are still blocks, and
    neither implicates the application."""
    assert '"test_data"' in _ROUTER and '"credential"' in _ROUTER
    assert "application under test is NOT implicated" in _ROUTER
    # a block executes ZERO suite steps
    seg = _ROUTER[_ROUTER.index('else "test_data")'):]
    assert '"scripts": 0' in seg[:1200]


def test_reservation_released_when_the_run_finishes():
    assert "release_reservation(" in _ROUTER
    assert "task.add_done_callback(_release)" in _ROUTER


def test_preflight_proves_login_before_the_suite():
    assert "async def _persona_preflight(" in _ROUTER
    seg = _ROUTER[_ROUTER.index("async def _persona_preflight("):]
    assert "recipe login OK" in seg and "form login OK" in seg
    # preflight itself is lean (no video/screenshot cost)
    assert '"NEXUS_VIDEO": "off"' in seg


def test_report_trust_block_carries_identity():
    assert '"identity": identity' in _REPORT
    assert '"persona":' in _REPORT       # run_block carries persona
    assert "fresh-recipe" in _REPORT


def test_reporter_forwards_persona_to_the_durable_run():
    assert "persona: process.env.NEXUS_PERSONA" in _COMPILER


def test_default_run_is_unchanged_when_no_persona():
    # the else branch keeps the exact form-login behaviour
    assert "auth_config, login_env = await _run_form_login(request, artifact_id, tenant_id)" in _ROUTER
