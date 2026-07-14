"""P1 — run-time login skip. When a captured session (storageState / auth profile)
is injected, the recorded login REPLAY (whose password was never persisted) is both
redundant and doomed on the login page, so the generator's login prologue is dropped
and the single entry navigation is re-pointed at the first post-login URL.

Invariants pinned here:
  * WITHOUT an auth profile → byte-identical (the strip never runs).
  * WITH an auth profile but a NON-login flow → unchanged (conservative no-op).
  * WITH an auth profile AND a login prologue → prologue removed, entry re-pointed.
  * The env-routing-cookie path (which also fills storage_state) must NOT trigger
    the strip — only a genuine auth profile does.
"""
from __future__ import annotations

import os
import sys

_API_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_SDK_DIR = os.path.abspath(os.path.join(_API_DIR, "..", "..", "sdk", "nexus-sdk"))
for _p in (_API_DIR, _SDK_DIR):
    if os.path.isdir(_p) and _p not in sys.path:
        sys.path.insert(0, _p)

from nexus_sdk.models import ProductionTestCase, ProductionTestStep  # noqa: E402

from app.routers.test_factory import (
    _configured_files,
    _is_credential_fill,
    _strip_login_steps,
)


def _step(n, action, **observed):
    return ProductionTestStep(step_number=n, action=action, observed=dict(observed))


def _saucedemo_case():
    # The real shape captured from saucedemo (entry, username fill, sign-in nav,
    # post-login nav-assertion, then a post-login interaction).
    return ProductionTestCase(
        test_id="1d854b67", name="login flow",
        steps=[
            _step(1, "Open https://www.saucedemo.com/", verb="navigate",
                  url="https://www.saucedemo.com/"),
            _step(2, "Enter 'visual_user' in the 'Username' field", verb="type",
                  label="Username", value="visual_user"),
            _step(3, "Proceed to the inventory.html page", verb="navigate", url=""),
            _step(4, "Verify the application navigated to inventory.html", verb="navigate",
                  url="/inventory.html"),
            _step(5, "Click 'LinkedIn'", verb="click", label="LinkedIn"),
        ],
    )


def test_login_prologue_stripped_and_entry_repointed():
    out = _strip_login_steps(_saucedemo_case())
    steps = out.model_dump()["steps"]
    # entry(re-pointed) + post-login nav-assert + LinkedIn click == 3 steps
    assert len(steps) == 3
    assert steps[0]["observed"]["url"] == "/inventory.html"
    assert steps[0]["action"] == "Open /inventory.html"
    assert steps[1]["observed"]["url"] == "/inventory.html"  # toHaveURL now passes
    assert not any(_is_credential_fill(s) for s in steps)   # no credential left
    assert [s["step_number"] for s in steps] == [1, 2, 3]    # renumbered


def test_non_login_flow_is_unchanged():
    case = ProductionTestCase(
        test_id="t2", name="browse",
        steps=[
            _step(1, "Open https://shop.example/", verb="navigate", url="https://shop.example/"),
            _step(2, "Click 'Products'", verb="click", label="Products"),
            _step(3, "Verify navigated to products", verb="navigate", url="/products"),
        ],
    )
    assert _strip_login_steps(case).model_dump()["steps"] == case.model_dump()["steps"]


def test_credential_but_no_post_login_nav_is_unchanged():
    # A login that never produced a URL change (SPA) must not be mis-rewritten.
    case = ProductionTestCase(
        test_id="t3", name="spa login",
        steps=[
            _step(1, "Open https://app.example/login", verb="navigate", url="https://app.example/login"),
            _step(2, "Enter user", verb="type", label="Username", value="u"),
            _step(3, "Click Sign in", verb="click", label="Sign in"),
        ],
    )
    assert _strip_login_steps(case).model_dump()["steps"] == case.model_dump()["steps"]


def test_gate_only_strips_with_a_real_auth_profile():
    case = _saucedemo_case()
    base = "https://www.saucedemo.com/"

    # No auth profile → the login replay is preserved in the compiled spec.
    files_none = _configured_files([case], {}, base, {}, storage_state=None)
    spec_none = "\n".join(v for v in files_none.values() if isinstance(v, str))
    assert "visual_user" in spec_none

    # A genuine auth profile → login stripped, entry re-pointed to /inventory.html.
    ss = ('{"cookies":[{"name":"session-username","value":"standard_user",'
          '"domain":"www.saucedemo.com","path":"/"}],"origins":[]}')
    files_auth = _configured_files([case], {}, base, {}, storage_state=ss)
    spec_auth = "\n".join(v for v in files_auth.values() if isinstance(v, str))
    assert "/inventory.html" in spec_auth
    assert "visual_user" not in spec_auth   # credential fill is gone


def test_env_routing_cookies_do_not_trigger_strip():
    # env_context cookies fill storage_state via _merge_env_cookies, but carry NO
    # session — the strip must NOT fire (else the run lands logged-out on a deep URL).
    case = _saucedemo_case()
    base = "https://www.saucedemo.com/"
    env_context = {"cookies": [{"name": "canary", "value": "v", "domain": "www.saucedemo.com"}]}
    files = _configured_files([case], {}, base, {}, storage_state=None, env_context=env_context)
    spec = "\n".join(v for v in files.values() if isinstance(v, str))
    assert "visual_user" in spec   # login replay preserved (no auth profile present)
