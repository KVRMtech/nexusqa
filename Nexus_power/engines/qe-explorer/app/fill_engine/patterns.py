"""A BOUNDED, DETERMINISTIC SATISFIER FOR THE REGEXES AN APPLICATION DECLARES.

An HTML ``pattern`` attribute is the most precise thing an application ever says
about a value.  ``pattern="\\d{3}-\\d{2}-\\d{4}"`` does not hint at a national
identity number, it SPECIFIES one; ``pattern="[A-Z]{2}\\d{6}"`` specifies a
policy reference no amount of label-reading would ever produce.

The old generator read patterns to CLASSIFY and never to GENERATE, so a field
with a declared shape got a value chosen for its meaning and then rejected for
its shape.  This module closes that: given a pattern, produce a string the
pattern accepts.

THREE ENTRY POINTS, in the order a caller should try them:

  :func:`matches`   does this value already satisfy the pattern?  (HTML
                    ``pattern`` semantics — implicitly anchored at both ends.)
  :func:`reshape`   the value's own characters, re-punctuated to fit — an SSN
                    typed ``123456789`` becomes ``123-45-6789``.  This preserves
                    MEANING, which a freshly generated string cannot.
  :func:`satisfy`   a minimal string the pattern accepts.  Last resort: it is
                    grounded, because the application described it, but it means
                    nothing.

WHY A HAND-WRITTEN PARSER RATHER THAN A LIBRARY.  This runs inside the
quarantined explorer, on every constrained control of every crawl, against
patterns written by third parties.  A general regex-inverter is a large
dependency and an unbounded amount of work on a hostile input; the subset below
covers what applications actually declare and REFUSES everything else by
returning ``None``, which the caller reads as "leave the field honestly empty".

SUPPORTED SUBSET — literals, ``.``, escapes (``\\d \\w \\s \\D \\W \\S`` and
escaped punctuation), character classes including ranges and negation,
groups (capturing and non-capturing), alternation, and the quantifiers
``? * + {n} {n,} {n,m}``.  Anchors ``^``/``$`` are accepted and ignored, since
HTML anchors the whole value anyway.

REFUSED — backreferences, lookaround, named groups, unicode property escapes,
and anything that would need more than :data:`_MAX_NODES` nodes or produce more
than :data:`_MAX_LENGTH` characters.  A refusal returns ``None``.  It never
raises, never loops unboundedly, and never consults a clock or a random source:
the same pattern always yields the same string, so a value recorded in evidence
replays exactly.
"""
from __future__ import annotations

import re
from typing import Optional

__all__ = ["matches", "satisfy", "reshape", "PatternRefused"]

#: Hard ceilings.  A pattern that needs more than this is refused rather than
#: chased — an unbounded generator on a hostile pattern is a denial of service
#: against our own crawl.
_MAX_NODES = 400
_MAX_LENGTH = 256
#: How many repetitions an unbounded quantifier produces.  ``+`` needs one and
#: ``*`` needs none, and the smallest legal string is the one least likely to
#: breach a maxlength the same control also declares.
_STAR_REPEAT = 0
_PLUS_REPEAT = 1

#: Constructs this module refuses outright.  Each would need semantics the
#: subset does not have, and guessing at them would produce a string that looks
#: right and is not.
_UNSUPPORTED_RE = re.compile(
    r"\(\?<|\(\?[=!]|\(\?P|\\[1-9]|\\[pP]\{|\\[bBAZzGk]")

#: The canonical character each escape produces.  Digits pick ``5`` and letters
#: pick ``a`` rather than the first member of the class, because a leading zero
#: is rejected by a surprising number of applications that declare only a
#: pattern, and a value of all zeroes reads as missing data to a human.
_CLASS_SAMPLE = {
    "d": "5",
    "w": "a",
    "s": " ",
    "D": "a",
    "W": "-",
    "S": "a",
}
_ANY_SAMPLE = "a"


class PatternRefused(Exception):
    """The pattern is outside the supported subset.  Internal control flow only —
    every public function converts it to ``None``."""


# ── the tiny AST ─────────────────────────────────────────────────────────────
# Nodes are plain tuples so the whole parser stays readable in one screen:
#   ("lit", text)                a literal string
#   ("class", chars, negated)    a character class
#   ("group", alternatives)      alternatives, each a list of nodes
#   ("rep", node, lo, hi)        a quantified node (hi None ⇒ unbounded)


def _parse(pattern: str) -> list[list]:
    """Parse into alternatives.  Raises :class:`PatternRefused` on anything
    outside the subset."""
    if _UNSUPPORTED_RE.search(pattern):
        raise PatternRefused("unsupported construct")
    pos = 0
    budget = [_MAX_NODES]

    def parse_alternatives(depth: int) -> list[list]:
        nonlocal pos
        if depth > 12:
            raise PatternRefused("nesting too deep")
        alts: list[list] = []
        current: list = []
        while pos < len(pattern):
            ch = pattern[pos]
            if ch == ")":
                break
            if ch == "|":
                pos += 1
                alts.append(current)
                current = []
                continue
            current.append(parse_quantified(depth))
        alts.append(current)
        return alts

    def parse_quantified(depth: int):
        nonlocal pos
        node = parse_atom(depth)
        while pos < len(pattern) and pattern[pos] in "*+?{":
            ch = pattern[pos]
            if ch == "*":
                pos += 1
                node = ("rep", node, 0, None)
            elif ch == "+":
                pos += 1
                node = ("rep", node, 1, None)
            elif ch == "?":
                pos += 1
                node = ("rep", node, 0, 1)
            else:
                m = re.match(r"\{(\d+)(,(\d*)?)?\}", pattern[pos:])
                if not m:
                    break                      # a literal brace, handled as text
                pos += m.end()
                lo = int(m.group(1))
                if m.group(2) is None:
                    hi: Optional[int] = lo
                elif not m.group(3):
                    hi = None
                else:
                    hi = int(m.group(3))
                if lo > _MAX_LENGTH or (hi is not None and hi > _MAX_LENGTH * 2):
                    raise PatternRefused("quantifier too large")
                node = ("rep", node, lo, hi)
            # A lazy/possessive marker changes nothing about WHETHER a string
            # matches, only which one a matcher prefers, so it is consumed.
            if pos < len(pattern) and pattern[pos] in "?+":
                pos += 1
        return node

    def parse_atom(depth: int):
        nonlocal pos
        budget[0] -= 1
        if budget[0] < 0:
            raise PatternRefused("pattern too large")
        ch = pattern[pos]
        if ch == "(":
            pos += 1
            if pattern[pos:pos + 2] == "?:":
                pos += 2
            elif pattern[pos:pos + 1] == "?":
                raise PatternRefused("unsupported group flag")
            alts = parse_alternatives(depth + 1)
            if pos >= len(pattern) or pattern[pos] != ")":
                raise PatternRefused("unbalanced group")
            pos += 1
            return ("group", alts)
        if ch == "[":
            return parse_class()
        if ch == "\\":
            pos += 1
            if pos >= len(pattern):
                raise PatternRefused("trailing escape")
            esc = pattern[pos]
            pos += 1
            if esc in _CLASS_SAMPLE:
                return ("class", _CLASS_SAMPLE[esc], False)
            return ("lit", esc)
        if ch == ".":
            pos += 1
            return ("class", _ANY_SAMPLE, False)
        if ch in "^$":
            pos += 1
            return ("lit", "")             # HTML anchors the whole value anyway
        pos += 1
        return ("lit", ch)

    def parse_class():
        nonlocal pos
        pos += 1                            # consume '['
        negated = False
        if pos < len(pattern) and pattern[pos] == "^":
            negated = True
            pos += 1
        members: list[str] = []
        first = True
        while pos < len(pattern) and (pattern[pos] != "]" or first):
            first = False
            ch = pattern[pos]
            if ch == "\\":
                pos += 1
                if pos >= len(pattern):
                    raise PatternRefused("trailing escape in class")
                esc = pattern[pos]
                pos += 1
                members.append(_CLASS_SAMPLE.get(esc, esc))
                continue
            if (pos + 2 < len(pattern) and pattern[pos + 1] == "-"
                    and pattern[pos + 2] != "]"):
                lo_c, hi_c = pattern[pos], pattern[pos + 2]
                pos += 3
                if ord(hi_c) < ord(lo_c):
                    raise PatternRefused("inverted range")
                members.append(_range_sample(lo_c, hi_c))
                continue
            members.append(ch)
            pos += 1
        if pos >= len(pattern):
            raise PatternRefused("unterminated class")
        pos += 1                            # consume ']'
        if not members:
            raise PatternRefused("empty class")
        return ("class", "".join(members), negated)

    alts = parse_alternatives(0)
    if pos != len(pattern):
        raise PatternRefused("unconsumed input")
    return alts


def _range_sample(lo_c: str, hi_c: str) -> str:
    """A representative member of a range.

    Prefers a MIDDLE member over the first: ``[0-9]`` yields ``5`` rather than
    ``0``, because a leading zero is refused by a surprising number of
    applications whose only declaration is the pattern itself."""
    if lo_c == "0" and hi_c == "9":
        return "5"
    if lo_c == "a" and hi_c == "z":
        return "a"
    if lo_c == "A" and hi_c == "Z":
        return "A"
    span = ord(hi_c) - ord(lo_c)
    return chr(ord(lo_c) + (span // 2 if span > 1 else 0))


#: A character that is NOT in a negated class.  Tried in order, so a negated
#: class gets a letter before it gets punctuation.
_NEGATED_FALLBACKS = "abcdefghijklmnopqrstuvwxyz0123456789-_. "


def _sample_class(chars: str, negated: bool) -> str:
    if not negated:
        return chars[0] if chars else _ANY_SAMPLE
    for candidate in _NEGATED_FALLBACKS:
        if candidate not in chars:
            return candidate
    raise PatternRefused("negated class excludes every candidate")


def _render(nodes: list, out: list[str]) -> None:
    for node in nodes:
        kind = node[0]
        if kind == "lit":
            out.append(node[1])
        elif kind == "class":
            out.append(_sample_class(node[1], node[2]))
        elif kind == "group":
            # THE FIRST ALTERNATIVE, ALWAYS.  Determinism is the property that
            # makes a recorded value replayable, and "the first branch" is the
            # only choice rule that needs no state.
            _render(node[1][0], out)
        elif kind == "rep":
            lo, hi = node[2], node[3]
            count = lo if lo > 0 else (
                _PLUS_REPEAT if hi is None and lo == 1 else _STAR_REPEAT)
            if lo == 0 and hi is None:
                count = _STAR_REPEAT
            elif lo == 0 and hi is not None:
                count = 0
            for _ in range(count):
                _render([node[1]], out)
        if sum(len(s) for s in out) > _MAX_LENGTH:
            raise PatternRefused("generated value too long")


def _anchored(pattern: str) -> str:
    """HTML ``pattern`` is implicitly anchored at both ends, and every consumer
    here follows that rule so ``matches`` and ``satisfy`` agree."""
    return pattern


def matches(value: str, pattern: str) -> bool:
    """Does ``value`` satisfy ``pattern`` under HTML ``pattern`` semantics?

    Anchored at both ends, because that is what a browser does.  An
    UNCOMPILABLE pattern returns ``True``: the application declared something we
    cannot evaluate, and refusing every value against it would leave the field
    permanently unfillable on the strength of our own parser's limits."""
    if not pattern:
        return True
    try:
        return re.fullmatch(pattern, value or "") is not None
    except re.error:
        return True


def satisfy(pattern: str, *, hint: str = "") -> Optional[str]:
    """A minimal string the pattern accepts, or ``None`` when refused.

    ``hint`` is the value the caller WANTED — it is not used to build the
    result (that would make the output depend on unrelated input and break
    replay) but it is checked first: a hint that already matches is returned
    unchanged, which keeps a meaningful value in preference to a generated one."""
    if not pattern:
        return None
    if hint and matches(hint, pattern):
        return hint
    try:
        alts = _parse(pattern.strip("^$"))
    except PatternRefused:
        return None
    except Exception:                       # a malformed pattern is not our crash
        return None
    for alternative in alts:
        out: list[str] = []
        try:
            _render(alternative, out)
        except PatternRefused:
            continue
        except Exception:
            continue
        candidate = "".join(out)
        if matches(candidate, pattern):
            return candidate
    return None


def reshape(value: str, pattern: str) -> Optional[str]:
    """Re-punctuate the value's OWN characters to fit the pattern.

    THE CASE THIS EXISTS FOR.  A national identity number the persona generated
    as ``912-34-5678`` meets a field declaring ``\\d{9}``; the digits are right
    and only the punctuation is wrong.  Generating a fresh value would discard a
    number the persona chose so that it stays the same on every page of the
    funnel that asks for it — which is the whole reason the journey cache exists.

    Two reshapes are attempted, both information-preserving:
      * the value's digits alone, when the pattern wants only digits;
      * the value's digits redistributed into the pattern's literal punctuation,
        so ``123456789`` becomes ``123-45-6789`` for ``\\d{3}-\\d{2}-\\d{4}``.

    Returns ``None`` when neither works — never a value with characters the
    original did not contain."""
    if not value or not pattern:
        return None
    if matches(value, pattern):
        return value
    digits = re.sub(r"\D", "", value)
    if digits and matches(digits, pattern):
        return digits
    alnum = re.sub(r"[^A-Za-z0-9]", "", value)
    if alnum and matches(alnum, pattern):
        return alnum
    if not digits:
        return None
    try:
        alts = _parse(pattern.strip("^$"))
    except Exception:
        return None
    for alternative in alts:
        rebuilt = _redistribute(alternative, digits)
        if rebuilt is not None and matches(rebuilt, pattern):
            return rebuilt
    return None


def _redistribute(nodes: list, digits: str) -> Optional[str]:
    """Lay the value's digits into the pattern's shape, keeping its literals.

    Only digit-class nodes consume from the supply; a literal contributes
    itself.  Any other node makes the reshape impossible, and we say so by
    returning ``None`` rather than inventing the missing character."""
    out: list[str] = []
    supply = list(digits)

    def take() -> Optional[str]:
        return supply.pop(0) if supply else None

    def walk(node_list: list) -> bool:
        for node in node_list:
            kind = node[0]
            if kind == "lit":
                out.append(node[1])
            elif kind == "class":
                if node[2] or not node[1].isdigit():
                    return False
                ch = take()
                if ch is None:
                    return False
                out.append(ch)
            elif kind == "group":
                if len(node[1]) != 1:
                    return False
                if not walk(node[1][0]):
                    return False
            elif kind == "rep":
                lo, hi = node[2], node[3]
                count = lo if lo > 0 else 0
                if hi is None and lo == 0:
                    count = len(supply)
                for _ in range(count):
                    if not walk([node[1]]):
                        return False
            else:
                return False
            if len(out) > _MAX_LENGTH:
                return False
        return True

    if not walk(nodes):
        return None
    return "".join(out)
