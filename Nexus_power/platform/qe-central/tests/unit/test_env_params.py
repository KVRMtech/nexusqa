"""Environment {box}/{runway} parameter substitution (Phase 4 — record-once envs).

Pins that a parameterized environment (CIT box URL, runway cookie) resolves its
{param} placeholders at run time, and that an UNKNOWN placeholder is left intact and
surfaced (never fabricated into a wrong URL). Pure — no DB.
"""
from app.services import env_params as ep


def test_box_url_template():
    assert ep.substitute("http://{box}.usaa.com", {"box": "786"}) == "http://786.usaa.com"


def test_runway_cookie_value():
    cookies = [{"name": "runway", "value": "{runway}", "domain": "usaa.com"}]
    out = ep.substitute(cookies, {"runway": "RWA"})
    assert out[0]["value"] == "RWA"
    assert out[0]["name"] == "runway"  # non-placeholder untouched


def test_unknown_param_left_intact():
    assert ep.substitute("http://{box}.usaa.com", {}) == "http://{box}.usaa.com"


def test_resolve_env_params_full_context():
    ctx = {"environment_id": "e1", "base_url": "http://{box}.usaa.com",
           "cookies": [{"name": "runway", "value": "{runway}"}],
           "headers": {"X-Env": "{runway}"}}
    resolved, missing = ep.resolve_env_params(ctx, {"box": "787", "runway": "RWB"})
    assert resolved["base_url"] == "http://787.usaa.com"
    assert resolved["cookies"][0]["value"] == "RWB"
    assert resolved["headers"]["X-Env"] == "RWB"
    assert missing == []
    assert resolved["environment_id"] == "e1"  # non-routing key untouched


def test_missing_params_surfaced_not_fabricated():
    ctx = {"base_url": "http://{box}.usaa.com/{lane}"}
    resolved, missing = ep.resolve_env_params(ctx, {"box": "786"})
    assert resolved["base_url"] == "http://786.usaa.com/{lane}"  # lane left intact
    assert missing == ["lane"]


def test_find_params():
    assert ep.find_params("http://{box}.usaa.com/{lane}") == ["box", "lane"]
    assert ep.find_params({"a": ["{x}", "{y}"], "b": "z"}) == ["x", "y"]
