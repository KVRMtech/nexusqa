"""P1 / P4 / P5 — remember for one client, generalise for everyone, never leak.

The learning loop's whole value is that a client is not asked the same question
twice, and that the hundred-and-first client starts smarter than the first. Its
whole RISK is that the data making that possible is a regulated client's personal
data.

So there are two stores with deliberately different shapes:

  tp_field_memory   tenant-private, encrypted, holds the value
  field_priors      cross-tenant, holds ONLY that a signature meant a type

The tests below exist mostly to pin the boundary between them. A future change
that lets a value cross it would not look like a bug in review — it would look
like a convenient extra column.
"""
import inspect

from app.services.test_factory import field_learning as fl


# ── the value-free guarantee ─────────────────────────────────────────────────

def test_the_shared_priors_table_has_no_column_a_value_could_go_in():
    """THE ENFORCEMENT. Not a rule to remember — an absence. A future writer cannot
    leak through a column that does not exist."""
    cols = set(fl.FieldPriorRow.__table__.columns.keys())
    assert cols == {"signature", "semantic_type", "signature_version",
                    "tenant_count", "observations", "accepted", "rejected",
                    "first_seen", "last_seen"}
    for forbidden in ("value", "value_blob", "blob", "content", "data",
                      "label", "field_label", "text", "sample"):
        assert forbidden not in cols


def test_the_shared_priors_table_cannot_identify_a_client():
    cols = set(fl.FieldPriorRow.__table__.columns.keys())
    for forbidden in ("tenant_id", "artifact_id", "app_id", "url", "user_id"):
        assert forbidden not in cols


def test_observe_prior_has_no_parameter_a_value_could_travel_through():
    """Belt and braces: even if a column appeared, there is no way to pass one."""
    params = set(inspect.signature(fl.observe_prior).parameters)
    assert params == {"session", "tenant_id", "signature", "semantic_type",
                      "signature_version", "accepted"}


def test_the_contributor_table_holds_a_hash_not_a_tenant():
    """Counting distinct tenants must not mean being able to name one."""
    cols = set(fl.FieldPriorContributorRow.__table__.columns.keys())
    assert "tenant_id" not in cols
    assert "tenant_hash" in cols
    assert fl._tenant_hash("acme") != "acme"
    assert fl._tenant_hash("acme") == fl._tenant_hash("acme")
    assert fl._tenant_hash("acme") != fl._tenant_hash("acme2")


def test_the_value_free_guard_catches_a_leak_before_it_is_distributed():
    assert fl.priors_are_value_free({"a" * 32: {"type": "email", "confidence": 0.8}})
    for leaky in ({"a" * 32: {"type": "email", "value": "x@y.z"}},
                  {"a" * 32: {"type": "email", "sample": "1990-01-01"}},
                  {"a" * 32: {"type": "not_a_real_type"}},
                  {"a" * 32: "just-a-string"},
                  "not-a-map"):
        assert fl.priors_are_value_free(leaky) is False


# ── the cage on the stored type ──────────────────────────────────────────────

def test_only_the_closed_vocabulary_is_ever_written():
    """The one free-form string column on the shared table must not become a
    channel. Whatever proposed it — a model, a row, a future caller — only a
    vocabulary member survives."""
    for forged in ("'; drop table field_priors; --", "849-22-7710", "admin",
                   "<script>", "", None, "EMAIL ADDRESS OF USER"):
        assert fl._coerce_type(forged) == "unknown"
    assert fl._coerce_type("EMAIL") == "email"
    assert fl._coerce_type("date-of-birth") == "date_of_birth"


def test_the_two_vocabularies_cannot_drift_apart():
    """The explorer and the store deploy independently and duplicate the list on
    purpose. If they diverge, a type one side writes is unreadable by the other.

    Read, not imported: both services name their package `app`, so importing the
    explorer's would either resolve to this one or poison sys.modules for every
    test that runs after — a failure mode this repo has already paid for once."""
    import ast as _ast
    import pathlib
    src = (pathlib.Path(__file__).resolve().parents[3]
           / "engines" / "qe-explorer" / "app" / "field_semantics.py").read_text(encoding="utf-8")
    tree = _ast.parse(src)
    consts = {}
    for node in tree.body:
        if isinstance(node, _ast.Assign) and isinstance(node.value, _ast.Constant):
            for t in node.targets:
                if isinstance(t, _ast.Name):
                    consts[t.id] = node.value.value
        if (isinstance(node, _ast.Assign)
                and isinstance(node.value, _ast.Call)
                and getattr(node.value.func, "id", "") == "frozenset"
                and getattr(node.targets[0], "id", "") == "VOCABULARY"):
            members = {consts.get(getattr(e, "id", ""), getattr(e, "value", None))
                       for e in node.value.args[0].elts}
            assert members == set(fl.VOCABULARY), members ^ set(fl.VOCABULARY)
            return
    raise AssertionError("VOCABULARY not found in the explorer's field_semantics")


# ── confidence: breadth of agreement, not volume of noise ────────────────────

class _Prior:
    def __init__(self, tenants=0, obs=1, acc=0, rej=0):
        self.tenant_count, self.observations = tenants, obs
        self.accepted, self.rejected = acc, rej


def test_one_client_crawling_repeatedly_is_still_one_opinion():
    """Otherwise a single noisy tenant could establish a false prior for everyone."""
    loud = fl._confidence(_Prior(tenants=1, obs=50))
    broad = fl._confidence(_Prior(tenants=20, obs=20))
    assert broad > loud


def test_the_applications_own_rejections_lower_confidence():
    """The app is the only participant here that actually knows."""
    good = fl._confidence(_Prior(tenants=5, obs=10, acc=10, rej=0))
    bad = fl._confidence(_Prior(tenants=5, obs=10, acc=0, rej=10))
    assert good > bad


def test_confidence_never_reaches_certainty():
    """A learned prior is always a guess. Certainty belongs to what the app
    declared about itself."""
    assert fl._confidence(_Prior(tenants=999, obs=999, acc=999)) <= 0.95


# ── consent ──────────────────────────────────────────────────────────────────

def test_contributing_is_off_before_anyone_has_decided():
    """A regulated client must be able to use the product without their field
    shapes reaching a shared table, and the safe default is the one that applies
    before anyone has chosen."""
    src = inspect.getsource(fl.get_consent)
    assert '"contribute": False' in src
    assert '"consume": True' in src


def test_a_tenant_that_has_not_opted_in_contributes_nothing():
    src = inspect.getsource(fl.observe_prior)
    assert 'if not consent["contribute"]:' in src
    assert src.index("get_consent") < src.index("pg_insert(FieldPriorRow)")


def test_an_unknown_reading_is_never_stored():
    """Recording it would let a signature accumulate confidence in having no
    meaning, which is worse than having no prior at all."""
    src = inspect.getsource(fl.observe_prior)
    assert 'sem == "unknown"' in src


# ── memory: encryption, scoping, and the stale-value trap ────────────────────

def test_a_value_is_never_stored_in_plaintext():
    src = inspect.getsource(fl.remember)
    assert "if envelope is None:" in src
    assert "refusing to store a field value in plaintext" in src
    assert "envelope.encrypt(" in src


def test_the_ciphertext_is_bound_to_one_tenant_artifact_and_field():
    """A blob lifted from one row must not be decryptable in another's context."""
    a = fl._aad("t1", "art1", "sig1")
    for other in (fl._aad("t2", "art1", "sig1"), fl._aad("t1", "art2", "sig1"),
                  fl._aad("t1", "art1", "sig2")):
        assert a != other


def test_only_a_client_provided_value_is_ever_remembered():
    """A synthesized value regenerates identically from the identity seed every
    crawl, so storing it would add a real row holding real-looking personal data
    for no benefit whatsoever."""
    src = inspect.getsource(fl.remember)
    assert 'provenance="provided"' in src


def test_rewriting_a_value_resets_its_history():
    """The accept/reject record described the string it replaced. Carrying it over
    would make the new value inherit a proof it never earned."""
    src = inspect.getsource(fl.remember)
    assert '"accept_count": 0' in src and '"reject_count": 0' in src


def test_a_value_the_application_keeps_rejecting_stops_being_offered():
    """THE STALE-VALUE TRAP. A remembered wrong answer is worse than no answer,
    because it looks like an answer — and a suite goes green having exercised the
    application as the wrong person. That has happened on this product before."""
    src = inspect.getsource(fl.recall)
    assert "reject_count > r.accept_count" in src
    assert "continue" in src


def test_a_crawl_survives_a_memory_it_cannot_read():
    """Degraded, never broken: fill what we can, ask for the rest."""
    src = inspect.getsource(fl.recall)
    assert "return {}" in src
    assert "except Exception" in src


def test_a_client_can_make_us_forget():
    assert "delete" in inspect.getsource(fl.forget).lower()


def test_memory_is_bounded():
    """Past a point the crawl is learning noise, not answers."""
    assert fl.MAX_MEMORIES_PER_ARTIFACT > 0
    assert "MAX_MEMORIES_PER_ARTIFACT" in inspect.getsource(fl.remember)
    assert "MAX_VALUE_BYTES" in inspect.getsource(fl.remember)


# ── the endpoints ────────────────────────────────────────────────────────────

_ROUTER = open("app/routers/test_factory.py", encoding="utf-8").read()
_NEXT = chr(10) + "async def "


def _handler(name):
    i = _ROUTER.index("async def %s(" % name)
    nxt = _ROUTER.find(_NEXT, i + 10)
    return _ROUTER[i:nxt if nxt > 0 else len(_ROUTER)]


def test_the_resolution_endpoint_refuses_to_distribute_a_leak():
    """If priors ever stop being value-free, hand out nothing rather than
    distribute them. The check costs nothing; the failure is unrecoverable."""
    seg = _handler("field_resolution_endpoint")
    assert "priors_are_value_free(priors)" in seg
    assert "priors = {}" in seg


def test_the_resolution_endpoint_keeps_the_two_kinds_apart():
    """Separate keys because they carry entirely different risk and a caller must
    never conflate them."""
    seg = _handler("field_resolution_endpoint")
    assert '"recalled_values": recalled' in seg
    assert '"field_priors": priors' in seg


def test_saving_an_answer_requires_a_write_role_and_encryption():
    seg = _handler("remember_field_answers_endpoint")
    assert "_persona_write_ok(user)" in seg
    assert "503" in seg


def test_the_memory_listing_never_returns_a_value():
    seg = _handler("list_field_memory_endpoint")
    assert "list_memories(" in seg
    src = inspect.getsource(fl.list_memories)
    assert "value_blob" not in src and "decrypt" not in src


def test_every_learning_endpoint_is_artifact_scoped():
    for name in ("remember_field_answers_endpoint", "list_field_memory_endpoint",
                 "forget_field_memory_endpoint", "field_resolution_endpoint",
                 "field_outcome_endpoint"):
        assert "_require_artifact(session, artifact_id, tenant_id)" in _handler(name), name
