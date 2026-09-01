"""QE-Central — the REFUSE matrix R1-R8 as DATA (design §3.1).

Each rule is a frozen record of:
  * ``fixture_mutation``       — pure fn(bundle_dict) -> mutated bundle_dict
                                 (never mutates its input; deep-copies first),
  * ``chain_step``             — which factory interaction reveals honesty,
  * ``refusal_predicate``      — fn(chain_evidence) -> True when the chain
                                 refused HONESTLY (positive proof required),
  * ``green_wash_predicate``   — fn(chain_evidence) -> True when the chain
                                 made a positive claim it could not have
                                 earned (STRICT: fires only on proven claims),
  * ``pins``                   — the verified file:line contracts each
                                 predicate is grounded in.

The ``chain_evidence`` dict is assembled by ``app.harness.runner`` — one
entry per chain step (HTTP status + parsed JSON + errors) plus substrate
readback entries for the DB-level rules (R5/R6/R7).  Canonical entry keys
are documented on each predicate.

Everything in this module is PURE (stdlib only, no service imports) so the
unit suite pins the honesty logic without a database or network.

Verdict vocabulary (qe_harness_runs.verdict):
  REFUSED_CORRECTLY | GREEN_WASH_DETECTED | CHAIN_ERROR | PASS_BASELINE
Classification order is deliberate: green-wash evidence ALWAYS wins (a
chain that green-washed and then crashed must still alarm), a broken chain
is never counted as a refusal, and an ambiguous outcome (neither predicate
provable) classifies as CHAIN_ERROR — never as a quiet pass.
"""
from __future__ import annotations

import copy
import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)

# ─── Verdicts (pinned vocabulary — db/models.py QEHarnessRunRow) ─────────
VERDICT_REFUSED = "REFUSED_CORRECTLY"
VERDICT_GREEN_WASH = "GREEN_WASH_DETECTED"
VERDICT_CHAIN_ERROR = "CHAIN_ERROR"
VERDICT_PASS_BASELINE = "PASS_BASELINE"

BASELINE_RULE_ID = "baseline"
RULE_IDS = ("R1", "R2", "R3", "R4", "R5", "R6", "R7", "R8")

# Write policies (how the runner treats the substrate-write outcome):
#   require        — the write MUST succeed; refusal/failure = CHAIN_ERROR
#                    (the rule tests a FACTORY seam, not the writer).
#   allow_refusal  — an honest writer/schema refusal is an acceptable
#                    (and recorded) outcome for this rule.
#   expect_failure — the write MUST fail atomically (R7 kill-mid-write);
#                    a successful poisoned write is substrate green-wash.
WRITE_REQUIRE = "require"
WRITE_ALLOW_REFUSAL = "allow_refusal"
WRITE_EXPECT_FAILURE = "expect_failure"

# ─── Env-driven harness fixture parameters (never hardcoded targets) ─────
# RFC 5737 TEST-NET-1 (192.0.2.0/24) is reserved for documentation/tests and
# guaranteed non-routable — the honest "dead environment" for R3/R8.
UNREACHABLE_BASE_URL = os.getenv(
    "QEC_HARNESS_UNREACHABLE_BASE_URL", "http://192.0.2.1:9",
)

# R6 sentinel: a clearly-synthetic raw secret planted in the mutated bundle;
# the readback proves it never reaches a substrate row un-redacted
# (pins the storyboard.py:810 raw-value leak class + models.py:2519-2520
# "Form VALUES are PII-redacted AT SOURCE").
PII_SECRET_SENTINEL = "QEC-R6-RAW-SECRET-078051120-NEVER-STORE"
PII_FIELD_LABEL = "Social Security Number"

# Bundle-dict key candidates — the ExplorationBundle schema (app.substrate.
# schema) owns the canonical names; mutations detect whichever is present so
# the DATA rules survive schema evolution without silent no-ops.
_PAGE_KEYS = ("pages", "page_states", "visits", "states")
_ACTION_KEYS = ("actions", "page_actions")
_SCREENSHOT_KEYS = ("screenshots", "frames")

_FIXTURE_NAME_RE = re.compile(r"^[A-Za-z0-9_\-]{1,200}$")
_FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"


# ─── Fixture loading ─────────────────────────────────────────────────────

def load_fixture_bundle(fixture_name: str) -> dict:
    """Load a harness fixture bundle by name from ``harness/fixtures/``.

    The name is validated against a strict slug pattern (no traversal, no
    extensions) — the fixture set is repo-versioned data, never user files.

    Raises ``ValueError`` on an invalid name and ``FileNotFoundError`` when
    the fixture does not exist; both surface honestly as CHAIN_ERROR.
    """
    if not _FIXTURE_NAME_RE.match(fixture_name or ""):
        raise ValueError(f"invalid fixture name: {fixture_name!r}")
    path = _FIXTURES_DIR / f"{fixture_name}.json"
    if not path.is_file():
        raise FileNotFoundError(f"harness fixture not found: {path.name}")
    with path.open("r", encoding="utf-8") as fh:
        bundle = json.load(fh)
    if not isinstance(bundle, dict):
        raise ValueError(f"fixture {path.name} must contain a JSON object")
    return bundle


def _first_key(bundle: dict, candidates: tuple[str, ...]) -> str | None:
    """Return the first candidate key present in the bundle, else None."""
    for key in candidates:
        if key in bundle:
            return key
    return None


def _pages(bundle: dict) -> list:
    key = _first_key(bundle, _PAGE_KEYS)
    return bundle.get(key) or [] if key else []


def _action_lists(bundle: dict) -> list[list]:
    """Every action list in the bundle — mutations must reach them ALL.

    The canonical ExplorationBundle shape (app.substrate.schema) embeds
    ``actions`` inside each page; the raw explorer manifest (§3.2) carries
    a flat top-level list.  Walking both keeps every mutation effective on
    either shape — a mutation that silently no-ops would turn a REFUSE
    rule into a fake PASS.
    """
    lists: list[list] = []
    key = _first_key(bundle, _ACTION_KEYS)
    if key and isinstance(bundle.get(key), list):
        lists.append(bundle[key])
    for page in _pages(bundle):
        if isinstance(page, dict):
            page_key = _first_key(page, _ACTION_KEYS)
            if page_key and isinstance(page.get(page_key), list):
                lists.append(page[page_key])
    return lists


def _actions(bundle: dict) -> list:
    return [a for lst in _action_lists(bundle) for a in lst]


# ─── Fixture mutations (pure; deep-copy in, mutated copy out) ────────────

def mutate_identity(bundle: dict) -> dict:
    """No mutation — the rule breaks the CHAIN inputs, not the evidence."""
    return copy.deepcopy(bundle)


def mutate_zero_evidence(bundle: dict) -> dict:
    """R1 — remove ALL evidence: no page visits, actions or screenshots."""
    out = copy.deepcopy(bundle)
    for key in (*_PAGE_KEYS, *_ACTION_KEYS, *_SCREENSHOT_KEYS):
        if key in out:
            out[key] = []
    out["frame_count"] = 0
    return out


_GROUNDING_KEYS = ("anchor", "after", "url_changed", "screenshot_after")


def mutate_strip_anchors(bundle: dict) -> dict:
    """R2 — strip the anchor/after grounding bundles from every action.

    The factory unpacks ``anchor``/``after`` verbatim (service.py:152-157)
    and derives ``navigated`` fail-closed (service.py:171-175); without
    them no step can honestly claim a grounded oracle.
    """
    out = copy.deepcopy(bundle)
    for action_list in _action_lists(out):
        for action in action_list:
            if not isinstance(action, dict):
                continue
            for stripped in _GROUNDING_KEYS:
                action.pop(stripped, None)
            signals = action.get("evidence_signals")
            if isinstance(signals, dict):
                for stripped in ("anchor", "after", "url_changed"):
                    signals.pop(stripped, None)
    return out


def _scrub_screenshot_fields(node) -> None:
    """Recursively blank every field whose key mentions 'screenshot'."""
    if isinstance(node, dict):
        for key in list(node.keys()):
            if "screenshot" in key.lower():
                value = node[key]
                if isinstance(value, str):
                    node[key] = ""
                elif isinstance(value, list):
                    node[key] = []
                elif isinstance(value, dict):
                    node[key] = {}
                else:
                    node[key] = None
            else:
                _scrub_screenshot_fields(node[key])
    elif isinstance(node, list):
        for item in node:
            _scrub_screenshot_fields(item)


def mutate_drop_screenshots(bundle: dict) -> dict:
    """R4 — remove every screenshot so no visual baseline can exist."""
    out = copy.deepcopy(bundle)
    for key in _SCREENSHOT_KEYS:
        if key in out:
            out[key] = []
    out["frame_count"] = 0
    _scrub_screenshot_fields(out)
    return out


def mutate_inject_pii(bundle: dict) -> dict:
    """R6 — plant a raw PII secret in a form snapshot AND a fill action.

    The planted field is signal-flagged ``type='password'`` (the design's
    "fixture password field" mutation) so redaction at source is
    DETERMINISTIC — the writer redacts signal-flagged secrets whole,
    independent of any regex detector.  The substrate contract mandates
    redaction AT SOURCE (§2.1 page_actions "PII-REDACTED AT SOURCE";
    models.py:2519-2520) — the readback proves the sentinel never lands
    raw in any written row.
    """
    out = copy.deepcopy(bundle)
    pages = _pages(out)
    first_page = pages[0] if pages and isinstance(pages[0], dict) else None
    if first_page is not None:
        snapshot = first_page.setdefault("form_snapshot", {})
        if isinstance(snapshot, dict):
            snapshot[PII_FIELD_LABEL] = PII_SECRET_SENTINEL
        signals = first_page.setdefault("form_snapshot_signals", {})
        if isinstance(signals, dict):
            signals[PII_FIELD_LABEL] = {"type": "password", "required": True}

    # Plant the fill action next to the snapshot field: the first page's
    # embedded list (canonical bundle shape), falling back to the flat
    # top-level list (manifest shape).
    target_list: list | None = None
    if first_page is not None:
        page_key = _first_key(first_page, _ACTION_KEYS)
        if page_key and isinstance(first_page.get(page_key), list):
            target_list = first_page[page_key]
    if target_list is None:
        top_key = _first_key(out, _ACTION_KEYS)
        if top_key and isinstance(out.get(top_key), list):
            target_list = out[top_key]

    all_actions = [a for a in _actions(out) if isinstance(a, dict)]
    template = next(
        (a for a in all_actions
         if str(a.get("verb", "")).lower() in ("type", "fill")),
        all_actions[0] if all_actions else None,
    )
    if target_list is not None and template is not None:
        planted = copy.deepcopy(template)
        planted["verb"] = "type"
        planted["target_label"] = PII_FIELD_LABEL
        planted["target_kind"] = "text_field"
        planted["value"] = PII_SECRET_SENTINEL
        planted["subaction_index"] = 1 + max(
            (int(a.get("subaction_index") or 0)
             for a in target_list if isinstance(a, dict)),
            default=0,
        )
        planted["after"] = {
            "outcome": "value_committed",
            "detail": f"{PII_FIELD_LABEL} committed",
            "navigated": False,
        }
        qec = planted.get("qec")
        if isinstance(qec, dict):
            qec["input_type"] = "password"
        target_list.append(planted)
    return out


def mutate_version_hijack(bundle: dict) -> dict:
    """R7 — a concurrent write poisoned to DIE MID-WRITE.

    New crawl_id (→ a distinct ``qec_live_v1@…`` version) + a broken
    sequence: the last page reuses the first page's ``sequence_index``,
    violating monotonicity (schema refusal) or the DB uniqueness contract
    ``uq_page_visits_artifact_sequence_version`` (IntegrityError mid-
    transaction).  Either death path MUST leave zero rows of the hijack
    version (§2.3 rule 1: one write = one atomic version).
    """
    out = copy.deepcopy(bundle)
    out["crawl_id"] = f"{out.get('crawl_id', '')}-hx"
    pages = _pages(out)
    if len(pages) >= 2 and isinstance(pages[0], dict) and isinstance(pages[-1], dict):
        pages[-1]["sequence_index"] = pages[0].get("sequence_index", 0)
    return out


# ─── chain-evidence accessors (shared by the predicates) ─────────────────

def _step(chain: dict, name: str) -> dict:
    entry = chain.get(name)
    return entry if isinstance(entry, dict) else {}


def _status(chain: dict, name: str) -> int | None:
    return _step(chain, name).get("status")


def _json_of(chain: dict, name: str) -> dict:
    body = _step(chain, name).get("json")
    return body if isinstance(body, dict) else {}


def _write(chain: dict) -> dict:
    return _step(chain, "write")


def _write_refused(chain: dict) -> bool:
    return bool(_write(chain).get("refused"))


def _write_ok(chain: dict) -> bool:
    return bool(_write(chain).get("ok"))


def _generated_count(chain: dict) -> int:
    try:
        return int(_json_of(chain, "generate").get("generated") or 0)
    except (TypeError, ValueError):
        return 0


def _audit_report(chain: dict) -> dict:
    report = _step(chain, "playwright").get("audit_report")
    return report if isinstance(report, dict) else {}


def _suite_min_below_threshold(chain: dict) -> bool | None:
    """True/False when the delivered audit report is comparable, else None."""
    report = _audit_report(chain)
    if not report:
        return None
    try:
        suite_min = int(report.get("suite_min_overall"))
        threshold = int(report.get("min_score_threshold"))
    except (TypeError, ValueError):
        return None
    return suite_min < threshold


# ─── Refusal / green-wash predicates per rule ────────────────────────────
# Refusal predicates demand POSITIVE proof of the honest outcome; green-wash
# predicates fire only on a PROVEN false positive claim.  Anything in
# between classifies as CHAIN_ERROR (see classify_verdict).

def _r1_refused(chain: dict) -> bool:
    """R1: /generate → 0 cases AND /playwright → honest 404
    (test_factory.py:541-548; service.py:96-98 no-version → empty)."""
    return (
        _status(chain, "generate") == 200
        and _generated_count(chain) == 0
        and _status(chain, "playwright") == 404
    )


def _r1_green(chain: dict) -> bool:
    return (
        (_status(chain, "generate") == 200 and _generated_count(chain) > 0)
        or _status(chain, "playwright") == 200
    )


def _r2_refused(chain: dict) -> bool:
    """R2: writer/schema refusal, auditor-gate 409, honest 404, or a
    delivered report that DOWNGRADES below the gate threshold — never PASS
    (test_factory.py:566-681; playwright_auditor.score_spec grounding)."""
    if _write_refused(chain):
        return True
    pw_status = _status(chain, "playwright")
    if pw_status in (404, 409):
        return True
    return pw_status == 200 and _suite_min_below_threshold(chain) is True


def _r2_green(chain: dict) -> bool:
    """Anchor-stripped scripts delivered as a full-score certified suite."""
    return (
        _status(chain, "playwright") == 200
        and _suite_min_below_threshold(chain) is False
    )


def _readiness(chain: dict) -> dict:
    readiness = _json_of(chain, "verify").get("readiness")
    return readiness if isinstance(readiness, dict) else {}


def _r3_refused(chain: dict) -> bool:
    """R3: readiness.status == BLOCKED and reachable false
    (test_factory.py:4637-4667)."""
    readiness = _readiness(chain)
    return (
        _status(chain, "verify") == 200
        and readiness.get("status") == "BLOCKED"
        and readiness.get("reachable") is False
    )


def _r3_green(chain: dict) -> bool:
    return _readiness(chain).get("status") == "READY"


def _r4_refused(chain: dict) -> bool:
    """R4: the semantic oracle returns the uncertain sentinel — never
    'match' — when no baseline exists (semantic_oracle.py:148-167 + :57;
    surfaced at test_factory.py:3778-3801 via :3715-3748)."""
    if _write_refused(chain):
        return True
    return (
        _status(chain, "semantic_check") == 200
        and _json_of(chain, "semantic_check").get("semantic_match") == "uncertain"
    )


def _r4_green(chain: dict) -> bool:
    return _json_of(chain, "semantic_check").get("semantic_match") == "match"


def _r5_readback(chain: dict) -> dict:
    return _step(chain, "cross_tenant_readback")


def _r5_refused(chain: dict) -> bool:
    """R5: tenant-B JWT → 404 from _require_artifact (test_factory.py:
    167-178) AND raw SQL under B's GUC sees zero rows (RLS, migration 010/034)."""
    readback = _r5_readback(chain)
    return (
        _status(chain, "cross_tenant_test_cases") == 404
        and readback.get("page_visit_rows") == 0
    )


def _r5_green(chain: dict) -> bool:
    rows = _r5_readback(chain).get("page_visit_rows")
    return (
        _status(chain, "cross_tenant_test_cases") == 200
        or (isinstance(rows, int) and rows > 0)
    )


def _r6_refused(chain: dict) -> bool:
    """R6: the planted raw secret appears in NO written substrate row
    (page_actions.value / page_visits.form_snapshot / ground_truth_events)."""
    if _write_refused(chain):
        return True
    readback = _step(chain, "pii_readback")
    return _write_ok(chain) and readback.get("raw_secret_found") is False


def _r6_green(chain: dict) -> bool:
    return _step(chain, "pii_readback").get("raw_secret_found") is True


def _r7_readback(chain: dict) -> dict:
    return _step(chain, "version_readback")


def _r7_refused(chain: dict) -> bool:
    """R7: the poisoned concurrent write died atomically (zero hijack rows),
    the loader-mirrored current version is STILL the golden version
    (service.py:44-58), and /generate still produces cases from it."""
    readback = _r7_readback(chain)
    return (
        readback.get("hijack_write_failed") is True
        and readback.get("hijack_rows") == 0
        and bool(readback.get("golden_version"))
        and readback.get("current_version") == readback.get("golden_version")
        and _generated_count(chain) > 0
    )


def _r7_green(chain: dict) -> bool:
    readback = _r7_readback(chain)
    hijack_rows = readback.get("hijack_rows")
    hijack_version = readback.get("hijack_version")
    return (
        (isinstance(hijack_rows, int) and hijack_rows > 0)
        or (bool(hijack_version)
            and readback.get("current_version") == hijack_version)
    )


_R8_TERMINAL_FAILURES = frozenset(
    {"failed", "error", "timed_out", "stopped", "cancelled"}
)


def _run_status(chain: dict) -> dict:
    return _step(chain, "run_status_final")


def _r8_refused(chain: dict) -> bool:
    """R8: terminal state carries an honest stop diagnosis and NO
    clean_run_version — refuse-toward-human, never silent green
    (auto-heal contract, test_factory.py:2652-2657 + poll :2945-2963)."""
    status = str(_run_status(chain).get("status") or "").lower()
    terminal = _run_status(chain)
    if terminal.get("clean_run_version"):
        return False
    if status in ("passed", "running", ""):
        return False
    return bool(
        terminal.get("stop_reason")
        or terminal.get("terminal_state")
        or status in _R8_TERMINAL_FAILURES
    )


def _r8_green(chain: dict) -> bool:
    terminal = _run_status(chain)
    return (
        bool(terminal.get("clean_run_version"))
        or str(terminal.get("status") or "").lower() == "passed"
    )


def _baseline_passed(chain: dict) -> bool:
    """Golden fixture positive path: substrate write OK, >0 generated cases,
    playwright project delivered (200)."""
    return (
        _write_ok(chain)
        and _status(chain, "generate") == 200
        and _generated_count(chain) > 0
        and _status(chain, "playwright") == 200
    )


def _never(_chain: dict) -> bool:
    return False


# ─── The rule record ─────────────────────────────────────────────────────

@dataclass(frozen=True)
class RefuseRule:
    """One REFUSE-matrix rule as data (design §3.1)."""

    rule_id: str
    name: str
    expected: str
    fixture_mutation: Callable[[dict], dict]
    chain_step: str
    refusal_predicate: Callable[[dict], bool]
    green_wash_predicate: Callable[[dict], bool]
    pins: tuple[str, ...]
    write_policy: str = WRITE_REQUIRE
    # Extra chain inputs (e.g. the deliberately-dead base_url for R3/R8).
    chain_overrides: dict = field(default_factory=dict)


BASELINE_RULE = RefuseRule(
    rule_id=BASELINE_RULE_ID,
    name="golden fixture positive path",
    expected=("substrate write OK; /generate > 0 cases with PROVEN "
              "navigations; /playwright delivers the project (200)"),
    fixture_mutation=mutate_identity,
    chain_step="baseline_chain",
    refusal_predicate=_baseline_passed,   # for baseline: 'honest' == PASS
    green_wash_predicate=_never,
    pins=(
        "platform/api/app/services/test_factory/service.py:92-178",
        "platform/api/app/services/test_factory/generator.py:556,581-585",
        "platform/api/app/routers/test_factory.py:181-199,505-549",
    ),
    write_policy=WRITE_REQUIRE,
)

REFUSE_RULES: tuple[RefuseRule, ...] = (
    RefuseRule(
        rule_id="R1",
        name="zero evidence",
        expected="/generate -> 0 cases; /playwright -> honest 404 detail",
        fixture_mutation=mutate_zero_evidence,
        chain_step="generate_playwright",
        refusal_predicate=_r1_refused,
        green_wash_predicate=_r1_green,
        pins=(
            "platform/api/app/routers/test_factory.py:541-548",
            "platform/api/app/services/test_factory/service.py:96-98",
        ),
        write_policy=WRITE_ALLOW_REFUSAL,
    ),
    RefuseRule(
        rule_id="R2",
        name="ungrounded action (anchors stripped)",
        expected=("HONEST-10 auditor blocks (409) / downgrades below the "
                  "gate threshold — the suite never reads as full-score PASS"),
        fixture_mutation=mutate_strip_anchors,
        chain_step="generate_playwright",
        refusal_predicate=_r2_refused,
        green_wash_predicate=_r2_green,
        pins=(
            "platform/api/app/routers/test_factory.py:566-681",
            "platform/api/app/services/test_factory/service.py:152-175",
            "docker-compose.yml:460-461 (NEXUS_AUDITOR_GATE/MIN_SCORE)",
        ),
        write_policy=WRITE_ALLOW_REFUSAL,
    ),
    RefuseRule(
        rule_id="R3",
        name="dead environment",
        expected="/verify readiness.status == BLOCKED, never READY",
        fixture_mutation=mutate_identity,
        chain_step="verify_readiness",
        refusal_predicate=_r3_refused,
        green_wash_predicate=_r3_green,
        pins=("platform/api/app/routers/test_factory.py:4637-4667",),
        write_policy=WRITE_REQUIRE,
        chain_overrides={"base_url": UNREACHABLE_BASE_URL},
    ),
    RefuseRule(
        rule_id="R4",
        name="missing visual baseline",
        expected=("semantic oracle returns the 'uncertain' sentinel — the "
                  "response never claims semantic 'match' without a baseline"),
        fixture_mutation=mutate_drop_screenshots,
        chain_step="semantic_check",
        refusal_predicate=_r4_refused,
        green_wash_predicate=_r4_green,
        pins=(
            "platform/api/app/services/test_factory/semantic_oracle.py:148-167",
            "platform/api/app/services/test_factory/semantic_oracle.py:57",
            "platform/api/app/routers/test_factory.py:3778-3801,3715-3748",
        ),
        write_policy=WRITE_ALLOW_REFUSAL,
    ),
    RefuseRule(
        rule_id="R5",
        name="cross-tenant access",
        expected=("tenant-B JWT -> 404 from _require_artifact AND raw SQL "
                  "under B's GUC sees zero substrate rows"),
        fixture_mutation=mutate_identity,
        chain_step="cross_tenant_read",
        refusal_predicate=_r5_refused,
        green_wash_predicate=_r5_green,
        pins=(
            "platform/api/app/routers/test_factory.py:167-178",
            "platform/api/alembic/versions/010_row_level_security.py:32-76",
            "platform/api/alembic/versions/034_page_visits.py:178-200",
        ),
        write_policy=WRITE_REQUIRE,
    ),
    RefuseRule(
        rule_id="R6",
        name="PII in captured evidence",
        expected=("planted raw secret appears in NO written row: "
                  "page_actions.value / page_visits.form_snapshot / "
                  "ground_truth_events.value are redacted at source"),
        fixture_mutation=mutate_inject_pii,
        chain_step="pii_readback",
        refusal_predicate=_r6_refused,
        green_wash_predicate=_r6_green,
        pins=(
            "platform/api/app/models.py:2519-2520",
            "platform/api/app/routers/storyboard.py:810 (leak class pinned)",
        ),
        write_policy=WRITE_ALLOW_REFUSAL,
    ),
    RefuseRule(
        rule_id="R7",
        name="version hijack / kill-mid-write",
        expected=("poisoned concurrent write dies atomically (0 rows of the "
                  "hijack version); loader-current version stays the golden "
                  "write; /generate still yields cases"),
        fixture_mutation=mutate_version_hijack,
        chain_step="version_hijack_readback",
        refusal_predicate=_r7_refused,
        green_wash_predicate=_r7_green,
        pins=(
            "platform/api/app/services/test_factory/service.py:44-58",
            "platform/api/alembic/versions/034_page_visits.py:156-159",
            "QECentral/docs/IMPLEMENTATION_DESIGN.md §2.3",
        ),
        write_policy=WRITE_EXPECT_FAILURE,
    ),
    RefuseRule(
        rule_id="R8",
        name="unprovable step (auto-heal honesty)",
        expected=("terminal run state carries stop_reason / terminal_state "
                  "and NO clean_run_version — refuse-toward-human"),
        fixture_mutation=mutate_identity,
        chain_step="auto_heal_poll",
        refusal_predicate=_r8_refused,
        green_wash_predicate=_r8_green,
        pins=(
            "platform/api/app/routers/test_factory.py:2652-2657,2676-2683",
            "platform/api/app/routers/test_factory.py:2945-2963",
        ),
        write_policy=WRITE_REQUIRE,
        chain_overrides={"base_url": UNREACHABLE_BASE_URL},
    ),
)

RULES_BY_ID: dict[str, RefuseRule] = {r.rule_id: r for r in REFUSE_RULES}


def select_rules(rule_ids: list[str] | None) -> list[RefuseRule]:
    """Resolve a rule subset; None/empty = baseline + the full R1-R8 matrix.

    Raises ``ValueError`` on unknown ids (the router pre-validates, the CLI
    relies on this).
    """
    if not rule_ids:
        return [BASELINE_RULE, *REFUSE_RULES]
    unknown = [r for r in rule_ids if r not in RULES_BY_ID]
    if unknown:
        raise ValueError(f"unknown harness rule ids: {unknown} (expected R1..R8)")
    return [RULES_BY_ID[r] for r in rule_ids]


def classify_verdict(rule: RefuseRule, chain: dict) -> str:
    """Classify one rule's chain evidence into the pinned verdict vocabulary.

    Order is load-bearing:
      1. PROVEN green-wash always wins — even when the chain later errored.
      2. A broken chain (connection failure, missing steps) is CHAIN_ERROR —
         it NEVER counts as a refusal.
      3. Positive proof of the honest outcome → REFUSED_CORRECTLY
         (PASS_BASELINE for the baseline rule).
      4. Ambiguity (neither predicate provable) → CHAIN_ERROR, never a pass.
    """
    if rule.green_wash_predicate(chain):
        return VERDICT_GREEN_WASH
    if chain.get("chain_error"):
        return VERDICT_CHAIN_ERROR
    if rule.refusal_predicate(chain):
        return (
            VERDICT_PASS_BASELINE
            if rule.rule_id == BASELINE_RULE_ID
            else VERDICT_REFUSED
        )
    logger.warning(
        "qec.harness.ambiguous_outcome",
        extra={"rule_id": rule.rule_id, "chain_steps": sorted(chain.keys())},
    )
    return VERDICT_CHAIN_ERROR
