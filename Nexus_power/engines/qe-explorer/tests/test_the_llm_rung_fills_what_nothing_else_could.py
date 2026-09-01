"""THE LLM RUNG FILLS WHAT NOTHING ELSE COULD — AND ONLY THAT.

The operator can choose, in the portal, that a crawl must never stop for data.
This is the rung that honours the choice: after the client's answer key, the
journey's own memory, recalled values and the constrained generator have all had
their turn, whatever is still empty is handed to a model — and stamped
``provenance: llm`` so the evidence never confuses a model's plausible value
with the client's real one.

MEASURED need (VKPower + Dolibarr + Odoo journeys, 2026-08-30): 39 of 48 live
fill rejections were the literal placeholder "autotest" typed into free-text
fields; the remaining residue was fields no deterministic rung could answer.

WHAT THE RUNG MUST NEVER DO, each pinned below:

  * override a truer rung — the answer key wins and the model is not consulted;
  * answer an option the control does not offer — off-list replies are clamped
    to None rather than committed;
  * answer credentials — a made-up OTP is a burned auth attempt, not data;
  * stop the crawl — no key, HTTP failure, the cap and the open breaker all
    return None, which is exactly the residue behaviour the crawl always had.
"""
from __future__ import annotations

import os

import httpx

from app.forms import AnswerKey, PROV_LLM, _llm_should_answer, resolve_field
from app.identity_pack import derive
from app.llm_data import LLMDataAgent


class _FakeLLM:
    """The seam resolve_field sees: value_for(**kw) -> Optional[str]."""

    def __init__(self, value="Yes"):
        self.value = value
        self.calls = []

    def value_for(self, **kw):
        self.calls.append(kw)
        return self.value


def _identity():
    return derive("llm-rung-test")


def _radio(question="Do you smoke?"):
    return {"name": "Yes", "kind": "radio", "question_label": question,
            "group_id": "g1", "group_options": ["Yes", "No"],
            "options": ["Yes", "No"], "input_type": "radio"}


def _Key(value=None):
    """The real AnswerKey: empty, or answering this question exactly."""
    if value is None:
        return AnswerKey()
    # AnswerKey.resolve normalises the NAME before lookup; exact keys
    # are stored as given, so the key must be pre-normalised too.
    return AnswerKey(exact={"do you smoke?": value})


# ── the ladder order ───────────────────────────────────────────────────────

def test_the_llm_fills_a_choice_the_user_mode_left_unanswered():
    """User+LLM mode: data_mode stays 'user' (the deterministic generator
    declines semantic choices), and the model answers what the client did not.

    One question, one answer, member by member: the model says "No", so the
    "No" MEMBER is the fill and the "Yes" member is its sibling — the browser
    owns exclusivity, and filling both would check both."""
    llm = _FakeLLM("No")
    no_member = dict(_radio(), name="No")
    d = resolve_field(no_member, "radio", "Do you smoke?", _Key(None), _identity(),
                      data_mode="user", llm=llm)
    assert d["value"] == "No"
    assert d["entry"]["provenance"] == PROV_LLM
    assert d["entry"]["filled"] is True
    assert llm.calls[0]["options"] == ["Yes", "No"], "the enumeration must travel"
    # the other member of the SAME answered question steps aside
    d2 = resolve_field(_radio(), "radio", "Do you smoke?", _Key(None), _identity(),
                       data_mode="user", llm=_FakeLLM("No"))
    assert d2["value"] is None
    assert d2["entry"]["provenance"] == "group_sibling"


def test_the_answer_key_wins_and_the_model_is_not_consulted():
    llm = _FakeLLM("No")
    d = resolve_field(_radio(), "radio", "Do you smoke?", _Key("Yes"), _identity(),
                      data_mode="user", llm=llm)
    assert d["value"] == "Yes"
    assert d["entry"]["provenance"] != PROV_LLM
    assert llm.calls == [], "a truer rung answered; the model must stay silent"


def test_with_no_agent_the_field_stays_residue_exactly_as_before():
    d = resolve_field(_radio(), "radio", "Do you smoke?", _Key(None), _identity(),
                      data_mode="user", llm=None)
    assert d["value"] is None


def test_the_placeholder_rule():
    """"autotest" is the generator's own admission it had nothing to say."""
    assert _llm_should_answer(None) is True
    assert _llm_should_answer("autotest") is True
    assert _llm_should_answer("178") is False
    assert _llm_should_answer("Austin") is False


# ── the agent's own guarantees ─────────────────────────────────────────────

def _agent(handler, **kw):
    """DIRECT mode on purpose: these tests exercise the provider-call rules
    (clamp, breaker, cap) in isolation. Direct refuses without the dev flag —
    which its own test below asserts — so the flag is set here explicitly."""
    os.environ["QEC_DATA_LLM_DIRECT"] = "true"
    return LLMDataAgent(mode="direct", transport=httpx.MockTransport(handler), **kw)


def _ok(value):
    def handler(request):
        return httpx.Response(200, json={
            "choices": [{"message": {"content": value}}]})
    return handler


def test_an_off_list_reply_is_clamped_never_committed():
    os.environ.setdefault("OPENAI_API_KEY", "test-key")
    a = _agent(_ok("Maybe"))
    got = a.value_for(name="Do you smoke?", semantic_type="choice", kind="radio",
                      options=["Yes", "No"])
    assert got is None, "an answer the control does not offer must not commit"


def test_an_on_list_reply_is_returned_verbatim_from_the_control():
    os.environ.setdefault("OPENAI_API_KEY", "test-key")
    a = _agent(_ok("  yes "))
    got = a.value_for(name="Do you smoke?", semantic_type="choice", kind="radio",
                      options=["Yes", "No"])
    assert got == "Yes", "match case-insensitively, return the control's own label"


def test_credentials_are_refused_without_a_call():
    a = _agent(_ok("hunter2"))
    assert a.value_for(name="One-time code", semantic_type="otp",
                       kind="text") is None
    assert a.calls == 0, "a credential must not even cost a call"


def test_the_breaker_opens_and_then_costs_nothing():
    os.environ.setdefault("OPENAI_API_KEY", "test-key")
    def boom(request):
        return httpx.Response(500)
    a = _agent(boom, breaker_threshold=3)
    for _ in range(3):
        assert a.value_for(name="Notes", semantic_type="free_text",
                           kind="text") is None
    assert a.breaker_open is True
    calls_before = a.calls
    assert a.value_for(name="Notes", semantic_type="free_text",
                       kind="text") is None
    assert a.calls == calls_before, "an open breaker must not spend calls"


def test_the_cap_ends_consultations_quietly():
    os.environ.setdefault("OPENAI_API_KEY", "test-key")
    a = _agent(_ok("fine"), max_calls=2)
    assert a.value_for(name="A", semantic_type="free_text", kind="text") == "fine"
    assert a.value_for(name="B", semantic_type="free_text", kind="text") == "fine"
    assert a.value_for(name="C", semantic_type="free_text", kind="text") is None
    assert a.calls == 2



# ── the wire itself: no second route ───────────────────────────────────────

def test_direct_mode_refuses_itself_without_the_dev_flag(monkeypatch):
    """T-SEC-12's point, held on THIS side of the fleet: a direct provider call
    bypasses the PII egress guard, so it must refuse unless a developer
    explicitly opted in — and the refusal costs no HTTP call."""
    monkeypatch.delenv("QEC_DATA_LLM_DIRECT", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    def explode(request):
        raise AssertionError("no request may leave direct mode without the flag")

    a = LLMDataAgent(mode="direct", transport=httpx.MockTransport(explode))
    assert a.value_for(name="Notes", semantic_type="free_text", kind="text") is None


def test_central_mode_posts_a_signed_request_and_takes_the_answer(monkeypatch):
    """The DEFAULT transport: qe-central's /internal/pick-value, signed with the
    same envelope as /pick-advance. The server owns the model and the scan."""
    seen = {}

    class _Settings:
        callback_url = "http://qe-central:8000"
        explorer_token = "tok-1"

        def sign_payload(self, payload, *, scope=""):
            seen["scope"] = scope
            return "sig-abc"

    def handler(request):
        seen["url"] = str(request.url)
        seen["sig"] = request.headers.get("X-QEC-Signature")
        seen["tok"] = request.headers.get("X-QEC-Token")
        import json as _json
        seen["body"] = _json.loads(request.content.decode("utf-8"))
        return httpx.Response(200, json={"value": "No", "status": "answered",
                                         "usage": {}})

    # A TENANT IS NOW PART OF THE SIGNATURE SCOPE. qe-central's
    # /internal/pick-value verifies tenant_scope("pick-value", tenant, crawl),
    # and tenant_scope FAILS CLOSED on an empty component — so an agent with no
    # tenant cannot sign at all and the consultation degrades to None. That is
    # the correct behaviour and it is pinned separately below; here we supply
    # one, because this test is about the happy path.
    a = LLMDataAgent(settings=_Settings(), crawl_id="c-9", tenant_id="t-1",
                     transport=httpx.MockTransport(handler))
    got = a.value_for(name="Do you smoke?", semantic_type="choice", kind="radio",
                      options=["Yes", "No"])
    assert got == "No"
    assert seen["url"].endswith("/internal/pick-value")
    # The migrated scope: operation + tenant + crawl, not operation + crawl.
    from app.hmac_auth import tenant_scope
    assert seen["scope"] == tenant_scope("pick-value", "t-1", "c-9")
    assert seen["body"]["tenant_id"] == "t-1"
    assert seen["sig"] == "sig-abc" and seen["tok"] == "tok-1"
    assert seen["body"]["options"] == ["Yes", "No"]


def test_central_none_and_unavailable_both_become_residue():
    class _Settings:
        callback_url = "http://qe-central:8000"
        explorer_token = "t"

        def sign_payload(self, payload, *, scope=""):
            return "s"

    for status in ("none", "unavailable"):
        def handler(request, _s=status):
            return httpx.Response(200, json={"value": None, "status": _s})
        a = LLMDataAgent(settings=_Settings(), crawl_id="c",
                         transport=httpx.MockTransport(handler))
        assert a.value_for(name="Notes", semantic_type="free_text",
                           kind="text") is None
        assert a.breaker_open is False, "an honest non-answer is not a failure"


def test_no_module_but_this_one_may_even_name_a_model_host():
    """The explorer-side mirror of qe-central's T-SEC-12 AST guard. A second
    model client in this service would again sail past qe-central's suite, so
    the invariant is pinned where the first violation actually lived."""
    import pathlib
    app_dir = pathlib.Path(__file__).resolve().parents[1] / "app"
    offenders = []
    for py in app_dir.rglob("*.py"):
        if py.name == "llm_data.py":
            continue
        text = py.read_text(encoding="utf-8", errors="replace")
        for host in ("api.openai.com", "api.anthropic.com",
                     "generativelanguage.googleapis.com"):
            if host in text:
                offenders.append(f"{py.name}: {host}")
    assert offenders == [], offenders


def test_a_value_consultation_without_a_tenant_declines_rather_than_signing_weakly():
    """THE CONTROL for the tenant-scoped migration.

    `tenant_scope` fails closed on an empty component rather than emitting a
    weaker `pick-value::c-9`. So an agent constructed without a tenant cannot
    sign, and must DECLINE — not fall back to the old scope, and not send an
    unsigned request. Without this test the happy-path assertion above would
    also pass against an implementation that silently signed weakly.
    """
    class _Settings:
        callback_url = "http://qe-central:8000"
        explorer_token = "tok-1"

        def sign_payload(self, payload, *, scope=""):   # pragma: no cover
            raise AssertionError(
                "sign_payload was reached with an unsignable tenant; the scope "
                f"would have been {scope!r}")

    def handler(request):                      # pragma: no cover - must not run
        raise AssertionError("a request was sent without a signable tenant")

    a = LLMDataAgent(settings=_Settings(), crawl_id="c-9", tenant_id="",
                     transport=httpx.MockTransport(handler))
    assert a.value_for(name="Do you smoke?", semantic_type="choice",
                       kind="radio", options=["Yes", "No"]) is None
