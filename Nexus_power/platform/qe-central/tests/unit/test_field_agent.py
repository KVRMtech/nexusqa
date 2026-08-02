"""P3 — the field agent names a field and is never allowed to fill one.

The exploration planner established the cage: an LLM proposes, deterministic code
disposes. This is the same cage around a different question, and the tests below
pin the bars rather than the behaviour — the behaviour is a model's, and models
change.

Two properties matter more than accuracy:

  * the model never SEES a value, so it cannot memorise, leak or reproduce client
    data — and no amount of accuracy would be worth giving that up;
  * the model never PRODUCES a value. It returns `email`; a deterministic generator
    produces the address from a fictional identity. That is what makes every value
    reproducible from evidence and provably not a real person's.

Pure — no network. `classify_fields` is exercised through its validator.
"""
import inspect
import json

from app.services import field_agent as fa

SIG_A = "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6"
SIG_B = "b1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6"


# ── what the model is shown ──────────────────────────────────────────────────

def test_the_model_is_shown_only_what_the_application_declared():
    """No value, no URL, no tenant, no page. If a key could carry what a user
    typed, it must not be in the description."""
    shown = fa._describe({
        "signature": SIG_A, "tokens": ["social", "security", "number"],
        "input_type": "text", "constraints": "max=11", "option_shape": "",
        # everything below is deliberately NOT presentable
        "value": "849-22-7710", "committed_value": "849-22-7710",
        "url": "https://client.example/apply", "tenant_id": "acme",
        "page_title": "Apply for cover",
    })
    assert set(shown) == {"id", "label_tokens", "input_type", "constraints", "option_count"}
    blob = json.dumps(shown)
    for leak in ("849-22-7710", "client.example", "acme", "Apply for cover"):
        assert leak not in blob


def test_the_prompt_carries_no_value_and_forbids_producing_one():
    prompt = fa._build_prompt([fa._describe(
        {"signature": SIG_A, "tokens": ["email"], "input_type": "email"})])
    assert "must not produce any" in prompt
    assert "not given any values" in prompt.lower() or "NOT given any values" in prompt


def test_the_prompt_prefers_an_honest_unknown():
    """A confident wrong answer causes the wrong value to be typed into a real
    application; an unknown only causes a question to be asked."""
    prompt = fa._build_prompt([fa._describe(
        {"signature": SIG_A, "tokens": ["reference"], "input_type": "text"})])
    assert "unknown" in prompt
    assert "worse than an honest unknown" in prompt


# ── what the model may return ────────────────────────────────────────────────

def test_a_type_outside_the_vocabulary_is_dropped():
    """THE CAGE. The model may propose anything; only a vocabulary member survives,
    so it can never introduce a category and therefore never a behaviour."""
    raw = json.dumps({"fields": [
        {"id": SIG_A, "type": "exfiltrate", "confidence": 0.99},
        {"id": SIG_B, "type": "email", "confidence": 0.6},
    ]})
    out = fa._validate(raw, {SIG_A, SIG_B})
    assert SIG_A not in out
    assert out[SIG_B]["type"] == "email"


def test_the_model_may_only_answer_questions_it_was_asked():
    """GROUNDING. Without this it could invent a signature and have it stored as
    fact about an application nobody crawled."""
    raw = json.dumps({"fields": [{"id": "f" * 32, "type": "email", "confidence": 0.9}]})
    assert fa._validate(raw, {SIG_A}) == {}


def test_a_malformed_signature_is_dropped():
    for bad in ("../../etc/passwd", "'; drop table field_priors; --", "x", ""):
        raw = json.dumps({"fields": [{"id": bad, "type": "email", "confidence": 0.9}]})
        assert fa._validate(raw, {bad}) == {}


def test_the_models_confidence_is_capped_below_a_declaration():
    """A guess must never outrank what the application said about itself, however
    certain the model claims to be."""
    raw = json.dumps({"fields": [{"id": SIG_A, "type": "national_id", "confidence": 1.0}]})
    assert fa._validate(raw, {SIG_A})[SIG_A]["confidence"] <= 0.7


def test_a_barely_confident_answer_still_carries_a_floor():
    raw = json.dumps({"fields": [{"id": SIG_A, "type": "email", "confidence": 0.0}]})
    assert fa._validate(raw, {SIG_A})[SIG_A]["confidence"] >= 0.4


def test_an_unknown_answer_is_not_recorded():
    raw = json.dumps({"fields": [{"id": SIG_A, "type": "unknown", "confidence": 0.9}]})
    assert fa._validate(raw, {SIG_A}) == {}


def test_garbage_output_yields_an_empty_map_not_an_exception():
    """Fail-open: an empty result means the crawl behaves exactly as before."""
    for junk in ("", "not json", "[]", '{"fields": "nope"}', "null",
                 '{"wrong_key": []}', "```json\n{}\n```"):
        assert fa._validate(junk, {SIG_A}) == {}


def test_a_fenced_json_answer_is_still_read():
    raw = "```json\n" + json.dumps({"fields": [
        {"id": SIG_A, "type": "postal_code", "confidence": 0.6}]}) + "\n```"
    assert fa._validate(raw, {SIG_A})[SIG_A]["type"] == "postal_code"


def test_the_answer_is_bounded():
    """A plan is an optimiser, never an attack surface."""
    many = [{"id": SIG_A[:-2] + f"{i:02x}", "type": "email", "confidence": 0.5}
            for i in range(500)]
    asked = {f["id"] for f in many}
    assert len(fa._validate(json.dumps({"fields": many}), asked)) <= fa._MAX_FIELDS


# ── the agent can never produce a value ──────────────────────────────────────

def test_the_output_surface_is_a_type_and_nothing_else():
    """The module's entire output is {signature: {type, confidence, source}}. If a
    value could ride along in it, the whole no-PII-through-the-model guarantee is
    gone — so the shape is asserted, not the wording of the source."""
    raw = json.dumps({"fields": [{
        "id": SIG_A, "type": "email", "confidence": 0.6,
        # a model that tried to be helpful and emit a value must not be obeyed
        "value": "someone@real.example", "example": "1990-01-01",
        "suggested_input": "849-22-7710",
    }]})
    out = fa._validate(raw, {SIG_A})
    assert set(out[SIG_A]) == {"type", "confidence", "source"}
    blob = json.dumps(out)
    for leak in ("someone@real.example", "1990-01-01", "849-22-7710"):
        assert leak not in blob


def test_the_classifier_never_raises():
    """A classifier that throws would break a crawl over an optimisation."""
    src = inspect.getsource(fa.classify_fields)
    assert "except Exception" in src
    assert "return {}" in src


def test_no_llm_configured_means_a_byte_identical_crawl():
    src = inspect.getsource(fa.classify_fields)
    assert "if not llm.ok:" in src
    assert src.index("if not llm.ok:") < src.index("_validate(")


def test_a_field_with_no_label_tokens_is_not_sent():
    """Nothing to classify, and sending it would spend a call to be told unknown."""
    src = inspect.getsource(fa.classify_fields)
    assert 'c["label_tokens"]' in src


def test_the_vocabulary_matches_the_platform_store():
    """The two services deploy independently and duplicate the list on purpose;
    divergence means one side writes types the other cannot read."""
    import ast as _ast
    import pathlib
    p = (pathlib.Path(__file__).resolve().parents[3] / "api" / "app"
         / "services" / "test_factory" / "field_learning.py")
    tree = _ast.parse(p.read_text(encoding="utf-8"))
    for node in tree.body:
        if (isinstance(node, _ast.Assign)
                and getattr(node.targets[0], "id", "") == "VOCABULARY"):
            members = {e.value for e in node.value.args[0].elts}
            assert members == set(fa.VOCABULARY), members ^ set(fa.VOCABULARY)
            return
    raise AssertionError("VOCABULARY not found in field_learning")
