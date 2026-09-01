"""Deterministic pipeline fixes — proven against the harness baseline (Phase 3).

Each function is a GENERIC, no-hardcoding transform on a CanonicalDoc, so its measured
impact can be shown (baseline X -> fix -> Y) before porting it into the live pipeline.
"""
from __future__ import annotations

import os
import re
import sys
from collections import defaultdict
from dataclasses import replace

sys.path.insert(0, os.path.dirname(__file__))
from harness import CanonicalDoc, norm_page_key  # noqa: E402

_SEP = re.compile(r"\s[-–—:|]\s")  # " - ", " : ", " | " separators


def decompose_repeated_prefix_labels(doc: CanonicalDoc) -> CanonicalDoc:
    """DISAMBIGUATOR. When >=2 actions on the SAME page share a common PREFIX before a
    separator (e.g. '<action> - <row label>' repeated per row/card), the prefix is the
    real control name and the suffix is the per-row anchor. Recovers the usable control
    name (and the anchor) WITHOUT hardcoding — the repeated structure reveals it. This is
    the fix for the over-qualified-locator defect, where the real control name was buried
    inside a row-qualified label so the locator matched 0 (or N) elements."""
    by_page = defaultdict(list)
    for i, a in enumerate(doc.actions):
        by_page[a.page_key].append(i)
    new = list(doc.actions)
    for _page, idxs in by_page.items():
        prefixes = defaultdict(list)
        for i in idxs:
            parts = _SEP.split(doc.actions[i].target_label, 1)
            if len(parts) == 2 and parts[0].strip() and parts[1].strip():
                prefixes[parts[0].strip().lower()].append((i, parts[0].strip(), parts[1].strip()))
        for _pk, items in prefixes.items():
            if len(items) >= 2:  # repeated prefix => control name + per-row anchor
                for i, prefix, _suffix in items:
                    new[i] = replace(new[i], target_label=prefix)
    return CanonicalDoc(page_nodes=list(doc.page_nodes), actions=new, edges=list(doc.edges))


# Recording-environment chrome: video-conferencing / screen-share tools and cloud/dev
# consoles used to RECORD — never the app under test. These are generic recording-TOOL
# categories (not app-under-test hosts), and the list is meant to be config-extensible
# per deployment. No app-specific identifiers appear here.
_CHROME_RX = re.compile(
    r"video\s*call|video\s*conference|streaming application|main feed|screen.?share|"
    r"zoom|google meet|microsoft teams|webex|google cloud|cloud console",
    re.I)


def drop_recording_chrome_pages(doc: CanonicalDoc) -> CanonicalDoc:
    """Drop page-nodes whose identity is the RECORDING environment (a video-call UI, a
    browser/console shell) rather than the app under test — generic chrome words, no app
    hosts. Also drops actions stranded on those nodes. Fixes the phantom-page defect."""
    bad = {norm_page_key(n.page_key) for n in doc.page_nodes
           if _CHROME_RX.search(n.location or "") or _CHROME_RX.search(n.page_key or "")}
    nodes = [n for n in doc.page_nodes if norm_page_key(n.page_key) not in bad]
    actions = [a for a in doc.actions if norm_page_key(a.page_key) not in bad]
    edges = [e for e in doc.edges
             if norm_page_key(e.from_key) not in bad and norm_page_key(e.to_key) not in bad]
    return CanonicalDoc(page_nodes=nodes, actions=actions, edges=edges)


def kill_low_confidence_fabricated_navigations(doc: CanonicalDoc, floor: float = 0.6) -> CanonicalDoc:
    """Drop verb=navigate actions whose confidence sits at/under the fabrication floor
    (the `navigate@0.55` rule synthesizes these from an OCR URL flicker with no on-page
    evidence). Keeps grounded navigations. Fixes the fabricated-transition defect."""
    actions = [a for a in doc.actions
               if not (a.verb.strip().lower() == "navigate" and a.confidence <= floor)]
    return CanonicalDoc(page_nodes=list(doc.page_nodes), actions=actions, edges=list(doc.edges))


def apply_all(doc: CanonicalDoc) -> CanonicalDoc:
    """The full deterministic clean-up stack."""
    doc = decompose_repeated_prefix_labels(doc)
    doc = drop_recording_chrome_pages(doc)
    doc = kill_low_confidence_fabricated_navigations(doc)
    return doc
