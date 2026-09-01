"""Auth permanence — a form-login profile makes a server-side run self-login, so
the green is reproducible without re-importing an expiring captured session.

venkata (2026-07-25): onboarded with an IMPORTED session cookie (8h TTL), so the
47/47 green only held until the cookie died. A form-login profile stores the
credentials (envelope-encrypted) + the form shape; the runner's globalSetup logs
in FRESH each run (compiler `_AUTH_SETUP_TS`, strategy 'form'), reading creds
from the run ENV (never the bundle).

These pin the split + that every server-side run path injects it.

Run from Nexus_power/platform/api:
    python -m pytest tests/test_auth_permanence_form_login.py -q
"""
from __future__ import annotations

import os
import re

from app.services.test_factory import auth_profiles as ap

_HERE = os.path.dirname(__file__)
_ROUTER = open(
    os.path.join(_HERE, "..", "app", "routers", "test_factory.py"), encoding="utf-8").read()


def test_build_bundle_splits_secrets_from_config():
    cfg = {"user": "member@vkpowerlife.com", "password": "Password123",
           "login_path": "/login", "submit_label": "Sign in",
           "fields": [{"label": "Email address", "value": "user"},
                      {"label": "Password", "value": "password"}]}
    auth_config, login_env = ap.build_form_login_bundle(cfg)
    # the config (goes into the bundle) is 'form' and carries NO secret
    assert auth_config["strategy"] == "form"
    assert auth_config["loginPath"] == "/login"
    assert auth_config["submitLabel"] == "Sign in"
    blob = str(auth_config)
    assert "Password123" not in blob and "member@vkpowerlife.com" not in blob
    # the secrets go ONLY into the run env, under the config's env var names
    assert login_env["NEXUS_LOGIN_USER"] == "member@vkpowerlife.com"
    assert login_env["NEXUS_LOGIN_PASSWORD"] == "Password123"


def test_build_bundle_defaults_are_safe():
    auth_config, login_env = ap.build_form_login_bundle(
        {"user": "u", "password": "p"})
    assert auth_config["strategy"] == "form"
    assert auth_config["fields"]           # a default field map exists
    assert login_env == {"NEXUS_LOGIN_USER": "u", "NEXUS_LOGIN_PASSWORD": "p"}


def test_form_login_never_persists_stray_fields():
    """Only the whitelisted keys are kept — no accidental secret spillover."""
    assert set(ap._FORMLOGIN_FIELDS) == {
        "login_path", "submit_label", "fields", "user", "password",
        "user_env", "password_env"}


def test_configured_files_accepts_auth_config_and_overrides_config_json():
    # the bundle writer must overwrite vkpower.auth.config.json when given a config
    m = re.search(r"def _configured_files\(.*?\n\S", _ROUTER, re.S)
    assert m and "auth_config" in m.group(0), "_configured_files must take auth_config"
    assert 'files["vkpower.auth.config.json"] = json.dumps(auth_config' in _ROUTER


def test_all_server_run_env_dicts_inject_login_env():
    """Every place that fetches form-login must spread it into the run env."""
    # helper exists
    assert "async def _run_form_login(" in _ROUTER
    # certification, client run, run-live, heal capture/verify all inject creds
    assert _ROUTER.count("_run_form_login(request, artifact_id, tenant_id)") >= 4
    # creds ride the env, never the bundle
    assert "**login_env," in _ROUTER
