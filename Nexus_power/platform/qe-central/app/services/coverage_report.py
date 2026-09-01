"""THE CLIENT COVERAGE REPORT — what the crawl asked, answered, and could not (E2).

WHAT THIS IS FOR. A client reading an evidence bundle should not have to learn
this system's vocabulary to find out what happened. ``data_account`` says
``{"synthesized": 41, "harvested": 4, "answered_to_unblock": 7, ...}``, which is
precise and means nothing to the person paying for the crawl. This module turns
that into the two sentences they actually asked for:

    "56 fields. 0 came from your data, 4 the application supplied itself,
     48 we chose, and 4 still need an answer from you."

EVERY NUMBER IS DERIVED, NEVER TYPED. That is the whole discipline here, and it
is why this module is PURE — a bundle in, a report model out, no I/O, no clock,
no database. Each figure carries the bundle key it came from
(:attr:`Figure.source`), so a reader can check any sentence against the evidence
rather than trusting the prose. A report that cannot be traced back to the
bundle is marketing.

WHY THE PROVENANCE GROUPING IS THE INTERESTING PART. The fill records fourteen
distinct provenances and they are NOT interchangeable to a client:

  * ``provided`` / ``recalled``            — THEIR data. The highest-trust answer.
  * ``app_supplied`` / ``harvested`` /
    ``minted`` / ``sandbox`` / ``journey`` — the application's OWN values, read
                                             back rather than invented.
  * ``synthesized`` / ``llm`` / ``planned`` /
    ``answered_to_unblock``                — values WE chose. A journey completed
                                             on these is real, but it is not
                                             proof the client's data works.
  * ``needs_input`` / ``intent_unmet``     — unanswered, and the reason differs:
                                             one is a question we declined to
                                             invent, the other a fill that did
                                             not take.
  * ``group_sibling``                      — NOT a gap. Its question was answered
                                             by the member that IS the answer,
                                             and counting it as unfilled would
                                             ask the client for a value we hold.

Collapsing those four groups into one "coverage %" is the number this report
deliberately refuses to print. "90% covered" is true and useless when every one
of those values was invented by us.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Sequence

# ── the provenance taxonomy, grouped as a client reads it ───────────────────
#
# Kept as literals rather than imported from the explorer: the two services
# cannot share an interpreter (both ship a top-level ``app``), so this is a
# frozen contract in the same sense as anything under contracts/ — and
# `test_every_provenance_is_accounted_for` fails the moment the fill grows a
# provenance this grouping does not know, so it cannot drift silently.
FROM_CLIENT = ("provided", "recalled")
FROM_APPLICATION = ("app_supplied", "harvested", "minted", "sandbox", "journey")
CHOSEN_BY_US = ("synthesized", "llm", "planned", "answered_to_unblock")
UNANSWERED = ("needs_input", "intent_unmet")
#: Not a gap and not an answer — excluded from the totals entirely.
NOT_A_QUESTION = ("group_sibling",)

ALL_PROVENANCES = (
    FROM_CLIENT + FROM_APPLICATION + CHOSEN_BY_US + UNANSWERED + NOT_A_QUESTION)


@dataclass(frozen=True)
class Figure:
    """One number, and the bundle key it was derived from.

    ``source`` exists so every sentence in the rendered report can be checked
    against the evidence. A figure with no source is a figure somebody typed.
    """
    value: int
    source: str
    label: str = ""

    def as_dict(self) -> dict:
        return {"value": self.value, "source": self.source, "label": self.label}


@dataclass
class CoverageReport:
    """The client-facing model. Pure data; the portal renders it."""
    fields_total: Figure
    from_client: Figure
    from_application: Figure
    chosen_by_us: Figure
    unanswered: Figure
    journeys_completed: Figure
    boundaries_crossed: Figure
    questions_needing_you: list = field(default_factory=list)
    rejections: list = field(default_factory=list)
    near_misses: list = field(default_factory=list)
    unknown_provenances: list = field(default_factory=list)

    def headline(self) -> str:
        """The sentence, assembled from the figures above and nothing else."""
        return (
            f"{self.fields_total.value} fields. "
            f"{self.from_client.value} came from your data, "
            f"{self.from_application.value} the application supplied itself, "
            f"{self.chosen_by_us.value} we chose, and "
            f"{self.unanswered.value} still need an answer from you."
        )

    def as_dict(self) -> dict:
        return {
            "headline": self.headline(),
            "figures": {
                "fields_total": self.fields_total.as_dict(),
                "from_client": self.from_client.as_dict(),
                "from_application": self.from_application.as_dict(),
                "chosen_by_us": self.chosen_by_us.as_dict(),
                "unanswered": self.unanswered.as_dict(),
                "journeys_completed": self.journeys_completed.as_dict(),
                "boundaries_crossed": self.boundaries_crossed.as_dict(),
            },
            "questions_needing_you": list(self.questions_needing_you),
            "rejections": list(self.rejections),
            "near_misses": list(self.near_misses),
            "unknown_provenances": list(self.unknown_provenances),
        }


def _int_at(bundle: Mapping[str, Any], key: str) -> int:
    value = bundle.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _rows(bundle: Mapping[str, Any], key: str) -> list:
    value = bundle.get(key)
    return list(value) if isinstance(value, (list, tuple)) else []


def _sum_over(account: Mapping[str, Any], names: Sequence[str]) -> int:
    total = 0
    for name in names:
        value = account.get(name)
        if isinstance(value, int) and not isinstance(value, bool):
            total += value
    return total


def build_report(bundle: Optional[Mapping[str, Any]]) -> CoverageReport:
    """Bundle → report model. Pure, total, and defensive about missing keys.

    A bundle is written by a crawl that may have stopped anywhere, so every key
    here is optional. A missing key yields ZERO rather than an exception — but
    it yields zero with its source named, so "0 from your data" is
    distinguishable in the model from "this bundle never had a data_account".
    """
    bundle = bundle or {}
    account = bundle.get("data_account")
    account = account if isinstance(account, Mapping) else {}

    # A provenance the fill grew that this grouping does not know would be
    # SILENTLY DROPPED from every total, so the report would understate the work
    # and nobody would see it. Surfaced instead.
    unknown = sorted(str(k) for k in account if str(k) not in ALL_PROVENANCES)

    from_client = _sum_over(account, FROM_CLIENT)
    from_app = _sum_over(account, FROM_APPLICATION)
    chosen = _sum_over(account, CHOSEN_BY_US)
    unanswered = _sum_over(account, UNANSWERED)
    # The total is the SUM OF THE PARTS, deliberately — not len(field_ledger).
    # If the two disagree the parts are what the sentence is built from, and the
    # discrepancy shows up as an unknown provenance rather than as a total that
    # cannot be reconciled with the numbers beside it.
    total = from_client + from_app + chosen + unanswered

    needing_you = [
        {"name": str(r.get("name") or ""), "url": str(r.get("url") or ""),
         "reason": str(r.get("reason") or "")}
        for r in _rows(bundle, "field_ledger")
        if str(r.get("provenance") or "") == "needs_input"
    ]
    rejections = [
        {"field": str(r.get("field") or ""), "rule": str(r.get("rule") or ""),
         "anchored_by": str(r.get("anchored_by") or "")}
        for r in _rows(bundle, "validation_rejections")
    ]

    flow = bundle.get("flow_summary")
    flow = flow if isinstance(flow, Mapping) else {}

    return CoverageReport(
        fields_total=Figure(total, "data_account (sum of parts)", "fields asked"),
        from_client=Figure(from_client, f"data_account{list(FROM_CLIENT)}",
                           "from your data"),
        from_application=Figure(from_app, f"data_account{list(FROM_APPLICATION)}",
                                "supplied by the application"),
        chosen_by_us=Figure(chosen, f"data_account{list(CHOSEN_BY_US)}",
                            "chosen by the crawl"),
        unanswered=Figure(unanswered, f"data_account{list(UNANSWERED)}",
                          "still need an answer"),
        journeys_completed=Figure(
            _int_at(flow, "journeys_completed") or _int_at(bundle, "journeys_completed"),
            "flow_summary.journeys_completed", "journeys completed"),
        boundaries_crossed=Figure(
            _int_at(flow, "boundaries_crossed") or _int_at(bundle, "boundaries_crossed"),
            "flow_summary.boundaries_crossed", "boundaries crossed"),
        questions_needing_you=needing_you,
        rejections=rejections,
        near_misses=_rows(bundle, "seed_near_misses"),
        unknown_provenances=unknown,
    )
