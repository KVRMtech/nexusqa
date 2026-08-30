"""THE LLM DATA AGENT — rung 8 of the value ladder (opt-in, provenance-stamped).

WHY THIS EXISTS. The operator can choose, in the portal, that a crawl must never
stop for data: every field gets the most plausible value the system can produce,
and the evidence records where each value came from. The deterministic ladder
already answers most classes; what it cannot answer it hands to this agent
before anything is allowed to become residue:

  * FREE TEXT — "describe your condition" fell back to the literal string
    "autotest" (MEASURED: 39 of 48 live rejections were exactly this);
  * fields whose meaning no deterministic rung could establish;
  * REPAIR — the application rejected a value and named its rule; the agent is
    asked for a value that satisfies the stated rule.

WHAT IT MUST NEVER DO, and the tests hold it to:

  * override a truer rung — the client's answer key, journey memory, a recalled
    value always win; the agent only fills what would otherwise be empty (or
    the known placeholder);
  * answer a CREDENTIAL — a password or one-time code is not data, and a made-up
    one only burns an auth attempt the auth flow owns;
  * return an option the control does not offer — an enumerable field's answer
    is validated against the enumeration and clamped to None when off-list;
  * stop the crawl — every failure path (no key, timeout, HTTP error, breaker,
    cap) returns None, which is exactly the residue behaviour the crawl has
    always had. The agent can only ever ADD answers.

GOVERNMENT / FINANCIAL IDENTIFIERS come only from designated test ranges (the
prompt instructs 900-series SSNs, Luhn test PANs, test routing numbers), so a
generated value is structurally valid and provably nobody's.

Resilience mirrors the advance oracle: a per-crawl call cap and a
consecutive-failure circuit breaker, both of which end consultations with None
rather than exceptions. The API key is read from the environment at call time
and never logged.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Optional, Sequence

import httpx

logger = logging.getLogger(__name__)

#: Semantic types that are credentials, not data. The agent refuses these
#: unconditionally — a fabricated OTP is an auth attempt, not an answer.
_CREDENTIAL_TYPES = frozenset({"password", "otp", "one_time_code"})

_SYSTEM_PROMPT = (
    "You supply ONE test value for ONE form field in a disposable test "
    "environment. Reply with the value only — no quotes, no explanation, no "
    "labels. Rules: "
    "(1) If a list of allowed options is given, reply with EXACTLY one of them, "
    "verbatim. "
    "(2) Respect every stated constraint (pattern, min, max, maxlength, date "
    "windows). "
    "(3) For government or financial identifiers use designated TEST ranges "
    "only: SSNs in the 900-series, card numbers that pass Luhn from published "
    "test prefixes (e.g. 4111111111111111), routing number 021000021. "
    "(4) Prefer a plausible, internally consistent value over a clever one; "
    "short over long. "
    "(5) If a rejection message from the application is provided, your value "
    "MUST satisfy the rule that message states. "
    "(6) Never reply with a refusal or a question — always produce a value."
)


class LLMDataAgent:
    """Sync value provider. Built once per crawl; never raises; None on failure."""

    def __init__(self, *, model: str = "", max_calls: int = 150,
                 breaker_threshold: int = 3, timeout_s: float = 10.0,
                 transport: Optional[httpx.BaseTransport] = None) -> None:
        self.model = model or os.environ.get("QEC_DATA_LLM_MODEL", "gpt-4o-mini")
        self.max_calls = max_calls
        self.breaker_threshold = breaker_threshold
        self.calls = 0
        self.answered = 0
        self.failures_in_a_row = 0
        self.breaker_open = False
        self._client = httpx.Client(timeout=timeout_s, transport=transport)

    # -- the one entry point --------------------------------------------------
    def value_for(self, *, name: str, semantic_type: str, kind: str,
                  options: Sequence[str] = (), constraints: str = "",
                  section: str = "", page_title: str = "",
                  rejection: str = "") -> Optional[str]:
        if semantic_type in _CREDENTIAL_TYPES or kind == "password":
            return None
        if self.breaker_open or self.calls >= self.max_calls:
            if self.calls == self.max_calls and not self.breaker_open:
                logger.info("qec.llm_data.cap_reached max_calls=%d", self.max_calls)
            return None
        key = os.environ.get("OPENAI_API_KEY", "")
        if not key:
            return None
        self.calls += 1
        parts = [f"Field: {name or '(unnamed)'}", f"Kind: {kind or 'text'}"]
        if semantic_type:
            parts.append(f"Semantic type: {semantic_type}")
        if section:
            parts.append(f"Section: {section}")
        if page_title:
            parts.append(f"Page: {page_title}")
        if constraints:
            parts.append(f"Constraints: {constraints}")
        if options:
            parts.append("Allowed options (reply with one, verbatim): "
                         + " | ".join(str(o) for o in list(options)[:40]))
        if rejection:
            parts.append("The application rejected the previous value with: "
                         + repr(rejection))
        try:
            resp = self._client.post(
                "https://api.openai.com/v1/chat/completions",
                headers={"Authorization": "Bearer " + key},
                json={"model": self.model, "temperature": 0.2, "max_tokens": 60,
                      "messages": [
                          {"role": "system", "content": _SYSTEM_PROMPT},
                          {"role": "user", "content": "\n".join(parts)},
                      ]},
            )
            if resp.status_code != 200:
                raise RuntimeError("http " + str(resp.status_code))
            value = (resp.json()["choices"][0]["message"]["content"] or "").strip()
        except Exception as exc:                                   # noqa: BLE001
            self.failures_in_a_row += 1
            if self.failures_in_a_row >= self.breaker_threshold:
                self.breaker_open = True
                logger.warning("qec.llm_data.breaker_open failures=%d",
                               self.failures_in_a_row)
            else:
                logger.info("qec.llm_data.unavailable error=%s", str(exc)[:120])
            return None
        self.failures_in_a_row = 0
        value = value.strip().strip('"').strip("'").split("\n")[0][:500]
        if not value:
            return None
        # An enumerable field's answer must be one the control offers. Clamp,
        # never trust: an off-list reply becomes None and the field stays
        # residue rather than committing an impossible choice.
        if options:
            for o in options:
                if value.lower() == str(o).strip().lower():
                    self.answered += 1
                    return str(o)
            logger.info("qec.llm_data.off_list field=%r got=%r",
                        str(name)[:40], value[:40])
            return None
        self.answered += 1
        return value

    def stats(self) -> dict[str, Any]:
        return {"calls": self.calls, "answered": self.answered,
                "breaker_open": self.breaker_open}

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:                                          # noqa: BLE001
            pass
