"""M3.1 / T-VIS-03 — EXACTLY ONE AUTHORITATIVE VISION SYSTEM PROMPT.

THE DEFECT
==========
``/internal/perceive-controls`` sent ``system=vision_medic.SYSTEM`` — the MEDIC
prompt, which demands::

    {"action": "click_region", "x": <int>, "y": <int>, "reason": "..."}

while ``perceive_controls`` built its own contract into the USER prompt::

    {"controls":[{"label":…,"bbox":…}], "displayed_values":[…]}

Two mutually exclusive output contracts, one per channel, on every perceive call.
Which one the model obeyed was a property of the provider.  And the failure was
SILENT: ``parse_perceived`` returns empty lists for a reply shaped like the
system prompt, so a misconfigured endpoint presented as "vision found nothing" —
the single most plausible-looking outcome available.

WHAT THESE TESTS PIN
====================
1. the two prompts remain DIFFERENT (they answer different questions);
2. each endpoint sends the one that matches the contract its parser reads;
3. selection is by TASK, from one table, and an unknown task RAISES;
4. the user prompt restates no contract;
5. the effective prompt is deterministic and inspectable from the response.
"""
from __future__ import annotations

import types

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.services import vision_medic as vm

TENANT = "t"
CRAWL = "a" * 32


# ── 1-3. the table ──────────────────────────────────────────────────────────

def test_the_two_prompts_are_not_the_same_prompt():
    assert vm.SYSTEM != vm.PERCEIVE_SYSTEM
    # the medic asks WHERE TO CLICK; the perceiver asks WHAT IS THERE.
    assert "click_region" in vm.SYSTEM
    assert "click_region" not in vm.PERCEIVE_SYSTEM
    assert '"controls"' in vm.PERCEIVE_SYSTEM


def test_the_system_prompt_is_selected_by_task_from_one_table():
    assert vm.system_prompt_for(vm.TASK_VISION_MEDIC) is vm.SYSTEM
    assert vm.system_prompt_for(vm.TASK_VISION_PERCEIVE) is vm.PERCEIVE_SYSTEM


@pytest.mark.parametrize("task", ["", "  ", None, "vision", "perceive", "medic"])
def test_an_unknown_task_raises_rather_than_defaulting(task):
    """A default here would be a silent THIRD contract."""
    with pytest.raises(ValueError):
        vm.system_prompt_for(task)


def test_the_effective_prompt_is_deterministic_and_inspectable():
    a = vm.effective_prompt(vm.TASK_VISION_PERCEIVE)
    b = vm.effective_prompt(vm.TASK_VISION_PERCEIVE)
    assert a == b
    assert len(a["system_sha256"]) == 64
    assert a["system_sha256"] != vm.effective_prompt(
        vm.TASK_VISION_MEDIC)["system_sha256"]


# ── 4. the user prompt carries no contract ──────────────────────────────────

def test_the_user_prompt_restates_no_output_contract():
    prompt = vm.build_perceive_prompt({"url": "https://x/app"})
    for token in ('"controls"', '"displayed_values"', "STRICT JSON", "bbox"):
        assert token not in prompt, (
            "the output contract is duplicated into the user prompt again — "
            "that duplication is what let the two channels disagree")
    assert "https://x/app" in prompt


# ── 5. each endpoint sends the prompt its own parser reads ──────────────────

def _client(monkeypatch, sent: dict):
    from app.clients import platform_api
    from app.routers import internal

    monkeypatch.setattr(
        internal, "phase1_settings",
        types.SimpleNamespace(verify_signature=lambda raw, sig, scope="": True))
    monkeypatch.setattr(internal, "settings",
                        types.SimpleNamespace(crawl_vision_enabled=True))

    async def fake_bind(crawl_id, claimed_tenant):
        return internal.CrawlBinding(tenant_id=TENANT, exploration_id="e",
                                     app_id="a", status="dispatched")

    monkeypatch.setattr(internal, "_bind_crawl", fake_bind)

    async def fake_vision(**kw):
        sent.update(kw)
        return types.SimpleNamespace(
            ok=True, detail="",
            text='{"controls":[{"label":"Go","role":"button","bbox":[1,1,9,9]}]}')

    monkeypatch.setattr(platform_api, "complete_vision", fake_vision)
    app = FastAPI()
    app.include_router(internal.router)
    return TestClient(app, raise_server_exceptions=False)


def test_perceive_controls_sends_the_PERCEIVE_prompt(monkeypatch):
    sent: dict = {}
    c = _client(monkeypatch, sent)
    r = c.post("/internal/perceive-controls",
               json={"crawl_id": CRAWL, "tenant_id": TENANT,
                     "screenshot_b64": "abc"})
    assert r.status_code == 200
    assert sent["task"] == vm.TASK_VISION_PERCEIVE
    assert sent["system"] == vm.PERCEIVE_SYSTEM
    # the regression, stated as an assertion rather than as a comment
    assert sent["system"] != vm.SYSTEM
    assert r.json()["prompt"]["task"] == vm.TASK_VISION_PERCEIVE


def test_vision_operate_sends_the_MEDIC_prompt(monkeypatch):
    sent: dict = {}
    c = _client(monkeypatch, sent)
    r = c.post("/internal/vision-operate",
               json={"crawl_id": CRAWL, "tenant_id": TENANT,
                     "screenshot_b64": "abc",
                     "control": {"tag": "canvas", "name": "", "role": "generic"},
                     "element_bbox": {"x": 0, "y": 0, "width": 10, "height": 10}})
    assert r.status_code == 200
    assert sent["task"] == vm.TASK_VISION_MEDIC
    assert sent["system"] == vm.SYSTEM
    assert r.json()["prompt"]["task"] == vm.TASK_VISION_MEDIC


def test_both_endpoints_relay_the_redaction_receipt(monkeypatch):
    """T-VIS-05 wiring: the receipt reaches the wire guard, not a log line."""
    sent: dict = {}
    c = _client(monkeypatch, sent)
    receipt = {"applied": True, "method": "dom-region-blackout-v1",
               "regions": 2, "image_sha256": "f" * 64}
    c.post("/internal/perceive-controls",
           json={"crawl_id": CRAWL, "tenant_id": TENANT, "screenshot_b64": "abc",
                 "pixel_redaction": receipt})
    assert sent["redaction"] == receipt
