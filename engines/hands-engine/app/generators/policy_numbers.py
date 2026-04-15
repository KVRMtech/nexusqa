"""
Hands Engine — Policy Number Generator.

Generates synthetic policy, claim, and agent numbers from configurable
patterns. Uses token-based substitution for carrier-specific formats.
"""

from __future__ import annotations

import re
import random
import string
from datetime import date

from app.constants import US_STATES


class PolicyNumberGenerator:
    """Generates synthetic policy, claim, and agent numbers."""

    @staticmethod
    def generate(
        pattern: str,
        count: int,
        start_sequence: int = 1,
    ) -> list[str]:
        """
        Generate numbers from a pattern.
        Supported tokens:
          {STATE}     → random 2-letter state code
          {YEAR}      → current 4-digit year
          {YY}        → 2-digit year
          {SEQ:06d}   → zero-padded sequence
          {ALPHA:N}   → N random uppercase letters
          {DIGIT:N}   → N random digits
        """
        rng = random.Random(42)
        results: list[str] = []
        year4 = str(date.today().year)
        year2 = year4[-2:]
        states = list(US_STATES.keys())

        for i in range(count):
            seq = start_sequence + i
            number = pattern

            number = number.replace("{STATE}", rng.choice(states))
            number = number.replace("{YEAR}", year4)
            number = number.replace("{YY}", year2)

            # Handle {SEQ:Nd} patterns
            seq_match = re.search(r"\{SEQ:(\d+)d\}", number)
            if seq_match:
                pad = int(seq_match.group(1))
                number = number[:seq_match.start()] + str(seq).zfill(pad) + number[seq_match.end():]

            # Handle {ALPHA:N}
            alpha_match = re.search(r"\{ALPHA:(\d+)\}", number)
            if alpha_match:
                n = int(alpha_match.group(1))
                letters = "".join(rng.choices(string.ascii_uppercase, k=n))
                number = number[:alpha_match.start()] + letters + number[alpha_match.end():]

            # Handle {DIGIT:N}
            digit_match = re.search(r"\{DIGIT:(\d+)\}", number)
            if digit_match:
                n = int(digit_match.group(1))
                digits = "".join(rng.choices(string.digits, k=n))
                number = number[:digit_match.start()] + digits + number[digit_match.end():]

            results.append(number)

        return results
