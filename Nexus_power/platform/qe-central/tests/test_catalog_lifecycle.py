"""M2.3 — the question lifecycle, stated as rules.

The milestone's PROOF is a real application change re-crawled end to end
(``tests/contract/test_m23_retirement_regression.py``, driven by two real
Chromium crawls). This module is the complement, not the proof: it pins the
edges that one real change cannot exercise — the transient crawl, the revival,
the question that lives on two pages and loses one, the malformed row.

Every one of them is a way the lifecycle could lie:

  * retire on a crawl that never looked → a catalogue that deletes questions the
    application still asks;
  * fail to revive → a question the application brought back stays retired for
    ever, and the "removed" report becomes permanently wrong;
  * retire a question still asked elsewhere → the same lie, one page over.

Pure: plain dicts in, verdicts out. No DB, no crawl.
"""
from __future__ import annotations

from app.services.catalog import (
    LIFECYCLE_ACTIVE,
    LIFECYCLE_RETIRED,
    LIFECYCLE_STALE,
    RETIREMENT_MISS_THRESHOLD,
    apply_control_lifecycle,
    build_master_catalog,
    control_lifecycle_state,
    crawl_evidence,
    observed_question_ids,
    question_id_for,
)

NOW = "2026-08-19T12:00:00+00:00"
LATER = "2026-08-20T12:00:00+00:00"


def _ctrl(name: str, **extra):
    c = {"name": name, "type": "text", "options": [], "required": False}
    c["question_id"] = question_id_for(c)
    c.update(extra)
    return c


def _conclusive_coverage(**over):
    cov = {
        "states": [{"ax_fingerprint": "fp1", "location": "u",
                    "form_snapshot_signals": {}}],
        "flows": [{"steps": []}],
        "flow_summary": {"advances_by_tier": {}},
        "inventory_failures": 0,
        "auth_blocked": False,
    }
    cov.update(over)
    return cov


def _catalog(controls, **node):
    n = {"node_fp": "fp1", "url": "http://a.test/p", "controls_inventory": controls}
    n.update(node)
    return build_master_catalog([n], **({}))


# ── T-ST-01 · MARK STALE ─────────────────────────────────────────────────────

def test_a_question_the_crawl_looked_for_and_did_not_find_is_marked():
    inv = [_ctrl("Email"), _ctrl("Beneficiary")]
    out = apply_control_lifecycle(
        inv, {inv[0]["question_id"]}, crawl_ref="c2", now_iso=NOW,
        conclusive=True)
    by_name = {c["name"]: c for c in out}
    assert by_name["Email"]["stale"] is False
    assert by_name["Beneficiary"]["stale"] is True


def test_the_record_is_never_deleted():
    """The first requirement: a question must never silently disappear."""
    inv = [_ctrl("Email"), _ctrl("Beneficiary", options=["a", "b"], required=True)]
    out = apply_control_lifecycle(
        inv, set(), crawl_ref="c2", now_iso=NOW, conclusive=True)
    assert len(out) == 2, "an unobserved control was dropped from the inventory"
    gone = next(c for c in out if c["name"] == "Beneficiary")
    assert gone["options"] == ["a", "b"] and gone["required"] is True, (
        "retirement destroyed the content it exists to preserve")


# ── T-ST-02 · RETIREMENT STATE ───────────────────────────────────────────────

def test_a_conclusive_absence_retires_with_its_timestamp_and_crawl():
    inv = [_ctrl("Beneficiary")]
    out = apply_control_lifecycle(
        inv, set(), crawl_ref="crawl-42", now_iso=NOW, conclusive=True)[0]
    assert control_lifecycle_state(out) == LIFECYCLE_RETIRED
    assert out["retired_at"] == NOW
    assert out["retired_in_crawl"] == "crawl-42"
    assert out["retire_reason"] == "conclusive_absence"
    assert out["question_id"] == inv[0]["question_id"], "the historical id moved"


def test_the_first_retirement_keeps_its_date_across_later_crawls():
    """An auditor asking when the application stopped is owed the crawl that
    ESTABLISHED it, not the most recent one to agree."""
    out = apply_control_lifecycle(
        [_ctrl("Beneficiary")], set(), crawl_ref="c2", now_iso=NOW,
        conclusive=True)
    again = apply_control_lifecycle(
        out, set(), crawl_ref="c3", now_iso=LATER, conclusive=True)[0]
    assert again["retired_at"] == NOW and again["retired_in_crawl"] == "c2"
    assert again["missed_crawls"] == 2, "the evidence trail stopped counting"


def test_a_retired_question_leaves_the_active_catalogue_and_stays_in_the_audit():
    retired = apply_control_lifecycle(
        [_ctrl("Email"), _ctrl("Beneficiary")],
        {question_id_for({"name": "Email"})},
        crawl_ref="c2", now_iso=NOW, conclusive=True)

    active = _catalog(retired)
    audit = build_master_catalog(
        [{"node_fp": "fp1", "url": "http://a.test/p", "controls_inventory": retired}],
        include_retired=True)

    assert [q["name"] for q in active["questions"]] == ["Email"]
    assert active["summary"]["retired_count"] == 1, (
        "the active catalogue does not declare what is being withheld from it")
    audited = {q["name"]: q for q in audit["questions"]}
    assert set(audited) == {"Email", "Beneficiary"}
    assert audited["Beneficiary"]["lifecycle"] == LIFECYCLE_RETIRED
    assert audited["Beneficiary"]["retired_at"] == NOW
    assert audited["Beneficiary"]["retired_in_crawl"] == "c2"


# ── T-ST-03 · A TRANSIENT CRAWL MAY NOT RETIRE ───────────────────────────────

def test_a_crawl_that_could_not_read_a_page_is_not_conclusive():
    assert crawl_evidence(_conclusive_coverage())["conclusive"] is True
    for over, reason in (
        ({"states": []}, "no_states"),
        ({"flows": []}, "no_flows"),
        ({"auth_blocked": True}, "auth_blocked"),
        ({"inventory_failures": 2}, "inventory_failures"),
        ({"flow_summary": {}}, "pre_hardening"),
    ):
        verdict = crawl_evidence(_conclusive_coverage(**over))
        assert verdict["conclusive"] is False, f"{over} should not be conclusive"
        assert reason in verdict["reason"]
    assert crawl_evidence(None)["conclusive"] is False


def test_auth_incomplete_alone_does_not_make_a_crawl_inconclusive():
    """Measured, not assumed — see ``crawl_evidence``. acme-life keeps its login
    in sessionStorage and reports ``auth_incomplete`` on every crawl while still
    walking the whole funnel; treating that as inconclusive would make retirement
    impossible for most single-page applications."""
    cov = _conclusive_coverage(auth_incomplete=True, auth_reason="not_persisted")
    assert crawl_evidence(cov)["conclusive"] is True


def test_one_inconclusive_crawl_marks_stale_but_does_not_retire():
    """THE STOP CONDITION'S RULE: not removed on one transient failure."""
    out = apply_control_lifecycle(
        [_ctrl("Beneficiary")], set(), crawl_ref="c2", now_iso=NOW,
        conclusive=False)[0]
    assert control_lifecycle_state(out) == LIFECYCLE_STALE
    assert out["stale"] is True
    assert out.get("retired_at") is None
    # …and it is still in the ACTIVE catalogue, so nothing stops planning
    # against it on the strength of one bad crawl.
    active = _catalog([out])
    assert [q["name"] for q in active["questions"]] == ["Beneficiary"]
    assert active["summary"]["stale_count"] == 1


def test_repeated_inconclusive_absence_eventually_retires():
    """No single degraded crawl is trusted; their agreement is what carries."""
    entry = _ctrl("Beneficiary")
    out = [entry]
    for i in range(RETIREMENT_MISS_THRESHOLD):
        out = apply_control_lifecycle(
            out, set(), crawl_ref=f"c{i}", now_iso=NOW, conclusive=False)
        assert out[0]["missed_crawls"] == i + 1
    assert control_lifecycle_state(out[0]) == LIFECYCLE_RETIRED
    assert out[0]["retire_reason"] == "repeated_absence"


# ── REVIVAL ──────────────────────────────────────────────────────────────────

def test_an_application_that_asks_again_revives_the_question():
    """Retirement is a record, not a tombstone. Without this a question the
    application restored would stay 'removed' for ever and the report would be
    permanently wrong."""
    qid = question_id_for({"name": "Beneficiary"})
    retired = apply_control_lifecycle(
        [_ctrl("Beneficiary")], set(), crawl_ref="c2", now_iso=NOW,
        conclusive=True)
    revived = apply_control_lifecycle(
        retired, {qid}, crawl_ref="c3", now_iso=LATER, conclusive=True)[0]
    assert control_lifecycle_state(revived) == LIFECYCLE_ACTIVE
    assert revived["stale"] is False
    assert "retired_at" not in revived and "retire_reason" not in revived
    assert revived["missed_crawls"] == 0
    assert revived["last_seen_crawl"] == "c3"
    assert [q["name"] for q in _catalog([revived])["questions"]] == ["Beneficiary"]


# ── AGGREGATION ACROSS PAGES ─────────────────────────────────────────────────

def test_a_question_still_asked_on_another_page_is_not_retired():
    """A question is only as retired as its LAST surviving sighting."""
    gone_here = apply_control_lifecycle(
        [_ctrl("Beneficiary")], set(), crawl_ref="c2", now_iso=NOW,
        conclusive=True)
    still_there = [_ctrl("Beneficiary")]
    master = build_master_catalog([
        {"node_fp": "fp1", "url": "http://a.test/apply",
         "controls_inventory": gone_here},
        {"node_fp": "fp2", "url": "http://a.test/review",
         "controls_inventory": still_there},
    ])
    q = master["questions"][0]
    assert q["lifecycle"] == LIFECYCLE_ACTIVE
    assert q["pages"] == ["http://a.test/review"], (
        "a page the question has LEFT is still being reported as a page it is "
        "on; a client following it would be sent to a screen without it")


def test_a_question_retired_on_every_page_takes_the_latest_retirement_date():
    a = apply_control_lifecycle([_ctrl("Beneficiary")], set(), crawl_ref="c2",
                                now_iso=NOW, conclusive=True)
    b = apply_control_lifecycle([_ctrl("Beneficiary")], set(), crawl_ref="c5",
                                now_iso=LATER, conclusive=True)
    master = build_master_catalog([
        {"node_fp": "fp1", "url": "http://a.test/apply", "controls_inventory": a},
        {"node_fp": "fp2", "url": "http://a.test/review", "controls_inventory": b},
    ], include_retired=True)
    q = master["questions"][0]
    assert q["lifecycle"] == LIFECYCLE_RETIRED
    assert q["retired_at"] == LATER and q["retired_in_crawl"] == "c5", (
        "the retirement date must be the moment the application stopped asking "
        "ANYWHERE — the last page to drop it, not the first")


# ── OBSERVATION IDENTITY ─────────────────────────────────────────────────────

def test_observed_ids_are_the_same_identity_the_catalogue_is_built_with():
    """If these two ever disagreed, retirement would fire on questions that
    never moved — the id space is the whole safety property."""
    state = {"ax_fingerprint": "fp1", "location": "http://a.test/p",
             "form_snapshot_signals": {
                 "Email": {"type": "email", "required": True},
                 "State": {"type": "select", "options": ["CA", "NY"]}}}
    observed = observed_question_ids(state)
    catalogued = {q["question_id"] for q in build_master_catalog([{
        "node_fp": "fp1", "url": "http://a.test/p",
        "controls_inventory": [
            {"name": "Email", "type": "email", "required": True},
            {"name": "State", "type": "select", "options": ["CA", "NY"]}],
    }])["questions"]}
    assert observed == catalogued


# ── TOLERANCE ────────────────────────────────────────────────────────────────

def test_a_malformed_inventory_costs_nothing_but_itself():
    out = apply_control_lifecycle(
        [None, "junk", _ctrl("Email"), {"name": "Loose"}],
        set(), crawl_ref="c2", now_iso=NOW, conclusive=True)
    assert [c["name"] for c in out] == ["Email", "Loose"]
    assert all(c["question_id"] for c in out), "a row was left without an identity"


def test_an_unreadable_missed_count_restarts_rather_than_raising():
    out = apply_control_lifecycle(
        [_ctrl("Beneficiary", missed_crawls="not a number")],
        set(), crawl_ref="c2", now_iso=NOW, conclusive=False)[0]
    assert out["missed_crawls"] == 1


def test_a_build_with_no_lifecycle_at_all_is_entirely_active():
    """Every row written before M2.3 carries no lifecycle keys. They are what
    they always were — active — and the summary says so."""
    master = _catalog([{"name": "Email", "type": "text"},
                       {"name": "State", "type": "select"}])
    assert all(q["lifecycle"] == LIFECYCLE_ACTIVE for q in master["questions"])
    assert master["summary"]["retired_count"] == 0
    assert master["summary"]["stale_count"] == 0
    assert master["summary"]["active_count"] == 2
