"""The value-free decision-point signature — the EXPLORER's half (M2.6 / T-CAP-02).

WHY A SECOND IMPLEMENTATION EXISTS.  A crawl remembers an advance by the shape
of the decision it faced, and that key has to be identical on both sides of the
wire: qe-central computes it when it answers ``/internal/pick-advance``, and the
explorer must compute the SAME string when it advances WITHOUT asking — because
a deterministic tier-1/2 advance is exactly as proven as a tier-3 one and is
supposed to be recallable at the same decision point.  The two services share no
library (they are separately deployed containers with separate dependency sets),
so this is a deliberate MIRROR of
:func:`app.services.advance_agent.compute_signature` in qe-central, kept
data-identical by a FROZEN VECTOR pinned in BOTH suites:

    qe-explorer/tests/test_advance_signature.py::test_signature_parity_vector
    qe-central/tests/test_advance_memory.py::test_signature_parity_vector

That is the same doctrine ``app/vocab.py`` and qe-central's ``advance_vocab``
already live under, and the only one available: a cross-process contract cannot
be proven inside one process, so it is frozen as DATA that both processes assert
against.  Change the basis here and that vector fails on both sides — which is
the point.  Changing it silently would not corrupt memory (a different basis
just yields a different key, so old rows stop matching), but it WOULD quietly
stop every remembered advance from ever being recalled again.

WHAT GOES INTO IT — and what must never.  Control kinds and accessible NAMES
(product UI text) plus the page title's word shape.  No URLs, no hosts, no
values, nothing tenant-identifying: the signature is persisted as tenant memory
and echoed into crawl evidence, so it has to be safe in both places.  Sorted, so
DOM order does not change the identity of the same decision point.
"""
from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from typing import Any

#: Title tokenisation.  Deliberately letters-only: a title carrying an order
#: number or a policy id must not give every visit its own decision point.
_WORD_RE = re.compile(r"[a-z]+")

_MAX_TITLE_SHAPE = 120


def compute_signature(
    candidates: Sequence[Mapping[str, Any]], page_title: str = "",
) -> str:
    """The decision-point signature for ``candidates`` on a page titled
    ``page_title``.

    ``candidates`` MUST be the set that would have been sent to the oracle —
    i.e. the output of ``Walker._tier3_candidates`` — because that is the list
    qe-central computes over.  Handing it the full inventory instead would
    produce a key no recall could ever hit.
    """
    names = sorted(
        f"{str(c.get('kind') or '')}::{str(c.get('name') or '').strip().lower()}"
        for c in (candidates or ())
    )
    title_shape = " ".join(
        _WORD_RE.findall(str(page_title or "").lower()))[:_MAX_TITLE_SHAPE]
    basis = "\n".join(names) + "\n#title::" + title_shape
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()


__all__ = ["compute_signature"]
