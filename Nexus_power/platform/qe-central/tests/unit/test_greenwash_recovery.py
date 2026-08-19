"""M1.7 — qe-central's half of the green-wash closure (T-GW-02 … T-GW-05).

The engine can only refuse to LIE about a crawl.  It is qe-central that decides
what happens to the claim: whether a failed crawl is allowed to write substrate,
whether a lost callback is recoverable, whether a resume reaches the worker under
the identity it needs, and whether learning survives the crawl that produced it.

These tests cover that half.  They are deliberately DB-free — the rows and
sessions are exercised by ``tests/contract/`` under a real Postgres — so the
decision logic is provable in every environment, including the ones where the
contract suite skips.
"""
from __future__ import annotations

import json

import pytest

from app.controlplane import completion_recovery, reaper
from app.routers.internal import CompletionCallback, _disposition_of
from app.services import rule_store


def _callback(**overrides) -> CompletionCallback:
    body = {"tenant_id": "t1", "exploration_id": "e1", "crawl_id": "c1"}
    body.update(overrides)
    return CompletionCallback.model_validate(body)


# ══════════════════════════════════════════════════════════════════════════════
# The disposition contract — a failure on the engine is a failure here
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("stop_reason", [
    "inventory_failed", "resume_unrecoverable", "no_evidence", "error", "auth_failed",
])
def test_a_failed_stop_reason_never_reads_as_completed(stop_reason):
    assert _disposition_of(_callback(stop_reason=stop_reason)) == "failed"


@pytest.mark.parametrize("stop_reason", [
    "completed", "budget_max_states", "budget_max_wall_ms", "budget_max_requests",
])
def test_a_covered_crawl_completes(stop_reason):
    assert _disposition_of(_callback(stop_reason=stop_reason)) == "completed"


@pytest.mark.parametrize("stop_reason", ["cancelled", "auth_required_no_credentials"])
def test_an_honest_stop_is_incomplete_not_failed(stop_reason):
    """A crawl the operator cancelled, or one that hit a login wall with no
    credentials, did not FAIL — but it did not cover what it set out to cover
    either.  Collapsing either into a neighbouring word sends the operator after
    the wrong remediation."""
    assert _disposition_of(_callback(stop_reason=stop_reason)) == "incomplete"


def test_the_explorers_own_adjudication_wins():
    """A NEWER explorer adjudicated against evidence this service cannot see —
    the inventory-failure count, the resumed-state count.  Re-deriving the
    judgement here would mean two mappings that drift."""
    assert _disposition_of(
        _callback(stop_reason="completed", disposition="failed")) == "failed"


def test_an_unclassified_reason_fails_closed():
    assert _disposition_of(_callback(stop_reason="something_new")) == "failed"
    assert _disposition_of(_callback(disposition="not-a-disposition",
                                     stop_reason="error")) == "failed"


def test_the_two_services_agree_on_what_counts_as_a_failure():
    """THE SHARED CONTRACT.  qe-explorer and qe-central ship no common library,
    so ``FAILED_STOP_REASONS`` exists twice.  A new failure reason added on one
    side and not the other would be invented by the engine and read as a success
    here — which is the whole class of bug this milestone closes.
    """
    import ast
    import pathlib

    source = (pathlib.Path(__file__).resolve().parents[4]
              / "engines" / "qe-explorer" / "app" / "crawl_constants.py")
    if not source.is_file():                        # pragma: no cover
        pytest.skip("qe-explorer checkout not present next to qe-central")

    # Read the constant out of the SOURCE rather than importing the module:
    # ``crawl_constants`` pulls in the whole explorer package, which is a
    # different service with its own dependencies. Parsing is enough — the
    # contract is about the literal set of strings, and a test that needed the
    # other service installed would simply be skipped in CI, which is how a
    # cross-service contract quietly stops being checked.
    tree = ast.parse(source.read_text(encoding="utf-8"))
    explorer_reasons: set[str] | None = None
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(t, ast.Name) and t.id == "FAILED_STOP_REASONS"
                   for t in node.targets):
            continue
        # ``frozenset({STOP_ERROR, ...})`` — resolve each Name to its own
        # module-level string literal.
        literals = {}
        for other in ast.walk(tree):
            if not isinstance(other, ast.Assign):
                continue
            if not isinstance(other.value, ast.Constant):
                continue
            if not isinstance(other.value.value, str):
                continue
            for target in other.targets:
                if isinstance(target, ast.Name):
                    literals[target.id] = other.value.value
        members = node.value.args[0].elts        # the set inside frozenset(...)
        explorer_reasons = {literals[m.id] for m in members if isinstance(m, ast.Name)}
        break

    assert explorer_reasons, "FAILED_STOP_REASONS not found in the explorer source"
    from app.routers.internal import _FAILED_STOP_REASONS
    assert explorer_reasons == set(_FAILED_STOP_REASONS), (
        "the two services disagree about what counts as a failed crawl: "
        f"explorer={sorted(explorer_reasons)} qe-central={sorted(_FAILED_STOP_REASONS)}")


# ══════════════════════════════════════════════════════════════════════════════
# T-GW-02 — orphan detection and recovery
# ══════════════════════════════════════════════════════════════════════════════


def _volume(tmp_path, monkeypatch):
    monkeypatch.setattr(completion_recovery.phase1_settings, "crawl_storage_root",
                        str(tmp_path), raising=False)
    return tmp_path


def test_a_completion_with_no_ack_is_an_orphan(tmp_path, monkeypatch):
    _volume(tmp_path, monkeypatch)
    (tmp_path / "c1").mkdir()
    body = {"crawl_id": "c1", "tenant_id": "t1", "exploration_id": "e1"}
    (tmp_path / "c1" / completion_recovery.COMPLETION_FILENAME).write_text(
        json.dumps(body), encoding="utf-8")

    assert completion_recovery.read_orphaned_completion("c1") == body

    completion_recovery.mark_acknowledged("c1")
    assert completion_recovery.read_orphaned_completion("c1") is None


def test_a_crawl_that_never_finished_has_nothing_to_recover(tmp_path, monkeypatch):
    """The distinction the reaper turns on: no completion record means the crawl
    really did die mid-walk, and ``stalled`` is the honest answer."""
    _volume(tmp_path, monkeypatch)
    (tmp_path / "c1").mkdir()
    (tmp_path / "c1" / "manifest.jsonl").write_text('{"type":"page_state"}\n')

    assert completion_recovery.read_orphaned_completion("c1") is None


def test_a_corrupt_completion_is_treated_as_absent(tmp_path, monkeypatch):
    _volume(tmp_path, monkeypatch)
    (tmp_path / "c1").mkdir()
    (tmp_path / "c1" / completion_recovery.COMPLETION_FILENAME).write_text("{oops")

    assert completion_recovery.read_orphaned_completion("c1") is None


@pytest.mark.parametrize("crawl_id", ["../etc", "a/b", "..", "", "a\\b"])
def test_a_crawl_id_can_never_escape_the_storage_root(crawl_id, tmp_path, monkeypatch):
    _volume(tmp_path, monkeypatch)
    assert completion_recovery.read_orphaned_completion(crawl_id) is None


@pytest.mark.asyncio
async def test_recovery_refuses_to_deliver_unsigned(tmp_path, monkeypatch):
    """FAIL-CLOSED.  With no fleet secret there is no way to authenticate the
    re-delivery, and an unsigned POST would simply be refused — spending a
    request to discover what is already known."""
    _volume(tmp_path, monkeypatch)
    monkeypatch.setattr(completion_recovery.phase1_settings, "explorer_token", "",
                        raising=False)
    assert await completion_recovery.redeliver_completion("c1", {"crawl_id": "c1"}) is False


@pytest.mark.asyncio
async def test_a_reconcilable_row_is_not_reaped(tmp_path, monkeypatch):
    """T-GW-02 ACCEPTANCE, at the decision point.

    A stale row whose crawl left a durable completion must be RECONCILED, not
    marked ``stalled`` — the crawl did not stall, it finished and lost one POST.
    """
    _volume(tmp_path, monkeypatch)
    (tmp_path / "c1").mkdir()
    (tmp_path / "c1" / completion_recovery.COMPLETION_FILENAME).write_text(
        json.dumps({"crawl_id": "c1", "tenant_id": "t1", "exploration_id": "e1"}))

    delivered: list = []

    async def _fake_redeliver(crawl_id, body):
        delivered.append((crawl_id, body))
        return True

    monkeypatch.setattr(completion_recovery, "redeliver_completion", _fake_redeliver)

    row = {"exploration_id": "e1", "stats": {"crawl_id": "c1"}}
    assert await reaper._reconcile_from_manifest(row) is True
    assert delivered and delivered[0][0] == "c1"


@pytest.mark.asyncio
async def test_a_row_with_no_durable_completion_is_reaped(tmp_path, monkeypatch):
    _volume(tmp_path, monkeypatch)
    row = {"exploration_id": "e1", "stats": {"crawl_id": "c1"}}
    assert await reaper._reconcile_from_manifest(row) is False


@pytest.mark.asyncio
async def test_a_recovery_failure_degrades_to_the_old_behaviour(tmp_path, monkeypatch):
    """A reconciliation that cannot run must leave the row to be reaped — never
    leave it un-terminalized and spinning in the UI forever."""
    _volume(tmp_path, monkeypatch)
    (tmp_path / "c1").mkdir()
    (tmp_path / "c1" / completion_recovery.COMPLETION_FILENAME).write_text(
        json.dumps({"crawl_id": "c1", "tenant_id": "t1", "exploration_id": "e1"}))

    async def _boom(crawl_id, body):
        raise RuntimeError("the network is on fire")

    monkeypatch.setattr(completion_recovery, "redeliver_completion", _boom)
    assert await reaper._reconcile_from_manifest(
        {"exploration_id": "e1", "stats": {"crawl_id": "c1"}}) is False


@pytest.mark.asyncio
async def test_a_pre_liveness_row_without_a_crawl_id_is_reaped(monkeypatch):
    assert await reaper._reconcile_from_manifest({"exploration_id": "e1",
                                                  "stats": {}}) is False


# ══════════════════════════════════════════════════════════════════════════════
# T-GW-04 — the rule store's validation and metrics (DB-free half)
# ══════════════════════════════════════════════════════════════════════════════


def test_a_rule_fragment_is_refused():
    """A rule missing its key, its blocked control or its field is not a rule —
    storing it would put an un-lookup-able row in a table whose whole purpose is
    lookup."""
    assert rule_store._clean({"key": "rule:1", "blocked_label": "Continue"}) is None
    assert rule_store._clean({"blocked_label": "Continue", "field_label": "None"}) is None
    assert rule_store._clean("not a mapping") is None
    assert rule_store._clean({"key": "rule:1", "blocked_label": "Continue",
                              "field_label": "None"}) is not None


def test_incoming_rule_strings_are_bounded_to_their_columns():
    """The explorer holds the fleet secret AND runs untrusted application
    JavaScript in the same container, so it is a semi-trusted sender: every
    string it hands us is truncated to its column width rather than trusted."""
    cleaned = rule_store._clean({
        "key": "k" * 200, "blocked_label": "b" * 400, "field_label": "f" * 400,
        "proof": "p" * 2000, "url_template": "u" * 2000, "kind": "x" * 100,
    })
    assert len(cleaned["rule_key"]) == 64
    assert len(cleaned["blocked_label"]) == 120
    assert len(cleaned["field_label"]) == 120
    assert len(cleaned["proof"]) == 500
    assert len(cleaned["url_template"]) == 500
    assert len(cleaned["kind"]) == 32


def test_the_row_id_is_derived_and_stable():
    """Derived so an UPSERT can name its own conflict target without a round
    trip, and so the same rule is the same row in every environment."""
    first = rule_store._row_id("t1", "a1", "rule:abc")
    assert first == rule_store._row_id("t1", "a1", "rule:abc")
    assert first != rule_store._row_id("t2", "a1", "rule:abc")
    assert first != rule_store._row_id("t1", "a2", "rule:abc")


@pytest.mark.asyncio
async def test_persisting_nothing_touches_no_database():
    """Cheap guard on the hot path: a crawl that proved no rules — which is most
    crawls — must not open a session to say so."""
    assert await rule_store.persist_rules("t1", "a1", []) == 0
    assert await rule_store.persist_rules("", "a1", [{"key": "k"}]) == 0
    assert await rule_store.fetch_rules("t1", "") == []


def test_reuse_metrics_are_read_from_one_place():
    assert rule_store.reuse_metrics({"rule_reuse": {"known": 4, "hits": 3, "misses": 1}}) == {
        "rules_known": 4, "rule_lookups": 4, "rules_reused": 3, "rule_reuse_rate": 0.75}
    # A crawl that met no blocked advance gets 0.0, never a flattering 1.0.
    assert rule_store.reuse_metrics({})["rule_reuse_rate"] == 0.0
    assert rule_store.reuse_metrics(None)["rule_lookups"] == 0
    assert rule_store.reuse_metrics({"rule_reuse": {"hits": "junk"}})["rules_reused"] == 0
