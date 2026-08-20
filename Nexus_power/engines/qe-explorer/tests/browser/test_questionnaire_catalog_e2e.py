"""M2.1 — THE QUESTIONNAIRE PROVING GROUND, CRAWLED AND CATALOGUED.

The stop condition for this milestone is not that unit tests pass. It is that a
REAL crawl of a REAL application, driven through the production Chromium port and
the production :class:`app.crawler.Crawler`, produces a catalogue in which:

  * every question is worded the way the APPLICATION words it, or is honestly
    marked UNVERIFIED — never `"Question 3"`, which no element on any page has
    ever contained;
  * a choice group is ONE question carrying its answers, not one question per
    answer plus a duplicate minted from its own branch rows;
  * the same application re-crawled keeps the same ``question_id``;
  * answering the trigger activates the child question through the projector,
    analytically, with no third crawl.

Everything here runs against ``proving-grounds/questionnaire-life``. The crawl
half is the explorer's; the catalogue half is qe-central's ``catalog`` +
``journey_projector``, imported directly as the pure functions they are — the DB
seam between them (``journey_fold`` → ``catalog_store``) is proven against real
Postgres in the qe-central suite and is not re-proven here.

Two crawls run, and both are ordinary product behaviour:

  1. a BASE crawl — answers the questionnaire the way any crawl does (preferring
     the declining answer), which is what discovers the questions;
  2. a PLANNED RE-CRAWL — the same crawl with ``choice_overrides`` forcing the
     tobacco question to "Yes", exactly as ``branch_planner`` dispatches one to
     enumerate a branch nobody walked. That is the crawl that records what the
     "Yes" answer REVEALS.

The re-crawl keys its override on the question id the FIRST crawl recorded, so
the identity being stable across crawls is not asserted separately — the second
crawl cannot even be aimed without it.
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path
from typing import Any

import pytest

import _harness as H

pytestmark = [pytest.mark.browser, pytest.mark.playwright, pytest.mark.proving_ground]

PROVING_GROUNDS = H.SERVICE_ROOT.parent.parent / "proving-grounds"
GROUND = "questionnaire-life"
CRAWL_OUT = H.HERE / "_crawl_out"

#: BOTH SERVICES SHIP A TOP-LEVEL ``app`` PACKAGE, and in this process the
#: explorer's wins — so ``import app.services.catalog`` cannot reach qe-central's
#: no matter what is on ``sys.path``. The two catalogue modules are pure (stdlib
#: only, plus one relative import between them), so they are loaded under a
#: synthetic package name instead. Nothing is copied: these ARE the production
#: modules, read off disk, and a change to either is a change to what this lane
#: proves.
QE_CENTRAL = H.SERVICE_ROOT.parent.parent / "platform" / "qe-central"
_SERVICES_PKG = "_qec_central_services"


def _catalog_services():
    import importlib
    import types
    if _SERVICES_PKG not in sys.modules:
        pkg = types.ModuleType(_SERVICES_PKG)
        pkg.__path__ = [str(QE_CENTRAL / "app" / "services")]
        sys.modules[_SERVICES_PKG] = pkg
    catalog = importlib.import_module(_SERVICES_PKG + ".catalog")
    projector = importlib.import_module(_SERVICES_PKG + ".journey_projector")
    return catalog, projector


@pytest.fixture(scope="session")
def ground_server() -> Any:
    if not (PROVING_GROUNDS / GROUND / "index.html").exists():
        pytest.skip(f"{GROUND} not found under {PROVING_GROUNDS}")
    srv = H.FixtureServer(root=PROVING_GROUNDS).start()
    yield srv
    srv.stop()


def _crawl(pw, url: str, tag: str, choice_overrides: dict | None = None) -> dict:
    """One REAL crawl. Returns ``{"records": [...], "summary": {...}}``."""
    from app.auth import AuthWindow
    from app.crawl_constants import TRAVERSAL_FULL
    from app.crawler import Budget, Crawler, GuardContext
    from app.guard import load_refuse_pack
    from app.main import EXPLORER_VERSION, PlaywrightBrowserPort

    pack = load_refuse_pack(str(H.SERVICE_ROOT / "app" / "refuse_pack.yaml"))
    guard_ctx = GuardContext(
        refuse_pack=pack,
        auth_window=AuthWindow(max_requests=200, window_ms=120_000),
        # A disposable attestation is what lets the walk ANSWER the
        # questionnaire: answering is a fill, not a crossing, but it is gated on
        # traversal rights. Nothing here is submitted — ``submit_flow_approved``
        # stays False, so the boundary is observed and never crossed.
        attestation={"env_kind": "disposable", "attested_by": "m2.1-proving-ground",
                     "expires_at_ms": 4102444800000, "reset_procedure": "static file"},
        submit_flow_approved=False,
        idp_domains=frozenset(),
    )
    budget = Budget.from_dict({
        "max_states": 20, "max_actions": 120, "max_requests": 400,
        "max_duration_ms": 240_000,
    })
    work_dir = CRAWL_OUT / f"{GROUND}-{tag}"
    if work_dir.exists():
        shutil.rmtree(work_dir, ignore_errors=True)
    work_dir.mkdir(parents=True, exist_ok=True)

    crawl_id = f"pg-{GROUND}-{tag}"
    crawler = Crawler(
        PlaywrightBrowserPort(pw.page, pw.context),
        crawl_id=crawl_id, tenant_id="proving-ground", target_url=url,
        work_dir=str(work_dir), refuse_pack=pack, budget=budget,
        explorer_version=EXPLORER_VERSION, guard_version=EXPLORER_VERSION,
        refuse_pack_version=pack.version,
        config_fingerprint=f"m2.1-{tag}", guard_context=guard_ctx,
        identity_seed="qec-m2.1", traversal=TRAVERSAL_FULL,
        choice_overrides=dict(choice_overrides or {}),
    )
    summary = pw.run(crawler.run())

    manifest = work_dir / crawl_id / "manifest.jsonl"
    assert manifest.exists(), f"[{tag}] the crawl produced NO manifest at {manifest}"
    records = [json.loads(line) for line in
               manifest.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert records, f"[{tag}] the manifest is empty"
    # The coverage ledger is the crawl's own return value — the same object
    # ``qe-central`` receives on the completion callback and folds.
    return {"records": records, "summary": {"coverage": summary.coverage},
            "crawl_summary": summary, "work_dir": work_dir}


# ─── The fold, in the same shape journey_fold writes to the DB ────────────────

def _nodes_and_branches(catalog, crawls: list[dict]) -> tuple[list, list]:
    """Turn one or more crawl coverages into the node/branch rows the catalogue
    is built from — the SAME derivation ``journey_fold`` performs, minus the DB.

    Branch keying mirrors ``journey_fold`` exactly: ``group_id or
    control_signature``, and ``control_label_norm`` from the decision point's
    label, which since M2.1 is the QUESTION rather than one of its answers.
    """
    nodes: dict[str, dict] = {}
    branches: dict[tuple, dict] = {}
    for crawl in crawls:
        coverage = crawl["summary"].get("coverage") or {}
        states = catalog.build_states_index(coverage)
        ledger = catalog.build_ledger_by_url(coverage)
        for fp, state in states.items():
            controls = catalog.extract_controls(state, ledger)
            node = nodes.setdefault(
                fp, {"node_fp": fp, "url": state.get("location", ""),
                     "title": "", "controls": []})
            node["controls"] = catalog.merge_controls(node["controls"], controls)
        for flow in coverage.get("flows") or []:
            for step in flow.get("steps") or []:
                fp = str(step.get("fingerprint") or "")
                for dp in step.get("decision_points") or []:
                    sig = str(dp.get("group_id") or "") or str(
                        dp.get("control_signature") or "")
                    if not sig:
                        continue
                    label = str(dp.get("control_label") or "").strip().lower()
                    choice = str(dp.get("choice") or "").strip().lower()
                    reveals = [str(r) for r in (dp.get("reveals") or [])]
                    for opt in dp.get("options") or []:
                        o = str(opt).strip().lower()
                        if not o:
                            continue
                        row = branches.setdefault(
                            (fp, sig, o),
                            {"node_fp": fp, "control_signature": sig,
                             "control_label_norm": label, "option_label_norm": o,
                             "reveals": []})
                        if label:
                            row["control_label_norm"] = label
                        if o == choice and reveals:
                            for r in reveals:
                                if r not in row["reveals"]:
                                    row["reveals"].append(r)
    return list(nodes.values()), list(branches.values())


def _by_name(questions: list[dict]) -> dict[str, dict]:
    return {str(q.get("name") or ""): q for q in questions}


@pytest.fixture(scope="module")
def crawled(pw, ground_server) -> dict:
    """BASE crawl → find the tobacco question → PLANNED RE-CRAWL forcing 'Yes'.

    Module-scoped: two real Chromium crawls of a real application are the
    expensive part, and every assertion below reads the same evidence.
    """
    catalog, projector = _catalog_services()
    url = ground_server.url(GROUND)

    base = _crawl(pw, url, "base")
    base_nodes, base_branches = _nodes_and_branches(catalog, [base])
    base_master = catalog.build_master_catalog(
        base_nodes, edges=[], branches=base_branches)

    # The trigger question, found the way the branch planner finds one: by
    # reading the branch rows the FIRST crawl recorded. Its signature is the
    # override key, which is why a re-crawl is only aimable at all if identity
    # survived the first one.
    trigger_sig = ""
    for b in base_branches:
        if "tobacco" in str(b.get("control_label_norm") or ""):
            trigger_sig = str(b["control_signature"])
            break
    assert trigger_sig, (
        "the base crawl recorded no branch row for the tobacco question — "
        "there is nothing to plan a re-crawl against.\n"
        f"branch labels seen: {sorted({b['control_label_norm'] for b in base_branches})}")

    # A bare-button question's branch signature is ``q:<question id>`` and the
    # walker keys its override on exactly that.
    replay = _crawl(pw, url, "replay", choice_overrides={trigger_sig: "Yes"})

    nodes, branches = _nodes_and_branches(catalog, [base, replay])
    master = catalog.build_master_catalog(nodes, edges=[], branches=branches)
    rules = projector.rules_from_branches(branches, master["questions"])
    return {
        "catalog": catalog, "projector": projector,
        "base": base, "replay": replay,
        "base_master": base_master, "master": master,
        "branches": branches, "nodes": nodes, "rules": rules,
        "trigger_sig": trigger_sig,
    }


# ── T-QT-01 · the question, in the application's own words ────────────────────

def test_the_crawl_captures_the_real_question_wording(crawled):
    """Every `<legend>` on the page is a question in the catalogue, verbatim."""
    names = set(_by_name(crawled["master"]["questions"]))
    expected = {
        "Have you used tobacco or nicotine products in the last 12 months?",
        "Do you consume more than 14 units of alcohol per week?",
        "Do you take part in scuba diving, motorsport or private aviation?",
        "Gender",
        "Which product are you applying for?",       # radiogroup aria-label
        "Which of these have you been diagnosed with?",
    }
    missing = expected - names
    assert not missing, (
        "the application states these questions and the catalogue does not "
        f"carry them:\n  {sorted(missing)}\ncatalogued names were:\n  {sorted(names)}")


def test_no_question_is_named_by_an_invented_ordinal(crawled):
    """`"Question 1"` … `"Question N"` is the defect this milestone closes.

    Not a spelling preference: that text appears nowhere in the application, so
    a client reading the catalogue could not tell which row asked about tobacco,
    and a regression diff could not tell a REWORDED question from a REORDERED
    one."""
    import re
    invented = [q["name"] for q in crawled["master"]["questions"]
                if re.fullmatch(r"(?i)question\s*\d+", str(q.get("name") or "").strip())]
    assert not invented, f"catalogue still publishes invented wording: {invented}"


def test_a_question_the_application_words_nowhere_is_UNVERIFIED_not_invented(crawled):
    """Subject B: a declared question with no legend, no aria-label, no heading.

    It must be CATALOGUED (it is a real question with real answers), stably
    identified, and its wording marked missing rather than supplied."""
    cat = crawled["catalog"]
    unverified = [q for q in crawled["master"]["questions"]
                  if q.get("name_status") == cat.QUESTION_NAME_UNVERIFIED]
    assert unverified, (
        "the page contains a fieldset that states no wording at all; the "
        "catalogue reports every question as worded, which means something "
        "supplied text the application never had")
    for q in unverified:
        assert not str(q.get("name") or "").strip()
        assert q["question_id"], "an unverified question must still be identified"


def test_every_worded_question_says_which_dom_rung_worded_it(crawled):
    """Provenance for the wording itself: a reader can weigh a `<legend>`
    differently from a heading scraped out of a container."""
    cat = crawled["catalog"]
    worded = [q for q in crawled["master"]["questions"]
              if q.get("name_status") == cat.QUESTION_NAME_OBSERVED
              and q.get("source") == "question_group"]
    assert worded, "no grouped question came back worded"
    for q in worded:
        assert q.get("name_source") in ("legend", "aria-label", "aria-labelledby",
                                        "heading"), (
            f"{q['name']!r} claims wording from an undeclared rung: "
            f"{q.get('name_source')!r}")


# ── T-QT-02 · a choice group is ONE question ─────────────────────────────────

def test_a_radio_group_is_one_question_carrying_its_answers(crawled):
    """`Gender → [Female, Male, Prefer not to say]`.

    Before M2.1 this was THREE questions named after the answers, each offering
    none of them, plus a fourth minted from the group's own branch rows and
    named after whichever answer was seen first."""
    q = _by_name(crawled["master"]["questions"]).get("Gender")
    assert q is not None, "the Gender question is absent from the catalogue"
    assert {o.lower() for o in q["options"]} == {
        "female", "male", "prefer not to say"}, q["options"]
    assert q["type"] in ("radio", "choice"), q["type"]


def test_the_answers_are_not_questions_of_their_own(crawled):
    """No catalogue row is named after one of the answers to another row."""
    names = {str(q.get("name") or "").strip().lower()
             for q in crawled["master"]["questions"]}
    strays = names & {"female", "male", "prefer not to say", "term life",
                      "whole life", "universal life", "diabetes", "cancer",
                      "heart disease", "none of these", "yes", "no",
                      "1 to 10", "11 to 20", "more than 20"}
    assert not strays, (
        f"these are ANSWERS, and the catalogue lists them as questions: {sorted(strays)}")


def test_per_option_identity_survives_as_member_metadata(crawled):
    """T-QT-02 folds the members away as ROWS; it must not lose them as
    EVIDENCE — a planned walk still has to force one specific answer."""
    q = _by_name(crawled["master"]["questions"])["Gender"]
    members = {str(m.get("name") or "").lower() for m in (q.get("members") or [])}
    assert {"female", "male", "prefer not to say"} <= members, q.get("members")


def test_ungrouped_controls_are_left_exactly_as_they_were(crawled):
    """Subject D. A text input, a select and a lone checkbox are each their own
    question and their accessible name IS the wording — folding must not touch
    them, or the fix for choice groups would eat the rest of the form."""
    names = set(_by_name(crawled["master"]["questions"]))
    for expected in ("Height in centimetres", "State of residence",
                     "I consent to a medical records check"):
        assert expected in names, (
            f"{expected!r} is an ungrouped control and must be its own question; "
            f"catalogued names: {sorted(names)}")


def test_the_catalogue_is_not_inflated_by_group_members(crawled):
    """The whole-artifact check. This page asks a dozen questions; before M2.1
    the same page catalogued roughly twice that, and every extra row was either
    an answer wearing a question's clothes or a branch-minted duplicate."""
    questions = crawled["master"]["questions"]
    assert len(questions) <= 14, (
        f"{len(questions)} catalogue rows for an application that asks about a "
        f"dozen questions:\n  " + "\n  ".join(
            f"{q['question_id']} {q.get('name')!r} opts={q.get('options')}"
            for q in questions))


# ── T-QT-04 · one canonical id, stable across crawls ─────────────────────────

def test_one_group_produces_exactly_one_question_id(crawled):
    """Branch metadata must not create a second catalogue question. The branch
    rows for `Gender` key on the DOM's declared group, which is the same
    signature the folded control row carries, so both land on ONE id."""
    ids = [q["question_id"] for q in crawled["master"]["questions"]]
    assert len(ids) == len(set(ids)), "duplicate question_ids in the catalogue"

    gender = _by_name(crawled["master"]["questions"])["Gender"]
    same_options = [q for q in crawled["master"]["questions"]
                    if q is not gender
                    and {o.lower() for o in q.get("options") or []}
                    & {"female", "male"}]
    assert not same_options, (
        "a second question carries Gender's answers — the branch rows minted a "
        f"duplicate: {[(q['question_id'], q.get('name')) for q in same_options]}")


def test_recrawling_the_same_application_does_not_rekey_a_question(crawled):
    """The base crawl and the planned re-crawl are two independent crawls of an
    unchanged application. Every question the first one found must keep its id
    in the second, or the catalogue reports a rewrite where nothing moved."""
    before = {q["question_id"] for q in crawled["base_master"]["questions"]}
    after = {q["question_id"] for q in crawled["master"]["questions"]}
    lost = before - after
    assert not lost, (
        f"{len(lost)} question(s) were re-keyed by a re-crawl of an unchanged "
        f"application: {sorted(lost)}")


# ── T-QT-03 · the trigger activates the child, analytically ──────────────────

def test_the_trigger_answer_activates_the_child_question(crawled):
    """THE MILESTONE'S STOP CONDITION.

    `Have you used tobacco…` = **Yes** must analytically activate
    `How many cigarettes per day…` — computed by the projector from the
    catalogue and the recorded branch reveals, with no further crawl."""
    cat, proj = crawled["catalog"], crawled["projector"]
    master, rules = crawled["master"], crawled["rules"]
    by_name = _by_name(master["questions"])

    trigger = by_name.get(
        "Have you used tobacco or nicotine products in the last 12 months?")
    child = by_name.get("How many cigarettes per day do you smoke?")
    assert trigger is not None, "the trigger question is not in the catalogue"
    assert child is not None, (
        "the child question is not in the catalogue — the planned re-crawl "
        "never reached the revealed question")
    assert rules, (
        "no trigger→child rules were derived from the recorded branch reveals.\n"
        f"branches with reveals: "
        f"{[(b['control_label_norm'], b['option_label_norm'], b['reveals']) for b in crawled['branches'] if b['reveals']]}")

    yes = proj.project_traversal(
        master["questions"], rules, {trigger["question_id"]: "yes"})
    assert child["question_id"] in yes["activated"], (
        "answering the tobacco question 'Yes' did not activate the cigarettes "
        f"question.\nrules: {rules}\nactivated: {yes['activated']}")

    no = proj.project_traversal(
        master["questions"], rules, {trigger["question_id"]: "no"})
    assert child["question_id"] in no["skipped"], (
        "answering 'No' must SKIP the child; a rule that activates it either "
        "way states nothing about the application")
    assert child["question_id"] not in no["activated"]


def test_the_reveal_names_the_child_QUESTION_not_one_of_its_answers(crawled):
    """The reconciliation depends on the reveal identity being resolvable. A
    revealed radio group's members are named "1 to 10"/"11 to 20"/"More than
    20"; naming the reveal by a member is how a reveal resolves to the wrong
    question on a page where twenty questions share their answers' names."""
    revealing = [b for b in crawled["branches"] if b["reveals"]]
    assert revealing, "no branch recorded what its answer revealed"
    assert any(r.startswith("group:") for b in revealing for r in b["reveals"]), (
        "no reveal names a declared question group; every identity is an "
        f"answer name: {[b['reveals'] for b in revealing]}")


# ── Evidence, printed so CI archives it ──────────────────────────────────────

def test_print_the_catalogue_as_evidence(crawled):
    """Not an assertion — the artifact this milestone is judged on."""
    master = crawled["master"]
    print(f"\n=== {GROUND}: MASTER CATALOG "
          f"({master['summary']['question_count']} questions) ===")
    for q in master["questions"]:
        print(f"  {q['question_id']}  {q.get('name_status'):<10} "
              f"{q.get('type'):<9} {str(q.get('name'))[:64]!r}")
        if q.get("options"):
            print(f"        answers: {q['options']}")
    print(f"  summary: worded={master['summary'].get('with_observed_name')} "
          f"unverified={master['summary'].get('name_unverified')}")
    print(f"=== TRIGGER RULES ===\n  {json.dumps(crawled['rules'], indent=2)}")
