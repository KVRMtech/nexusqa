"""T-SEC-12 — PII EGRESS COVERAGE (an identifier reaching a third-party model).

ATTACK
======
The PII egress guard existed and worked — and exactly ONE caller invoked it
(``routers/apps`` → ``guard_inventory``).  Ten other ``complete_llm`` sites and
both screenshot endpoints reached the model with no scan at all.  A guard each
caller must remember to call is a convention, and ten unguarded sites are what a
convention looks like after a year: a policyholder's SSN in a field label, an
account number in a captured option, a name in a page title, all egressing to a
cloud model for a regulated buyer.

EXPECTED
========
Every LLM and screenshot request passes through the guard, because the guard
lives at the WIRE — inside the only two functions in this service that talk to a
model — and there is no second route.
"""
from __future__ import annotations

import ast
import pathlib

import pytest

from app.services.pii_egress_guard import guard_text

SERVICE = pathlib.Path(__file__).resolve().parents[2]

# Representative PII of the classes a regulated buyer cares about.
PII_SAMPLES = {
    "ssn": "the applicant's SSN is 123-45-6789",
    "card": "card on file 4111 1111 1111 1111",
}
CLEAN_SAMPLES = [
    "Which control advances this wizard? Options: Continue, Back",
    "label=Coverage Amount type=select options=100000,250000,500000",
    "",
]


# ── the guard's own verdict ────────────────────────────────────────────────

@pytest.mark.parametrize("name,text", sorted(PII_SAMPLES.items()))
def test_pii_is_detected_and_blocked(name, text):
    verdict = guard_text(text, site="test")
    assert verdict["safe"] is False
    assert verdict["matches"], "a block must name the pattern class"


@pytest.mark.parametrize("text", CLEAN_SAMPLES)
def test_clean_payloads_are_not_blocked(text):
    """POSITIVE half — a guard that blocks everything is an outage, not a control."""
    assert guard_text(text, site="test")["safe"] is True


def test_the_guard_fails_CLOSED_when_the_detector_cannot_run(monkeypatch):
    """An unavailable detector must block, never wave the payload through."""
    from app.services import pii_egress_guard

    def _broken():
        raise RuntimeError("detector module missing")

    monkeypatch.setattr(pii_egress_guard, "_detector", _broken)
    verdict = pii_egress_guard.guard_text("anything at all", site="test")
    assert verdict["safe"] is False


def test_the_block_reason_never_echoes_the_matched_value():
    """Refusing to send an identifier and then logging it is not a refusal."""
    verdict = guard_text(PII_SAMPLES["ssn"], site="test")
    assert "123-45-6789" not in verdict["reason"]
    assert all("123-45-6789" not in str(m) for m in verdict["matches"])


# ── the chokepoint: both egress functions scan ─────────────────────────────

@pytest.mark.anyio
async def _unused():  # pragma: no cover - anyio marker placeholder
    ...


def _run(coro):
    import asyncio

    return asyncio.run(coro)


def test_complete_llm_blocks_a_pii_prompt_before_any_http(monkeypatch):
    """The request must never be BUILT, let alone sent."""
    import httpx

    from app.clients import platform_api

    def _explode(*a, **k):  # pragma: no cover — must not be reached
        raise AssertionError("an HTTP client was constructed for a blocked payload")

    monkeypatch.setattr(httpx, "AsyncClient", _explode)
    res = _run(platform_api.complete_llm(
        tenant_id="t", prompt=PII_SAMPLES["ssn"], task="brief_compile"))
    assert res.ok is False
    assert "egress blocked" in res.detail


def test_complete_vision_blocks_a_pii_prompt_before_any_http(monkeypatch):
    import httpx

    from app.clients import platform_api

    def _explode(*a, **k):  # pragma: no cover
        raise AssertionError("an HTTP client was constructed for a blocked payload")

    monkeypatch.setattr(httpx, "AsyncClient", _explode)
    res = _run(platform_api.complete_vision(
        tenant_id="t", prompt=PII_SAMPLES["card"], screenshot_b64="abc",
        task="vision_medic"))
    assert res.ok is False
    assert "egress blocked" in res.detail


def test_the_system_prompt_is_scanned_too(monkeypatch):
    """PII can arrive in either half of the payload."""
    import httpx

    from app.clients import platform_api

    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("HTTP client constructed")))
    res = _run(platform_api.complete_llm(
        tenant_id="t", prompt="which control advances?",
        system=PII_SAMPLES["ssn"]))
    assert res.ok is False


def test_a_blocked_call_degrades_like_an_unavailable_model(monkeypatch):
    """The refusal shape must be one every caller already handles, so a block
    falls back to the deterministic floor instead of raising into a crawl."""
    from app.clients import platform_api
    from app.clients.platform_api import LLMResult

    res = _run(platform_api.complete_llm(tenant_id="t", prompt=PII_SAMPLES["ssn"]))
    assert isinstance(res, LLMResult) and res.ok is False and res.text == ""


# ── no second route ────────────────────────────────────────────────────────

_ALLOWED_HTTP_CALLERS = {
    # The only two functions permitted to POST at a model endpoint.
    "complete_llm", "complete_vision",
}


def test_both_model_functions_call_the_guard():
    import inspect

    from app.clients import platform_api

    for name in sorted(_ALLOWED_HTTP_CALLERS):
        src = inspect.getsource(getattr(platform_api, name))
        assert "_assert_egress_clean" in src, f"{name} reaches a model unguarded"


def test_no_module_outside_the_client_talks_to_a_model_endpoint():
    """THE structural claim: there is no alternate direct call path.

    Scans the whole service for the model endpoints; only the guarded client may
    name them.  A new unguarded route fails this test the day it is written."""
    offenders = []
    for path in (SERVICE / "app").rglob("*.py"):
        text = path.read_text(encoding="utf-8", errors="replace")
        if "/api/v1/llm/" not in text:
            continue
        if path.name == "platform_api.py":
            continue
        offenders.append(str(path.relative_to(SERVICE)))
    assert not offenders, f"model endpoint reached outside the guarded client: {offenders}"


def test_no_direct_provider_sdk_is_imported():
    """A provider SDK anywhere in this service would be an egress route the
    guard cannot see."""
    banned = {"openai", "anthropic", "google.generativeai", "cohere", "mistralai",
              "litellm", "ollama"}
    offenders = []
    for path in (SERVICE / "app").rglob("*.py"):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:  # pragma: no cover
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            for n in names:
                root = n.split(".")[0]
                if root in banned or n in banned:
                    offenders.append(f"{path.relative_to(SERVICE)}:{n}")
    assert not offenders, f"unguarded provider SDK import: {offenders}"


def test_every_llm_call_site_goes_through_the_guarded_client():
    """Enumerate the call sites the brief counted and prove each uses the client.

    If a site were rewritten to build its own request, its module would no
    longer import the client — and would be caught by the endpoint scan above —
    but naming them here makes the coverage claim explicit and reviewable."""
    sites = []
    for path in (SERVICE / "app").rglob("*.py"):
        text = path.read_text(encoding="utf-8", errors="replace")
        for fn in ("complete_llm", "complete_vision"):
            if f"platform_api.{fn}(" in text or f"{fn}(" in text and "platform_api" in text:
                if path.name != "platform_api.py":
                    sites.append((str(path.relative_to(SERVICE)), fn))
    # The brief counted 10 LLM sites + 2 screenshot endpoints; the exact number
    # moves with the product, so assert the SHAPE: there are many, and every one
    # of them is reached through the guarded client rather than a raw request.
    assert len(sites) >= 8, sites
    for module, _fn in sites:
        text = (SERVICE / module).read_text(encoding="utf-8", errors="replace")
        assert "platform_api" in text
        assert "/api/v1/llm/" not in text


def test_both_screenshot_endpoints_are_covered():
    """``vision-operate`` and ``perceive-controls`` are the two screenshot
    routes; both reach the model only via ``complete_vision``."""
    src = (SERVICE / "app/routers/internal.py").read_text(encoding="utf-8")
    assert src.count("platform_api.complete_vision(") == 2
    assert "/api/v1/llm/" not in src


def test_the_guard_can_be_disabled_only_loudly(monkeypatch, caplog):
    """An escape hatch must be visible in the logs of the deployment using it."""
    import logging

    monkeypatch.setenv("QEC_PII_EGRESS_GUARD", "0")
    with caplog.at_level(logging.WARNING):
        assert guard_text(PII_SAMPLES["ssn"], site="test")["safe"] is True
    assert any("pii_guard_disabled" in r.message for r in caplog.records)
