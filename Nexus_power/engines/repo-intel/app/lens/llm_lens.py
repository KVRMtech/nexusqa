"""Optional LLM explanation lens — quote-validated, OFF by default.

The lens may ONLY attach a human-readable explanation to an atom, and only
one grounded in a VERBATIM quote that appears in the atom's stored quote /
source. It NEVER sets confidence, NEVER scores, NEVER changes an atom's kind
or band. Any explanation whose supporting quote is not verbatim demotes the
whole judgment to ``unverifiable`` (clone of the platform verbatim-quote
demotion doctrine).

Default is a pure no-op: when the env flag is off or no LLM client is
available, :func:`explain_atoms` returns the atoms unchanged. This keeps the
deterministic App Model authoritative — the lens is decoration, never data.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from app.extract.registry import Atom

logger = logging.getLogger("repo_intel.lens")

_LENS_FLAG = "REPO_INTEL_LLM_LENS_ENABLED"

UNVERIFIABLE = "unverifiable"


@dataclass
class AtomExplanation:
    """A lens output — an atom plus an OPTIONAL, verbatim-grounded note.

    ``status`` is ``verified`` only when ``quote`` appears verbatim in the
    atom's stored quote or provided source; otherwise ``unverifiable`` and the
    explanation text is discarded (never surfaced as if grounded)."""

    atom: Atom
    status: str = UNVERIFIABLE
    explanation: str = ""
    quote: str = ""


def lens_enabled() -> bool:
    return os.environ.get(_LENS_FLAG, "").strip().lower() in {"1", "true", "yes", "on"}


def quote_is_verbatim(quote: str, *sources: str) -> bool:
    """True iff the non-empty ``quote`` appears verbatim in any source."""
    q = (quote or "").strip()
    if not q:
        return False
    return any(q in (s or "") for s in sources)


def explain_atoms(
    atoms: List[Atom],
    *,
    llm_client: Optional[object] = None,
    enabled: Optional[bool] = None,
    source_reader: Optional[Callable[[Atom], str]] = None,
) -> List[AtomExplanation]:
    """Attach verbatim-grounded explanations. No-op unless enabled AND a
    client is provided.

    ``source_reader`` optionally returns the atom's source text so the quote
    can be validated against the full construct (not just the truncated
    stored quote). Absent ⇒ validate against the stored quote only.
    """
    use = lens_enabled() if enabled is None else enabled
    if not use or llm_client is None:
        # Pure pass-through — the App Model is unchanged.
        return [AtomExplanation(atom=a, status=UNVERIFIABLE) for a in atoms]

    out: List[AtomExplanation] = []
    for atom in atoms:
        try:
            proposal = _propose(llm_client, atom)
        except Exception as exc:  # lens failure never corrupts the model
            logger.debug("lens proposal failed: %s", exc)
            out.append(AtomExplanation(atom=atom, status=UNVERIFIABLE))
            continue
        quote = (proposal or {}).get("quote", "")
        text = (proposal or {}).get("explanation", "")
        sources = [atom.quote]
        if source_reader is not None:
            try:
                sources.append(source_reader(atom))
            except Exception:
                pass
        if quote and quote_is_verbatim(quote, *sources):
            out.append(AtomExplanation(atom=atom, status="verified",
                                       explanation=text, quote=quote))
        else:
            # Demote: a non-verbatim (possibly hallucinated) quote is not trusted.
            out.append(AtomExplanation(atom=atom, status=UNVERIFIABLE))
    return out


def _propose(llm_client: object, atom: Atom) -> Dict:
    """Ask the client for a {explanation, quote} grounded in the atom.

    The client contract is intentionally minimal: a callable/duck-typed
    object exposing ``explain_atom(atom_dict) -> {explanation, quote}``. This
    keeps the lens decoupled from any specific SDK client version.
    """
    fn = getattr(llm_client, "explain_atom", None)
    if not callable(fn):
        return {}
    result = fn({"kind": atom.kind, "value": atom.value, "quote": atom.quote,
                 "provenance": f"{atom.provenance_path}:{atom.provenance_line}"})
    return result if isinstance(result, dict) else {}
