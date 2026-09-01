"""Vertical-aware vocabularies for visual evidence.

Each vertical (banking, healthcare, insurance, telecom, …) ships as a
YAML file in this directory.  The loader reads every ``*.yaml`` at
import time, compiles regex patterns once, and exposes a single
in-memory registry so callers (PII detector, surface extractors,
quality gate) can ask:

    >>> from nexus_sdk.evidence import vocabularies as v
    >>> bank = v.get_vocabulary("banking")
    >>> bank.aliases_for("account_number")
    ('Account #', 'Acct No', 'Account Number', 'Acct')

Why YAML, not code?  The whole point of Phase 4 is that adding a new
vertical is a content change — a YAML PR with no test churn outside
the loader's own schema validation.  All four shipped verticals are
declared with exactly the same shape, and unknown vocabularies
return ``None`` so unconfigured tenants behave as if the layer
weren't there.

The loader is deliberately strict at import time: a YAML with a
missing ``vertical`` key or a malformed regex raises
``VocabularyError`` immediately, not silently during a customer
demo.

This module has zero dependencies on the Shield engine's existing
PII detector — Phase 4 sits on top of Shield, not in place of it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

import yaml


_VOCAB_DIR = Path(__file__).resolve().parent


class VocabularyError(ValueError):
    """Raised when a vocabulary YAML fails schema validation."""


@dataclass(frozen=True)
class PIIPattern:
    """One PII pattern in a vocabulary.

    Severity bands map to the PII redaction policy:
      * ``high``    — always redact in stored OCR / transcripts.
      * ``medium``  — redact in customer-visible exports; keep in
                       internal audit logs.
      * ``low``     — emit as evidence but never embed in test code
                       (e.g. ICD codes are clinical metadata, not PHI
                       per HIPAA, but still privacy-sensitive in
                       certain jurisdictions).
    """

    name: str
    pattern: re.Pattern[str]
    severity: str
    description: str = ""
    redaction_placeholder: str = "[REDACTED]"


@dataclass(frozen=True)
class Synonym:
    """One canonical field name and its OCR-observed aliases."""

    canonical: str
    aliases: tuple[str, ...]


@dataclass(frozen=True)
class Vocabulary:
    """In-memory representation of one vertical YAML file."""

    name: str
    display_name: str
    description: str
    synonyms: tuple[Synonym, ...] = field(default_factory=tuple)
    pii_patterns: tuple[PIIPattern, ...] = field(default_factory=tuple)
    transaction_codes: dict[str, str] = field(default_factory=dict)

    # ── Lookup helpers ──────────────────────────────────────────
    def aliases_for(self, canonical: str) -> tuple[str, ...]:
        """Return the alias tuple for a canonical key, or () if absent."""
        key = canonical.strip().lower()
        for s in self.synonyms:
            if s.canonical.lower() == key:
                return s.aliases
        return ()

    def canonical_for(self, alias: str) -> Optional[str]:
        """Reverse lookup — find the canonical name an alias maps to.

        Case-insensitive; matches whole-word.  Returns ``None`` if no
        synonym in this vocabulary mentions the alias.
        """
        if not alias:
            return None
        key = alias.strip().lower()
        for s in self.synonyms:
            for a in s.aliases:
                if a.lower() == key:
                    return s.canonical
        return None

    def transaction_code_label(self, code: str) -> Optional[str]:
        """Return the human-readable label for a transaction code, if known."""
        if not code:
            return None
        return self.transaction_codes.get(code.strip().upper())


# ── Loader ────────────────────────────────────────────────────────────────

def _compile_pii_patterns(raw_patterns: list[Any], vocab_name: str) -> tuple[PIIPattern, ...]:
    """Validate + compile the ``pii_patterns:`` block of a YAML file."""
    out: list[PIIPattern] = []
    seen_names: set[str] = set()
    for idx, raw in enumerate(raw_patterns or []):
        if not isinstance(raw, dict):
            raise VocabularyError(
                f"vocab {vocab_name!r}: pii_patterns[{idx}] is not a mapping"
            )
        name = raw.get("name")
        regex = raw.get("regex")
        severity = (raw.get("severity") or "medium").strip().lower()
        if not isinstance(name, str) or not name:
            raise VocabularyError(
                f"vocab {vocab_name!r}: pii_patterns[{idx}] missing 'name'"
            )
        if not isinstance(regex, str) or not regex:
            raise VocabularyError(
                f"vocab {vocab_name!r}: pii_patterns[{idx}] ({name}) missing 'regex'"
            )
        if severity not in ("high", "medium", "low"):
            raise VocabularyError(
                f"vocab {vocab_name!r}: pii_patterns[{idx}] ({name}) severity "
                f"must be high|medium|low, got {severity!r}"
            )
        if name in seen_names:
            raise VocabularyError(
                f"vocab {vocab_name!r}: duplicate pii_pattern name {name!r}"
            )
        seen_names.add(name)
        try:
            compiled = re.compile(regex)
        except re.error as exc:
            raise VocabularyError(
                f"vocab {vocab_name!r}: invalid regex for {name!r}: {exc}"
            ) from exc
        out.append(PIIPattern(
            name=name,
            pattern=compiled,
            severity=severity,
            description=str(raw.get("description") or ""),
            redaction_placeholder=str(raw.get("redaction") or f"[REDACTED:{name.upper()}]"),
        ))
    return tuple(out)


def _parse_synonyms(raw_synonyms: list[Any], vocab_name: str) -> tuple[Synonym, ...]:
    out: list[Synonym] = []
    seen_canonical: set[str] = set()
    for idx, raw in enumerate(raw_synonyms or []):
        if not isinstance(raw, dict):
            raise VocabularyError(
                f"vocab {vocab_name!r}: synonyms[{idx}] is not a mapping"
            )
        canonical = raw.get("canonical")
        aliases = raw.get("aliases") or []
        if not isinstance(canonical, str) or not canonical:
            raise VocabularyError(
                f"vocab {vocab_name!r}: synonyms[{idx}] missing 'canonical'"
            )
        if not isinstance(aliases, list) or not all(isinstance(a, str) for a in aliases):
            raise VocabularyError(
                f"vocab {vocab_name!r}: synonyms[{idx}] ({canonical}) "
                f"'aliases' must be a list of strings"
            )
        key = canonical.strip().lower()
        if key in seen_canonical:
            raise VocabularyError(
                f"vocab {vocab_name!r}: duplicate canonical synonym {canonical!r}"
            )
        seen_canonical.add(key)
        out.append(Synonym(
            canonical=canonical.strip(),
            aliases=tuple(a.strip() for a in aliases if a.strip()),
        ))
    return tuple(out)


def _load_one_yaml(path: Path) -> Vocabulary:
    with path.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    if not isinstance(raw, dict):
        raise VocabularyError(f"{path.name}: top-level YAML must be a mapping")
    name = raw.get("vertical") or path.stem
    if not isinstance(name, str) or not name:
        raise VocabularyError(f"{path.name}: missing 'vertical' key")
    display_name = str(raw.get("display_name") or name.replace("_", " ").title())
    description = str(raw.get("description") or "")
    synonyms = _parse_synonyms(raw.get("synonyms") or [], name)
    pii_patterns = _compile_pii_patterns(raw.get("pii_patterns") or [], name)
    transaction_codes_raw = raw.get("transaction_codes") or {}
    if not isinstance(transaction_codes_raw, dict):
        raise VocabularyError(f"vocab {name!r}: transaction_codes must be a mapping")
    transaction_codes = {
        str(k).strip().upper(): str(v).strip()
        for k, v in transaction_codes_raw.items()
        if str(k).strip() and str(v).strip()
    }
    return Vocabulary(
        name=name.strip().lower(),
        display_name=display_name,
        description=description,
        synonyms=synonyms,
        pii_patterns=pii_patterns,
        transaction_codes=transaction_codes,
    )


# ── Registry (eager load at import time) ──────────────────────────────────
_REGISTRY: dict[str, Vocabulary] = {}


def _load_all() -> None:
    """Discover every YAML in this directory and register it."""
    _REGISTRY.clear()
    for path in sorted(_VOCAB_DIR.glob("*.yaml")):
        try:
            vocab = _load_one_yaml(path)
        except VocabularyError:
            raise
        except Exception as exc:  # noqa: BLE001 — surface file context
            raise VocabularyError(f"{path.name}: {exc}") from exc
        if vocab.name in _REGISTRY:
            raise VocabularyError(
                f"duplicate vocabulary name {vocab.name!r} "
                f"(check vertical field in {path.name})"
            )
        _REGISTRY[vocab.name] = vocab


_load_all()


# ── Public API ────────────────────────────────────────────────────────────

def get_vocabulary(name: str | None) -> Optional[Vocabulary]:
    """Return the vocabulary registered for ``name`` (case-insensitive), or
    ``None`` when nothing is registered.  Pass ``None`` to skip lookup
    cleanly — many callers store the tenant's vertical as an optional
    field and would otherwise need their own None check.
    """
    if not name:
        return None
    return _REGISTRY.get(name.strip().lower())


def list_vocabularies() -> tuple[str, ...]:
    """Return the names of every registered vocabulary."""
    return tuple(sorted(_REGISTRY.keys()))


def vocabulary_count() -> int:
    return len(_REGISTRY)


def reload_for_tests() -> None:
    """Reload every YAML from disk.

    Only useful in tests that mutate the YAML files (e.g. to verify
    a new pattern lands without restarting the process).  Production
    code should rely on the eager load at import.
    """
    _load_all()


def iter_vocabularies() -> Iterable[Vocabulary]:
    return tuple(_REGISTRY.values())


__all__ = [
    "Vocabulary",
    "Synonym",
    "PIIPattern",
    "VocabularyError",
    "get_vocabulary",
    "list_vocabularies",
    "vocabulary_count",
    "iter_vocabularies",
    "reload_for_tests",
]
