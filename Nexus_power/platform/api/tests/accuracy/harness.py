"""Canonical Trust Baseline — accuracy harness (Milestone 1A, MEASURE-ONLY).

Offline, pure-stdlib scorer. It compares an EXTRACTED canonical artifact against a
hand-verified LABEL sidecar and emits the five TRUST metrics. No app imports, no DB,
no LLM — it runs in CI and standalone. This is the measuring stick the M1 plan
requires: it turns "it produces rows" into "the rows are TRUE and the number is
trustworthy."

Never-green-wash applies to the harness too. It reports:
  1. FAITHFULNESS     — fabrication_rate among rows emitted at/above the automation
                        threshold (target ~0).
  2. COMPLETENESS     — recall, split into CONFIDENT-recall (capped by the pixel
                        ceiling) and HONEST-recall (a missed item that the system
                        FLAGGED as a MISSING_*/UNCERTAIN placeholder still counts).
                        A SILENT drop (missed AND not flagged) is the only failure.
  3. CALIBRATION      — Expected Calibration Error (ECE) + reliability buckets +
                        overconfident-wrong mass. A 0.9 should be right ~90%.
  4. PAGE-GRAPH       — node + edge precision/recall + graph-edit-distance.
  5. VALUE-SURVIVAL   — every typed value the label marks visible_in_video must
                        appear in some extracted action (kills the dropped-login class).

The LABEL carries visible_in_video on every item so we separate "video physically
could never show this" (needs the ground-truth overlay) from "video showed it, we
missed it" (a real defect). Only the latter counts against confident recall.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import re

# ── Normalization (domain-blind; no host lists, no per-app constants) ──────────

_WS = re.compile(r"\s+")


def norm(text: str) -> str:
    """Lowercase + collapse whitespace + strip surrounding punctuation. Generic."""
    t = _WS.sub(" ", (text or "").strip().lower())
    return t.strip(" \t\r\n.:;,!?-–")


def norm_page_key(key: str) -> str:
    """Semantic page key: drop scheme/host/query so two readings of the same page
    match. Path-and-screen-name only — works for URL apps AND no-URL desktop apps."""
    k = (key or "").strip()
    m = re.match(r"https?://[^/]+(/[^?#]*)", k)
    if m:
        k = m.group(1)
    k = k.split("?", 1)[0].split("#", 1)[0]
    return norm(k.rstrip("/")) or "/"


# Placeholder markers the pipeline emits for an HONEST miss (vs a silent drop).
PLACEHOLDER_TOKENS = {"missing_page", "missing_value", "uncertain_action",
                      "conflicting_value", "possible_missing_page"}


def _is_placeholder(s: str) -> bool:
    return norm(s).replace(" ", "_") in PLACEHOLDER_TOKENS


# ── Data model (a common shape for both the extraction and the label) ──────────


@dataclass
class Action:
    verb: str = ""
    target_label: str = ""
    value: str = ""
    page_key: str = ""
    confidence: float = 0.0
    automation_ready: bool = False
    source: str = ""            # ocr | url_regex | vision | overlay | llm | ground_truth | user
    visible_in_video: bool = True   # label-only; ignored on the extracted side
    placeholder: str = ""       # MISSING_VALUE / UNCERTAIN_ACTION / "" if a real row

    def is_placeholder(self) -> bool:
        return bool(self.placeholder) or _is_placeholder(self.verb)


@dataclass
class PageNode:
    page_key: str = ""
    location: str = ""
    confidence: float = 0.0
    source: str = ""
    visible_in_video: bool = True
    placeholder: str = ""       # MISSING_PAGE / "" if a real row

    def is_placeholder(self) -> bool:
        return bool(self.placeholder) or _is_placeholder(self.page_key)


@dataclass
class Edge:
    from_key: str = ""
    to_key: str = ""


@dataclass
class CanonicalDoc:
    """Either an extracted artifact or a hand-verified label (same shape)."""
    page_nodes: list = field(default_factory=list)
    actions: list = field(default_factory=list)
    edges: list = field(default_factory=list)

    @staticmethod
    def from_dict(d: dict) -> "CanonicalDoc":
        d = d or {}
        return CanonicalDoc(
            page_nodes=[PageNode(**p) for p in d.get("page_nodes", [])],
            actions=[Action(**a) for a in d.get("actions", [])],
            edges=[Edge(**e) for e in d.get("edges", [])],
        )

    @staticmethod
    def load(path: str) -> "CanonicalDoc":
        with open(path, "r", encoding="utf-8") as fh:
            return CanonicalDoc.from_dict(json.load(fh))


# ── Matcher (domain-blind; greedy on a similarity score, never exact-string) ────


def _label_sim(a: str, b: str) -> float:
    """Token-overlap similarity in [0,1] — survives benign OCR variance."""
    ta, tb = set(norm(a).split()), set(norm(b).split())
    if not ta and not tb:
        return 1.0
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def _action_matches(ext: Action, lab: Action, label_threshold: float) -> bool:
    if norm(ext.verb) != norm(lab.verb):
        return False
    if norm_page_key(ext.page_key) != norm_page_key(lab.page_key):
        return False
    if _label_sim(ext.target_label, lab.target_label) < label_threshold:
        return False
    # If the label pins a value, the extracted value must be close (or present).
    if lab.value:
        if not ext.value:
            return False
        if _label_sim(ext.value, lab.value) < 0.5 and norm(ext.value) != norm(lab.value):
            return False
    return True


def _match_actions(extracted: list, labels: list, label_threshold: float):
    """Greedy 1:1 alignment. Returns (matched_pairs, unmatched_ext, unmatched_lab)."""
    used_ext = set()
    matched = []
    for li, lab in enumerate(labels):
        best = -1
        best_sim = -1.0
        for ei, ext in enumerate(extracted):
            if ei in used_ext:
                continue
            if _action_matches(ext, lab, label_threshold):
                sim = _label_sim(ext.target_label, lab.target_label)
                if sim > best_sim:
                    best, best_sim = ei, sim
        if best >= 0:
            used_ext.add(best)
            matched.append((best, li))
    unmatched_ext = [i for i in range(len(extracted)) if i not in used_ext]
    unmatched_lab = [li for li in range(len(labels))
                     if li not in {m[1] for m in matched}]
    return matched, unmatched_ext, unmatched_lab


# ── Metric families ────────────────────────────────────────────────────────────


def _prf(tp: int, fp: int, fn: int) -> dict:
    p = tp / (tp + fp) if (tp + fp) else 1.0
    r = tp / (tp + fn) if (tp + fn) else 1.0
    f1 = 2 * p * r / (p + r) if (p + r) else 0.0
    return {"precision": round(p, 4), "recall": round(r, 4), "f1": round(f1, 4),
            "tp": tp, "fp": fp, "fn": fn}


def action_metrics(ext_doc: CanonicalDoc, lab_doc: CanonicalDoc, label_threshold: float) -> dict:
    ext = [a for a in ext_doc.actions if not a.is_placeholder()]
    labs = [a for a in lab_doc.actions if not a.is_placeholder()]
    matched, un_ext, un_lab = _match_actions(ext, labs, label_threshold)
    overall = _prf(len(matched), len(un_ext), len(un_lab))
    # Per-verb F1.
    per_verb: dict = {}
    verbs = {norm(a.verb) for a in labs} | {norm(a.verb) for a in ext}
    for vb in sorted(v for v in verbs if v):
        e = [a for a in ext if norm(a.verb) == vb]
        l = [a for a in labs if norm(a.verb) == vb]
        m, ue, ul = _match_actions(e, l, label_threshold)
        per_verb[vb] = _prf(len(m), len(ue), len(ul))
    return {"overall": overall, "per_verb": per_verb,
            "_matched": matched, "_un_ext": un_ext, "_un_lab": un_lab}


def fabrication_rate(ext_doc: CanonicalDoc, lab_doc: CanonicalDoc,
                     label_threshold: float, automation_threshold: float) -> dict:
    """Of rows emitted AT/ABOVE the automation threshold (or automation_ready), the
    fraction with NO matching label. The headline never-green-wash number. Measured
    AT the threshold so a just-under-threshold fabrication rule is exposed."""
    ext = [a for a in ext_doc.actions if not a.is_placeholder()]
    labs = [a for a in lab_doc.actions if not a.is_placeholder()]
    confident = [a for a in ext if a.confidence >= automation_threshold or a.automation_ready]
    matched, _, _ = _match_actions(confident, labs, label_threshold)
    matched_ext = {m[0] for m in matched}
    fabricated = [confident[i] for i in range(len(confident)) if i not in matched_ext]
    rate = len(fabricated) / len(confident) if confident else 0.0
    return {"rate": round(rate, 4), "fabricated_count": len(fabricated),
            "confident_count": len(confident),
            "examples": [f"{a.verb} '{a.target_label}' -> {a.page_key} (conf {a.confidence})"
                         for a in fabricated[:5]]}


def calibration(ext_doc: CanonicalDoc, lab_doc: CanonicalDoc, label_threshold: float,
                buckets: int = 5) -> dict:
    """ECE + reliability buckets + overconfident-wrong mass. 'correct' == the row
    matched a label. A 0.9 row should be correct ~90% of the time."""
    ext = [a for a in ext_doc.actions if not a.is_placeholder()]
    labs = [a for a in lab_doc.actions if not a.is_placeholder()]
    matched, _, _ = _match_actions(ext, labs, label_threshold)
    correct_idx = {m[0] for m in matched}
    rows = [(a.confidence, (i in correct_idx)) for i, a in enumerate(ext)]
    if not rows:
        return {"ece": 0.0, "n": 0, "buckets": [], "overconfident_wrong_mass": 0.0}
    edges = [i / buckets for i in range(buckets + 1)]
    ece = 0.0
    bucket_report = []
    overconf_wrong = 0
    for b in range(buckets):
        lo, hi = edges[b], edges[b + 1]
        in_b = [(c, ok) for (c, ok) in rows if (lo <= c < hi or (b == buckets - 1 and c == hi))]
        if not in_b:
            continue
        avg_conf = sum(c for c, _ in in_b) / len(in_b)
        acc = sum(1 for _, ok in in_b if ok) / len(in_b)
        ece += (len(in_b) / len(rows)) * abs(avg_conf - acc)
        bucket_report.append({"range": f"{lo:.1f}-{hi:.1f}", "n": len(in_b),
                              "avg_confidence": round(avg_conf, 3), "accuracy": round(acc, 3)})
    for c, ok in rows:
        if c >= 0.8 and not ok:
            overconf_wrong += 1
    return {"ece": round(ece, 4), "n": len(rows), "buckets": bucket_report,
            "overconfident_wrong_mass": round(overconf_wrong / len(rows), 4)}


def _value_survived(label_val: str, ext_values: list) -> bool:
    """A value SURVIVED only on exact match or full-TOKEN containment — never a raw
    substring (else 'admin' would 'survive' via an unrelated 'administrator district',
    and '1' via '100': the metric would green-wash a real drop — review finding H2).
    Underscores are treated as token separators so 'visual_user' == 'visual user' (a
    benign OCR/format variance), which only reduces false DROPS (the safe direction)."""
    lv = norm(label_val).replace("_", " ")
    lv_tokens = set(lv.split())
    for ev_raw in ext_values:
        ev = (ev_raw or "").replace("_", " ")
        if lv and lv == ev:
            return True
        ev_tokens = set(ev.split())
        # Every label token must appear as a WHOLE token in the extracted value, and a
        # trivial single short token must not match incidentally.
        if lv_tokens and lv_tokens <= ev_tokens and (len(lv_tokens) > 1 or len(lv) >= 4):
            return True
    return False


def value_survival(ext_doc: CanonicalDoc, lab_doc: CanonicalDoc) -> dict:
    """Every typed value the label marks visible_in_video must appear in some extracted
    action value. The dropped-login / dropped-value detector."""
    ext_values = [norm(a.value) for a in ext_doc.actions if a.value and not a.is_placeholder()]
    targets = [a for a in lab_doc.actions
               if a.value and a.visible_in_video and not a.is_placeholder()]
    survived, dropped = 0, []
    for a in targets:
        if _value_survived(a.value, ext_values):
            survived += 1
        else:
            dropped.append(f"'{a.value}' in '{a.target_label}'")
    recall = survived / len(targets) if targets else 1.0
    return {"value_recall": round(recall, 4), "targets": len(targets),
            "survived": survived, "dropped": dropped}


def page_graph(ext_doc: CanonicalDoc, lab_doc: CanonicalDoc) -> dict:
    def keyset(nodes):
        return [norm_page_key(n.page_key) for n in nodes if not n.is_placeholder()]
    ext_nodes, lab_nodes = set(keyset(ext_doc.page_nodes)), set(keyset(lab_doc.page_nodes))
    node = _prf(len(ext_nodes & lab_nodes), len(ext_nodes - lab_nodes), len(lab_nodes - ext_nodes))
    ext_edges = {(norm_page_key(e.from_key), norm_page_key(e.to_key)) for e in ext_doc.edges}
    lab_edges = {(norm_page_key(e.from_key), norm_page_key(e.to_key)) for e in lab_doc.edges}
    edge = _prf(len(ext_edges & lab_edges), len(ext_edges - lab_edges), len(lab_edges - ext_edges))
    ged = len(ext_edges ^ lab_edges) + len(ext_nodes ^ lab_nodes)
    return {"node": node, "edge": edge, "graph_edit_distance": ged,
            "fabricated_edges": [f"{a}->{b}" for (a, b) in sorted(ext_edges - lab_edges)][:5]}


def _count_silent(misses: list, placeholders: list, label_threshold: float) -> tuple:
    """A miss is FLAGGED (honest) only when an UNCONSUMED placeholder PLAUSIBLY CORRESPONDS
    to it — same page AND a matching control label (and verb, when both name one). Greedy
    1:1. A placeholder for control B must NOT launder a silent drop of control A on the
    same page (review finding H1-residual), else the honesty metric green-washes itself.
    Returns (silent_count, flagged_count)."""
    consumed = [False] * len(placeholders)
    flagged = 0
    for m in misses:
        mk = norm_page_key(getattr(m, "page_key", "") or "")
        mlabel = getattr(m, "target_label", "") or ""
        mverb = norm(getattr(m, "verb", "") or "")
        for i, ph in enumerate(placeholders):
            if consumed[i]:
                continue
            if norm_page_key(getattr(ph, "page_key", "") or "") != mk:
                continue
            if _label_sim(mlabel, getattr(ph, "target_label", "") or "") < label_threshold:
                continue
            phverb = norm(getattr(ph, "verb", "") or "")
            if mverb and phverb and mverb != phverb and phverb not in PLACEHOLDER_TOKENS:
                continue
            consumed[i] = True
            flagged += 1
            break
    return len(misses) - flagged, flagged


def silent_drops(ext_doc: CanonicalDoc, lab_doc: CanonicalDoc, label_threshold: float) -> dict:
    """A miss is HONEST when the artifact carries a placeholder (MISSING_*) that PLAUSIBLY
    CORRESPONDS to it (same page + matching control), and a SILENT failure otherwise.
    completeness-honest requires silent_drops == 0. Only ACTION placeholders can cover an
    ACTION miss — neither an unrelated MISSING_PAGE nor a placeholder for a DIFFERENT
    control can mask a silently-dropped action (review findings H1 + H1-residual)."""
    am = action_metrics(ext_doc, lab_doc, label_threshold)
    labs = [a for a in lab_doc.actions if not a.is_placeholder()]
    missed = [labs[i] for i in am["_un_lab"] if labs[i].visible_in_video]
    action_ph = [a for a in ext_doc.actions if a.is_placeholder()]
    silent, flagged = _count_silent(missed, action_ph, label_threshold)
    return {"visible_misses": len(missed), "placeholders_emitted": len(action_ph),
            "flagged_misses": flagged, "silent_drops": silent,
            "missed_examples": [f"{a.verb} '{a.target_label}'" for a in missed[:5]]}


# ── Top-level scorer ────────────────────────────────────────────────────────────


def score(extracted, label, *, label_threshold: float = 0.5,
          automation_threshold: float = 0.65) -> dict:
    """Score one extracted artifact against its label. Both are CanonicalDoc or dict."""
    ext = extracted if isinstance(extracted, CanonicalDoc) else CanonicalDoc.from_dict(extracted)
    lab = label if isinstance(label, CanonicalDoc) else CanonicalDoc.from_dict(label)
    am = action_metrics(ext, lab, label_threshold)
    return {
        "faithfulness": fabrication_rate(ext, lab, label_threshold, automation_threshold),
        "completeness": {
            "actions": am["overall"],
            "per_verb": am["per_verb"],
            "silent_drops": silent_drops(ext, lab, label_threshold),
        },
        "calibration": calibration(ext, lab, label_threshold),
        "page_graph": page_graph(ext, lab),
        "value_survival": value_survival(ext, lab),
    }


def aggregate(scorecards: list) -> dict:
    """Corpus-level roll-up (means of the headline numbers)."""
    if not scorecards:
        return {}
    def mean(path):
        vals = []
        for s in scorecards:
            cur = s
            for k in path:
                cur = cur.get(k, {}) if isinstance(cur, dict) else {}
            if isinstance(cur, (int, float)):
                vals.append(cur)
        return round(sum(vals) / len(vals), 4) if vals else None
    return {
        "n_videos": len(scorecards),
        "fabrication_rate": mean(["faithfulness", "rate"]),
        "action_f1": mean(["completeness", "actions", "f1"]),
        "ece": mean(["calibration", "ece"]),
        "value_recall": mean(["value_survival", "value_recall"]),
        "total_silent_drops": sum(
            (s.get("completeness", {}).get("silent_drops", {}).get("silent_drops", 0) or 0)
            for s in scorecards),
    }
